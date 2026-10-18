"""Watchdog HTTP entrypoint: identity checks, one tick at a time, and the routes it refuses.

The tick runner, the clock and Google's certificate endpoint are fakes; the RSA key is generated here.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from lib import control_watchdog_server as server
from lib.https_transport import HttpResponse, TransportError

AUDIENCE = "https://spot-control-watchdog-123456789.us-central1.run.app"
INVOKER = "spot-watchdog-invoker@homelab-424523.iam.gserviceaccount.com"
NOW = 1_800_000_000


def _probable_prime(rng: random.Random, bits: int) -> int:
    while True:
        candidate = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if all(candidate % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)) and _miller_rabin(candidate, rng):
            return candidate


def _miller_rabin(n: int, rng: random.Random, rounds: int = 24) -> bool:
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def make_key(seed: int):
    rng = random.Random(seed)
    e = 65537
    while True:
        p, q = _probable_prime(rng, 512), _probable_prime(rng, 512)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


KEY = make_key(1)
OTHER_KEY = make_key(2)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(key, message: bytes) -> bytes:
    n, _e, d = key
    k = (n.bit_length() + 7) // 8
    t = server._SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def token(key=KEY, kid="k1", alg="RS256", signed=True, **claims):
    values = {"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": INVOKER, "email_verified": True,
              "iat": NOW - 10, "exp": NOW + 3000, "sub": "1"}
    values.update(claims)
    values = {k: v for k, v in values.items() if v is not None}
    head = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    body = b64(json.dumps(values).encode())
    signature = b64(sign(key, f"{head}.{body}".encode())) if signed else ""
    return f"{head}.{body}.{signature}"


class FakeCerts:
    def __init__(self):
        self.keys = {"k1": KEY}
        self.calls = 0
        self.down = False

    def __call__(self, method, url, headers, body, timeout):
        assert (method, url) == ("GET", server.GOOGLE_CERTS_URL)
        self.calls += 1
        if self.down:
            raise TransportError("unreachable")
        keys = [{"kty": "RSA", "kid": kid, "alg": "RS256", "n": b64(n.to_bytes((n.bit_length() + 7) // 8, "big")),
                 "e": b64(e.to_bytes(3, "big"))} for kid, (n, e, _d) in self.keys.items()]
        return HttpResponse(200, {}, json.dumps({"keys": keys}).encode())


def verifier(**kw):
    certs = kw.pop("certs", None) or FakeCerts()
    clock = kw.pop("clock", None) or (lambda: NOW)
    v = server.IdentityVerifier(kw.pop("audience", AUDIENCE), kw.pop("invoker", INVOKER), transport=certs,
                                clock=clock, **kw)
    return v, certs


class RsaTests(unittest.TestCase):
    def test_pkcs1_v15_sha256(self):
        n, e, _d = KEY
        message = b"header.payload"
        signature = sign(KEY, message)
        self.assertTrue(server.rsa_pkcs1_sha256_verify(n, e, signature, message))
        self.assertFalse(server.rsa_pkcs1_sha256_verify(n, e, signature, message + b"x"))
        tampered = bytes([signature[0] ^ 1]) + signature[1:]
        self.assertFalse(server.rsa_pkcs1_sha256_verify(n, e, tampered, message))
        self.assertFalse(server.rsa_pkcs1_sha256_verify(n, e, signature[1:], message))
        self.assertFalse(server.rsa_pkcs1_sha256_verify(n, e, sign(OTHER_KEY, message), message))
        # The whole encoded message must match, not just its DigestInfo tail (padding forgeries).
        k = (n.bit_length() + 7) // 8
        t = server._SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
        for em in (b"\x00\x01" + b"\x00" * (k - len(t) - 2) + t, b"\x00\x02" + b"\xff" * (k - len(t) - 3) + b"\x00" + t):
            forged = pow(int.from_bytes(em, "big"), KEY[2], n).to_bytes(k, "big")
            self.assertFalse(server.rsa_pkcs1_sha256_verify(n, e, forged, message))


class IdentityTests(unittest.TestCase):
    def check(self, value, header="Authorization", **kw):
        v, certs = verifier(**kw)
        return v.verify({header: f"Bearer {value}"}), certs

    def refused(self, value, words, header="Authorization", **kw):
        v, _ = verifier(**kw)
        with self.assertRaises(PermissionError) as ctx:
            v.verify({header: f"Bearer {value}"} if value is not None else {})
        self.assertIn(words, str(ctx.exception))

    def test_the_invokers_signed_token_is_accepted(self):
        claims, certs = self.check(token())
        self.assertEqual(claims["email"], INVOKER)
        self.check(token(), header="X-Serverless-Authorization")
        self.check(token(aud=[AUDIENCE, "other"]))
        self.check(token(iss="accounts.google.com", email=INVOKER.upper()))

    def test_every_claim_and_the_signature_are_checked(self):
        self.refused(None, "no bearer identity token")
        self.refused("abc", "not a JWT")
        self.refused(token(aud="https://other.run.app"), "another audience")
        self.refused(token(email="someone@evil.example"), "not the invoker")
        self.refused(token(email_verified=False), "not the invoker")
        self.refused(token(email_verified=None), "not the invoker")
        self.refused(token(iss="https://evil.example"), "not issued by Google")
        self.refused(token(exp=NOW - server.CLOCK_SKEW_SECONDS - 1), "expired")
        self.refused(token(exp=None), "expired")
        self.refused(token(iat=NOW + server.CLOCK_SKEW_SECONDS + 1), "not valid yet")
        self.refused(token(key=OTHER_KEY), "signature is invalid")
        self.refused(token(signed=False), "not RS256-signed")
        self.refused(token(alg="none"), "not RS256-signed")
        self.refused(token(kid="unknown"), "not a current Google key")
        self.refused(token(), "are not set", audience="")
        self.refused(token(), "are not set", invoker="")

    def test_unsigned_claims_only_when_signature_checks_are_off(self):
        self.check(token(signed=False, alg="none"), verify_signature=False)
        self.refused(token(signed=False, aud="https://other.run.app"), "another audience", verify_signature=False)

    def test_certificates_are_cached_and_refreshed_for_a_new_key(self):
        v, certs = verifier()
        v.verify({"Authorization": f"Bearer {token()}"})
        v.verify({"Authorization": f"Bearer {token()}"})
        self.assertEqual(certs.calls, 1)
        certs.keys["k2"] = OTHER_KEY  # Google rotated its keys
        v.verify({"Authorization": f"Bearer {token(key=OTHER_KEY, kid='k2')}"})
        self.assertEqual(certs.calls, 2)
        certs.down = True
        fresh, _ = verifier(certs=certs)
        with self.assertRaisesRegex(PermissionError, "certificates unavailable"):
            fresh.verify({"Authorization": f"Bearer {token()}"})


class FakeProcess:
    pid = 4242

    def __init__(self, fn, argv, kwargs):
        self.fn, self.argv, self.kwargs = fn, argv, kwargs
        self.returncode = None
        self.calls = 0

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls > 1:
            return "", ""
        result = self.fn(self.argv, dict(self.kwargs, timeout=timeout))
        self.returncode = result.returncode
        return result.stdout, result.stderr


def fake_popen(fn, made=None):
    def popen(argv, **kwargs):
        process = FakeProcess(fn, argv, kwargs)
        if made is not None:
            made.append(process)
        return process
    return popen


def fake_runner(fn, signals=None, **kw):
    def killpg(pid, sig):
        if signals is not None:
            signals.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError  # the group is gone
    return server.TickRunner(["x"], popen=fake_popen(fn), killpg=killpg, **kw)


GRANDCHILD = """
import os, signal, subprocess, sys, time
path = sys.argv[1]
script = ("import os, signal, sys, time\\n"
          "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"  # like a gcloud that ignores SIGTERM
          "open(sys.argv[1], 'w').write(str(os.getpid()))\\n"
          "time.sleep(120)\\n")
