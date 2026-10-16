"""GitHub OAuth 2.0 login and multi-target administrator verification for the Web UI Dashboard.

This module is standard-library only. It provides:

* GitHub OAuth 2.0 Device Flow (RFC 8628), which needs only a client ID.
* GitHub OAuth 2.0 Authorization Code flow, enabled only when a client secret is configured.
* Fail-closed authorization: the GitHub login must be on the allowlist AND, for every
  configured controlled target, the user must be an administrator
  (``GET /repos/{owner}/{repo}`` -> ``permissions.admin is True``; ``GET /user/memberships/orgs/{org}``
  -> ``state == "active"`` and ``role == "admin"``). Any API error denies access.
* Stateless HMAC-SHA256 tokens for sessions, CSRF ``state`` values, and device-flow handles.
  The GitHub access token is never stored in any token or cookie.

Token format (verifiable by any process holding the same session secret, for example an
edge worker or a replacement VM)::

    purpose_key = HMAC-SHA256(key=session_secret_utf8, msg=b"spot-dashboard/<purpose>/v1")
    token       = base64url_nopad(compact_json) + "." + hex(HMAC-SHA256(purpose_key, base64url_part_ascii))

``<purpose>`` is ``session``, ``oauth-state``, or ``device``. Session payloads are
``{"v": 1, "sub": login, "uid": github_id, "iat": epoch, "exp": epoch, "tgt": [verified target keys]}``.
A session grants access only while ``exp`` is in the future, ``sub`` is still allowlisted, and every
currently configured controlled target key appears in ``tgt``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ALLOWED_ADMINS: frozenset[str] = frozenset({"swackhamer", "xbmc4lyfe"})

ENV_CLIENT_ID = "GITHUB_OAUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "GITHUB_OAUTH_CLIENT_SECRET"
ENV_SCOPES = "GITHUB_OAUTH_SCOPES"
ENV_REDIRECT_URI = "GITHUB_OAUTH_REDIRECT_URI"
ENV_SESSION_SECRET = "DASHBOARD_SESSION_SECRET"
ENV_SESSION_TTL = "DASHBOARD_SESSION_TTL_SECONDS"
ENV_ALLOWED_ADMINS = "DASHBOARD_ALLOWED_ADMINS"
ENV_CONTROLLED_TARGETS = "DASHBOARD_CONTROLLED_TARGETS"

SESSION_COOKIE_NAME = "spot_session"
OAUTH_STATE_COOKIE_NAME = "spot_oauth_state"
OAUTH_STATE_COOKIE_PATH = "/auth/github"

DEFAULT_SCOPES = "repo read:org"
DEFAULT_SESSION_TTL_SECONDS = 604_800  # Contract default for create_session_cookie_value.
DEFAULT_DASHBOARD_SESSION_TTL_SECONDS = 43_200  # Sessions issued by the dashboard: 12 hours.
MAX_SESSION_TTL_SECONDS = 604_800
MIN_SESSION_TTL_SECONDS = 60
DEFAULT_STATE_TTL_SECONDS = 600
DEFAULT_VERIFICATION_CACHE_TTL_SECONDS = 300.0
MAX_VERIFICATION_CACHE_ENTRIES = 1024
MAX_DEVICE_HANDLE_TTL_SECONDS = 1800
MIN_SESSION_SECRET_BYTES = 32
MAX_TOKEN_CHARS = 4096
MAX_GITHUB_RESPONSE_BYTES = 1_048_576
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
CLOCK_SKEW_SECONDS = 60

GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_API_BASE = "https://api.github.com"
ALLOWED_GITHUB_HOSTS = frozenset({"github.com", "api.github.com"})
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
USER_AGENT = "spot-runner-dashboard/1.0"

_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_OPAQUE_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,512}\Z")

# requester(method, url, headers, body, timeout) -> (http_status, response_body_bytes)
HttpRequester = Callable[..., "tuple[int, bytes]"]


class GitHubApiError(RuntimeError):
    """Raised when a GitHub OAuth or REST call fails or returns an unusable response."""


# --- Allowlist and controlled-target parsing ---


def _split_list(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\s]+", raw or "") if p.strip()]


def normalize_admin_logins(logins: Iterable[str]) -> frozenset[str]:
    """Validate GitHub logins and return them lowercased. Raises ValueError on an invalid login."""
    out: set[str] = set()
    for login in logins:
        clean = str(login).strip()
        if not _LOGIN_RE.match(clean):
            raise ValueError(f"Invalid GitHub login in admin allowlist: {clean!r}")
        out.add(clean.lower())
    return frozenset(out)


def parse_allowed_admins(raw: str | None) -> frozenset[str]:
    """Parse a comma/whitespace separated allowlist; empty or unset means the default allowlist."""
    items = _split_list(raw or "")
    if not items:
        return DEFAULT_ALLOWED_ADMINS
    return normalize_admin_logins(items)


def is_admin_login(login: str, allowed_admins: Iterable[str]) -> bool:
    """Return True when ``login`` is in ``allowed_admins`` (GitHub logins are case-insensitive)."""
    clean = str(login or "").strip()
    if not _LOGIN_RE.match(clean):
        return False
    return clean.lower() in {str(a).strip().lower() for a in allowed_admins}


@dataclass(frozen=True)
class ControlledTarget:
    """A GitHub organization or repository on which dashboard users must hold admin rights."""

    kind: str  # "org" or "repo"
    owner: str
    repo: str | None = None

    @property
    def name(self) -> str:
        return f"{self.owner}/{self.repo}" if self.repo else self.owner

    @property
    def key(self) -> str:
        return self.name.lower()

    def api_path(self) -> str:
        if self.kind == "repo":
            return "/repos/{}/{}".format(
                urllib.parse.quote(self.owner, safe=""), urllib.parse.quote(str(self.repo), safe="")
            )
        return "/user/memberships/orgs/{}".format(urllib.parse.quote(self.owner, safe=""))

    def to_dict(self) -> dict[str, str]:
        return {"type": self.kind, "name": self.name}


def parse_controlled_target(value: Any) -> ControlledTarget:
    """Parse ``"org"``, ``"owner/repo"``, or ``{"type": "org"|"repo", "name": ...}``. Raises ValueError."""
    kind_hint = ""
    if isinstance(value, ControlledTarget):
        return value
    if isinstance(value, Mapping):
        name = str(value.get("name", "")).strip()
        kind_hint = str(value.get("type", value.get("target_type", "")) or "").strip().lower()
    elif isinstance(value, str):
        name = value.strip()
    else:
        raise ValueError(f"Unsupported controlled target: {value!r}")
    if not name:
        raise ValueError("Controlled target name cannot be empty")
    if "/" in name:
        if kind_hint not in ("", "repo"):
            raise ValueError(f"Controlled target {name!r} looks like a repository but type is {kind_hint!r}")
        parts = name.split("/")
        if len(parts) != 2:
            raise ValueError(f"Repository target {name!r} must be 'owner/repo'")
        owner, repo = parts
        if not _LOGIN_RE.match(owner) or not _REPO_RE.match(repo) or repo in (".", ".."):
            raise ValueError(f"Invalid repository target {name!r}")
        return ControlledTarget(kind="repo", owner=owner, repo=repo)
    if kind_hint not in ("", "org"):
        raise ValueError(f"Controlled target {name!r} has no '/' but type is {kind_hint!r}")
    if not _LOGIN_RE.match(name):
        raise ValueError(f"Invalid organization target {name!r}")
    return ControlledTarget(kind="org", owner=name)


def parse_controlled_targets(values: Iterable[Any] | None) -> list[ControlledTarget]:
    """Parse and de-duplicate (case-insensitively) a controlled target list. Raises on any invalid entry."""
    out: list[ControlledTarget] = []
    seen: set[str] = set()
    for value in values or []:
        target = parse_controlled_target(value)
        if target.key not in seen:
            seen.add(target.key)
            out.append(target)
    return out


def parse_controlled_targets_env(raw: str | None) -> list[ControlledTarget]:
    return parse_controlled_targets(_split_list(raw or ""))


# --- Signed token primitives ---


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def derive_purpose_key(secret: str | bytes, purpose: str) -> bytes:
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    return hmac.new(secret_bytes, f"spot-dashboard/{purpose}/v1".encode("ascii"), hashlib.sha256).digest()


def sign_token(key: bytes, payload: Mapping[str, Any]) -> str:
    body = _b64encode(json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(key: bytes, token: Any) -> dict[str, Any] | None:
    """Return the payload of a correctly signed token, or None. Never raises on malformed input."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    body, sig = parts
    if not _B64URL_RE.match(body) or not _HEX64_RE.match(sig):
        return None
    expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode("ascii"), expected.encode("ascii")):
        return None
    try:
        data = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def safe_return_path(value: Any) -> str:
    """Accept only same-origin absolute paths (no scheme, host, backslash, or control characters)."""
    if not isinstance(value, str) or not value or len(value) > 512:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value):
        return "/"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


