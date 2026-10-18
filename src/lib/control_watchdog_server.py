"""HTTP entrypoint of the control VM watchdog, a Cloud Run service (``src/control_watchdog_server.py``).

Cloud Scheduler sends ``POST /tick`` with an OIDC identity token of the invoker service account. Each accepted
request runs exactly one ``control_vm.py ensure --apply --skip-if-idle --skip-if-locked`` step, in a child process
so that every tick starts from fresh module state and a hung step can be killed, and answers 200 with a JSON
summary, or 500 when the step failed or timed out. ``GET /health`` answers 200. Everything else is refused.

Authentication is Cloud Run IAM: the service allows no unauthenticated callers, and only the invoker account holds
``roles/run.invoker`` on it. As a second line of defense this server checks the identity token itself, with no
app-level secret: an RS256 signature by a key from Google's published certificates, the issuer, the audience (the
service URL), the expiry, and the verified e-mail of the invoker account.

One tick at a time: the service runs with concurrency 1 and at most one instance, and this server answers 429 to
a tick that arrives while another one runs. The Cloud Storage lease serializes ticks across instances and
workstations anyway.

Standard library only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .https_transport import Transport, TransportError, https_request

LOG = logging.getLogger("spot_runner_autoscaler.watchdog")

ROOT = Path(__file__).resolve().parents[2]
CONTROL_VM = ROOT / "src" / "control_vm.py"
ENV_PROJECT = "WATCHDOG_PROJECT"
ENV_AUDIENCE = "WATCHDOG_AUDIENCE"
ENV_INVOKER = "WATCHDOG_INVOKER"
ENV_VERIFY_SIGNATURE = "WATCHDOG_VERIFY_SIGNATURE"
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
# Tick timing. The tick's child process takes the lease (LEASE_TTL_SECONDS = 1200) only after it starts, and a
# contender may break the lease LEASE_STALE_GRACE_SECONDS = 60 after the last renewal sent by the child, so no
# other controller can act earlier than 1260 s after the tick starts. A timed-out tick's whole process group is gone
# by TICK_TIMEOUT_SECONDS + TERM_GRACE_SECONDS + KILL_WAIT_SECONDS = 840 + 10 + 5 = 855 s, 405 s before that, and
# before the service's 900 s request timeout.
TICK_TIMEOUT_SECONDS = 840
TERM_GRACE_SECONDS = 10
KILL_WAIT_SECONDS = 5
CERTS_CACHE_SECONDS = 3600
CLOCK_SKEW_SECONDS = 60
MAX_BODY_BYTES = 4096
MAX_SUMMARY_CHARS = 2000
# DER prefix of a SHA-256 DigestInfo (PKCS #1 v1.5, RFC 8017 section 9.2).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def tick_command(project: str) -> list[str]:
    return [sys.executable, "-B", str(CONTROL_VM), "ensure", "--apply", "--skip-if-idle", "--skip-if-locked",
            f"--project={project}"]


# --------------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------------

def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def rsa_pkcs1_sha256_verify(n: int, e: int, signature: bytes, message: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 verification with SHA-256 (RFC 8017 section 8.2.2), in pure Python."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k or k < len(_SHA256_DIGEST_INFO) + 32 + 11:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


class IdentityVerifier:
    """Checks the Google-signed OIDC token that Cloud Scheduler attaches (Cloud Run IAM checked it first)."""

    def __init__(self, audience: str, invoker: str, *, verify_signature: bool = True,
                 transport: Transport | None = None, clock: Callable[[], float] = time.time) -> None:
        self.audience = audience
        self.invoker = invoker.lower()
        self.verify_signature = verify_signature
        self.transport = transport or https_request
        self.clock = clock
        self._keys: dict[str, tuple[int, int]] = {}
        self._keys_at = 0.0

    def _certs(self, refresh: bool = False) -> dict[str, tuple[int, int]]:
        if self._keys and not refresh and self.clock() - self._keys_at < CERTS_CACHE_SECONDS:
            return self._keys
        try:
            response = self.transport("GET", GOOGLE_CERTS_URL, {"User-Agent": "spot-control-plane/1.0"}, None, 10.0)
        except TransportError as exc:
            raise PermissionError(f"Google certificates unavailable: {exc}") from exc
        if response.status != 200:
            raise PermissionError(f"Google certificates returned HTTP {response.status}")
        keys = {}
        try:
            for key in json.loads(response.body.decode("utf-8")).get("keys", []):
                if key.get("kty") == "RSA" and key.get("kid"):
                    keys[key["kid"]] = (int.from_bytes(_b64url(key["n"]), "big"),
                                        int.from_bytes(_b64url(key["e"]), "big"))
        except (ValueError, AttributeError, KeyError, TypeError) as exc:
            raise PermissionError("Google certificates are malformed") from exc
        self._keys, self._keys_at = keys, self.clock()
        return keys

    def verify(self, headers: Mapping[str, str]) -> dict[str, Any]:
        """The token's claims; raises PermissionError with the reason when the request isn't the invoker's."""
        if not self.audience or not self.invoker:
            raise PermissionError(f"{ENV_AUDIENCE} and {ENV_INVOKER} are not set; refusing every tick")
        header = headers.get("X-Serverless-Authorization") or headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            raise PermissionError("no bearer identity token")
        parts = header[len("Bearer "):].strip().split(".")
        if len(parts) != 3:
            raise PermissionError("the identity token is not a JWT")
        try:
            jose = json.loads(_b64url(parts[0]))
            claims = json.loads(_b64url(parts[1]))
            signature = _b64url(parts[2]) if parts[2] else b""
        except (ValueError, UnicodeDecodeError) as exc:
            raise PermissionError("the identity token is malformed") from exc
        if not isinstance(jose, dict) or not isinstance(claims, dict):
            raise PermissionError("the identity token is malformed")
        if self.verify_signature:
            if jose.get("alg") != "RS256" or not signature:
                raise PermissionError("the identity token is not RS256-signed")
            kid = str(jose.get("kid") or "")
            key = self._certs().get(kid) or self._certs(refresh=True).get(kid)
            if key is None:
                raise PermissionError("the identity token's key is not a current Google key")
            if not rsa_pkcs1_sha256_verify(key[0], key[1], signature, f"{parts[0]}.{parts[1]}".encode("ascii")):
                raise PermissionError("the identity token's signature is invalid")
        now = self.clock()
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise PermissionError("the identity token was not issued by Google")
        audiences = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
        if self.audience not in audiences:
            raise PermissionError("the identity token is for another audience")
        exp, iat = claims.get("exp"), claims.get("iat")
        if not isinstance(exp, (int, float)) or exp + CLOCK_SKEW_SECONDS < now:
            raise PermissionError("the identity token expired")
        if isinstance(iat, (int, float)) and iat - CLOCK_SKEW_SECONDS > now:
            raise PermissionError("the identity token is not valid yet")
        if str(claims.get("email") or "").lower() != self.invoker or claims.get("email_verified") is not True:
            raise PermissionError("the identity token is not the invoker service account's")
        return claims