subprocess.Popen([sys.executable, "-c", script, path])
time.sleep(120)
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ProcessGroupTests(unittest.TestCase):
    """Real processes: a tick whose gcloud-like grandchild ignores SIGTERM."""

    def start(self, timeout):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.pidfile = Path(directory.name) / "grandchild.pid"
        return server.TickRunner([sys.executable, "-c", GRANDCHILD, str(self.pidfile)], timeout=timeout,
                                 term_grace=0.5)

    def grandchild(self):
        for _ in range(500):
            if self.pidfile.exists() and self.pidfile.read_text():
                return int(self.pidfile.read_text())
            time.sleep(0.01)
        self.fail("the grandchild never started")

    def assert_gone(self, pid):
        for _ in range(200):
            if not alive(pid):
                return
            time.sleep(0.025)
        os.kill(pid, signal.SIGKILL)
        self.fail(f"grandchild {pid} survived the tick")

    def test_timeout_kills_the_whole_process_group(self):
        runner = self.start(timeout=2)
        status, summary = runner.run()
        self.assertEqual((status, summary["status"]), (500, "timeout"))
        self.assert_gone(self.grandchild())

    def test_server_sigterm_kills_the_running_tick_group(self):
        runner = self.start(timeout=60)
        results = []
        worker = threading.Thread(target=lambda: results.append(runner.run()))
        worker.start()
        pid = self.grandchild()
        runner.terminate()
        worker.join(10)
        self.assertEqual((results[0][0], results[0][1]["status"]), (500, "terminated"))
        self.assert_gone(pid)

    def test_sigterm_handler_terminates_the_tick_and_exits(self):
        calls = []
        runner = SimpleNamespace(terminate=lambda: calls.append("terminate"))
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        server.install_sigterm(None, runner)
        handler = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(SystemExit):
            handler(signal.SIGTERM, None)
        self.assertEqual(calls, ["terminate"])


