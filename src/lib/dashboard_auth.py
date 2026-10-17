"""GitHub OAuth 2.0 login and multi-target administrator verification for the Web UI Dashboard.

This module is standard-library only. It provides:

* GitHub OAuth 2.0 Authorization Code flow with PKCE (S256), enabled when a client secret is configured.
* GitHub OAuth 2.0 Device Flow (RFC 8628), disabled unless ``GITHUB_OAUTH_DEVICE_FLOW=1``.
* Fail-closed authorization: the GitHub identity must be the dashboard owner (login ``swackhamer`` with
  numeric ID 619624, hardcoded; there is no override) or an administrator that the owner added on the
  dashboard's Admins page (matched by numeric GitHub ID only, so a rename keeps access and a recycled
  login never inherits it; the caller passes the current list on every check) AND, unless the identity
  is a global administrator, the user must be an administrator of every configured controlled target
  (``GET /repos/{owner}/{repo}`` -> ``permissions.admin is True``; ``GET /user/memberships/orgs/{org}``
  -> ``state == "active"`` and ``role == "admin"``). Any API error denies access.
* Stateless HMAC-SHA256 tokens for sessions and CSRF ``state`` values. The GitHub access token is
  never stored in any token or cookie.
* Bounded, thread-safe rate limiting for the unauthenticated login endpoints, a bounded single-use
  nonce set for ``state`` that refuses new sign-ins instead of evicting live nonces, and
  server-enforced device-flow polling intervals.

Token format (verifiable by any process holding the same session secret, for example an
edge worker or a replacement VM)::

    purpose_key = HMAC-SHA256(key=session_secret_utf8, msg=b"spot-dashboard/<purpose>/v1")
    token       = base64url_nopad(compact_json) + "." + hex(HMAC-SHA256(purpose_key, base64url_part_ascii))

``<purpose>`` is ``session`` or ``oauth-state``. Session payloads are
``{"v": 1, "sub": login, "uid": github_id, "iat": epoch, "exp": epoch, "tgt": [verified target keys]}``
and travel in the ``__Host-spot_session`` cookie. A session grants access only while
``iat - 60 <= now < exp``, ``iat >= DASHBOARD_SESSION_NOT_BEFORE``, the identity is the owner
(``sub == "swackhamer"`` and ``uid == 619624``) or ``uid`` is in the added-administrator list passed to
:meth:`GitHubOAuthManager.check_session` for THIS request (so removing an administrator rejects its
existing sessions on their next request), and, unless the identity is a global administrator, every
currently configured controlled target key appears in ``tgt``.

A pending controlled-target change travels inside the signed ``state`` (web flow) or the server-side
device handshake, never in the session. It commits only after a fresh, uncached GitHub check.

Per-process state (login handshakes only): device-flow handles are opaque random strings that map to
the GitHub ``device_code`` in a bounded in-memory table, so the browser never sees the ``device_code``.
The device poll must reach the process that issued the handle; a VM replacement abandons in-flight
device logins (request a new code). Sessions stay stateless.

Residual risk (stateless sessions): a session cannot be revoked individually. A GitHub admin removal on
an existing target is noticed at the next sign-in, so a session can outlive it by up to
``DASHBOARD_SESSION_TTL_SECONDS``. For bulk revocation set ``DASHBOARD_SESSION_NOT_BEFORE`` to the
current Unix time on every dashboard and edge worker, or rotate ``DASHBOARD_SESSION_SECRET``.

The PKCE ``code_verifier`` is derived statelessly as ``base64url(HMAC-SHA256(pkce_key, state))`` with
``pkce_key = HMAC-SHA256(session_secret, "spot-dashboard/pkce/v1")``; it never leaves the server.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.parse
import urllib.request

# The dashboard owner. Hardcoded on purpose: there is no environment or CLI override. The owner can always
# sign in (subject to the per-target policy) and is the only identity that may change the list of added
# administrators. Added administrators live in dashboard state and are matched by numeric ID only.
ALLOWED_ADMIN_LOGIN = "swackhamer"
ALLOWED_ADMIN_ID = 619624
DEFAULT_ALLOWED_ADMINS: frozenset[str] = frozenset({ALLOWED_ADMIN_LOGIN})
OWNER_IDENTITIES: frozenset[tuple[str, int]] = frozenset({(ALLOWED_ADMIN_LOGIN, ALLOWED_ADMIN_ID)})
MAX_ADDED_ADMINS = 50

ENV_CLIENT_ID = "GITHUB_OAUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "GITHUB_OAUTH_CLIENT_SECRET"
ENV_SCOPES = "GITHUB_OAUTH_SCOPES"
ENV_REDIRECT_URI = "GITHUB_OAUTH_REDIRECT_URI"
ENV_SESSION_SECRET = "DASHBOARD_SESSION_SECRET"
ENV_SESSION_TTL = "DASHBOARD_SESSION_TTL_SECONDS"
ENV_CONTROLLED_TARGETS = "DASHBOARD_CONTROLLED_TARGETS"
ENV_DEVICE_FLOW = "GITHUB_OAUTH_DEVICE_FLOW"
ENV_SESSION_NOT_BEFORE = "DASHBOARD_SESSION_NOT_BEFORE"
DEFAULT_CONFIG_REPO = "swack-tools/spot-runners-config"
DEFAULT_CONFIG_TARGETS_TTL_SECONDS = 300.0
MAX_USED_STATE_NONCES = 4096
MAX_DEVICE_HANDSHAKES = 1024
MAX_DYNAMIC_CONTROLLED_TARGETS = 32
DEVICE_MIN_INTERVAL_SECONDS = 5
DEVICE_SLOW_DOWN_INCREMENT_SECONDS = 5
DEVICE_MAX_INTERVAL_SECONDS = 60
# Rate limits as (per-client-IP requests, global requests) per 60-second window.
LOGIN_START_RATE_LIMIT = (10, 30)  # /auth/device/code, /auth/github/login, target-change re-auth
LOGIN_COMPLETE_RATE_LIMIT = (60, 240)  # /auth/device/token, /auth/github/callback
RATE_LIMIT_WINDOW_SECONDS = 60.0
MAX_RATE_LIMIT_KEYS = 4096

# ``__Host-`` cookies must be Secure, Path=/, and carry no Domain, so a sibling subdomain cannot set,
# overwrite, or shadow them. The edge Worker (cloudflare/worker.js) reads the same session cookie name.
SESSION_COOKIE_NAME = "__Host-spot_session"
OAUTH_STATE_COOKIE_NAME = "__Host-spot_oauth_state"
OAUTH_STATE_COOKIE_PATH = "/"

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
# Browsers cap a cookie (name, value, and attributes) near 4096 bytes; keep headroom.
MAX_SESSION_COOKIE_BYTES = 3900
MAX_STATE_TOKEN_CHARS = 3072  # Keeps the state cookie well under the browser cookie limit.
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

AUDIT_LOG = logging.getLogger("spot_dashboard.auth")


def _audit_login_field(login: Any) -> str:
    return str(login or "-") if _LOGIN_RE.match(str(login or "")) else "-"


def audit_login(login: str, uid: Any, result: str, detail: str = "") -> None:
    """Write one login audit line. Never pass tokens, codes, or secrets here."""
    clean_login = _audit_login_field(login)
    clean_uid = str(uid) if _is_int(uid) else "-"
    clean_detail = re.sub(r"[^A-Za-z0-9 ._:/,'()=-]", "?", str(detail or ""))[:300]
    AUDIT_LOG.info("dashboard login login=%s uid=%s result=%s detail=%s", clean_login, clean_uid, result, clean_detail)


def audit_admin_change(action: str, login: Any, uid: Any, actor: Any, actor_uid: Any) -> None:
    """Write one audit line for an added-administrator change (``action`` is ``added`` or ``removed``)."""
    clean_action = action if action in ("added", "removed") else "-"
    AUDIT_LOG.info(
        "dashboard admin %s login=%s uid=%s by=%s by_uid=%s", clean_action, _audit_login_field(login),
        str(uid) if _is_int(uid) else "-", _audit_login_field(actor), str(actor_uid) if _is_int(actor_uid) else "-",
    )


class GitHubApiError(RuntimeError):
    """Raised when a GitHub OAuth or REST call fails or returns an unusable response."""


class GitHubUserLookupError(RuntimeError):
    """A GitHub login could not be resolved to a user account.

    ``status`` is the HTTP status the dashboard answers with: 400 (invalid login, or the account is not a
    user, for example an organization), 404 (no such account), or 424 (GitHub failed or gave an unusable
    answer; ``retryable`` is True and nothing was changed).
    """

    def __init__(self, message: str, status: int, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = int(status)
        self.retryable = bool(retryable)


class AuthCapacityError(RuntimeError):
    """Raised when a bounded login table is full; callers answer 429 with ``Retry-After``."""

    def __init__(self, message: str, retry_after: float = 60.0) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(math.ceil(retry_after)))


# --- Rate limiting ---


class RateLimiter:
    """Thread-safe fixed-window rate limiter with a per-key limit, a global limit, and bounded keys.

    When the key table is full and no window has expired, unseen keys are refused (fail closed)
    instead of evicting live windows, so rotating source addresses cannot reset other clients.
    """

    def __init__(
        self,
        per_key_limit: int,
        global_limit: int,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        *,
        max_keys: int = MAX_RATE_LIMIT_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_key_limit < 1 or global_limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("Rate limits, window, and key capacity must be positive")
        self.per_key_limit = int(per_key_limit)
        self.global_limit = int(global_limit)
        self.window_seconds = float(window_seconds)
        self.max_keys = int(max_keys)
        self._clock = clock
        self._keys: dict[str, list[float]] = {}  # key -> [window_start, count]
        self._global = [float("-inf"), 0]
        self._lock = threading.Lock()

    def check(self, key: str) -> float:
        """Count one request. Returns 0.0 when allowed, otherwise the seconds to wait (Retry-After)."""
        now = float(self._clock())
        with self._lock:
            if now - self._global[0] >= self.window_seconds:
                self._global = [now, 0]
            entry = self._keys.get(key)
            if entry is not None and now - entry[0] >= self.window_seconds:
                entry = None
                del self._keys[key]
            if entry is None and len(self._keys) >= self.max_keys:
                for k in [k for k, e in self._keys.items() if now - e[0] >= self.window_seconds]:
                    del self._keys[k]
                if len(self._keys) >= self.max_keys:
                    oldest = min(e[0] for e in self._keys.values())
                    return max(1.0, oldest + self.window_seconds - now)
            if self._global[1] >= self.global_limit:
                return max(1.0, self._global[0] + self.window_seconds - now)
            if entry is not None and entry[1] >= self.per_key_limit:
                return max(1.0, entry[0] + self.window_seconds - now)
            if entry is None:
                entry = [now, 0]
                self._keys[key] = entry
            entry[1] += 1
            self._global[1] += 1
            return 0.0


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


ENV_GLOBAL_ADMINS = "DASHBOARD_GLOBAL_ADMINS"
DEFAULT_GLOBAL_ADMINS = f"{ALLOWED_ADMIN_LOGIN}:{ALLOWED_ADMIN_ID}"


def parse_global_admins(raw: str | None) -> frozenset[tuple[str, int]]:
    """Parse ``login:uid`` entries (comma or whitespace separated) into pinned identities.

    Global administrators may sign in without being an administrator of every controlled target.
    Each entry is pinned to the numeric GitHub user ID so a renamed or recycled login never
    inherits the exception. Unset or empty means the default (the hardcoded owner), so an
    empty line copied from .env.example changes nothing; ``none`` disables global administrators,
    which also holds administrators added on the Admins page to the per-target checks.
    """
    text = str(raw or "").strip() or DEFAULT_GLOBAL_ADMINS
    if text.lower() == "none":
        return frozenset()
    out = set()
    for item in _split_list(text):
        login, sep, uid = item.partition(":")
        if not sep or not _LOGIN_RE.match(login) or not uid.isdigit() or int(uid) <= 0:
            raise ValueError(f"{ENV_GLOBAL_ADMINS} entries must be login:numeric_id, got {item!r}")
        if not is_allowed_identity(login, int(uid)):
            # Owners stay deploy-configured and fixed: this setting may list only the hardcoded owner. Other
            # administrators are added on the dashboard's Admins page, which only the owner can change.
            raise ValueError(f"{ENV_GLOBAL_ADMINS} may only list the dashboard owner "
                             f"{ALLOWED_ADMIN_LOGIN}:{ALLOWED_ADMIN_ID}, got {item!r}; add other "
                             "administrators on the dashboard's Admins page")
        out.add((login.lower(), int(uid)))
    return frozenset(out)


def is_allowed_identity(login: Any, uid: Any) -> bool:
    """True only for the hardcoded dashboard owner (login compared case-insensitively, ID exactly)."""
    return (
        isinstance(login, str)
        and login.lower() == ALLOWED_ADMIN_LOGIN
        and _is_int(uid)
        and uid == ALLOWED_ADMIN_ID
    )


def normalize_added_admin_uids(values: Any) -> frozenset[int]:
    """The positive integer GitHub IDs in ``values``; anything else is dropped (it never grants access).

    Owner IDs are dropped too: an owner is recognized only by login AND ID, so a session that carries an
    owner's ID under another login can never pass as an added administrator.
    """
    if values is None or isinstance(values, (str, bytes)):
        return frozenset()
    try:
        items = list(values)
    except TypeError:
        return frozenset()
    owner_ids = {uid for _, uid in OWNER_IDENTITIES}
    return frozenset(v for v in items if _is_int(v) and v > 0 and v not in owner_ids)


def normalize_github_login(value: Any) -> str:
    """Validate a GitHub login typed into the Admins page (one leading ``@`` allowed). Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError("login must be a GitHub username")
    clean = value.strip()
    if clean.startswith("@"):
        clean = clean[1:]
    # The same login syntax as everywhere else in this module (it admits legacy GitHub logins with double
    # or trailing hyphens); GitHub itself then decides whether the account exists.
    if not _LOGIN_RE.match(clean):
        raise ValueError("login must be a GitHub username: 1-39 letters, digits, or hyphens, not starting "
                         "with a hyphen")
    return clean


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