@dataclass(frozen=True)
class SessionData:
    user_login: str
    user_id: int | None
    issued_at: int
    expires_at: int
    verified_targets: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "v": 1,
            "sub": self.user_login,
            "uid": self.user_id,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "tgt": sorted(set(self.verified_targets)),
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> SessionData | None:
        if data.get("v") != 1:
            return None
        sub, uid, iat, exp, tgt = data.get("sub"), data.get("uid"), data.get("iat"), data.get("exp"), data.get("tgt")
        if not isinstance(sub, str) or not _LOGIN_RE.match(sub):
            return None
        if uid is not None and not _is_int(uid):
            return None
        if not _is_int(iat) or not _is_int(exp) or exp <= iat:
            return None
        if not isinstance(tgt, list) or not all(isinstance(t, str) for t in tgt):
            return None
        return cls(user_login=sub, user_id=uid, issued_at=iat, expires_at=exp, verified_targets=tuple(tgt))


def format_session_cookie(token: str, max_age: int) -> str:
    return f"{SESSION_COOKIE_NAME}={token}; Path=/; Max-Age={int(max_age)}; HttpOnly; Secure; SameSite=Lax"


def format_cleared_session_cookie() -> str:
    return (
        f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
        "HttpOnly; Secure; SameSite=Lax"
    )