class TickRunnerTests(unittest.TestCase):
    def test_timing_leaves_the_lease_to_the_tick(self):
        from lib import gcs_control_state as gcs
        killed_by = server.TICK_TIMEOUT_SECONDS + server.TERM_GRACE_SECONDS + server.KILL_WAIT_SECONDS
        breakable_after = gcs.LEASE_TTL_SECONDS + gcs.LEASE_STALE_GRACE_SECONDS
        self.assertLess(killed_by + 300, breakable_after, "the group must be dead well before the lease can break")
        self.assertLess(killed_by, 900, "and before the service's request timeout")

    def test_timeout_signals_the_group_term_then_kill(self):
        signals = []

        def slow(argv, kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        status, summary = fake_runner(slow, signals).run()
        self.assertEqual((status, summary["status"]), (500, "timeout"))
        self.assertEqual([s for _, s in signals][:2], [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(all(pid == FakeProcess.pid for pid, _ in signals))

    def test_ticks_start_in_their_own_session(self):
        made = []
        runner = server.TickRunner(["x"], popen=fake_popen(
            lambda a, k: SimpleNamespace(returncode=0, stdout="", stderr=""), made), killpg=lambda p, s: None)
        runner.run()
        self.assertIs(made[0].kwargs["start_new_session"], True)

    def test_command_is_one_ensure_apply_step(self):
        command = server.tick_command("homelab-424523")
        self.assertEqual(command[1:2], ["-B"])
        self.assertTrue(command[2].endswith("src/control_vm.py"))
        self.assertEqual(command[3:], ["ensure", "--apply", "--skip-if-idle", "--skip-if-locked",
                                       "--project=homelab-424523"])

    def test_outcomes(self):
        calls = []

        def ok(argv, kw):
            calls.append((argv, kw))
            return SimpleNamespace(returncode=0, stdout="Idle (no lock taken, nothing changed): noop\n", stderr="")
        status, summary = fake_runner(ok).run()
        self.assertEqual((status, summary["status"], summary["exit_code"]), (200, "ok", 0))
        self.assertIn("Idle", summary["result"])
        self.assertEqual(calls[0][1]["timeout"], server.TICK_TIMEOUT_SECONDS)
        self.assertLess(server.TICK_TIMEOUT_SECONDS, 900)
        failed = fake_runner(lambda a, k: SimpleNamespace(returncode=2, stdout="", stderr=""))
        self.assertEqual(failed.run()[0], 500)

    def test_one_tick_at_a_time(self):
        started, release = threading.Event(), threading.Event()

        def blocking(argv, kw):
            started.set()
            release.wait(5)
            return SimpleNamespace(returncode=0, stdout="done\n", stderr="")
        runner = fake_runner(blocking)
        results = []
        worker = threading.Thread(target=lambda: results.append(runner.run()))
        worker.start()
        self.assertTrue(started.wait(5))
        self.assertIsNone(runner.run(), "a second tick must not start while one runs")
        release.set()
        worker.join(5)
        self.assertEqual(results[0][0], 200)
        self.assertIsNotNone(runner.run())  # free again


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.ticks = []
        self.release = threading.Event()
        self.release.set()

        def run(argv, kw):
            self.ticks.append(argv)
            self.release.wait(5)
            return SimpleNamespace(returncode=0, stdout="Executed: noop\n", stderr="")
        v, _ = verifier()
        tick = server.TickRunner(["tick"], popen=fake_popen(run), killpg=lambda p, s: None)
        self.httpd = server.build_server({}, host="127.0.0.1", port=0, runner=tick, verifier=v)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, (json.loads(data) if data else None), response

    def auth(self, value=None):
        return {"Authorization": f"Bearer {value or token()}"}

    def test_routes(self):
        self.assertEqual(self.request("GET", "/health")[:2], (200, {"status": "ok"}))
        self.assertEqual(self.request("GET", "/tick")[0], 405)
        self.assertEqual(self.request("POST", "/health")[0], 405)
        self.assertEqual(self.request("GET", "/healthz")[0], 404)
        self.assertEqual(self.request("POST", "/tick/extra", self.auth())[0], 404)
        self.assertEqual(self.request("PUT", "/tick", self.auth())[0], 405)
        self.assertEqual(self.request("DELETE", "/tick", self.auth())[0], 405)
        self.assertEqual(self.ticks, [])

    def test_tick_needs_the_invokers_identity(self):
        self.assertEqual(self.request("POST", "/tick")[:2], (403, {"error": "forbidden"}))
        self.assertEqual(self.request("POST", "/tick", self.auth(token(email="x@y.example")))[0], 403)
        self.assertEqual(self.request("POST", "/tick", self.auth(token(key=OTHER_KEY)))[0], 403)
        self.assertEqual(self.ticks, [])
        status, summary, _ = self.request("POST", "/tick", self.auth())
        self.assertEqual((status, summary["status"], summary["result"]), (200, "ok", "Executed: noop"))
        self.assertEqual(self.ticks, [["tick"]])

    def test_bodies_are_bounded(self):
        status, _, _ = self.request("POST", "/tick", {**self.auth(), "Content-Length": "999999"}, body=b"x")
        self.assertEqual(status, 413)
        self.assertEqual(self.ticks, [])

    def test_a_concurrent_tick_gets_429(self):
        self.release.clear()
        first = []
        worker = threading.Thread(target=lambda: first.append(self.request("POST", "/tick", self.auth())))
        worker.start()
        for _ in range(500):
            if self.ticks:
                break
            threading.Event().wait(0.01)
        status, body, response = self.request("POST", "/tick", self.auth())
        self.assertEqual((status, body["error"]), (429, "tick_in_progress"))
        self.assertEqual(response.getheader("Retry-After"), "60")
        self.release.set()
        worker.join(10)
        self.assertEqual(first[0][0], 200)
        self.assertEqual(len(self.ticks), 1)

    def test_server_refuses_to_start_without_a_project(self):
        with self.assertRaisesRegex(RuntimeError, "WATCHDOG_PROJECT"):
            server.build_server({}, host="127.0.0.1", port=0)


if __name__ == "__main__":
    unittest.main()


class FloorStepTests(unittest.TestCase):
    def test_the_floor_step_runs_only_when_targets_are_configured(self):
        self.assertEqual(len(server.tick_commands("p", {})), 1)
        commands = server.tick_commands("p", {"SPOT_RUNNER_FLOOR_TARGETS": "swack-tools:secret=s"})
        self.assertEqual(commands[0], server.tick_command("p"))
        self.assertTrue(commands[1][2].endswith("src/runner_floor.py"))
        self.assertEqual(commands[1][3:], ["--project=p", "tick", "--apply"])

    def two_step(self, fn, clock=None):
        made = []
        runner = server.TickRunner(["control"], then=[["floor"]], popen=fake_popen(fn, made),
                                   killpg=lambda p, s: (_ for _ in ()).throw(ProcessLookupError()) if s == 0 else None,
                                   wall=lambda: 1_000.0, **({"clock": clock} if clock else {}))
        return runner, made

    def test_both_steps_share_one_deadline(self):
        times = iter([0.0, 0.0, 300.0, 300.0, 310.0])
        runner, made = self.two_step(lambda a, k: SimpleNamespace(returncode=0, stdout=f"{a[0]} ok\n", stderr=""),
                                     clock=lambda: next(times))
        status, summary = runner.run()
        self.assertEqual(status, 200)
        self.assertEqual([p.argv for p in made], [["control"], ["floor"]])
        deadline = 1_000.0 + server.TICK_TIMEOUT_SECONDS
        self.assertEqual({p.kwargs["env"]["SPOT_TICK_DEADLINE_EPOCH"] for p in made}, {f"{deadline:.3f}"})
        self.assertTrue(all(p.kwargs["start_new_session"] for p in made))
        self.assertEqual(summary["result"], "control ok")
        self.assertEqual([s["result"] for s in summary["steps"]], ["control ok", "floor ok"])

    def test_a_failed_floor_step_fails_the_tick(self):
        runner, _ = self.two_step(lambda a, k: SimpleNamespace(returncode=1 if a == ["floor"] else 0, stdout="x",
                                                               stderr=""))
        status, summary = runner.run()
        self.assertEqual((status, summary["status"], summary["exit_code"]), (500, "failed", 1))

    def test_a_timed_out_control_step_skips_the_floor(self):
        def slow(argv, kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        runner, made = self.two_step(slow)
        status, summary = runner.run()
        self.assertEqual((status, summary["status"]), (500, "timeout"))
        self.assertEqual([p.argv for p in made], [["control"]])

    def test_the_floor_gets_only_what_is_left(self):
        timeouts = []

        def record(argv, kw):
            timeouts.append(kw["timeout"])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        times = iter([0.0, 800.0, 800.0, 800.0])
        runner, _ = self.two_step(record, clock=lambda: next(times))
        runner.run()
        self.assertEqual(timeouts, [server.TICK_TIMEOUT_SECONDS, server.TICK_TIMEOUT_SECONDS - 800.0])


class ShutdownTests(unittest.TestCase):
    def test_no_floor_step_starts_after_sigterm_between_steps(self):
        made = []
        holder = {}

        def clock():
            # The clock is read for the second step's timeout, after the first child is gone and before the next
            # Popen: SIGTERM lands exactly in that gap.
            holder["calls"] = holder.get("calls", 0) + 1
            if holder["calls"] == 2:
                holder["runner"].terminate()
            return 0.0
        runner = server.TickRunner(["control"], then=[["floor"]], popen=fake_popen(
            lambda a, k: SimpleNamespace(returncode=0, stdout="ok", stderr=""), made),
            killpg=lambda p, s: None, clock=clock, wall=lambda: 1_000.0)
        holder["runner"] = runner
        status, summary = runner.run()
        self.assertEqual((status, summary["status"]), (500, "terminated"))
        self.assertEqual([p.argv for p in made], [["control"]])

    def test_a_terminated_server_starts_no_tick_at_all(self):
        made = []
        runner = server.TickRunner(["control"], then=[["floor"]], popen=fake_popen(
            lambda a, k: SimpleNamespace(returncode=0, stdout="ok", stderr=""), made), killpg=lambda p, s: None)
        runner.terminate()  # no child was running: the shutdown must still stick
        self.assertEqual(runner.run()[1]["status"], "terminated")
        self.assertEqual(made, [])