def target_list_digest(names: Iterable[str]) -> str:
    """SHA-256 of a dynamic target list, used as a compare-and-swap base for pending changes."""
    return hashlib.sha256(json.dumps([str(n) for n in names], separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TargetChange:
    """A proposed dynamic controlled-target list awaiting a fresh GitHub verification.

    ``targets`` is the proposed list, ``base`` is :func:`target_list_digest` of the list it replaces
    (the change is refused if the stored list moved on), and ``uid``/``login`` identify the session
    that proposed it; only the same GitHub account can complete it.
    """

    targets: tuple[str, ...]
    base: str
    uid: int
    login: str

    def to_payload(self) -> dict[str, Any]:
        return {"t": list(self.targets), "b": self.base, "u": self.uid, "l": self.login}

    @classmethod
    def from_payload(cls, data: Any) -> TargetChange | None:
        if not isinstance(data, Mapping):
            return None
        t, b, u, login = data.get("t"), data.get("b"), data.get("u"), data.get("l")
        if not isinstance(t, list) or len(t) > MAX_DYNAMIC_CONTROLLED_TARGETS:
            return None
        try:
            names = tuple(x.name for x in parse_controlled_targets(t))
        except ValueError:
            return None
        if len(names) != len(t) or not isinstance(b, str) or not _HEX64_RE.match(b):
            return None
        if not _is_int(u) or u <= 0 or not isinstance(login, str) or not _LOGIN_RE.match(login):
            return None
        return cls(targets=names, base=b, uid=u, login=login)


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
    """Accept only same-origin absolute paths of printable ASCII (no scheme, host, or backslash).

    Anything outside 0x21-0x7E (controls, spaces, DEL, non-ASCII) falls back to ``/`` so the value is
    always safe to emit in a ``Location`` header.
    """
    if not isinstance(value, str) or not value or len(value) > 512:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(not 0x21 <= ord(ch) <= 0x7E for ch in value):
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
        if not _is_int(uid):
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
    """Thread-safe, bounded cache of POSITIVE admin verifications keyed by (uid, login, target key).

    Keying by the GitHub numeric id as well as the login means a renamed or recreated account never
    inherits another account's cached verification. Denials and API errors are never cached, and a
    fresh denial or error drops any earlier positive entry for that account and target.
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
        self._entries: dict[tuple[int, str, str], float] = {}
        self._lock = threading.Lock()

    def is_verified(self, uid: int, login: str, target_key: str) -> bool:
        k = (int(uid), login.lower(), target_key.lower())
        now = self._clock()
        with self._lock:
            exp = self._entries.get(k)
            if exp is None:
                return False
            if exp <= now:
                del self._entries[k]
                return False
            return True

    def mark_verified(self, uid: int, login: str, target_key: str) -> None:
        if self.ttl_seconds <= 0:
            return
        now = self._clock()
        with self._lock:
            if len(self._entries) >= self.max_entries:
                for k in [k for k, exp in self._entries.items() if exp <= now]:
                    del self._entries[k]
                while len(self._entries) >= self.max_entries:
                    del self._entries[next(iter(self._entries))]
            self._entries[(int(uid), login.lower(), target_key.lower())] = now + self.ttl_seconds

    def invalidate(self, uid: int, login: str, target_key: str) -> None:
        """Drop a cached grant once a fresh check failed to confirm it."""
        with self._lock:
            self._entries.pop((int(uid), login.lower(), target_key.lower()), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


@dataclass(frozen=True)
class OAuthStateResult:
    """Outcome of validating an Authorization Code ``state`` against the browser's state cookie."""

    ok: bool
    return_to: str = "/"
    target_change: TargetChange | None = None


@dataclass
class DeviceHandshake:
    """Server-side record behind an opaque device handle (never sent to the browser)."""

    device_code: str
    expires_at: float
    interval: int
    next_poll_at: float
    target_change: TargetChange | None = None


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
        device_flow: bool = False,
        session_not_before: int = 0,
        global_admins: Iterable[tuple[str, int]] | None = None,
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
        if allowed_admins is not None and normalize_admin_logins(allowed_admins) != DEFAULT_ALLOWED_ADMINS:
            raise ValueError(
                f"The dashboard administrator is fixed to GitHub user '{ALLOWED_ADMIN_LOGIN}' (ID {ALLOWED_ADMIN_ID}) "
                "and cannot be overridden"
            )
        admins = DEFAULT_ALLOWED_ADMINS
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
        if not _is_int(session_not_before) or session_not_before < 0:
            raise ValueError(f"{ENV_SESSION_NOT_BEFORE} must be a non-negative integer Unix timestamp")
        if session_not_before > clock() + CLOCK_SKEW_SECONDS:
            # A future cut-off would also reject every newly issued session (sign-in loop).
            raise ValueError(f"{ENV_SESSION_NOT_BEFORE} must not be in the future")
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
        self.session_not_before = int(session_not_before)
        # Programmatic default: none. from_environ applies the configured default (the hardcoded admin).
        self.global_admins: frozenset[tuple[str, int]] = frozenset(
            (str(login).lower(), int(uid)) for login, uid in (global_admins or ()))
        self._session_key = derive_purpose_key(secret_bytes, "session")
        self._state_key = derive_purpose_key(secret_bytes, "oauth-state")
        self._pkce_key = derive_purpose_key(secret_bytes, "pkce")
        self._device_flow = bool(device_flow)
        self._used_state_nonces: dict[str, float] = {}
        self._nonce_lock = threading.Lock()
        self._device_handshakes: dict[str, DeviceHandshake] = {}
        self._device_lock = threading.Lock()
        self.rate_limiters: dict[str, RateLimiter] = {
            "start": RateLimiter(*LOGIN_START_RATE_LIMIT, clock=clock),
            "complete": RateLimiter(*LOGIN_COMPLETE_RATE_LIMIT, clock=clock),
        }

    # -- configuration --

    @property
    def device_flow_enabled(self) -> bool:
        return self._device_flow

    @property
    def web_flow_enabled(self) -> bool:
        return bool(self.client_secret)

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> GitHubOAuthManager | None:
        """Build a manager from the environment, or return None when OAuth is not configured.

        OAuth is enabled by ``GITHUB_OAUTH_CLIENT_ID``. When enabled, ``DASHBOARD_SESSION_SECRET`` is
        mandatory; there is no generated or hardcoded fallback secret. The device flow is enabled only
        by ``GITHUB_OAUTH_DEVICE_FLOW=1``. ``DASHBOARD_SESSION_NOT_BEFORE`` (Unix time) rejects every
        session issued before it.
        """
        env = os.environ if environ is None else environ
        client_id = str(env.get(ENV_CLIENT_ID, "") or "").strip()
        client_secret = str(env.get(ENV_CLIENT_SECRET, "") or "").strip()
        if not client_id:
            if client_secret:
                raise ValueError(f"{ENV_CLIENT_SECRET} is set but {ENV_CLIENT_ID} is not")
            return None
        ttl_raw = str(env.get(ENV_SESSION_TTL, "") or "").strip()
        try:
            ttl = int(ttl_raw) if ttl_raw else DEFAULT_DASHBOARD_SESSION_TTL_SECONDS
        except ValueError as exc:
            raise ValueError(f"{ENV_SESSION_TTL} must be an integer number of seconds") from exc
        nbf_raw = str(env.get(ENV_SESSION_NOT_BEFORE, "") or "").strip()
        try:
            nbf = int(nbf_raw) if nbf_raw else 0
        except ValueError as exc:
            raise ValueError(f"{ENV_SESSION_NOT_BEFORE} must be an integer Unix timestamp") from exc
        params: dict[str, Any] = {
            "scopes": str(env.get(ENV_SCOPES, "") or "") or DEFAULT_SCOPES,
            "redirect_uri": str(env.get(ENV_REDIRECT_URI, "") or ""),
            "session_ttl_seconds": ttl,
            "device_flow": str(env.get(ENV_DEVICE_FLOW, "") or "").strip() == "1",
            "session_not_before": nbf,
            "global_admins": parse_global_admins(env.get(ENV_GLOBAL_ADMINS)),
        }
        params.update(kwargs)
        return cls(client_id, client_secret, None, str(env.get(ENV_SESSION_SECRET, "") or ""), **params)

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
        """Request a device and user code. Raises GitHubApiError on any failure or when the flow is disabled."""
        if not self.device_flow_enabled:
            raise GitHubApiError(f"GitHub device flow is disabled (set {ENV_DEVICE_FLOW}=1 to enable it)")
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
        if not self.device_flow_enabled:
            return {"status": "error", "error": "device_flow_disabled"}
        if not isinstance(device_code, str) or not _OPAQUE_RE.match(device_code):
            return {"status": "error", "error": "invalid_request"}
        try:
            status, data = self._post_form(
                GITHUB_ACCESS_TOKEN_URL,
                {"client_id": self.client_id, "device_code": device_code, "grant_type": DEVICE_GRANT_TYPE},
            )
        except GitHubApiError:
            return {"status": "error", "error": "github_unavailable"}
        if status == 429 or status >= 500:
            # GitHub is overloaded or failing; the device code may still be valid, so keep the handshake.
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

    def pkce_verifier(self, state: str) -> str:
        """Stateless PKCE code_verifier bound to the signed ``state``; 43 base64url characters (RFC 7636)."""
        if not isinstance(state, str) or not state or len(state) > MAX_TOKEN_CHARS:
            raise ValueError("PKCE requires a non-empty OAuth state")
        return _b64encode(hmac.new(self._pkce_key, state.encode("utf-8"), hashlib.sha256).digest())

    @staticmethod
    def pkce_challenge(verifier: str) -> str:
        return _b64encode(hashlib.sha256(verifier.encode("ascii")).digest())

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
                "code_challenge": self.pkce_challenge(self.pkce_verifier(state)),
                "code_challenge_method": "S256",
            }
        )
        return f"{GITHUB_AUTHORIZE_URL}?{query}"

    def exchange_code_for_token(self, code: str, redirect_uri: str, state: str) -> dict[str, Any]:
        """Exchange an authorization code with its PKCE verifier. Returns ``{"access_token", "scope"}`` or raises."""
        if not self.web_flow_enabled:
            raise GitHubApiError("Web login is not configured")
        if not isinstance(code, str) or not _OPAQUE_RE.match(code):
            raise GitHubApiError("Invalid authorization code")
        try:
            verifier = self.pkce_verifier(state)
        except ValueError as exc:
            raise GitHubApiError("Missing OAuth state for the PKCE verifier") from exc
        status, data = self._post_form(
            GITHUB_ACCESS_TOKEN_URL,
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        if "error" in data:
            raise GitHubApiError(f"GitHub code exchange failed ({data.get('error')})")
        token = data.get("access_token")
        if status != 200 or not isinstance(token, str) or not token.strip():
            raise GitHubApiError(f"GitHub code exchange failed (HTTP {status})")
        return {"access_token": token.strip(), "scope": str(data.get("scope", ""))}

    # -- CSRF state (stateless, HMAC-bound, expiring, bound to a browser cookie) --

    def _prune_used_nonces(self, now: float) -> None:
        for k in [k for k, e in self._used_state_nonces.items() if e <= now]:
            del self._used_state_nonces[k]

    def _nonce_capacity_retry_after(self, now: float) -> float:
        return max(1.0, min(self._used_state_nonces.values()) - now) if self._used_state_nonces else 1.0

    def create_state(self, return_to: str = "/", *, target_change: TargetChange | None = None) -> str:
        """Create a signed, expiring ``state``; optionally bind a pending controlled-target change.

        Raises :class:`AuthCapacityError` when the single-use nonce table is full (new sign-ins are
        refused instead of evicting live nonces) and ValueError when the state would not fit in a cookie.
        """
        now_f = self._clock()
        with self._nonce_lock:
            self._prune_used_nonces(now_f)
            if len(self._used_state_nonces) >= MAX_USED_STATE_NONCES:
                raise AuthCapacityError(
                    "Too many recent sign-ins; try again later", self._nonce_capacity_retry_after(now_f)
                )
        now = int(now_f)
        payload: dict[str, Any] = {
            "v": 1,
            "n": secrets.token_hex(16),
            "iat": now,
            "exp": now + DEFAULT_STATE_TTL_SECONDS,
            "r": safe_return_path(return_to),
        }
        if target_change is not None:
            payload["tc"] = target_change.to_payload()
        state = sign_token(self._state_key, payload)
        if len(state) > MAX_STATE_TOKEN_CHARS:
            raise ValueError("The proposed controlled-target list is too large for a sign-in request")
        return state

    def consume_state(self, state: Any, cookie_state: Any) -> OAuthStateResult:
        """Validate the callback ``state`` against the browser's state cookie and mark it used (single use).

        On success the result carries the sanitized return path and any bound :class:`TargetChange`.
        When the used-nonce table is full the state is refused, because single use could not be
        guaranteed.
        """
        fail = OAuthStateResult(False)
        if not isinstance(state, str) or not isinstance(cookie_state, str) or not state or not cookie_state:
            return fail
        if len(state) > MAX_TOKEN_CHARS or len(cookie_state) > MAX_TOKEN_CHARS:
            return fail
        if not hmac.compare_digest(state.encode("utf-8", "replace"), cookie_state.encode("utf-8", "replace")):
            return fail
        data = verify_token(self._state_key, state)
        if data is None or data.get("v") != 1:
            return fail
        nonce, iat, exp = data.get("n"), data.get("iat"), data.get("exp")
        now = self._clock()
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce) or not _is_int(iat) or not _is_int(exp):
            return fail
        if exp <= now or iat > now + CLOCK_SKEW_SECONDS or exp - iat > DEFAULT_STATE_TTL_SECONDS:
            return fail
        change = None
        if "tc" in data:
            change = TargetChange.from_payload(data.get("tc"))
            if change is None:
                return fail
        with self._nonce_lock:
            self._prune_used_nonces(now)
            if nonce in self._used_state_nonces:
                return fail
            if len(self._used_state_nonces) >= MAX_USED_STATE_NONCES:
                return fail
            self._used_state_nonces[nonce] = float(exp)
        return OAuthStateResult(True, safe_return_path(data.get("r")), change)

    def validate_state(self, state: Any, cookie_state: Any) -> tuple[bool, str]:
        """Contract wrapper around :meth:`consume_state`: returns (ok, return_to)."""
        result = self.consume_state(state, cookie_state)
        return result.ok, result.return_to

    # -- Device login handshakes (opaque handle -> server-side device_code) --

    def _prune_device_handshakes(self, now: float) -> None:
        for k in [k for k, h in self._device_handshakes.items() if h.expires_at <= now]:
            del self._device_handshakes[k]

    def begin_device_login(self, target_change: TargetChange | None = None) -> dict[str, Any]:
        """Start a device login and return the browser-safe fields plus an opaque ``device_handle``.

        Raises :class:`AuthCapacityError` (before calling GitHub) when the handshake table is full,
        and :class:`GitHubApiError` when GitHub refuses or the device flow is disabled. The
        ``device_code`` stays server-side.
        """
        if not self.device_flow_enabled:
            raise GitHubApiError(f"GitHub device flow is disabled (set {ENV_DEVICE_FLOW}=1 to enable it)")
        with self._device_lock:
            self._prune_device_handshakes(self._clock())
            if len(self._device_handshakes) >= MAX_DEVICE_HANDSHAKES:
                raise AuthCapacityError("Too many device logins in progress; try again later")
        started = self.start_device_flow()
        now = self._clock()
        handle = secrets.token_urlsafe(32)
        with self._device_lock:
            self._prune_device_handshakes(now)
            if len(self._device_handshakes) >= MAX_DEVICE_HANDSHAKES:
                raise AuthCapacityError("Too many device logins in progress; try again later")
            self._device_handshakes[handle] = DeviceHandshake(
                device_code=started["device_code"],
                expires_at=now + started["expires_in"],
                interval=started["interval"],
                next_poll_at=now + started["interval"],
                target_change=target_change,
            )
        return {
            "user_code": started["user_code"],
            "verification_uri": started["verification_uri"],
            "expires_in": started["expires_in"],
            "interval": started["interval"],
            "device_handle": handle,
        }

    def poll_device_login(self, handle: Any) -> dict[str, Any]:
        """Poll GitHub for an opaque handle, enforcing the polling interval server-side.

        A poll that arrives before the handle's next allowed time is answered ``slow_down`` (with the
        interval raised by 5 seconds, as RFC 8628 requires of clients) without contacting GitHub.
        Terminal outcomes consume the handle; ``github_unavailable`` keeps it for a retry. ``success``
        carries ``access_token`` (never forward it) and the bound ``target_change`` (or None).
        """
        if not self.device_flow_enabled:
            return {"status": "error", "error": "device_flow_disabled"}
        if not isinstance(handle, str) or not handle or len(handle) > 128:
            return {"status": "invalid"}
        now = self._clock()
        with self._device_lock:
            entry = self._device_handshakes.get(handle)
            if entry is None:
                return {"status": "invalid"}
            if entry.expires_at <= now:
                del self._device_handshakes[handle]
                return {"status": "expired", "error": "expired_token"}
            if now < entry.next_poll_at:
                entry.interval = min(entry.interval + DEVICE_SLOW_DOWN_INCREMENT_SECONDS, DEVICE_MAX_INTERVAL_SECONDS)
                entry.next_poll_at = now + entry.interval
                return {"status": "slow_down", "error": "slow_down", "interval": entry.interval}
            entry.next_poll_at = now + entry.interval
            device_code, change = entry.device_code, entry.target_change
        polled = self.poll_device_token(device_code)
        status = polled.get("status")
        with self._device_lock:
            entry = self._device_handshakes.get(handle)
            if status == "slow_down" and entry is not None:
                gh_interval = polled.get("interval")
                bumped = entry.interval + DEVICE_SLOW_DOWN_INCREMENT_SECONDS
                if _is_int(gh_interval) and gh_interval > bumped:
                    bumped = gh_interval
                entry.interval = min(bumped, DEVICE_MAX_INTERVAL_SECONDS)
                entry.next_poll_at = self._clock() + entry.interval
                polled = {**polled, "interval": entry.interval}
            elif status not in ("pending", "slow_down") and not (
                status == "error" and polled.get("error") == "github_unavailable"
            ):
                self._device_handshakes.pop(handle, None)
        if status == "success":
            polled = {**polled, "target_change": change}
        return polled

    # -- GitHub identity and admin verification --

    def fetch_user(self, access_token: str) -> tuple[str, int]:
        status, data = self._api_get("/user", access_token)
        if status != 200 or data is None:
            raise GitHubApiError(f"GitHub /user lookup failed (HTTP {status})")
        login, uid = data.get("login"), data.get("id")
        if not isinstance(login, str) or not _LOGIN_RE.match(login) or not _is_int(uid):
            raise GitHubApiError("GitHub /user returned an invalid identity")
        return login, uid

    def lookup_user(self, login: Any, token: str = "") -> dict[str, Any]:
        """Resolve a GitHub login to ``{"login", "uid", "type"}`` with ``GET /users/{login}``.

        Uses the injectable transport. ``token`` (optional) raises the API rate limit; without it the
        request is anonymous. Raises :class:`GitHubUserLookupError`: 400 for an invalid login or an
        account that is not a user (organizations, bots), 404 when GitHub has no such account, and 424
        (retryable) when GitHub fails or answers with something unusable.
        """
        try:
            clean = normalize_github_login(login)
        except ValueError as exc:
            raise GitHubUserLookupError(str(exc), 400) from exc
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token and token.strip():
            headers["Authorization"] = f"Bearer {token.strip()}"
        path = "/users/" + urllib.parse.quote(clean, safe="")
        try:
            status, data = self._call("GET", GITHUB_API_BASE + path, headers, None)
        except GitHubApiError as exc:
            raise GitHubUserLookupError(f"Could not look up GitHub user '{clean}': {exc}", 424, retryable=True) from exc
        if status == 404:
            raise GitHubUserLookupError(f"GitHub has no user named '{clean}'", 404)
        if status != 200:
            raise GitHubUserLookupError(f"GitHub user lookup for '{clean}' returned HTTP {status}", 424, retryable=True)
        try:
            body = _json_object(data)
        except GitHubApiError as exc:
            raise GitHubUserLookupError(f"GitHub user lookup for '{clean}' failed: {exc}", 424, retryable=True) from exc
        found_login, uid, kind = body.get("login"), body.get("id"), body.get("type")
        if not isinstance(found_login, str) or not _LOGIN_RE.match(found_login) or not _is_int(uid) or uid <= 0:
            raise GitHubUserLookupError(f"GitHub returned an invalid identity for '{clean}'", 424, retryable=True)
        if found_login.lower() != clean.lower():
            raise GitHubUserLookupError(
                f"GitHub answered the lookup for '{clean}' with a different account '{found_login}'", 424)
        if kind != "User":
            raise GitHubUserLookupError(
                f"'{found_login}' is a GitHub {kind if isinstance(kind, str) else 'account'} account, not a user; "
                "only user accounts can sign in", 400)
        return {"login": found_login, "uid": uid, "type": kind}

    def check_target_admin(
        self,
        access_token: str,
        login: str,
        uid: int,
        target: ControlledTarget,
        *,
        use_cache: bool = True,
    ) -> TargetCheck:
        if use_cache and self.verification_cache.is_verified(uid, login, target.key):
            return TargetCheck(target.name, target.key, target.kind, True, "verified (cached)", cached=True)
        try:
            status, data = self._api_get(target.api_path(), access_token)
        except GitHubApiError as exc:
            self.verification_cache.invalidate(uid, login, target.key)
            return TargetCheck(target.name, target.key, target.kind, False, f"GitHub API error: {exc}")
        if status != 200 or data is None:
            self.verification_cache.invalidate(uid, login, target.key)
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
            self.verification_cache.mark_verified(uid, login, target.key)
        else:
            # A fresh denial supersedes any earlier positive result for this account and target.
            self.verification_cache.invalidate(uid, login, target.key)
        return TargetCheck(target.name, target.key, target.kind, ok, reason)

    def authorize(
        self,
        access_token: str,
        controlled_targets: Iterable[Any] | None,
        *,
        use_cache: bool = True,
        expected_uid: int | None = None,
        added_admins: Iterable[int] | None = None,
    ) -> AuthorizationDecision:
        """Fail-closed authorization: the owner or an added administrator AND admin on every controlled target.

        ``added_admins`` is the current list of added administrator GitHub IDs (from dashboard state; None
        or an unreadable list means owners only). ``use_cache=False`` forces a fresh GitHub check of every
        target (used before committing a controlled-target change). ``expected_uid`` requires the token to
        belong to that GitHub id. Global administrators skip the per-target checks.
        """
        decision = self._authorize(access_token, controlled_targets, use_cache=use_cache, expected_uid=expected_uid,
                                   added_admins=added_admins)
        audit_login(
            decision.user_login, decision.user_id, "allowed" if decision.authorized else "denied",
            "" if decision.authorized else decision.message,
        )
        return decision

    def _authorize(
        self,
        access_token: str,
        controlled_targets: Iterable[Any] | None,
        *,
        use_cache: bool = True,
        expected_uid: int | None = None,
        added_admins: Iterable[int] | None = None,
    ) -> AuthorizationDecision:
        added = normalize_added_admin_uids(added_admins)
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
        if not self.may_sign_in(login, uid, added):
            return AuthorizationDecision(
                False, 403, f"Forbidden: GitHub user '{login}' is not an authorized administrator",
                user_login=login, user_id=uid, controlled_targets=names,
            )
        if expected_uid is not None and uid != expected_uid:
            return AuthorizationDecision(
                False, 403,
                f"Forbidden: GitHub user '{login}' is not the account that requested this change",
                user_login=login, user_id=uid, controlled_targets=names,
            )
        if self.is_owner(login, uid):
            login = ALLOWED_ADMIN_LOGIN  # Canonical case, so the session matches the owner identity exactly.
        if self.is_global_admin(login, uid, added):
            # Configured exception: a global administrator is not required to administer every target.
            # No target was checked, so none is recorded as verified: if the policy later stops treating
            # this identity as global, its existing session no longer passes the target coverage check.
            return AuthorizationDecision(
                True, 200, f"Authorized global administrator '{login}' (per-target admin checks not required)",
                user_login=login, user_id=uid, controlled_targets=names, verified_targets=[],
            )
        checks = [self.check_target_admin(token, login, uid, t, use_cache=use_cache) for t in targets]
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

    def session_fits(self, verified_targets: Iterable[str]) -> bool:
        """Whether a session verified for these targets stays within token and cookie size limits.

        Measured with a worst-case login and user id, so the answer holds for any identity.
        """
        token = self.create_session_cookie_value(
            "a" * 39,
            self.session_ttl_seconds,
            user_id=2**53,
            verified_targets=verified_targets,
        )
        cookie = format_session_cookie(token, self.session_ttl_seconds)
        return len(token) <= MAX_TOKEN_CHARS and len(cookie.encode("ascii")) <= MAX_SESSION_COOKIE_BYTES

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
        if session.issued_at < self.session_not_before:  # Bulk revocation cut-off.
            return None
        return session

    @staticmethod
    def is_owner(login: Any, uid: Any) -> bool:
        """True only for the hardcoded owner: login (case-insensitive) AND numeric ID. Owners manage the admin list."""
        return isinstance(login, str) and _is_int(uid) and (login.lower(), uid) in OWNER_IDENTITIES

    def may_sign_in(self, login: Any, uid: Any, added_admins: Iterable[int] | None = None) -> bool:
        """The owner (login and ID) or an added administrator (ID only, so a renamed account keeps access)."""
        if self.is_owner(login, uid):
            return True
        return _is_int(uid) and uid > 0 and uid in normalize_added_admin_uids(added_admins)

    def is_global_admin(self, login: Any, uid: Any, added_admins: Iterable[int] | None = None) -> bool:
        """Exempt from per-target admin checks: a configured global administrator, or an added administrator
        while global administrators are enabled (``DASHBOARD_GLOBAL_ADMINS`` is not ``none``)."""
        if not isinstance(login, str) or not _is_int(uid):
            return False
        if (login.lower(), uid) in self.global_admins:
            return True
        return bool(self.global_admins) and uid > 0 and uid in normalize_added_admin_uids(added_admins)

    def _session_identity_ok(self, session: SessionData, added_admins: Iterable[int] | None = None) -> bool:
        if session.user_id is None:
            return False
        if self.is_owner(session.user_login, session.user_id):
            # Owner sessions carry the canonical login (see _authorize), as before.
            return session.user_login == ALLOWED_ADMIN_LOGIN
        return self.may_sign_in(session.user_login, session.user_id, added_admins)

    def verify_session_cookie_value(self, cookie_val: Any, added_admins: Iterable[int] | None = None) -> tuple[bool, str]:
        """Return (True, login) for a correctly signed, unexpired session of the owner or an added administrator."""
        session = self.decode_session(cookie_val)
        if session is None or not self._session_identity_ok(session, added_admins):
            return False, ""
        return True, session.user_login

    def check_session(
        self,
        cookie_val: Any,
        controlled_targets: Iterable[Any] | None,
        added_admins: Iterable[int] | None = None,
    ) -> SessionCheck:
        """Validate a session against the CURRENT policy; call it on every request.

        ``added_admins`` is the current added-administrator ID list, so removing an administrator rejects
        its existing sessions on their next request. None means owners only (fail closed).
        """
        added = normalize_added_admin_uids(added_admins)
        session = self.decode_session(cookie_val)
        if session is None:
            return SessionCheck("none", message="No valid dashboard session")
        if not self._session_identity_ok(session, added):
            return SessionCheck(
                "forbidden", session, f"Forbidden: GitHub user '{session.user_login}' is not an authorized administrator"
            )
        try:
            targets = parse_controlled_targets(controlled_targets)
        except ValueError as exc:
            return SessionCheck("forbidden", session, f"Forbidden: controlled target configuration is invalid ({exc})")
        if self.is_global_admin(session.user_login, session.user_id, added):
            return SessionCheck("ok", session)
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