def format_state_cookie(state: str, max_age: int = DEFAULT_STATE_TTL_SECONDS) -> str:
    return (
        f"{OAUTH_STATE_COOKIE_NAME}={state}; Path={OAUTH_STATE_COOKIE_PATH}; Max-Age={int(max_age)}; "
        "HttpOnly; Secure; SameSite=Lax"
    )


def format_cleared_state_cookie() -> str:
    return (
        f"{OAUTH_STATE_COOKIE_NAME}=; Path={OAUTH_STATE_COOKIE_PATH}; Max-Age=0; "
        "Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; Secure; SameSite=Lax"
    )


def parse_cookie_header(raw: str | None) -> dict[str, str]:
    """Tolerant Cookie header parser; the first occurrence of a name wins."""
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        if name and name not in out:
            out[name] = value
    return out


# --- HTTP transport ---


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D401
        return None


def default_http_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, bytes]:
    """HTTPS-only GitHub transport with bounded response size, a timeout, and no redirects."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "") not in ALLOWED_GITHUB_HOSTS:
        raise GitHubApiError(f"Refusing request to non-GitHub URL: {parsed.scheme}://{parsed.hostname}")
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = int(resp.status)
            data = resp.read(MAX_GITHUB_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            data = exc.read(MAX_GITHUB_RESPONSE_BYTES + 1) if exc.fp else b""
        except OSError:
            data = b""
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GitHubApiError(f"GitHub request failed: {type(exc).__name__}") from exc
    if len(data) > MAX_GITHUB_RESPONSE_BYTES:
        raise GitHubApiError("GitHub response exceeded the size limit")
    return status, data


def _json_object(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_GITHUB_RESPONSE_BYTES:
        raise GitHubApiError("GitHub response exceeded the size limit")
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GitHubApiError("GitHub returned a non-JSON response") from exc
    if not isinstance(parsed, dict):
        raise GitHubApiError("GitHub returned an unexpected JSON shape")
    return parsed


# --- Authorization results and cache ---


@dataclass(frozen=True)
class TargetCheck:
    target: str
    key: str
    kind: str
    is_admin: bool
    reason: str
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "type": self.kind,
            "is_admin": self.is_admin,
            "reason": self.reason,
            "cached": self.cached,
        }


@dataclass
class AuthorizationDecision:
    authorized: bool
    status_code: int
    message: str
    user_login: str = ""
    user_id: int | None = None
    controlled_targets: list[str] = field(default_factory=list)
    verified_targets: list[str] = field(default_factory=list)
    missing_targets: list[str] = field(default_factory=list)
    checks: list[TargetCheck] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorized": self.authorized,
            "status_code": self.status_code,
            "message": self.message,
            "user": self.user_login,
            "user_id": self.user_id,
            "controlled_targets": list(self.controlled_targets),
            "verified_targets": list(self.verified_targets),
            "missing_targets": list(self.missing_targets),
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass(frozen=True)
class SessionCheck:
    """Outcome of validating a session cookie against the current allowlist and target list."""

    status: str  # "none" (no valid session), "ok", or "forbidden"
    session: SessionData | None = None
    message: str = ""
    missing_targets: tuple[str, ...] = ()


class TargetVerificationCache:
    """Thread-safe, bounded cache of POSITIVE admin verifications keyed by (login, target key).

    Denials and API errors are never cached, so a newly granted admin is not locked out and a
    transient GitHub error never turns into a cached decision.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_VERIFICATION_CACHE_TTL_SECONDS,
        max_entries: int = MAX_VERIFICATION_CACHE_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._entries: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def is_verified(self, login: str, target_key: str) -> bool:
        k = (login.lower(), target_key.lower())
        now = self._clock()
        with self._lock:
            exp = self._entries.get(k)
            if exp is None:
                return False
            if exp <= now:
                del self._entries[k]
                return False
            return True

    def mark_verified(self, login: str, target_key: str) -> None:
        if self.ttl_seconds <= 0:
            return
        now = self._clock()
        with self._lock:
            if len(self._entries) >= self.max_entries:
                for k in [k for k, exp in self._entries.items() if exp <= now]:
                    del self._entries[k]
                while len(self._entries) >= self.max_entries:
                    del self._entries[next(iter(self._entries))]
            self._entries[(login.lower(), target_key.lower())] = now + self.ttl_seconds

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# --- OAuth manager ---