# --------------------------------------------------------------------------------------------
# Tick
# --------------------------------------------------------------------------------------------

class TickRunner:
    """Runs one tick at a time in its own process group; ``run`` returns None when another tick is running.

    The child runs ``gcloud`` as grandchildren, whose 600 s timeouts the child enforces. Killing only the child would
    leave them running unsupervised, possibly after another controller took the lease, so a timeout (or the
    server's own SIGTERM, see ``terminate``) signals the whole group: SIGTERM, a short grace, then SIGKILL.
    """

    def __init__(self, command: Sequence[str], *, timeout: float = TICK_TIMEOUT_SECONDS,
                 term_grace: float = TERM_GRACE_SECONDS, popen: Callable[..., Any] = subprocess.Popen,
                 killpg: Callable[[int, int], None] = os.killpg, clock: Callable[[], float] = time.monotonic) -> None:
        self.command = list(command)
        self.timeout = timeout
        self.term_grace = term_grace
        self.popen = popen
        self.killpg = killpg
        self.clock = clock
        self._lock = threading.Lock()
        self._process: Any = None
        self._terminated = False

    def _signal_group(self, process: Any, sig: int) -> bool:
        try:
            self.killpg(process.pid, sig)  # pgid == pid: the child leads its own session
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _kill_group(self, process: Any) -> tuple[str, str]:
        """SIGTERM the group, wait ``term_grace``, SIGKILL the group, and reap. Returns the remaining output."""
        self._signal_group(process, signal.SIGTERM)
        try:
            out, err = process.communicate(timeout=self.term_grace)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        # Unconditionally: the child may have exited on SIGTERM while a grandchild ignored it.
        self._signal_group(process, signal.SIGKILL)
        try:
            more_out, more_err = process.communicate(timeout=KILL_WAIT_SECONDS)
            out, err = (out or "") + (more_out or ""), (err or "") + (more_err or "")
        except (subprocess.TimeoutExpired, ValueError):
            pass
        reap_orphans()
        deadline = self.clock() + KILL_WAIT_SECONDS
        while self._signal_group(process, 0) and self.clock() < deadline:
            time.sleep(0.05)
            reap_orphans()
        if self._signal_group(process, 0):
            LOG.error("Watchdog tick process group %d still exists after SIGKILL", process.pid)
        return out or "", err or ""

    def terminate(self) -> None:
        """Kill the running tick's process group (the server got SIGTERM)."""
        process = self._process
        if process is not None:
            self._terminated = True
            self._signal_group(process, signal.SIGTERM)
            self._signal_group(process, signal.SIGKILL)

    def run(self) -> tuple[int, dict[str, Any]] | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            started = self.clock()
            self._terminated = False
            try:
                process = self.popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     start_new_session=True)
            except OSError as exc:
                LOG.error("Watchdog tick could not start: %s", exc)
                return 500, {"status": "error", "error": type(exc).__name__}
            self._process = process
            try:
                try:
                    stdout, stderr = process.communicate(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    LOG.error("Watchdog tick timed out after %ss; killing its process group", self.timeout)
                    _out, stderr = self._kill_group(process)
                    _write_stderr(stderr)
                    return 500, {"status": "timeout", "seconds": round(self.clock() - started, 1)}
            finally:
                self._process = None
            # The child's log lines (stderr) go to Cloud Logging; the response carries only the step summary.
            _write_stderr(stderr)
            if self._terminated:
                return 500, {"status": "terminated", "seconds": round(self.clock() - started, 1)}
            returncode = process.returncode
            lines = [line for line in (stdout or "").splitlines() if line.strip()]
            summary = {"status": "ok" if returncode == 0 else "failed", "exit_code": returncode,
                       "result": (lines[-1] if lines else "")[:MAX_SUMMARY_CHARS],
                       "seconds": round(self.clock() - started, 1)}
            return (200 if returncode == 0 else 500), summary
        finally:
            self._lock.release()


def _write_stderr(text: str | None) -> None:
    if text:
        sys.stderr.write(text if text.endswith("\n") else text + "\n")
        sys.stderr.flush()


def reap_orphans() -> None:
    """Reap exited children without blocking. As PID 1 in the container this server inherits a killed tick's
    orphaned grandchildren, which would otherwise stay zombies."""
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def make_handler(runner: TickRunner, verifier: IdentityVerifier) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "spot-control-watchdog"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - BaseHTTPRequestHandler hook
            LOG.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload: Mapping[str, Any], extra: Mapping[str, str] | None = None) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _refuse_body(self) -> bool:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, {"error": "bad_content_length"})
                return True
            if length < 0 or length > MAX_BODY_BYTES or self.headers.get("Transfer-Encoding"):
                self._send(413, {"error": "body_too_large"})
                return True
            if length:
                self.rfile.read(length)  # ignored: a tick takes no input
            return False

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler hook
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            elif self.path == "/tick":
                self._send(405, {"error": "method_not_allowed"}, {"Allow": "POST"})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler hook
            if self.path == "/health":
                self._send(405, {"error": "method_not_allowed"}, {"Allow": "GET"})
                return
            if self.path != "/tick":
                self._send(404, {"error": "not_found"})
                return
            if self._refuse_body():
                return
            try:
                verifier.verify(self.headers)
            except PermissionError as exc:
                LOG.warning("Watchdog tick refused: %s", exc)
                self._send(403, {"error": "forbidden"})
                return
            outcome = runner.run()
            if outcome is None:
                self._send(429, {"error": "tick_in_progress"}, {"Retry-After": "60"})
                return
            status, summary = outcome
            self._send(status, summary)

        def _not_allowed(self) -> None:
            self._send(405, {"error": "method_not_allowed"}, {"Allow": "GET, POST"})

        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_allowed  # noqa: N815

    return Handler