# --- Controlled targets from the private config repository ---


class ConfigTargetsError(RuntimeError):
    """The config-repo target listing could not be read; callers must deny access."""


class ConfigRepoTargets:
    """Cached list of ``owner/repo`` targets from ``repos/<owner>/<repo>.yaml`` in the config repo.

    Successful listings are cached for ``ttl_seconds`` (default 5 minutes). Failures are never cached and
    raise :class:`ConfigTargetsError` so authorization fails closed.
    """

    def __init__(
        self,
        config_repo: str = DEFAULT_CONFIG_REPO,
        *,
        lister: Callable[[], Iterable[str]] | None = None,
        ttl_seconds: float = DEFAULT_CONFIG_TARGETS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config_repo = config_repo
        self.ttl_seconds = float(ttl_seconds)
        self._lister = lister or self._default_lister
        self._clock = clock
        self._cached: tuple[float, list[str]] | None = None
        self._lock = threading.Lock()

    def _default_lister(self) -> list[str]:
        from .runner_patch import list_config_repo_targets

        targets, _sha = list_config_repo_targets(self.config_repo)
        return targets

    def get(self) -> list[str]:
        with self._lock:
            now = self._clock()
            if self._cached is not None and now < self._cached[0]:
                return list(self._cached[1])
            try:
                names = [t.name for t in parse_controlled_targets(list(self._lister()))]
            except Exception as exc:  # noqa: BLE001 - every failure must deny access
                self._cached = None
                raise ConfigTargetsError(
                    f"could not list controlled repositories from {self.config_repo} "
                    f"(repos/<owner>/<repo>.yaml at the default branch HEAD): {type(exc).__name__}: {exc}. "
                    "Access is denied until the listing succeeds; check SPOT_CONFIG_GH_TOKEN or GH_TOKEN"
                ) from exc
            self._cached = (now + self.ttl_seconds, names)
            return list(names)

    def __call__(self) -> list[str]:
        return self.get()

    def clear(self) -> None:
        with self._lock:
            self._cached = None


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
    device_script = """
  const start = document.getElementById("device-start");
  if (start) start.addEventListener("click", async function () {
    start.disabled = true; show("");
    const [res, d] = await postJson("/auth/device/code", {});
    if (!res.ok) {
      show(isControlVmWait(res, d) ? "The dashboard is restarting; try again in a moment."
        : (d.error || "Device login is unavailable"));
      start.disabled = false; return;
    }
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
      if (pr.status === 0 || pd.retryable === true || isControlVmWait(pr, pd)) {
        // Network failure, GitHub briefly unreachable, or the control VM restarting: keep the same code
        // and keep polling until it expires.
        status.textContent = "Connection lost; retrying...";
        setTimeout(poll, Math.max(interval, Number(pr.headers.get("Retry-After")) || interval) * 1000); return;
      }
      if (pr.status === 429) {
        setTimeout(poll, Math.max(interval, Number(pr.headers.get("Retry-After")) || interval) * 1000); return;
      }
      if (pr.ok && (pd.status === "pending" || pd.status === "slow_down")) {
        if (pd.interval) interval = Math.max(interval, Number(pd.interval) || interval);
        status.textContent = "Waiting for approval...";
        setTimeout(poll, interval * 1000); return;
      }
      status.textContent = pd.message || pd.error || "Sign-in failed"; start.disabled = false;
    }
    setTimeout(poll, interval * 1000);
  });
""" if device_flow else ""
    script = """
(function () {
  const msg = document.getElementById("msg");
  function show(text) { msg.textContent = text || ""; }
  async function postJson(url, body) {
    let res;
    try {
      res = await fetch(url, {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
    } catch (_e) {
      // Transport failure (offline, reset): report status 0 so callers can recover instead of hanging.
      return [{ok: false, status: 0, headers: new Headers()}, {error: "Network error; check your connection and try again"}];
    }
    let data = {};
    try { data = await res.json(); } catch (_e) { data = {}; }
    return [res, data];
  }
  function isControlVmWait(res, data) {
    // The edge Worker's 503 while the control VM restarts or is replaced: transient, retry later.
    return res.status === 503 && Boolean(data) && data.error === "control_vm_unavailable";
  }
  const TOKEN_KEY = "dashboard_auth_token";
  const ATTEMPT_KEY = "dashboard_gate_token_attempt";
  function storage(fn) { try { return fn(); } catch (_e) { return null; } }
  function takeUrlToken() {
    // Same handling as the dashboard: accept #token=..., strip ?token= without using it.
    return storage(function () {
      const url = new URL(window.location.href);
      const hash = new URLSearchParams((url.hash || "").replace(/^#/, ""));
      const token = hash.get("token") || "";
      const hadQuery = url.searchParams.has("token");
      url.searchParams.delete("token");
      hash.delete("token");
      url.hash = hash.toString() ? "#" + hash.toString() : "";
      if (token || hadQuery) window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
      return token;
    }) || "";
  }
  (async function tryExistingToken() {
    // No session cookie does not mean a stored or linked dashboard token is stale: try it first,
    // and discard it only when the server rejects it.
    const fromUrl = takeUrlToken().trim();
    const candidate = fromUrl || (storage(function () {
      return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY);
    }) || "").trim();
    if (!candidate) return;
    // A fresh #token= link is always tried: it is removed from the URL, so it cannot cause a loop.
    // A stored token is retried at most every 30s in case its cookie did not stick last time.
    const last = Number(storage(function () { return sessionStorage.getItem(ATTEMPT_KEY); }) || 0);
    if (!fromUrl && Date.now() - last < 30000) return;
    storage(function () { sessionStorage.setItem(ATTEMPT_KEY, String(Date.now())); });
    const [res] = await postJson("/api/auth/login", {token: candidate});
    if (res.ok) {
      storage(function () { sessionStorage.setItem(TOKEN_KEY, candidate); });
      window.location.replace("/");
      return;
    }
    if (res.status === 401 || res.status === 403) {
      // The server rejected the credential: it is stale, so discard it.
      storage(function () {
        sessionStorage.removeItem(TOKEN_KEY); localStorage.removeItem(TOKEN_KEY); sessionStorage.removeItem(ATTEMPT_KEY);
      });
      return;
    }
    // Transient failure (network, 429, 5xx, control VM restarting): keep the credential (including
    // one taken from the URL) so a reload can retry it.
    storage(function () { sessionStorage.setItem(TOKEN_KEY, candidate); sessionStorage.removeItem(ATTEMPT_KEY); });
    show("Could not reach the dashboard to use your saved token; reload to retry.");
  })();
  const logout = document.getElementById("logout");
  if (logout) logout.addEventListener("click", async function () {
    await postJson("/auth/logout", {}); window.location.assign("/");
  });
__DEVICE_JS__  const form = document.getElementById("token-form");
  if (form) form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    const [res, d] = await postJson("/api/auth/login", {token: document.getElementById("token").value});
    if (res.ok) { window.location.assign("/"); } else { show(d.error || "Invalid token"); }
  });
})();
""".replace("__DEVICE_JS__", device_script)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Spot Dashboard sign-in</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;"
        "color:#1f2328;background:#fff}.btn{display:inline-block;padding:.6rem 1rem;background:#1f2328;color:#fff;"
        "border-radius:6px;text-decoration:none}.msg{color:#cf222e}code{font-size:1.4rem}"
        "section{margin-top:1.5rem}</style></head><body>"
        "<h1>Spot Dashboard sign-in</h1>"
        "<p>Access is limited to the dashboard owner and the GitHub accounts the owner added as "
        "administrators.</p>"
        f"{msg_html}{who}{web}{device}{legacy}"
        f"<script>{script}</script></body></html>"
    )