class GitHubOAuthManager:
    """GitHub OAuth (Device Flow and Authorization Code flow), admin verification, and stateless sessions."""

    def __init__(
        self,
        client_id: str,
        client_secret: str = "",
        allowed_admins: Iterable[str] | None = None,
        session_secret: str | bytes = "",
        *,
        scopes: str = DEFAULT_SCOPES,
        redirect_uri: str = "",
        session_ttl_seconds: int = DEFAULT_DASHBOARD_SESSION_TTL_SECONDS,
        http_requester: HttpRequester | None = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        verification_cache: TargetVerificationCache | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        cid = str(client_id or "").strip()
        if not _CLIENT_ID_RE.match(cid):
            raise ValueError(f"{ENV_CLIENT_ID} is required and must be a valid GitHub OAuth client ID")
        secret_bytes = session_secret.encode("utf-8") if isinstance(session_secret, str) else bytes(session_secret or b"")
        if len(secret_bytes.strip()) < MIN_SESSION_SECRET_BYTES:
            raise ValueError(
                f"{ENV_SESSION_SECRET} must be set to at least {MIN_SESSION_SECRET_BYTES} bytes "
                "(for example: python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
            )
        if len(set(secret_bytes)) < 8:
            raise ValueError(f"{ENV_SESSION_SECRET} has too little variety to be a random secret")
        admins = DEFAULT_ALLOWED_ADMINS if allowed_admins is None else normalize_admin_logins(allowed_admins)
        if not admins:
            raise ValueError("The dashboard admin allowlist cannot be empty")
        ttl = int(session_ttl_seconds)
        if not MIN_SESSION_TTL_SECONDS <= ttl <= MAX_SESSION_TTL_SECONDS:
            raise ValueError(
                f"Session TTL must be between {MIN_SESSION_TTL_SECONDS} and {MAX_SESSION_TTL_SECONDS} seconds"
            )
        redirect = str(redirect_uri or "").strip()
        if redirect:
            parsed = urllib.parse.urlsplit(redirect)
            if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.fragment:
                raise ValueError(f"{ENV_REDIRECT_URI} must be an absolute http(s) URL without a fragment")
        self.client_id = cid
        self.client_secret = str(client_secret or "").strip()
        self.allowed_admins: frozenset[str] = admins
        self.scopes = " ".join(_split_list(scopes)) or DEFAULT_SCOPES
        self.redirect_uri = redirect
        self.session_ttl_seconds = ttl
        self.timeout_seconds = float(timeout_seconds)
        self._request = http_requester or default_http_request
        self._clock = clock
        self.verification_cache = verification_cache or TargetVerificationCache(clock=clock)
        self._session_key = derive_purpose_key(secret_bytes, "session")
        self._state_key = derive_purpose_key(secret_bytes, "oauth-state")
        self._device_key = derive_purpose_key(secret_bytes, "device")
        self._used_state_nonces: dict[str, float] = {}
        self._nonce_lock = threading.Lock()

    # -- configuration --

    @property
    def device_flow_enabled(self) -> bool:
        return True

    @property
    def web_flow_enabled(self) -> bool:
        return bool(self.client_secret)

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        allowed_admins: Iterable[str] | None = None,
        **kwargs: Any,
    ) -> GitHubOAuthManager | None:
        """Build a manager from the environment, or return None when OAuth is not configured.

        OAuth is enabled by ``GITHUB_OAUTH_CLIENT_ID``. When enabled, ``DASHBOARD_SESSION_SECRET`` is
        mandatory; there is no generated or hardcoded fallback secret.
        """
        env = os.environ if environ is None else environ
        client_id = str(env.get(ENV_CLIENT_ID, "") or "").strip()
        client_secret = str(env.get(ENV_CLIENT_SECRET, "") or "").strip()
        if not client_id:
            if client_secret:
                raise ValueError(f"{ENV_CLIENT_SECRET} is set but {ENV_CLIENT_ID} is not")
            return None
        admins = (
            normalize_admin_logins(allowed_admins)
            if allowed_admins is not None
            else parse_allowed_admins(env.get(ENV_ALLOWED_ADMINS))
        )
        ttl_raw = str(env.get(ENV_SESSION_TTL, "") or "").strip()
        try:
            ttl = int(ttl_raw) if ttl_raw else DEFAULT_DASHBOARD_SESSION_TTL_SECONDS
        except ValueError as exc:
            raise ValueError(f"{ENV_SESSION_TTL} must be an integer number of seconds") from exc
        params: dict[str, Any] = {
            "scopes": str(env.get(ENV_SCOPES, "") or "") or DEFAULT_SCOPES,
            "redirect_uri": str(env.get(ENV_REDIRECT_URI, "") or ""),
            "session_ttl_seconds": ttl,
        }
        params.update(kwargs)
        return cls(client_id, client_secret, admins, str(env.get(ENV_SESSION_SECRET, "") or ""), **params)

    # -- low-level HTTP --

    def _call(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, bytes]:
        """Invoke the (injectable) transport; any transport failure becomes GitHubApiError (fail closed)."""
        try:
            status, data = self._request(method, url, headers, body, self.timeout_seconds)
        except GitHubApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GitHubApiError(f"GitHub request failed: {type(exc).__name__}") from exc
        if not _is_int(status) or not isinstance(data, (bytes, bytearray)):
            raise GitHubApiError("GitHub transport returned an invalid response")
        if len(data) > MAX_GITHUB_RESPONSE_BYTES:
            raise GitHubApiError("GitHub response exceeded the size limit")
        return status, bytes(data)

    def _post_form(self, url: str, fields: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        body = urllib.parse.urlencode(dict(fields)).encode("ascii")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        }
        status, data = self._call("POST", url, headers, body)
        return status, _json_object(data)

    def _api_get(self, path: str, access_token: str) -> tuple[int, dict[str, Any] | None]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        status, data = self._call("GET", GITHUB_API_BASE + path, headers, None)
        if status != 200:
            return status, None
        return status, _json_object(data)

    # -- Device Flow (RFC 8628) --

    def start_device_flow(self, scope: str | None = None) -> dict[str, Any]:
        """Request a device and user code. Raises GitHubApiError on any failure."""
        req_scope = " ".join(_split_list(scope)) if scope else self.scopes
        status, data = self._post_form(GITHUB_DEVICE_CODE_URL, {"client_id": self.client_id, "scope": req_scope})
        if status != 200 or "error" in data:
            raise GitHubApiError(f"GitHub device authorization failed (HTTP {status}, {data.get('error', 'no error code')})")
        device_code = data.get("device_code")
        user_code = data.get("user_code")
        verification_uri = data.get("verification_uri")
        expires_in = data.get("expires_in", 900)
        interval = data.get("interval", 5)
        if not isinstance(device_code, str) or not _OPAQUE_RE.match(device_code):
            raise GitHubApiError("GitHub returned an invalid device_code")
        if not isinstance(user_code, str) or not re.fullmatch(r"[A-Za-z0-9-]{4,32}", user_code):
            raise GitHubApiError("GitHub returned an invalid user_code")
        if not isinstance(verification_uri, str) or not verification_uri.startswith("https://github.com/"):
            raise GitHubApiError("GitHub returned an unexpected verification_uri")
        if not _is_int(expires_in) or not _is_int(interval) or expires_in <= 0 or interval < 0:
            raise GitHubApiError("GitHub returned invalid device flow timing")
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "expires_in": min(expires_in, MAX_DEVICE_HANDLE_TTL_SECONDS),
            "interval": max(interval, 5),
        }

    def poll_device_token(self, device_code: str) -> dict[str, Any]:
        """Poll once. Returns ``{"status": pending|slow_down|expired|denied|error|success, ...}``.

        Only the ``success`` result carries ``access_token``; callers must never forward it to a browser.
        """
        if not isinstance(device_code, str) or not _OPAQUE_RE.match(device_code):
            return {"status": "error", "error": "invalid_request"}
        try:
            status, data = self._post_form(
                GITHUB_ACCESS_TOKEN_URL,
                {"client_id": self.client_id, "device_code": device_code, "grant_type": DEVICE_GRANT_TYPE},
            )
        except GitHubApiError:
            return {"status": "error", "error": "github_unavailable"}
        err = data.get("error")
        if isinstance(err, str) and err:
            if err == "authorization_pending":
                return {"status": "pending", "error": err}
            if err == "slow_down":
                interval = data.get("interval")
                return {"status": "slow_down", "error": err, "interval": interval if _is_int(interval) and interval > 0 else 10}
            if err == "expired_token":
                return {"status": "expired", "error": err}
            if err == "access_denied":
                return {"status": "denied", "error": err}
            return {"status": "error", "error": err if re.fullmatch(r"[a-z_]{1,64}", err) else "unknown_error"}
        token = data.get("access_token")
        if status != 200 or not isinstance(token, str) or not token.strip():
            return {"status": "error", "error": "invalid_token_response"}
        return {"status": "success", "access_token": token.strip(), "scope": str(data.get("scope", ""))}

    # -- Authorization Code flow --

    def create_authorize_url(self, redirect_uri: str, state: str) -> str:
        if not self.web_flow_enabled:
            raise ValueError(f"Web login requires {ENV_CLIENT_ID} and {ENV_CLIENT_SECRET}")
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.scopes,
                "state": state,
                "allow_signup": "false",
            }
        )
        return f"{GITHUB_AUTHORIZE_URL}?{query}"

    def exchange_code_for_token(self, code: str, redirect_uri: str) -> dict[str, Any]:
        """Exchange an authorization code. Returns ``{"access_token": ..., "scope": ...}`` or raises."""
        if not self.web_flow_enabled:
            raise GitHubApiError("Web login is not configured")
        if not isinstance(code, str) or not _OPAQUE_RE.match(code):
            raise GitHubApiError("Invalid authorization code")
        status, data = self._post_form(
            GITHUB_ACCESS_TOKEN_URL,
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        if "error" in data:
            raise GitHubApiError(f"GitHub code exchange failed ({data.get('error')})")
        token = data.get("access_token")
        if status != 200 or not isinstance(token, str) or not token.strip():
            raise GitHubApiError(f"GitHub code exchange failed (HTTP {status})")
        return {"access_token": token.strip(), "scope": str(data.get("scope", ""))}

    # -- CSRF state (stateless, HMAC-bound, expiring, bound to a browser cookie) --

    def create_state(self, return_to: str = "/") -> str:
        now = int(self._clock())
        return sign_token(
            self._state_key,
            {"v": 1, "n": secrets.token_hex(16), "iat": now, "exp": now + DEFAULT_STATE_TTL_SECONDS, "r": safe_return_path(return_to)},
        )

    def validate_state(self, state: Any, cookie_state: Any) -> tuple[bool, str]:
        """Validate the callback ``state`` against the signed-cookie copy. Returns (ok, return_to)."""
        if not isinstance(state, str) or not isinstance(cookie_state, str) or not state or not cookie_state:
            return False, "/"
        if len(state) > MAX_TOKEN_CHARS or len(cookie_state) > MAX_TOKEN_CHARS:
            return False, "/"
        if not hmac.compare_digest(state.encode("utf-8", "replace"), cookie_state.encode("utf-8", "replace")):
            return False, "/"
        data = verify_token(self._state_key, state)
        if data is None or data.get("v") != 1:
            return False, "/"
        nonce, iat, exp = data.get("n"), data.get("iat"), data.get("exp")
        now = self._clock()
        if not isinstance(nonce, str) or not _is_int(iat) or not _is_int(exp):
            return False, "/"
        if exp <= now or iat > now + CLOCK_SKEW_SECONDS or exp - iat > DEFAULT_STATE_TTL_SECONDS:
            return False, "/"
        with self._nonce_lock:
            for k in [k for k, e in self._used_state_nonces.items() if e <= now]:
                del self._used_state_nonces[k]
            if nonce in self._used_state_nonces:
                return False, "/"
            self._used_state_nonces[nonce] = float(exp)
        return True, safe_return_path(data.get("r"))

    # -- Device handle (keeps the browser from submitting arbitrary device codes) --

    def seal_device_code(self, device_code: str, expires_in: int) -> str:
        now = int(self._clock())
        ttl = max(1, min(int(expires_in), MAX_DEVICE_HANDLE_TTL_SECONDS))
        return sign_token(self._device_key, {"v": 1, "dc": device_code, "iat": now, "exp": now + ttl})

    def open_device_handle(self, handle: Any) -> str | None:
        data = verify_token(self._device_key, handle)
        if data is None or data.get("v") != 1:
            return None
        dc, exp = data.get("dc"), data.get("exp")
        if not isinstance(dc, str) or not _OPAQUE_RE.match(dc) or not _is_int(exp) or exp <= self._clock():
            return None
        return dc

    # -- GitHub identity and admin verification --

    def fetch_user(self, access_token: str) -> tuple[str, int]:
        status, data = self._api_get("/user", access_token)
        if status != 200 or data is None:
            raise GitHubApiError(f"GitHub /user lookup failed (HTTP {status})")
        login, uid = data.get("login"), data.get("id")
        if not isinstance(login, str) or not _LOGIN_RE.match(login) or not _is_int(uid):
            raise GitHubApiError("GitHub /user returned an invalid identity")
        return login, uid

    def check_target_admin(self, access_token: str, login: str, target: ControlledTarget) -> TargetCheck:
        if self.verification_cache.is_verified(login, target.key):
            return TargetCheck(target.name, target.key, target.kind, True, "verified (cached)", cached=True)
        try:
            status, data = self._api_get(target.api_path(), access_token)
        except GitHubApiError as exc:
            return TargetCheck(target.name, target.key, target.kind, False, f"GitHub API error: {exc}")
        if status != 200 or data is None:
            return TargetCheck(target.name, target.key, target.kind, False, f"GitHub API returned HTTP {status}")
        if target.kind == "repo":
            perms = data.get("permissions")
            ok = isinstance(perms, dict) and perms.get("admin") is True
            reason = "repository admin" if ok else "permissions.admin is not true"
        else:
            ok = data.get("state") == "active" and data.get("role") == "admin"
            reason = "active organization admin" if ok else (
                f"membership state={data.get('state')!r} role={data.get('role')!r}; need active admin"
            )
        if ok:
            self.verification_cache.mark_verified(login, target.key)
        return TargetCheck(target.name, target.key, target.kind, ok, reason)

    def authorize(self, access_token: str, controlled_targets: Iterable[Any] | None) -> AuthorizationDecision:
        """Fail-closed authorization: allowlisted login AND admin on every controlled target."""
        if not isinstance(access_token, str) or not access_token.strip():
            return AuthorizationDecision(False, 403, "Forbidden: missing GitHub access token")
        token = access_token.strip()
        try:
            targets = parse_controlled_targets(controlled_targets)
        except ValueError as exc:
            return AuthorizationDecision(False, 403, f"Forbidden: controlled target configuration is invalid ({exc})")
        names = [t.name for t in targets]
        try:
            login, uid = self.fetch_user(token)
        except GitHubApiError as exc:
            return AuthorizationDecision(False, 403, f"Forbidden: could not verify GitHub identity ({exc})", controlled_targets=names)
        if not is_admin_login(login, self.allowed_admins):
            return AuthorizationDecision(
                False, 403, f"Forbidden: GitHub user '{login}' is not an authorized administrator",
                user_login=login, user_id=uid, controlled_targets=names,
            )
        checks = [self.check_target_admin(token, login, t) for t in targets]
        verified = [c.target for c in checks if c.is_admin]
        missing = [c.target for c in checks if not c.is_admin]
        if missing:
            return AuthorizationDecision(
                False, 403,
                "Forbidden: administrator privileges required on all controlled targets; missing: " + ", ".join(missing),
                user_login=login, user_id=uid, controlled_targets=names,
                verified_targets=verified, missing_targets=missing, checks=checks,
            )
        return AuthorizationDecision(
            True, 200, f"Authorized GitHub administrator '{login}'",
            user_login=login, user_id=uid, controlled_targets=names, verified_targets=verified, checks=checks,
        )

    def verify_and_authorize_token(
        self,
        access_token: str,
        controlled_targets: Iterable[Any] | None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Contract wrapper around :meth:`authorize`: returns (authorized, message, decision_dict)."""
        decision = self.authorize(access_token, controlled_targets)
        return decision.authorized, decision.message, decision.to_dict()

    # -- Sessions --

    def create_session_cookie_value(
        self,
        user: str,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        *,
        user_id: int | None = None,
        verified_targets: Iterable[str] = (),
    ) -> str:
        if not isinstance(user, str) or not _LOGIN_RE.match(user):
            raise ValueError("Session user must be a valid GitHub login")
        ttl = max(1, min(int(ttl_seconds), MAX_SESSION_TTL_SECONDS))
        now = int(self._clock())
        session = SessionData(
            user_login=user,
            user_id=user_id,
            issued_at=now,
            expires_at=now + ttl,
            verified_targets=tuple(str(t).lower() for t in verified_targets),
        )
        return sign_token(self._session_key, session.to_payload())

    def issue_session(self, decision: AuthorizationDecision) -> str:
        if not decision.authorized:
            raise ValueError("Cannot issue a session for an unauthorized decision")
        return self.create_session_cookie_value(
            decision.user_login,
            self.session_ttl_seconds,
            user_id=decision.user_id,
            verified_targets=decision.verified_targets,
        )

    def decode_session(self, cookie_val: Any) -> SessionData | None:
        data = verify_token(self._session_key, cookie_val)
        if data is None:
            return None
        session = SessionData.from_payload(data)
        if session is None:
            return None
        now = self._clock()
        if session.expires_at <= now or session.issued_at > now + CLOCK_SKEW_SECONDS:
            return None
        if session.expires_at - session.issued_at > MAX_SESSION_TTL_SECONDS:
            return None
        return session

    def verify_session_cookie_value(self, cookie_val: Any) -> tuple[bool, str]:
        """Return (True, login) for a correctly signed, unexpired session of an allowlisted user."""
        session = self.decode_session(cookie_val)
        if session is None or not is_admin_login(session.user_login, self.allowed_admins):
            return False, ""
        return True, session.user_login

    def check_session(self, cookie_val: Any, controlled_targets: Iterable[Any] | None) -> SessionCheck:
        session = self.decode_session(cookie_val)
        if session is None:
            return SessionCheck("none", message="No valid dashboard session")
        if not is_admin_login(session.user_login, self.allowed_admins):
            return SessionCheck(
                "forbidden", session, f"Forbidden: GitHub user '{session.user_login}' is not an authorized administrator"
            )
        try:
            targets = parse_controlled_targets(controlled_targets)
        except ValueError as exc:
            return SessionCheck("forbidden", session, f"Forbidden: controlled target configuration is invalid ({exc})")
        verified = {t.lower() for t in session.verified_targets}
        missing = tuple(t.name for t in targets if t.key not in verified)
        if missing:
            return SessionCheck(
                "forbidden",
                session,
                "Forbidden: session was not verified for all controlled targets; sign in again. Missing: "
                + ", ".join(missing),
                missing,
            )
        return SessionCheck("ok", session)


# --- Login gate page ---


def render_login_gate_html(
    *,
    device_flow: bool,
    web_flow: bool,
    legacy_token: bool,
    message: str = "",
    signed_in_as: str = "",
) -> str:
    """Render the unauthenticated login gate (device flow, web login link, optional legacy token form)."""
    msg_html = f'<p class="msg" id="msg">{html.escape(message)}</p>' if message else '<p class="msg" id="msg"></p>'
    who = (
        f'<p>Signed in as <strong>{html.escape(signed_in_as)}</strong>. '
        '<button type="button" id="logout">Sign out</button></p>'
        if signed_in_as
        else ""
    )
    web = (
        '<p><a class="btn" id="web-login" href="/auth/github/login">Sign in with GitHub</a></p>' if web_flow else ""
    )
    device = (
        '<section><h2>Sign in with a device code</h2>'
        '<button type="button" id="device-start">Get a GitHub device code</button>'
        '<div id="device-box" hidden><p>Open <a id="device-link" target="_blank" rel="noopener noreferrer"></a> '
        'and enter code <code id="device-code"></code></p><p id="device-status">Waiting for approval...</p></div>'
        "</section>"
        if device_flow
        else ""
    )
    legacy = (
        '<section><h2>Operator token</h2><form id="token-form"><input type="password" id="token" '
        'autocomplete="off" placeholder="Dashboard token" aria-label="Dashboard token"> '
        '<button type="submit">Sign in</button></form></section>'
        if legacy_token
        else ""
    )
    script = """
(function () {
  const msg = document.getElementById("msg");
  function show(text) { msg.textContent = text || ""; }
  async function postJson(url, body) {
    const res = await fetch(url, {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
    let data = {};
    try { data = await res.json(); } catch (_e) { data = {}; }
    return [res, data];
  }
  const logout = document.getElementById("logout");
  if (logout) logout.addEventListener("click", async function () {
    await postJson("/auth/logout", {}); window.location.assign("/");
  });
  const start = document.getElementById("device-start");
  if (start) start.addEventListener("click", async function () {
    start.disabled = true; show("");
    const [res, d] = await postJson("/auth/device/code", {});
    if (!res.ok) { show(d.error || "Device login is unavailable"); start.disabled = false; return; }
    document.getElementById("device-box").hidden = false;
    document.getElementById("device-code").textContent = d.user_code;
    const link = document.getElementById("device-link");
    link.href = d.verification_uri; link.textContent = d.verification_uri;
    const status = document.getElementById("device-status");
    let interval = Math.max(5, Number(d.interval) || 5);
    const deadline = Date.now() + (Number(d.expires_in) || 900) * 1000;
    async function poll() {
      if (Date.now() > deadline) { status.textContent = "Code expired. Request a new one."; start.disabled = false; return; }
      const [pr, pd] = await postJson("/auth/device/token", {device_handle: d.device_handle});
      if (pr.ok && pd.status === "authorized") { window.location.assign("/"); return; }
      if (pr.ok && (pd.status === "pending" || pd.status === "slow_down")) {
        if (pd.interval) interval = Math.max(interval, Number(pd.interval) || interval);
        setTimeout(poll, interval * 1000); return;
      }
      status.textContent = pd.message || pd.error || "Sign-in failed"; start.disabled = false;
    }
    setTimeout(poll, interval * 1000);
  });
  const form = document.getElementById("token-form");
  if (form) form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    const [res, d] = await postJson("/api/auth/login", {token: document.getElementById("token").value});
    if (res.ok) { window.location.assign("/"); } else { show(d.error || "Invalid token"); }
  });
})();
"""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Spot Dashboard sign-in</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;"
        "color:#1f2328;background:#fff}.btn{display:inline-block;padding:.6rem 1rem;background:#1f2328;color:#fff;"
        "border-radius:6px;text-decoration:none}.msg{color:#cf222e}code{font-size:1.4rem}"
        "section{margin-top:1.5rem}</style></head><body>"
        "<h1>Spot Dashboard sign-in</h1>"
        "<p>Access is limited to allowlisted GitHub administrators of every controlled repository and organization.</p>"
        f"{msg_html}{who}{web}{device}{legacy}"
        f"<script>{script}</script></body></html>"
    )