def build_server(env: Mapping[str, str] | None = None, *, host: str = "", port: int | None = None,
                 runner: TickRunner | None = None, verifier: IdentityVerifier | None = None) -> ThreadingHTTPServer:
    env = os.environ if env is None else env
    project = str(env.get(ENV_PROJECT, "") or "").strip()
    if runner is None:
        if not project:
            raise RuntimeError(f"{ENV_PROJECT} is not set")
        runner = TickRunner(tick_command(project))
    verifier = verifier or IdentityVerifier(
        str(env.get(ENV_AUDIENCE, "") or "").strip(), str(env.get(ENV_INVOKER, "") or "").strip(),
        verify_signature=str(env.get(ENV_VERIFY_SIGNATURE, "1")).strip() not in ("0", "false", "no", "off"))
    port = int(env.get("PORT", "8080")) if port is None else port
    return ThreadingHTTPServer((host, port), make_handler(runner, verifier))


def main() -> int:
    from .log_format import configure_logging

    configure_logging()
    runner = TickRunner(tick_command(str(os.environ.get(ENV_PROJECT, "") or "").strip())) \
        if str(os.environ.get(ENV_PROJECT, "") or "").strip() else None
    server = build_server(runner=runner)
    install_sigterm(server, runner)
    LOG.info("Control VM watchdog listening on port %d", server.server_address[1])
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
    return 0


def install_sigterm(server: ThreadingHTTPServer, runner: TickRunner | None) -> None:
    """Cloud Run sends SIGTERM before it stops the instance: kill a running tick's whole process group first."""
    def handle(signum: int, frame: Any) -> None:
        LOG.warning("Watchdog got signal %d; killing any running tick's process group", signum)
        if runner is not None:
            runner.terminate()
        reap_orphans()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, handle)
