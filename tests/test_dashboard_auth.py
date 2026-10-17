"""Unit and HTTP integration tests for GitHub OAuth login and admin verification (dashboard_auth)."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse

from lib import dashboard_auth
from lib.dashboard_auth import (
    ALLOWED_ADMIN_ID,
    ALLOWED_ADMIN_LOGIN,
    DEFAULT_ALLOWED_ADMINS,
    GITHUB_ACCESS_TOKEN_URL,
    GITHUB_AUTHORIZE_URL,
    GITHUB_DEVICE_CODE_URL,
    MAX_GITHUB_RESPONSE_BYTES,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthCapacityError,
    ConfigRepoTargets,
    ConfigTargetsError,
    GitHubApiError,
    GitHubOAuthManager,
    RateLimiter,
    TargetChange,
    default_http_request,
    derive_purpose_key,
    format_session_cookie,
    format_state_cookie,
    normalize_admin_logins,
    parse_controlled_target,
    parse_controlled_targets,
    parse_cookie_header,
    safe_return_path,
    sign_token,
    target_list_digest,
)
try:
    from test_edge_state import FakeKvRequester, make_kv
except ImportError:  # pragma: no cover - when run as a package module
    from tests.test_edge_state import FakeKvRequester, make_kv
from lib.dashboard_server import (
    DashboardFrozenError,
    DashboardStore,
    build_oauth_config,
    create_dashboard_server,
)

SECRET = "test-session-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
CLIENT_ID = "Iv1.testclientid"
ADMIN_ID = 619624
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "session_vector.json"


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeGitHub:
    """Injectable GitHub transport: records calls and never touches the network."""

    def __init__(self) -> None:
        self.users: dict[str, tuple[str, int]] = {}
        self.resources: dict[tuple[str, str], tuple[int, object]] = {}
        self.device_polls: list[dict] = []
        self.device_code_response: dict = {
            "device_code": "dc0123456789abcdef0123456789abcdef012345",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 899,
            "interval": 5,
        }
        self.device_code_status = 200
        self.code_tokens: dict[str, str] = {}
        self.code_verifiers: list[str] = []
        self.expected_challenges: set[str] | None = None
        # code -> PKCE code_challenge that GitHub bound to the code at /login/oauth/authorize.
        self.code_challenges: dict[str, str] = {}
        self.calls: list[dict] = []
        self.raise_on_api = False
        self.device_poll_errors = 0  # number of device-token polls that fail at the transport level
        self.on_api_call = None  # optional hook(path) run before answering a REST call

    def issue_code(self, code: str, token: str, authorize_url: str) -> None:
        """Simulate GitHub issuing ``code`` for an authorize request (binding its PKCE challenge)."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)
        assert query.get("code_challenge_method") == ["S256"], query
        self.code_tokens[code] = token
        self.code_challenges[code] = query["code_challenge"][0]

    def token_calls(self) -> list[dict]:
        return [c for c in self.calls if c["url"] == GITHUB_ACCESS_TOKEN_URL]

    def add_user(self, token: str, login: str, uid: int = 1000) -> None:
        self.users[token] = (login, uid)

    def set_repo(self, token: str, full_name: str, status: int = 200, admin: object = True) -> None:
        payload = {"full_name": full_name, "permissions": {"admin": admin, "push": True, "pull": True}}
        self.resources[(token, "/repos/" + full_name)] = (status, payload if status == 200 else {"message": "x"})

    def set_org(self, token: str, org: str, status: int = 200, role: str = "admin", state: str = "active") -> None:
        payload = {"role": role, "state": state, "organization": {"login": org}}
        self.resources[(token, "/user/memberships/orgs/" + org)] = (status, payload if status == 200 else {"message": "x"})

    def api_paths(self) -> list[str]:
        return [urllib.parse.urlsplit(c["url"]).path for c in self.calls if "api.github.com" in c["url"]]

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        if url == GITHUB_DEVICE_CODE_URL:
            return self.device_code_status, json.dumps(self.device_code_response).encode()
        if url == GITHUB_ACCESS_TOKEN_URL:
            form = urllib.parse.parse_qs((body or b"").decode())
            if form.get("grant_type") == ["urn:ietf:params:oauth:grant-type:device_code"]:
                if self.device_poll_errors > 0:
                    self.device_poll_errors -= 1
                    raise GitHubApiError("simulated GitHub timeout")
                resp = self.device_polls.pop(0) if self.device_polls else {"error": "authorization_pending"}
                return 200, json.dumps(resp).encode()
            code = form.get("code", [""])[0]
            verifier = form.get("code_verifier", [""])[0]
            self.code_verifiers.append(verifier)
            if code in self.code_challenges:
                digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if digest != self.code_challenges[code]:
                    return 200, json.dumps({"error": "invalid_grant"}).encode()
            if self.expected_challenges is not None:
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if challenge not in self.expected_challenges:
                    return 200, json.dumps({"error": "invalid_grant"}).encode()
            if code in self.code_tokens and form.get("client_secret") == ["web-secret"]:
                return 200, json.dumps({"access_token": self.code_tokens[code], "token_type": "bearer"}).encode()
            return 200, json.dumps({"error": "bad_verification_code"}).encode()
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname != "api.github.com":
            return 404, b"{}"
        if self.raise_on_api:
            raise GitHubApiError("simulated network failure")
        if self.on_api_call is not None:
            self.on_api_call(parsed.path)
        token = headers.get("Authorization", "")[len("Bearer "):]
        if parsed.path == "/user":
            if token not in self.users:
                return 401, b'{"message": "Bad credentials"}'
            login, uid = self.users[token]
            return 200, json.dumps({"login": login, "id": uid}).encode()
        status, payload = self.resources.get((token, parsed.path), (404, {"message": "Not Found"}))
        return status, json.dumps(payload).encode()


_GATE_HARNESS = r"""
const [script, cfg] = [process.argv[1], JSON.parse(process.argv[2])];
function store(init) {
  const m = new Map(Object.entries(init));
  return {getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)),
          removeItem: k => m.delete(k), dump: () => Object.fromEntries(m)};
}
const session = store(cfg.session), local = store(cfg.local);
const out = {posts: [], replaced: null, url: null};
let href = "https://spot.example/" + cfg.hash;
global.window = {
  get location() { return {href, replace: u => { out.replaced = u; }, assign: u => { out.replaced = u; }}; },
  history: {replaceState: (_s, _t, u) => { out.url = u; href = "https://spot.example" + u; }},
};
const msgEl = {textContent: ""};
global.document = {title: "t", getElementById: id => (id === "msg" ? msgEl : null)};
global.sessionStorage = session; global.localStorage = local;
global.fetch = async (url, init) => { out.posts.push([url, JSON.parse(init.body)]);
  if (cfg.login_status === 0) throw new TypeError("Failed to fetch");
  const status = cfg.login_status || (cfg.login_ok ? 200 : 401);
  const body = status === 503 ? {error: "control_vm_unavailable"} : {};
  return {ok: status === 200, status, headers: new Headers(), json: async () => body}; };
new Function(script)();
setTimeout(() => { out.session = session.dump(); out.local = local.dump(); out.msg = msgEl.textContent;
  process.stdout.write(JSON.stringify(out)); }, 50);
"""


_DEVICE_HARNESS = r"""
const [script, cfg] = [process.argv[1], JSON.parse(process.argv[2])];
const els = {};
function el(id) {
  if (!els[id]) els[id] = {id, hidden: true, disabled: false, textContent: "", href: "", listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; }};
  return els[id];
}
["msg", "device-start", "device-box", "device-code", "device-link", "device-status"].forEach(el);
const queue = [];
global.setTimeout = fn => { queue.push(fn); return queue.length; };
global.window = {location: {href: "https://spot.example/", assign: u => { out.assigned = u; },
  replace: u => { out.assigned = u; }}, history: {replaceState() {}}};
global.document = {title: "t", getElementById: id => els[id] || null};
const mem = {getItem: () => null, setItem() {}, removeItem() {}};
global.sessionStorage = mem; global.localStorage = mem;
const out = {calls: [], assigned: null};
let polls = 0;
global.fetch = async (url) => {
  out.calls.push(url);
  if (url === "/auth/device/code") {
    if (cfg.code_fails) throw new TypeError("Failed to fetch");
    if (cfg.code_wait) {
      return {ok: false, status: 503, headers: new Headers({"Retry-After": "10"}),
        json: async () => ({error: "control_vm_unavailable"})};
    }
    return {ok: true, status: 200, headers: new Headers(), json: async () => ({
      user_code: "ABCD-1234", verification_uri: "https://github.com/login/device", interval: 5,
      expires_in: 900, device_handle: "h"})};
  }
  polls += 1;
  if (polls <= cfg.poll_failures) throw new TypeError("network reset");
  if (polls <= cfg.poll_failures + (cfg.upstream_failures || 0)) {
    return {ok: false, status: 503, headers: new Headers({"Retry-After": "10"}),
      json: async () => ({status: "retry", retryable: true})};
  }
  if (polls <= cfg.poll_failures + (cfg.upstream_failures || 0) + (cfg.edge_waits || 0)) {
    // The edge Worker's own outage response while the control VM restarts.
    return {ok: false, status: 503, headers: new Headers({"Retry-After": "10"}),
      json: async () => ({error: "control_vm_unavailable", retry_after_seconds: 10})};
  }
  return {ok: true, status: 200, headers: new Headers(), json: async () => ({status: "authorized"})};
};
new Function(script)();
(async () => {
  await els["device-start"].listeners.click();
  for (let i = 0; i < 10 && queue.length; i++) { await queue.shift()(); }
  out.msg = els.msg.textContent; out.disabled = els["device-start"].disabled;
  out.status = els["device-status"].textContent;
  process.stdout.write(JSON.stringify(out));
})();
"""


# Runs the dashboard's real fetch wrapper block (getDashboardAuthToken .. showToast) against fake backends.
_FETCH_WRAPPER_PRELUDE = r"""
const cfg = JSON.parse(process.argv[1]);
const AUTH_STORAGE_KEY = "dashboard_auth_token";  // Declared just before the extracted block in the page.
const out = {calls: [], assigned: null, prompted: false, resolved: null};
const mem = new Map(cfg.token ? [["dashboard_auth_token", cfg.token]] : []);
globalThis.sessionStorage = {getItem: k => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v),
  removeItem: k => mem.delete(k)};
globalThis.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
globalThis.setTimeout = (fn) => { fn(); return 0; };
const elements = {};
globalThis.document = {getElementById: (id) => elements[id] || null,
  createElement: () => ({setAttribute() {}, style: {}, hidden: false}),
  body: {appendChild: (el) => { elements[el.id] = el; }}};
globalThis.window = {crypto: globalThis.crypto, location: {assign: u => { out.assigned = u; }},
  prompt: () => { out.prompted = true; return null; }};
let statusWaits = cfg.status_waits || 0;
const jsonResponse = (status, body) => new Response(JSON.stringify(body), {status,
  headers: {"Content-Type": "application/json"}});
const NATIVE_FETCH = async (url, init) => {
  if (url === "/api/health") return jsonResponse(200, {status: "ok"});
  if (url === "/api/auth/status") {
    out.calls.push([url, null]);
    if (statusWaits > 0) { statusWaits -= 1; return jsonResponse(503, {error: "control_vm_unavailable"}); }
    return jsonResponse(200, {oauth: {enabled: cfg.oauth}, authenticated: Boolean(cfg.authenticated)});
  }
  const auth = init && init.headers ? init.headers.get("Authorization") : null;
  out.calls.push([url, auth, init.headers.get("Idempotency-Key")]);
  if (cfg.cookie_ok && !auth) return jsonResponse(200, {ok: true});
  return jsonResponse(cfg.code || 401, {error: "denied"});
};
"""

_FETCH_WRAPPER_DRIVER = r"""
(async () => {
  const res = await window.fetch(cfg.path || "/api/overview", cfg.method ? {method: cfg.method, body: "{}"} : {});
  out.resolved = res.status;
  out.stored = sessionStorage.getItem("dashboard_auth_token") || "";
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_node(harness: str, source: str, cfg: dict) -> dict:
    proc = subprocess.run(["node", "-e", harness, source, json.dumps(cfg)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout)


def _run_gate_script(script: str, *, stored: str, hash_part: str, login_ok: bool, recent: bool,
                     login_status: int | None = None) -> dict:
    import time as _time
    session = {"dashboard_auth_token": stored} if stored else {}
    if recent:
        session["dashboard_gate_token_attempt"] = str(int(_time.time() * 1000))
    cfg = {"session": session, "local": {}, "hash": hash_part, "login_ok": login_ok, "login_status": login_status}
    return _run_node(_GATE_HARNESS, script, cfg)


def _fetch_wrapper_block() -> str:
    from lib.dashboard_ui_assets import render_dashboard_html
    html = render_dashboard_html()
    script = html[html.index("<script>") + len("<script>"):html.rindex("</script>")]
    return script[script.index("    function getDashboardAuthToken()"):script.index("    function showToast(msg)")]


def _run_fetch_wrapper(cfg: dict) -> dict:
    program = _FETCH_WRAPPER_PRELUDE + _fetch_wrapper_block() + _FETCH_WRAPPER_DRIVER
    proc = subprocess.run(["node", "-e", program, json.dumps(cfg)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout)


def _long_repo_targets(count: int) -> list[str]:
    """Valid but long owner/repo names whose combined session would exceed cookie limits."""
    return [f"owner{i:02d}-{'o' * 30}/{'r' * 90}" for i in range(count)]


def make_manager(fake: FakeGitHub | None = None, **kwargs) -> GitHubOAuthManager:
    client_secret = kwargs.pop("client_secret", "web-secret")
    params = {"http_requester": fake or FakeGitHub()}
    params.update(kwargs)
    return GitHubOAuthManager(CLIENT_ID, client_secret, None, SECRET, **params)


def decode_payload(token: str) -> dict:
    body = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


class ConfigurationTests(unittest.TestCase):
    def test_default_admins_and_empty_targets(self):
        self.assertEqual(DEFAULT_ALLOWED_ADMINS, frozenset({"swackhamer"}))
        self.assertEqual((ALLOWED_ADMIN_LOGIN, ALLOWED_ADMIN_ID), ("swackhamer", 619624))
        mgr = make_manager()
        self.assertEqual(mgr.allowed_admins, frozenset({"swackhamer"}))
        manager, targets = build_oauth_config(environ={"GITHUB_OAUTH_CLIENT_ID": CLIENT_ID, "DASHBOARD_SESSION_SECRET": SECRET})
        self.assertIsNotNone(manager)
        self.assertEqual(targets, [])
        self.assertEqual(manager.allowed_admins, DEFAULT_ALLOWED_ADMINS)
        self.assertFalse(manager.device_flow_enabled)

    def test_admin_allowlist_cannot_be_overridden(self):
        env = {
            "GITHUB_OAUTH_CLIENT_ID": CLIENT_ID,
            "DASHBOARD_SESSION_SECRET": SECRET,
            "DASHBOARD_ALLOWED_ADMINS": "Alice, bob, xbmc4lyfe",
            "DASHBOARD_CONTROLLED_TARGETS": "swack-tools, xbmc4lyfe/newznab-tmux",
        }
        manager, targets = build_oauth_config(environ=env)
        self.assertEqual(manager.allowed_admins, frozenset({"swackhamer"}))
        self.assertEqual(targets, ["swack-tools", "xbmc4lyfe/newznab-tmux"])
        manager, targets = build_oauth_config(environ=env, controlled_targets="Appz4Fun")
        self.assertEqual(targets, ["Appz4Fun"])
        self.assertFalse(manager.web_flow_enabled)
        for bad in (["xbmc4lyfe"], ["swackhamer", "xbmc4lyfe"], []):
            with self.assertRaises(ValueError):
                GitHubOAuthManager(CLIENT_ID, "", bad, SECRET)
        self.assertEqual(GitHubOAuthManager(CLIENT_ID, "", ["Swackhamer"], SECRET).allowed_admins, DEFAULT_ALLOWED_ADMINS)
        with self.assertRaises(ValueError):
            normalize_admin_logins(["good", "bad/login"])

    def test_device_flow_requires_explicit_env(self):
        base = {"GITHUB_OAUTH_CLIENT_ID": CLIENT_ID, "DASHBOARD_SESSION_SECRET": SECRET}
        for value, enabled in (("", False), ("0", False), ("true", False), ("1", True)):
            manager = GitHubOAuthManager.from_environ({**base, "GITHUB_OAUTH_DEVICE_FLOW": value})
            self.assertEqual(manager.device_flow_enabled, enabled, value)
        disabled = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=FakeGitHub())
        with self.assertRaises(GitHubApiError):
            disabled.start_device_flow()
        self.assertEqual(disabled.poll_device_token("abc123")["status"], "error")

    def test_oauth_disabled_without_client_id(self):
        manager, targets = build_oauth_config(environ={})
        self.assertIsNone(manager)
        self.assertEqual(targets, [])
        with self.assertRaises(ValueError):
            GitHubOAuthManager.from_environ({"GITHUB_OAUTH_CLIENT_SECRET": "orphan"})

    def test_session_secret_is_required_and_long_enough(self):
        for bad in ("", "short", "a" * 64):
            with self.assertRaises(ValueError):
                GitHubOAuthManager(CLIENT_ID, "", None, bad)
        with self.assertRaises(ValueError):
            GitHubOAuthManager.from_environ({"GITHUB_OAUTH_CLIENT_ID": CLIENT_ID})

    def test_session_not_before_env(self):
        clock = FakeClock()
        base = {"GITHUB_OAUTH_CLIENT_ID": CLIENT_ID, "DASHBOARD_SESSION_SECRET": SECRET}
        mgr = GitHubOAuthManager.from_environ({**base, "DASHBOARD_SESSION_NOT_BEFORE": str(int(clock.now) - 5)},
                                              clock=clock)
        self.assertEqual(mgr.session_not_before, int(clock.now) - 5)
        self.assertEqual(GitHubOAuthManager.from_environ(base, clock=clock).session_not_before, 0)
        for bad in ("soon", "-1", str(int(clock.now) + 3600)):
            with self.assertRaises(ValueError, msg=bad):
                GitHubOAuthManager.from_environ({**base, "DASHBOARD_SESSION_NOT_BEFORE": bad}, clock=clock)

    def test_invalid_targets_rejected(self):
        for bad in ("", "a/b/c", "-org", "owner/..", "owner/re po", "org?x=1"):
            with self.assertRaises(ValueError):
                parse_controlled_target(bad)
        self.assertEqual(parse_controlled_target({"type": "repo", "name": "o/r"}).kind, "repo")
        with self.assertRaises(ValueError):
            parse_controlled_target({"type": "org", "name": "o/r"})
        self.assertEqual([t.name for t in parse_controlled_targets(["Org", "org", "o/r"])], ["Org", "o/r"])


class SessionCookieTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.mgr = make_manager(clock=self.clock)

    def test_sign_and_verify_roundtrip(self):
        token = self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=ADMIN_ID, verified_targets=["Org"])
        self.assertEqual(self.mgr.verify_session_cookie_value(token), (True, "swackhamer"))
        payload = decode_payload(token)
        self.assertEqual(set(payload), {"v", "sub", "uid", "iat", "exp", "tgt"})
        self.assertEqual(payload["tgt"], ["org"])
        self.assertNotIn("access_token", json.dumps(payload))

    def test_verifiable_by_another_instance_with_same_secret(self):
        token = self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=ADMIN_ID)
        other = make_manager(clock=self.clock)
        self.assertEqual(other.verify_session_cookie_value(token), (True, "swackhamer"))
        different = GitHubOAuthManager(CLIENT_ID, "", None, SECRET + "-other", clock=self.clock)
        self.assertEqual(different.verify_session_cookie_value(token), (False, ""))

    def test_session_identity_must_be_hardcoded_admin(self):
        for login, uid in (("xbmc4lyfe", ADMIN_ID), ("swackhamer", 167585258), ("Swackhamer", ADMIN_ID), ("swackhamer", None)):
            token = self.mgr.create_session_cookie_value(login, 3600, user_id=uid)
            self.assertEqual(self.mgr.verify_session_cookie_value(token), (False, ""), (login, uid))
            self.assertNotEqual(self.mgr.check_session(token, []).status, "ok", (login, uid))

    def test_session_test_vector(self):
        vector = json.loads(FIXTURE.read_text(encoding="utf-8"))
        clock = FakeClock(vector["valid_session"]["now"])
        mgr = GitHubOAuthManager(CLIENT_ID, "", None, vector["secret"], clock=clock)
        token = vector["valid_session"]["token"]
        self.assertEqual(decode_payload(token), vector["valid_session"]["payload"])
        self.assertEqual(mgr.verify_session_cookie_value(token), (True, "swackhamer"))
        self.assertEqual(mgr.check_session(token, ["swack-tools/oxidex"]).status, "ok")
        self.assertEqual(mgr.verify_session_cookie_value(vector["tampered_session_token"]), (False, ""))
        clock.now = vector["expired_now"]
        self.assertEqual(mgr.verify_session_cookie_value(token), (False, ""))
        # A token minted by this code with the vector secret and payload is byte-identical.
        clock.now = vector["valid_session"]["payload"]["iat"]
        minted = mgr.create_session_cookie_value(
            "swackhamer", 43200, user_id=ADMIN_ID, verified_targets=["swack-tools/oxidex"]
        )
        self.assertEqual(minted, token)

    def test_tampered_cookie_rejected(self):
        token = self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=ADMIN_ID)
        body, sig = token.split(".")
        forged_payload = decode_payload(token)
        forged_payload["sub"] = "xbmc4lyfe"
        forged_body = base64.urlsafe_b64encode(json.dumps(forged_payload).encode()).decode().rstrip("=")
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        for bad in (f"{forged_body}.{sig}", f"{body}.{flipped}", body, f"{token}.x", "", "é.é", "x" * 5000, None, 12):
            self.assertEqual(self.mgr.verify_session_cookie_value(bad), (False, ""), bad)

    def test_expired_cookie_rejected(self):
        token = self.mgr.create_session_cookie_value("swackhamer", 120, user_id=ADMIN_ID)
        self.clock.now += 119
        self.assertTrue(self.mgr.verify_session_cookie_value(token)[0])
        self.clock.now += 2
        self.assertEqual(self.mgr.verify_session_cookie_value(token), (False, ""))

    def test_state_token_is_not_a_session(self):
        state = self.mgr.create_state("/")
        self.assertEqual(self.mgr.verify_session_cookie_value(state), (False, ""))

    def test_removed_admin_and_new_target_forbid_session(self):
        token = self.mgr.create_session_cookie_value("eve", 3600, user_id=7)
        self.assertEqual(self.mgr.verify_session_cookie_value(token), (False, ""))
        self.assertEqual(self.mgr.check_session(token, []).status, "forbidden")
        token = self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=ADMIN_ID, verified_targets=["swack-tools"])
        self.assertEqual(self.mgr.check_session(token, ["Swack-Tools"]).status, "ok")
        check = self.mgr.check_session(token, ["swack-tools", "o/r"])
        self.assertEqual(check.status, "forbidden")
        self.assertEqual(check.missing_targets, ("o/r",))
        self.assertEqual(self.mgr.check_session(token, ["bad/target/name"]).status, "forbidden")
        self.assertEqual(self.mgr.check_session("garbage", []).status, "none")

    def test_not_before_revokes_older_sessions(self):
        old = self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=ADMIN_ID)
        self.clock.now += 100
        revoking = make_manager(clock=self.clock, session_not_before=int(self.clock.now))
        self.assertEqual(revoking.verify_session_cookie_value(old), (False, ""))
        self.assertEqual(revoking.check_session(old, []).status, "none")
        fresh = revoking.create_session_cookie_value("swackhamer", 3600, user_id=ADMIN_ID)
        self.assertTrue(revoking.verify_session_cookie_value(fresh)[0])
        self.assertTrue(self.mgr.verify_session_cookie_value(old)[0])  # without the cut-off it is still valid

    def test_uid_must_be_a_signed_integer(self):
        key = derive_purpose_key(SECRET, "session")
        now = int(self.clock.now)
        base = {"v": 1, "sub": "swackhamer", "iat": now, "exp": now + 600, "tgt": []}
        for payload in (base, {**base, "uid": None}, {**base, "uid": "619624"}, {**base, "uid": True}):
            forged = sign_token(key, payload)  # correctly signed, but no usable GitHub numeric id
            self.assertEqual(self.mgr.verify_session_cookie_value(forged), (False, ""), payload)
            self.assertEqual(self.mgr.check_session(forged, []).status, "none")
        self.assertTrue(self.mgr.verify_session_cookie_value(sign_token(key, {**base, "uid": ADMIN_ID}))[0])

    def test_cookie_attributes_use_host_prefix(self):
        self.assertEqual(SESSION_COOKIE_NAME, "__Host-spot_session")
        self.assertEqual(OAUTH_STATE_COOKIE_NAME, "__Host-spot_oauth_state")
        vector = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(vector["cookie_name"], SESSION_COOKIE_NAME)
        for header, name in ((format_session_cookie("abc.def", 600), SESSION_COOKIE_NAME),
                             (format_state_cookie("st.ate"), OAUTH_STATE_COOKIE_NAME)):
            attrs = [a.strip() for a in header.split(";")]
            self.assertTrue(attrs[0].startswith(name + "="))
            for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
                self.assertIn(attr, attrs, header)
            self.assertFalse(any(a.lower().startswith("domain") for a in attrs), header)
            self.assertFalse(any(a.lower().startswith("path=") and a != "Path=/" for a in attrs), header)
        self.assertIn("Max-Age=600", format_session_cookie("abc.def", 600))
        self.assertEqual(
            parse_cookie_header('a=1; __Host-spot_session="x.y"; a=2'), {"a": "1", "__Host-spot_session": "x.y"}
        )


class CsrfStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.mgr = make_manager(clock=self.clock)

    def test_valid_state_once(self):
        state = self.mgr.create_state("/dashboard?tab=1")
        self.assertEqual(self.mgr.validate_state(state, state), (True, "/dashboard?tab=1"))
        self.assertEqual(self.mgr.validate_state(state, state), (False, "/"))  # replay

    def test_tampered_state_rejected(self):
        state = self.mgr.create_state("/")
        body, sig = state.split(".")
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        tampered = f"{body}.{flipped}"
        self.assertEqual(self.mgr.validate_state(tampered, tampered), (False, "/"))
        other = GitHubOAuthManager(CLIENT_ID, "", None, SECRET + "-x", clock=self.clock).create_state("/")
        self.assertEqual(self.mgr.validate_state(other, other), (False, "/"))

    def test_state_must_match_browser_cookie(self):
        state = self.mgr.create_state("/")
        self.assertEqual(self.mgr.validate_state(state, ""), (False, "/"))
        self.assertEqual(self.mgr.validate_state(state, self.mgr.create_state("/")), (False, "/"))

    def test_expired_state_rejected(self):
        state = self.mgr.create_state("/")
        self.clock.now += 601
        self.assertEqual(self.mgr.validate_state(state, state), (False, "/"))

    def test_used_nonce_table_is_bounded_without_eviction(self):
        with mock.patch.object(dashboard_auth, "MAX_USED_STATE_NONCES", 2):
            states = [self.mgr.create_state("/") for _ in range(3)]
            self.assertTrue(self.mgr.validate_state(states[0], states[0])[0])
            self.assertTrue(self.mgr.validate_state(states[1], states[1])[0])
            # Full: new sign-ins are refused rather than evicting live nonces...
            with self.assertRaises(AuthCapacityError) as ctx:
                self.mgr.create_state("/")
            self.assertGreaterEqual(ctx.exception.retry_after, 1)
            self.assertFalse(self.mgr.validate_state(states[2], states[2])[0])
            # ...and the recorded nonces still block replay.
            self.assertFalse(self.mgr.validate_state(states[0], states[0])[0])
            self.clock.now += 601
            fresh = self.mgr.create_state("/")
            self.assertTrue(self.mgr.validate_state(fresh, fresh)[0])

    def test_non_ascii_return_path_rejected(self):
        for bad in ("/\u4e2d", "/caf\u00e9", "/\u00a0", "/\u2028", "/ok\x7f", "/a b"):
            self.assertEqual(safe_return_path(bad), "/", repr(bad))
        self.assertEqual(safe_return_path("/%E4%B8%AD?a=b~"), "/%E4%B8%AD?a=b~")

    def test_target_change_bound_into_state(self):
        change = TargetChange(("swack-tools",), target_list_digest([]), ADMIN_ID, "swackhamer")
        state = self.mgr.create_state("/", target_change=change)
        result = self.mgr.consume_state(state, state)
        self.assertTrue(result.ok)
        self.assertEqual(result.target_change, change)
        self.assertFalse(self.mgr.consume_state(state, state).ok)  # single use
        self.assertIsNone(self.mgr.consume_state(*([self.mgr.create_state("/")] * 2)).target_change)
        too_big = TargetChange(tuple(f"org{i:03d}/{'r' * 90}" for i in range(32)), "0" * 64, 1, "swackhamer")
        with self.assertRaises(ValueError):
            self.mgr.create_state("/", target_change=too_big)

    def test_open_redirects_sanitized(self):
        for bad in ("https://evil.example", "//evil.example", "/\\evil", "javascript:alert(1)", "/a\nb", ""):
            self.assertEqual(safe_return_path(bad), "/")
        state = self.mgr.create_state("//evil.example/x")
        self.assertEqual(self.mgr.validate_state(state, state), (True, "/"))


class DeviceFlowTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.clock = FakeClock()
        self.mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=self.fake, clock=self.clock,
                                      device_flow=True)

    def test_start_uses_client_id_only(self):
        started = self.mgr.start_device_flow()
        self.assertEqual(started["user_code"], "WDJB-MJHT")
        self.assertEqual(started["verification_uri"], "https://github.com/login/device")
        call = self.fake.calls[0]
        form = urllib.parse.parse_qs(call["body"].decode())
        self.assertEqual(form["client_id"], [CLIENT_ID])
        self.assertNotIn("client_secret", form)
        self.assertEqual(form["scope"], ["repo read:org"])
        self.assertGreater(call["timeout"], 0)

    def test_start_failures_raise(self):
        self.fake.device_code_status = 404
        with self.assertRaises(GitHubApiError):
            self.mgr.start_device_flow()
        self.fake.device_code_status = 200
        self.fake.device_code_response["verification_uri"] = "https://evil.example/device"
        with self.assertRaises(GitHubApiError):
            self.mgr.start_device_flow()

    def test_poll_states(self):
        dc = self.fake.device_code_response["device_code"]
        self.fake.device_polls = [
            {"error": "authorization_pending"},
            {"error": "slow_down", "interval": 15},
            {"access_token": "gho_device", "token_type": "bearer", "scope": "repo,read:org"},
            {"error": "access_denied"},
            {"error": "expired_token"},
        ]
        self.assertEqual(self.mgr.poll_device_token(dc)["status"], "pending")
        slow = self.mgr.poll_device_token(dc)
        self.assertEqual((slow["status"], slow["interval"]), ("slow_down", 15))
        ok = self.mgr.poll_device_token(dc)
        self.assertEqual((ok["status"], ok["access_token"]), ("success", "gho_device"))
        self.assertEqual(self.mgr.poll_device_token(dc)["status"], "denied")
        self.assertEqual(self.mgr.poll_device_token(dc)["status"], "expired")
        self.assertEqual(self.mgr.poll_device_token("bad code!")["status"], "error")

    def test_poll_transport_error_is_error(self):
        for exc in (GitHubApiError("down"), OSError("reset"), RuntimeError("bug")):
            def broken(*_args, _exc=exc):
                raise _exc

            mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=broken, device_flow=True)
            self.assertEqual(mgr.poll_device_token("abc123")["status"], "error")
            self.assertFalse(mgr.authorize("tok", []).authorized)
            with self.assertRaises(GitHubApiError):
                mgr.start_device_flow()

    def test_device_handle_is_opaque(self):
        started = self.mgr.begin_device_login()
        handle = started["device_handle"]
        dc = self.fake.device_code_response["device_code"]
        self.assertNotIn("device_code", started)
        self.assertNotIn(dc, json.dumps(started))
        self.assertNotIn(".", handle)  # not a signed payload
        with self.assertRaises(ValueError):
            decode_payload(handle)
        self.assertNotEqual(handle, self.mgr.begin_device_login()["device_handle"])
        for bad in ("", None, 12, "x" * 500, handle + "x", self.mgr.create_state("/")):
            self.assertEqual(self.mgr.poll_device_login(bad)["status"], "invalid")

    def test_device_login_disabled_without_env(self):
        disabled = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=self.fake, clock=self.clock)
        with self.assertRaises(GitHubApiError):
            disabled.begin_device_login()
        self.assertEqual(disabled.poll_device_login("x")["status"], "error")
        self.assertEqual(self.fake.calls, [])

    def test_device_interval_enforced_server_side(self):
        handle = self.mgr.begin_device_login()["device_handle"]
        polls_before = len(self.fake.token_calls())
        early = self.mgr.poll_device_login(handle)
        self.assertEqual((early["status"], early["interval"]), ("slow_down", 10))
        self.assertEqual(len(self.fake.token_calls()), polls_before)  # GitHub was not contacted
        self.clock.now += 9
        self.assertEqual(self.mgr.poll_device_login(handle)["interval"], 15)  # still early: interval grows
        self.assertEqual(len(self.fake.token_calls()), polls_before)
        self.clock.now += 15
        self.fake.device_polls = [{"error": "authorization_pending"}]
        self.assertEqual(self.mgr.poll_device_login(handle)["status"], "pending")
        self.assertEqual(len(self.fake.token_calls()), polls_before + 1)
        self.clock.now += 15
        self.fake.device_polls = [{"error": "slow_down", "interval": 30}]
        self.assertEqual(self.mgr.poll_device_login(handle)["interval"], 30)
        self.clock.now += 30
        self.fake.device_polls = [{"access_token": "gho_device"}]
        ok = self.mgr.poll_device_login(handle)
        self.assertEqual((ok["status"], ok["access_token"], ok["target_change"]), ("success", "gho_device", None))
        self.clock.now += 60
        self.assertEqual(self.mgr.poll_device_login(handle)["status"], "invalid")  # single use

    def test_device_handle_expires(self):
        handle = self.mgr.begin_device_login()["device_handle"]
        self.clock.now += 900
        self.assertEqual(self.mgr.poll_device_login(handle)["status"], "expired")
        self.assertEqual(self.mgr.poll_device_login(handle)["status"], "invalid")

    def test_device_handshake_table_is_bounded(self):
        with mock.patch.object(dashboard_auth, "MAX_DEVICE_HANDSHAKES", 2):
            self.mgr.begin_device_login()
            self.mgr.begin_device_login()
            calls = len(self.fake.calls)
            with self.assertRaises(AuthCapacityError):
                self.mgr.begin_device_login()
            self.assertEqual(len(self.fake.calls), calls)  # refused before contacting GitHub
            self.clock.now += 900
            self.mgr.begin_device_login()

    def test_github_429_and_5xx_keep_the_handshake(self):
        handle = self.mgr.begin_device_login()["device_handle"]
        original = self.fake.__call__

        def overloaded(method, url, headers, body, timeout):
            if url == GITHUB_ACCESS_TOKEN_URL and overloaded.statuses:
                return overloaded.statuses.pop(0), b'{"error": "temporarily_unavailable"}'
            return original(method, url, headers, body, timeout)

        overloaded.statuses = [429, 502]
        self.mgr._request = overloaded
        for _ in range(2):
            self.clock.now += 5
            self.assertEqual(self.mgr.poll_device_login(handle), {"status": "error", "error": "github_unavailable"})
        self.clock.now += 5
        self.fake.device_polls = [{"access_token": "gho_device"}]
        self.assertEqual(self.mgr.poll_device_login(handle)["status"], "success")


class RateLimiterTests(unittest.TestCase):
    def test_per_key_global_and_window(self):
        clock = FakeClock(0.0)
        limiter = RateLimiter(2, 3, 60, clock=clock)
        self.assertEqual(limiter.check("a"), 0.0)
        self.assertEqual(limiter.check("a"), 0.0)
        self.assertEqual(limiter.check("a"), 60.0)
        self.assertEqual(limiter.check("b"), 0.0)
        clock.now = 10
        self.assertEqual(limiter.check("c"), 50.0)  # global limit
        clock.now = 61
        self.assertEqual(limiter.check("a"), 0.0)

    def test_key_table_bounded_without_eviction(self):
        clock = FakeClock(0.0)
        limiter = RateLimiter(5, 100, 60, max_keys=2, clock=clock)
        self.assertEqual(limiter.check("a"), 0.0)
        self.assertEqual(limiter.check("b"), 0.0)
        self.assertGreater(limiter.check("c"), 0)  # full: unseen keys refused, live windows kept
        self.assertEqual(limiter.check("a"), 0.0)
        clock.now = 61
        self.assertEqual(limiter.check("c"), 0.0)

    def test_thread_safety(self):
        limiter = RateLimiter(1000, 50, 60)
        allowed = []

        def worker():
            for _ in range(20):
                allowed.append(limiter.check("k") == 0.0)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(allowed), 50)


class CodeExchangeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.mgr = make_manager(self.fake)

    def test_authorize_url(self):
        url = self.mgr.create_authorize_url("https://spot.example/auth/github/callback", "st.ate")
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", GITHUB_AUTHORIZE_URL)
        q = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(q["client_id"], [CLIENT_ID])
        self.assertEqual(q["state"], ["st.ate"])
        self.assertEqual(q["redirect_uri"], ["https://spot.example/auth/github/callback"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        verifier = self.mgr.pkce_verifier("st.ate")
        self.assertRegex(verifier, r"^[A-Za-z0-9_-]{43}$")
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(q["code_challenge"], [expected])
        self.assertNotIn(verifier, url)
        self.assertNotEqual(self.mgr.pkce_verifier("other"), verifier)
        other_secret = GitHubOAuthManager(CLIENT_ID, "web-secret", None, SECRET + "-x")
        self.assertNotEqual(other_secret.pkce_verifier("st.ate"), verifier)

    def test_exchange_success_and_failure(self):
        self.fake.code_tokens["code123"] = "gho_web"
        self.assertEqual(self.mgr.exchange_code_for_token("code123", "https://x/cb", "st.ate")["access_token"], "gho_web")
        form = urllib.parse.parse_qs(self.fake.calls[-1]["body"].decode())
        self.assertEqual(form["client_secret"], ["web-secret"])
        self.assertEqual(form["redirect_uri"], ["https://x/cb"])
        self.assertEqual(form["code_verifier"], [self.mgr.pkce_verifier("st.ate")])
        with self.assertRaises(GitHubApiError):
            self.mgr.exchange_code_for_token("wrong", "https://x/cb", "st.ate")
        with self.assertRaises(GitHubApiError):
            self.mgr.exchange_code_for_token("", "https://x/cb", "st.ate")
        with self.assertRaises(GitHubApiError):
            self.mgr.exchange_code_for_token("code123", "https://x/cb", "")

    def test_web_flow_requires_secret(self):
        mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=self.fake)
        self.assertFalse(mgr.web_flow_enabled)
        with self.assertRaises(ValueError):
            mgr.create_authorize_url("https://x/cb", "s")
        with self.assertRaises(GitHubApiError):
            mgr.exchange_code_for_token("code123", "https://x/cb", "s")


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.fake.add_user("tok-admin", "Swackhamer", ADMIN_ID)
        self.fake.add_user("tok-eve", "eve", 7)
        self.mgr = make_manager(self.fake)

    def test_allowlisted_user_with_empty_targets(self):
        with self.assertLogs("spot_dashboard.auth", level="INFO") as logs:
            ok, msg, data = self.mgr.verify_and_authorize_token("tok-admin", [])
        self.assertTrue(ok, msg)
        self.assertEqual(data["user"], "swackhamer")
        self.assertEqual(self.fake.api_paths(), ["/user"])
        line = "\n".join(logs.output)
        self.assertIn("login=swackhamer uid=619624 result=allowed", line)
        self.assertNotIn("tok-admin", line)

    def test_identity_requires_login_and_numeric_id(self):
        self.fake.add_user("tok-impostor-id", "swackhamer", 167585258)
        self.fake.add_user("tok-other-login", "xbmc4lyfe", ADMIN_ID)
        for token in ("tok-impostor-id", "tok-other-login"):
            with self.assertLogs("spot_dashboard.auth", level="INFO") as logs:
                decision = self.mgr.authorize(token, [])
            self.assertFalse(decision.authorized, token)
            self.assertEqual(decision.status_code, 403)
            self.assertIn("result=denied", "\n".join(logs.output))
            self.assertNotIn(token, "\n".join(logs.output))

    def test_non_allowlisted_user_denied_without_target_calls(self):
        ok, msg, data = self.mgr.verify_and_authorize_token("tok-eve", ["swack-tools"])
        self.assertFalse(ok)
        self.assertEqual(data["status_code"], 403)
        self.assertIn("not an authorized administrator", msg)
        self.assertEqual(self.fake.api_paths(), ["/user"])

    def test_repo_admin_pass_and_deny(self):
        self.fake.set_repo("tok-admin", "xbmc4lyfe/newznab-tmux", admin=True)
        self.assertTrue(self.mgr.authorize("tok-admin", ["xbmc4lyfe/newznab-tmux"]).authorized)
        for admin in (False, "true", 1, None):
            self.mgr.verification_cache.clear()
            self.fake.set_repo("tok-admin", "xbmc4lyfe/newznab-tmux", admin=admin)
            decision = self.mgr.authorize("tok-admin", ["xbmc4lyfe/newznab-tmux"])
            self.assertFalse(decision.authorized, admin)
            self.assertEqual(decision.status_code, 403)
            self.assertEqual(decision.missing_targets, ["xbmc4lyfe/newznab-tmux"])
        self.mgr.verification_cache.clear()
        self.fake.set_repo("tok-admin", "xbmc4lyfe/newznab-tmux", status=404)
        self.assertFalse(self.mgr.authorize("tok-admin", ["xbmc4lyfe/newznab-tmux"]).authorized)

    def test_org_admin_pass_deny_inactive(self):
        self.fake.set_org("tok-admin", "swack-tools")
        self.assertTrue(self.mgr.authorize("tok-admin", [{"type": "org", "name": "swack-tools"}]).authorized)
        for role, state in (("member", "active"), ("admin", "pending"), ("Admin", "active")):
            self.mgr.verification_cache.clear()
            self.fake.set_org("tok-admin", "swack-tools", role=role, state=state)
            self.assertFalse(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized, (role, state))
        self.mgr.verification_cache.clear()
        self.fake.set_org("tok-admin", "swack-tools", status=404)
        self.assertFalse(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized)

    def test_all_targets_required(self):
        self.fake.set_org("tok-admin", "swack-tools")
        self.fake.set_repo("tok-admin", "xbmc4lyfe/newznab-tmux", admin=False)
        decision = self.mgr.authorize("tok-admin", ["swack-tools", "xbmc4lyfe/newznab-tmux"])
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.verified_targets, ["swack-tools"])
        self.assertEqual(decision.missing_targets, ["xbmc4lyfe/newznab-tmux"])

    def test_api_errors_fail_closed(self):
        self.fake.set_org("tok-admin", "swack-tools", status=500)
        self.assertFalse(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized)
        self.fake.set_org("tok-admin", "swack-tools")
        self.fake.raise_on_api = True
        decision = self.mgr.authorize("tok-admin", ["swack-tools"])
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.status_code, 403)
        self.fake.raise_on_api = False
        self.assertFalse(self.mgr.authorize("tok-unknown", []).authorized)
        self.assertFalse(self.mgr.authorize("", []).authorized)
        self.assertFalse(self.mgr.authorize("tok-admin", ["bad/target/name"]).authorized)

    def test_malformed_github_payloads_fail_closed(self):
        def weird(method, url, headers, body, timeout):
            if url.endswith("/user"):
                return 200, b'{"login": "swackhamer", "id": "not-an-int"}'
            return 200, b"[]"

        mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=weird)
        self.assertFalse(mgr.authorize("tok", []).authorized)

        def oversized(method, url, headers, body, timeout):
            return 200, b" " * (MAX_GITHUB_RESPONSE_BYTES + 1)

        mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=oversized)
        self.assertFalse(mgr.authorize("tok", []).authorized)

    def test_positive_results_cached_denials_not_cached(self):
        self.fake.set_org("tok-admin", "swack-tools", role="member")
        self.assertFalse(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized)
        self.fake.set_org("tok-admin", "swack-tools")
        self.assertTrue(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized)
        before = self.fake.api_paths().count("/user/memberships/orgs/swack-tools")
        decision = self.mgr.authorize("tok-admin", ["swack-tools"])
        self.assertTrue(decision.authorized)
        self.assertTrue(decision.checks[0].cached)
        self.assertEqual(self.fake.api_paths().count("/user/memberships/orgs/swack-tools"), before)
        # A fresh check bypasses the cache and sees a revocation.
        self.fake.set_org("tok-admin", "swack-tools", role="member")
        self.assertFalse(self.mgr.authorize("tok-admin", ["swack-tools"], use_cache=False).authorized)

    def test_fresh_denial_invalidates_cached_grant(self):
        self.fake.set_org("tok-admin", "swack-tools")
        self.assertTrue(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized)
        self.fake.set_org("tok-admin", "swack-tools", role="member")
        self.assertFalse(self.mgr.authorize("tok-admin", ["swack-tools"], use_cache=False).authorized)
        # An ordinary sign-in right after the observed revocation must not reuse the old grant.
        decision = self.mgr.authorize("tok-admin", ["swack-tools"])
        self.assertFalse(decision.authorized)
        self.assertFalse(decision.checks[0].cached)
        # API errors and transport failures on a fresh check also drop the grant.
        for breaker in ("status", "raise"):
            self.fake.set_org("tok-admin", "swack-tools")
            self.assertTrue(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized)
            if breaker == "status":
                self.fake.set_org("tok-admin", "swack-tools", status=500)
            else:
                self.fake.raise_on_api = True
            self.mgr.check_target_admin("tok-admin", "swackhamer", ADMIN_ID, parse_controlled_target("swack-tools"),
                                        use_cache=False)
            self.fake.raise_on_api = False
            self.assertFalse(self.mgr.verification_cache.is_verified(ADMIN_ID, "swackhamer", "swack-tools"), breaker)

    def test_cache_is_keyed_by_github_uid(self):
        self.fake.set_org("tok-admin", "swack-tools")
        self.assertTrue(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized)
        cache = self.mgr.verification_cache
        self.assertTrue(cache.is_verified(ADMIN_ID, "swackhamer", "swack-tools"))
        self.assertFalse(cache.is_verified(ADMIN_ID + 1, "swackhamer", "swack-tools"))
        # Same login, different numeric id: denied before any target call, and never served from cache.
        self.fake.add_user("tok-impostor", "swackhamer", 999)
        calls = len(self.fake.api_paths())
        self.assertFalse(self.mgr.authorize("tok-impostor", ["swack-tools"]).authorized)
        self.assertEqual(self.fake.api_paths()[calls:], ["/user"])

    def test_expected_uid_mismatch_denied(self):
        self.assertTrue(self.mgr.authorize("tok-admin", [], expected_uid=ADMIN_ID).authorized)
        decision = self.mgr.authorize("tok-admin", [], expected_uid=ADMIN_ID + 1)
        self.assertFalse(decision.authorized)
        self.assertIn("not the account that requested", decision.message)

    def test_session_fits_rejects_oversized_target_lists(self):
        self.assertTrue(self.mgr.session_fits([]))
        self.assertTrue(self.mgr.session_fits(["swack-tools", "xbmc4lyfe/newznab-tmux"]))
        self.assertFalse(self.mgr.session_fits(_long_repo_targets(32)))

    def test_default_transport_refuses_non_github_hosts(self):
        with self.assertRaises(GitHubApiError):
            default_http_request("GET", "http://api.github.com/user", {}, None, 1.0)
        with self.assertRaises(GitHubApiError):
            default_http_request("GET", "https://evil.example/user", {}, None, 1.0)


class OAuthServerIntegrationTests(unittest.TestCase):
    LEGACY_TOKEN = "legacy-dashboard-token-123"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeGitHub()
        self.fake.add_user("tok-admin", "swackhamer", ADMIN_ID)
        self.fake.add_user("tok-eve", "eve", 7)
        self.config_targets: list[str] = []
        self.config_error: Exception | None = None
        self.config_calls = 0
        self.clock = FakeClock()

    def config_lister(self):
        self.config_calls += 1
        if self.config_error is not None:
            raise self.config_error
        return list(self.config_targets)

    def start(self, *, client_secret="web-secret", auth_token=None, controlled_targets=None, device_flow=True,
              **mgr_kwargs):
        mgr = GitHubOAuthManager(CLIENT_ID, client_secret, None, SECRET, http_requester=self.fake,
                                 device_flow=device_flow, clock=self.clock, **mgr_kwargs)
        self.mgr = mgr
        self.provider = ConfigRepoTargets(lister=self.config_lister, ttl_seconds=0)
        server, store = create_dashboard_server(
            host="127.0.0.1",
            port=0,
            state_path=Path(self.tmp.name) / "state.json",
            auth_token=auth_token,
            oauth_manager=mgr,
            controlled_targets=controlled_targets,
            config_targets_provider=self.provider,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        self.port = server.server_address[1]
        return store

    def request(self, method, path, *, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, resp.msg, raw
        finally:
            conn.close()

    @staticmethod
    def cookie_pair(msg, name):
        for header in msg.get_all("Set-Cookie") or []:
            pair = header.split(";", 1)[0]
            if pair.startswith(name + "=") and pair != name + "=":
                return pair, header
        return None, None

    def complete_device(self, handle, access_token):
        """Poll a device handle (respecting the server-enforced interval) until GitHub returns the token."""
        self.fake.device_polls = [{"error": "authorization_pending"}, {"access_token": access_token}]
        self.clock.now += 5
        status, _, raw = self.request("POST", "/auth/device/token", body={"device_handle": handle})
        self.assertEqual((status, json.loads(raw)["status"]), (200, "pending"))
        self.clock.now += 5
        return self.request("POST", "/auth/device/token", body={"device_handle": handle})

    def device_login(self, access_token):
        status, _, raw = self.request("POST", "/auth/device/code", body={})
        self.assertEqual(status, 200, raw)
        started = json.loads(raw)
        self.assertNotIn("device_code", started)
        self.assertNotIn(self.fake.device_code_response["device_code"], raw.decode())
        return self.complete_device(started["device_handle"], access_token)

    def login_cookie(self, access_token="tok-admin"):
        status, msg, raw = self.device_login(access_token)
        self.assertEqual(status, 200, raw)
        return self.cookie_pair(msg, SESSION_COOKIE_NAME)[0]

    def web_login(self, code, access_token, *, next_path="/"):
        _, msg, _ = self.request("GET", f"/auth/github/login?next={urllib.parse.quote(next_path)}",
                                 headers={"Host": "spot.example"})
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(msg["Location"]).query)["state"][0]
        self.fake.issue_code(code, access_token, msg["Location"])
        qs = urllib.parse.urlencode({"code": code, "state": state})
        return self.request("GET", f"/auth/github/callback?{qs}",
                            headers={"Host": "spot.example", "Cookie": f"{OAUTH_STATE_COOKIE_NAME}={state}"})

    def request_change(self, cookie, targets, **extra):
        return self.request("POST", "/api/auth/targets", headers={"Cookie": cookie, "Host": "spot.example"},
                            body={"targets": targets, **extra})

    def test_health_401_and_login_gate(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        status, _, raw = self.request("GET", "/api/health")
        self.assertEqual((status, json.loads(raw)["status"]), (200, "ok"))
        status, msg, _ = self.request("GET", "/api/overview")
        self.assertEqual(status, 401)
        self.assertIn("Bearer", msg.get("WWW-Authenticate", ""))
        status, _, raw = self.request("GET", "/")
        page = raw.decode()
        self.assertEqual(status, 200)
        for marker in ("Spot Dashboard sign-in", "/auth/device/code", 'href="/auth/github/login"', "token-form"):
            self.assertIn(marker, page)
        status, _, raw = self.request("GET", "/auth/status")
        info = json.loads(raw)
        self.assertEqual((status, info["authenticated"], info["oauth"]["web_flow"]), (200, False, True))

    def test_device_flow_admin_gets_session(self):
        self.start()
        status, msg, raw = self.device_login("tok-admin")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"status": "authorized", "user": "swackhamer"})
        self.assertNotIn(b"tok-admin", raw)
        pair, header = self.cookie_pair(msg, SESSION_COOKIE_NAME)
        self.assertIsNotNone(pair)
        for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            self.assertIn(attr, header)
        self.assertNotIn("Domain", header)
        self.assertNotIn("tok-admin", header)
        status, _, raw = self.request("GET", "/api/overview", headers={"Cookie": pair})
        self.assertEqual(status, 200)
        self.assertIn("profiles", json.loads(raw))
        status, _, raw = self.request("GET", "/", headers={"Cookie": pair})
        self.assertEqual(status, 200)
        self.assertNotIn("Spot Dashboard sign-in", raw.decode())
        status, _, raw = self.request("GET", "/api/auth/status", headers={"Cookie": pair})
        self.assertEqual(json.loads(raw)["user"], "swackhamer")
        status, msg, _ = self.request("POST", "/auth/logout", body={})
        self.assertEqual(status, 200)
        self.assertTrue(any(h.startswith(SESSION_COOKIE_NAME + "=;") and "Max-Age=0" in h
                            for h in msg.get_all("Set-Cookie")))

    def test_non_admin_login_is_forbidden(self):
        self.start()
        status, msg, raw = self.device_login("tok-eve")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["status"], "forbidden")
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])

    def test_missing_target_admin_is_forbidden(self):
        self.fake.set_org("tok-admin", "swack-tools", role="member")
        self.start(controlled_targets=["swack-tools"])
        status, _, raw = self.device_login("tok-admin")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["missing_targets"], ["swack-tools"])

    def test_new_target_forbids_existing_session(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        _, msg, _ = self.device_login("tok-admin")
        pair, _ = self.cookie_pair(msg, SESSION_COOKIE_NAME)
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        # The legacy token can no longer change controlled targets; only an OAuth session can.
        status, _, _ = self.request("POST", "/api/auth/targets", headers=bearer, body={"targets": ["swack-tools"]})
        self.assertEqual(status, 403)
        cookie = {"Cookie": pair}
        # An OAuth session only requests the change; it commits after a fresh, bound GitHub sign-in.
        self.fake.set_org("tok-admin", "swack-tools")
        status, _, raw = self.request("POST", "/api/auth/targets", headers=cookie,
                                      body={"targets": ["swack-tools"], "flow": "device"})
        self.assertEqual((status, json.loads(raw)["status"]), (202, "reauth_required"))
        status, msg, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], "tok-admin")
        self.assertEqual((status, json.loads(raw)["target_change"]["controlled_targets"]), (200, ["swack-tools"]))
        status, _, raw = self.request("GET", "/api/overview", headers=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["missing_targets"], ["swack-tools"])
        status, _, raw = self.request("GET", "/", headers=cookie)
        self.assertEqual(status, 403)
        self.assertIn("Sign out", raw.decode())
        fresh = {"Cookie": self.cookie_pair(msg, SESSION_COOKIE_NAME)[0]}
        self.assertEqual(self.request("GET", "/api/overview", headers=fresh)[0], 200)
        status, _, _ = self.request("POST", "/api/auth/targets", headers=fresh, body={"targets": ["bad/t/x"]})
        self.assertEqual(status, 400)

    def test_config_repo_targets_are_required_and_locked(self):
        self.config_targets = ["swack-tools/oxidex"]
        self.fake.set_repo("tok-admin", "swack-tools/oxidex", admin=True)
        self.start()
        status, msg, _ = self.device_login("tok-admin")
        self.assertEqual(status, 200)
        cookie = {"Cookie": self.cookie_pair(msg, SESSION_COOKIE_NAME)[0]}
        status, _, raw = self.request("GET", "/api/auth/targets", headers=cookie)
        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(body["config_targets"], ["swack-tools/oxidex"])
        self.assertIn("swack-tools/oxidex", body["locked_targets"])
        # Saving an empty dynamic list cannot drop a config-repo target.
        status, _, raw = self.request("POST", "/api/auth/targets", headers=cookie, body={"targets": []})
        self.assertEqual((status, json.loads(raw)["status"]), (200, "unchanged"))
        self.assertEqual(json.loads(raw)["controlled_targets"], ["swack-tools/oxidex"])
        # A new repo in the config repo forces a fresh login.
        self.config_targets = ["swack-tools/oxidex", "swack-tools/other"]
        status, _, raw = self.request("GET", "/api/overview", headers=cookie)
        self.assertEqual((status, json.loads(raw)["missing_targets"]), (403, ["swack-tools/other"]))

    def test_config_repo_admin_missing_is_forbidden(self):
        self.config_targets = ["swack-tools/oxidex"]
        self.fake.set_repo("tok-admin", "swack-tools/oxidex", admin=False)
        self.start()
        status, _, raw = self.device_login("tok-admin")
        self.assertEqual((status, json.loads(raw)["missing_targets"]), (403, ["swack-tools/oxidex"]))

    def test_config_repo_listing_failure_fails_closed(self):
        self.start()
        _, msg, _ = self.device_login("tok-admin")
        cookie = {"Cookie": self.cookie_pair(msg, SESSION_COOKIE_NAME)[0]}
        self.assertEqual(self.request("GET", "/api/overview", headers=cookie)[0], 200)
        self.config_error = RuntimeError("gh api failed (HTTP 404): Not Found")
        status, _, raw = self.request("GET", "/api/overview", headers=cookie)
        self.assertEqual(status, 403)
        self.assertIn("swack-tools/spot-runners-config", json.loads(raw)["error"])
        status, _, raw = self.request("GET", "/", headers=cookie)
        self.assertEqual(status, 403)
        self.assertIn("spot-runners-config", raw.decode())
        status, _, raw = self.device_login("tok-admin")
        self.assertEqual(status, 403)
        self.assertIn("could not list controlled repositories", json.loads(raw)["error"])

    def test_config_targets_cache(self):
        clock = FakeClock(100.0)
        calls = []

        def lister():
            calls.append(1)
            return ["Swack-Tools/oxidex"]

        provider = ConfigRepoTargets(lister=lister, ttl_seconds=300, clock=clock)
        self.assertEqual(provider.get(), ["Swack-Tools/oxidex"])
        clock.now += 299
        provider.get()
        self.assertEqual(len(calls), 1)
        clock.now += 2
        provider.get()
        self.assertEqual(len(calls), 2)

        def broken():
            raise RuntimeError("boom")

        failing = ConfigRepoTargets(lister=broken)
        with self.assertRaisesRegex(ConfigTargetsError, "spot-runners-config"):
            failing.get()
        bad_names = ConfigRepoTargets(lister=lambda: ["not/a/valid/name"])
        with self.assertRaises(ConfigTargetsError):
            bad_names.get()

    def test_device_flow_routes_404_when_disabled(self):
        self.start(device_flow=False)
        for path in ("/auth/device/code", "/auth/device/token"):
            status, _, _ = self.request("POST", path, body={})
            self.assertEqual(status, 404, path)
        status, _, raw = self.request("GET", "/")
        self.assertNotIn("/auth/device/code", raw.decode())
        self.assertIn('href="/auth/github/login"', raw.decode())
        self.assertFalse(json.loads(self.request("GET", "/auth/status")[2])["oauth"]["device_flow"])

    def test_legacy_token_rejected_when_proxied(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        self.assertEqual(self.request("GET", "/api/overview", headers=bearer)[0], 200)
        for proxy_header in ({"Cf-Connecting-Ip": "203.0.113.9"}, {"Cf-Ray": "8abc-IAD"},
                             {"X-Forwarded-For": "203.0.113.9"}):
            status, _, _ = self.request("GET", "/api/overview", headers={**bearer, **proxy_header})
            self.assertEqual(status, 401, proxy_header)
            status, _, _ = self.request("POST", "/api/auth/login", headers=proxy_header, body={"token": self.LEGACY_TOKEN})
            self.assertEqual(status, 401, proxy_header)
        status, msg, _ = self.request("POST", "/api/auth/login", body={"token": self.LEGACY_TOKEN})
        pair, _ = self.cookie_pair(msg, "dashboard_session")
        self.assertEqual(self.request("GET", "/api/overview", headers={"Cookie": pair})[0], 200)
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": pair, "Cf-Ray": "x"})
        self.assertEqual(status, 401)

    def test_forged_session_is_unauthenticated(self):
        self.start()
        other = GitHubOAuthManager(CLIENT_ID, "", None, SECRET + "-attacker")
        forged = other.create_session_cookie_value("swackhamer", 3600, user_id=ADMIN_ID)
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": f"{SESSION_COOKIE_NAME}={forged}"})
        self.assertEqual(status, 401)

    def test_web_code_flow(self):
        self.start(device_flow=False)
        self.fake.code_tokens["goodcode"] = "tok-admin"
        self.fake.expected_challenges = set()
        status, msg, _ = self.request("GET", "/auth/github/login?next=/index.html", headers={"Host": "spot.example"})
        self.assertEqual(status, 302)
        location = urllib.parse.urlsplit(msg["Location"])
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", GITHUB_AUTHORIZE_URL)
        query = urllib.parse.parse_qs(location.query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], ["http://spot.example/auth/github/callback"])
        state = query["state"][0]
        state_pair, state_header = self.cookie_pair(msg, OAUTH_STATE_COOKIE_NAME)
        self.assertEqual(state_pair, f"{OAUTH_STATE_COOKIE_NAME}={state}")
        for attr in ("Path=/;", "Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(attr, state_header)
        self.assertNotIn("Domain", state_header)

        qs = urllib.parse.urlencode({"code": "goodcode", "state": state})
        status, _, _ = self.request("GET", f"/auth/github/callback?{qs}", headers={"Host": "spot.example"})
        self.assertEqual(status, 400)  # no browser-bound state cookie

        query2 = urllib.parse.parse_qs(urllib.parse.urlsplit(
            self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})[1]["Location"]).query)
        state2 = query2["state"][0]
        self.fake.expected_challenges.add(query2["code_challenge"][0])
        qs = urllib.parse.urlencode({"code": "goodcode", "state": state2})
        status, msg, _ = self.request(
            "GET", f"/auth/github/callback?{qs}",
            headers={"Host": "spot.example", "Cookie": f"{OAUTH_STATE_COOKIE_NAME}={state2}"},
        )
        self.assertEqual(status, 302)
        self.assertEqual(msg["Location"], "/")
        self.assertTrue(self.fake.code_verifiers[-1])
        pair, _ = self.cookie_pair(msg, SESSION_COOKIE_NAME)
        payload = decode_payload(pair.split("=", 1)[1])
        self.assertEqual((payload["sub"], payload["uid"]), ("swackhamer", ADMIN_ID))
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": pair})
        self.assertEqual(status, 200)

        status, _, _ = self.request(
            "GET", f"/auth/github/callback?{qs}",
            headers={"Host": "spot.example", "Cookie": f"{OAUTH_STATE_COOKIE_NAME}={state2}"},
        )
        self.assertEqual(status, 400)  # replayed state

    def test_web_code_flow_non_admin_forbidden(self):
        self.start()
        self.fake.code_tokens["evecode"] = "tok-eve"
        _, msg, _ = self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(msg["Location"]).query)["state"][0]
        qs = urllib.parse.urlencode({"code": "evecode", "state": state})
        status, msg, raw = self.request(
            "GET", f"/auth/github/callback?{qs}",
            headers={"Host": "spot.example", "Cookie": f"{OAUTH_STATE_COOKIE_NAME}={state}"},
        )
        self.assertEqual(status, 403)
        self.assertIn("not an authorized administrator", raw.decode())
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])

    def test_web_code_flow_rejects_wrong_pkce_verifier(self):
        self.start(device_flow=False)
        self.fake.code_tokens["goodcode"] = "tok-admin"
        self.fake.expected_challenges = {"not-the-real-challenge"}
        _, msg, _ = self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(msg["Location"]).query)["state"][0]
        qs = urllib.parse.urlencode({"code": "goodcode", "state": state})
        status, msg, _ = self.request("GET", f"/auth/github/callback?{qs}",
                                      headers={"Host": "spot.example", "Cookie": f"{OAUTH_STATE_COOKIE_NAME}={state}"})
        self.assertEqual(status, 403)
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])

    def test_web_flow_disabled_without_secret(self):
        self.start(client_secret="")
        status, _, _ = self.request("GET", "/auth/github/login")
        self.assertEqual(status, 404)
        status, _, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn('href="/auth/github/login"', raw.decode())
        self.assertIn("/auth/device/code", raw.decode())

    def test_legacy_token_auth_still_works(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        status, _, _ = self.request("GET", "/api/overview", headers={"Authorization": f"Bearer {self.LEGACY_TOKEN}"})
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/api/overview", headers={"X-Dashboard-Token": "wrong"})
        self.assertEqual(status, 401)
        status, msg, _ = self.request("POST", "/api/auth/login", body={"token": self.LEGACY_TOKEN})
        self.assertEqual(status, 200)
        pair, _ = self.cookie_pair(msg, "dashboard_session")
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": pair})
        self.assertEqual(status, 200)
        status, _, raw = self.request("GET", "/", headers={"Cookie": pair})
        self.assertEqual(status, 200)
        self.assertNotIn("Spot Dashboard sign-in", raw.decode())

    def test_oauth_only_rejects_token_login(self):
        self.start()
        status, _, _ = self.request("POST", "/api/auth/login", body={"token": "anything"})
        self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/api/overview", headers={"Authorization": "Bearer anything"})
        self.assertEqual(status, 401)

    def test_device_token_rejects_forged_handle(self):
        self.start()
        status, _, raw = self.request("POST", "/auth/device/token", body={"device_handle": "abc.def"})
        self.assertEqual((status, json.loads(raw)["status"]), (400, "invalid"))
        self.assertFalse(any(c["url"] == GITHUB_ACCESS_TOKEN_URL for c in self.fake.calls))

    def test_non_loopback_bind_requires_some_auth(self):
        with self.assertRaisesRegex(ValueError, "without authentication"):
            create_dashboard_server(host="0.0.0.0", port=0, state_path=Path(self.tmp.name) / "s.json")
        mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=self.fake)
        with self.assertRaisesRegex(ValueError, "without TLS"):
            create_dashboard_server(host="0.0.0.0", port=0, state_path=Path(self.tmp.name) / "s.json", oauth_manager=mgr)


    # -- ported hardening: cookies, uid pinning, revocation --

    def test_unprefixed_cookie_name_is_ignored(self):
        self.start()
        pair = self.login_cookie()
        shadow = "spot_session=" + pair.split("=", 1)[1]
        self.assertEqual(self.request("GET", "/api/overview", headers={"Cookie": shadow})[0], 401)

    def test_session_without_uid_is_unauthenticated(self):
        self.start()
        now = int(self.clock.now)
        no_uid = sign_token(derive_purpose_key(SECRET, "session"),
                            {"v": 1, "sub": "swackhamer", "iat": now, "exp": now + 600, "tgt": []})
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": f"{SESSION_COOKIE_NAME}={no_uid}"})
        self.assertEqual(status, 401)

    def test_not_before_rejects_existing_sessions(self):
        self.start()
        pair = self.login_cookie()
        self.clock.now += 60
        revoking = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, clock=self.clock,
                                      session_not_before=int(self.clock.now))
        self.assertEqual(revoking.check_session(pair.split("=", 1)[1], []).status, "none")
        self.assertEqual(self.mgr.check_session(pair.split("=", 1)[1], []).status, "ok")
        # A dashboard started with the cut-off answers the old cookie with 401 (sign in again).
        self.mgr.session_not_before = int(self.clock.now)
        self.assertEqual(self.request("GET", "/api/overview", headers={"Cookie": pair})[0], 401)
        self.assertEqual(self.request("GET", "/api/overview", headers={"Cookie": self.login_cookie()})[0], 200)

    # -- ported hardening: authorization code flow --

    def test_code_replayed_with_other_browser_state_fails(self):
        self.start()
        _, victim, _ = self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})
        self.fake.issue_code("victimcode", "tok-admin", victim["Location"])
        _, attacker, _ = self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})
        attacker_state = urllib.parse.parse_qs(urllib.parse.urlsplit(attacker["Location"]).query)["state"][0]
        qs = urllib.parse.urlencode({"code": "victimcode", "state": attacker_state})
        status, msg, _ = self.request(
            "GET", f"/auth/github/callback?{qs}",
            headers={"Host": "spot.example", "Cookie": f"{OAUTH_STATE_COOKIE_NAME}={attacker_state}"},
        )
        self.assertEqual(status, 403)
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])

    def test_non_ascii_next_falls_back_to_root(self):
        self.start()
        status, msg, _ = self.web_login("c1", "tok-admin", next_path="/中")
        self.assertEqual(status, 302)
        self.assertEqual(msg["Location"], "/")
        self.assertIsNotNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])

    # -- ported hardening: target changes need a fresh, bound GitHub verification --

    def test_session_cannot_loosen_targets_without_fresh_verification(self):
        store = self.start()
        self.fake.set_repo("tok-admin", "swack-tools/oxidex")
        store.set_controlled_targets(["swack-tools/oxidex"])
        pair = self.login_cookie()
        status, msg, raw = self.request_change(pair, [])
        body = json.loads(raw)
        self.assertEqual((status, body["status"], body["flow"]), (202, "reauth_required", "web"))
        self.assertEqual(body["verify_targets"], ["swack-tools/oxidex"])
        self.assertIn("code_challenge=", body["authorize_url"])
        self.assertIsNotNone(self.cookie_pair(msg, OAUTH_STATE_COOKIE_NAME)[0])
        self.assertEqual(store.get_controlled_targets(), ["swack-tools/oxidex"])  # not committed
        self.assertEqual(store.get_controlled_targets_history()[-1]["targets"], ["swack-tools/oxidex"])

    def test_web_reauth_commits_change_and_reissues_session(self):
        store = self.start()
        self.config_targets = ["swack-tools/config-repo"]
        for repo in ("swack-tools/oxidex", "swack-tools/config-repo"):
            self.fake.set_repo("tok-admin", repo)
        self.fake.set_org("tok-admin", "swack-tools")
        store.set_controlled_targets(["swack-tools/oxidex"])
        pair = self.login_cookie()
        status, msg, raw = self.request_change(pair, ["swack-tools"], next="/?tab=auth")
        self.assertEqual(status, 202, raw)
        authorize_url = json.loads(raw)["authorize_url"]
        state_pair, _ = self.cookie_pair(msg, OAUTH_STATE_COOKIE_NAME)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)["state"][0]
        self.fake.issue_code("chg1", "tok-admin", authorize_url)
        api_calls = len(self.fake.api_paths())
        with self.assertLogs("spot_dashboard", "INFO") as logs:
            status, msg, _ = self.request(
                "GET", "/auth/github/callback?" + urllib.parse.urlencode({"code": "chg1", "state": state}),
                headers={"Host": "spot.example", "Cookie": state_pair},
            )
        self.assertEqual(status, 302)
        self.assertEqual(msg["Location"], "/?tab=auth")
        # Fresh (uncached) verification of the config-repo, removed, and added targets.
        self.assertEqual(sorted(self.fake.api_paths()[api_calls:]),
                         ["/repos/swack-tools/config-repo", "/repos/swack-tools/oxidex", "/user",
                          "/user/memberships/orgs/swack-tools"])
        self.assertEqual(store.get_controlled_targets(), ["swack-tools"])
        entry = store.get_controlled_targets_history()[-1]
        self.assertEqual((entry["actor"], entry["actor_uid"], entry["method"]), ("swackhamer", ADMIN_ID, "github_oauth"))
        self.assertEqual((entry["previous"], entry["targets"]), (["swack-tools/oxidex"], ["swack-tools"]))
        self.assertTrue(any("swackhamer" in line and "swack-tools" in line for line in logs.output))
        new_pair, _ = self.cookie_pair(msg, SESSION_COOKIE_NAME)
        self.assertEqual(self.request("GET", "/api/overview", headers={"Cookie": new_pair})[0], 200)
        # The state is single use: replaying the callback commits nothing more.
        status, _, _ = self.request(
            "GET", "/auth/github/callback?" + urllib.parse.urlencode({"code": "chg1", "state": state}),
            headers={"Host": "spot.example", "Cookie": state_pair},
        )
        self.assertEqual(status, 400)

    def test_lockout_is_impossible(self):
        store = self.start(client_secret="")
        pair = self.login_cookie()
        status, _, raw = self.request_change(pair, ["github"])
        body = json.loads(raw)
        self.assertEqual((status, body["flow"]), (202, "device"))
        self.assertNotIn("device_code", json.dumps(body))
        status, msg, raw = self.complete_device(body["device"]["device_handle"], "tok-admin")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["missing_targets"], ["github"])
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])
        self.assertEqual(store.get_controlled_targets(), [])  # nothing committed
        self.assertEqual(store.get_controlled_targets_history(), [])
        self.assertEqual(self.request("GET", "/api/overview", headers={"Cookie": pair})[0], 200)

    def test_device_reauth_commits_change(self):
        store = self.start(client_secret="")
        self.fake.set_org("tok-admin", "swack-tools")
        pair = self.login_cookie()
        _, _, raw = self.request_change(pair, ["swack-tools"])
        status, msg, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], "tok-admin")
        body = json.loads(raw)
        self.assertEqual((status, body["status"], body["target_change"]["committed"]), (200, "authorized", True))
        self.assertEqual(store.get_controlled_targets(), ["swack-tools"])
        new_pair, _ = self.cookie_pair(msg, SESSION_COOKIE_NAME)
        self.assertEqual(self.request("GET", "/api/overview", headers={"Cookie": new_pair})[0], 200)
        self.assertEqual(self.request("GET", "/api/overview", headers={"Cookie": pair})[0], 403)

    def test_removing_target_requires_current_admin(self):
        store = self.start(client_secret="")
        self.fake.set_repo("tok-admin", "swack-tools/oxidex")
        store.set_controlled_targets(["swack-tools/oxidex"])
        pair = self.login_cookie()
        self.fake.set_repo("tok-admin", "swack-tools/oxidex", admin=False)  # revoked on GitHub after sign-in
        _, _, raw = self.request_change(pair, [])
        status, _, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], "tok-admin")
        self.assertEqual(status, 403)  # the cached positive result is not reused
        self.assertEqual(store.get_controlled_targets(), ["swack-tools/oxidex"])

    def test_change_completed_by_other_account_rejected(self):
        store = self.start(client_secret="")
        self.fake.add_user("tok-x", "xbmc4lyfe", 2)
        self.fake.add_user("tok-same-login", "swackhamer", 2)
        pair = self.login_cookie()
        for token in ("tok-x", "tok-same-login"):
            _, _, raw = self.request_change(pair, ["swack-tools"])
            self.fake.set_org(token, "swack-tools")
            status, _, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], token)
            self.assertEqual(status, 403, token)
            self.assertIn("not an authorized administrator", json.loads(raw)["error"])
        self.assertEqual(store.get_controlled_targets(), [])

    def test_change_requires_the_requesting_uid(self):
        self.start()
        self.fake.set_org("tok-admin", "swack-tools")
        # A change bound to another GitHub id (forged into a state signed with the real secret) is refused.
        change = TargetChange(("swack-tools",), target_list_digest([]), ADMIN_ID + 1, "swackhamer")
        state = self.mgr.create_state("/", target_change=change)
        self.fake.issue_code("c-uid", "tok-admin", self.mgr.create_authorize_url("http://spot.example/auth/github/callback", state))
        status, _, raw = self.request("GET", "/auth/github/callback?" + urllib.parse.urlencode({"code": "c-uid", "state": state}),
                                      headers={"Host": "spot.example", "Cookie": f"{OAUTH_STATE_COOKIE_NAME}={state}"})
        self.assertEqual(status, 403)
        self.assertIn("not the account that requested", raw.decode())

    def test_stale_change_conflicts(self):
        store = self.start(client_secret="")
        self.fake.set_org("tok-admin", "swack-tools")
        pair = self.login_cookie()
        _, _, raw = self.request_change(pair, ["swack-tools"])
        store.set_controlled_targets(["o/r"])
        status, _, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], "tok-admin")
        self.assertEqual((status, json.loads(raw)["status"]), (409, "conflict"))
        self.assertEqual(store.get_controlled_targets(), ["o/r"])

    def test_change_racing_concurrent_update_conflicts(self):
        store = self.start(client_secret="")
        self.fake.set_org("tok-admin", "swack-tools")
        pair = self.login_cookie()
        _, _, raw = self.request_change(pair, ["swack-tools"])

        def concurrent_change(path):
            if path.startswith("/user/memberships/") and store.get_controlled_targets() == []:
                store.set_controlled_targets(["o/r"])

        self.fake.on_api_call = concurrent_change  # the list changes while GitHub verifies
        status, msg, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], "tok-admin")
        self.assertEqual((status, json.loads(raw)["status"]), (409, "conflict"))
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])
        self.assertEqual(store.get_controlled_targets(), ["o/r"])

    def test_change_commit_respects_durability_gates(self):
        store = self.start(client_secret="")
        self.fake.set_org("tok-admin", "swack-tools")
        pair = self.login_cookie()
        _, _, raw = self.request_change(pair, ["swack-tools"])
        handle = json.loads(raw)["device"]["device_handle"]
        with mock.patch.object(DashboardStore, "run_request",
                               side_effect=DashboardFrozenError("not the active slot")):
            status, msg, raw = self.complete_device(handle, "tok-admin")
        self.assertEqual((status, json.loads(raw)["status"]), (409, "not_accepting_changes"))
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])
        self.assertEqual(store.get_controlled_targets(), [])
        # While frozen, the request itself gets the shared wait response.
        store.frozen = True
        status, _, raw = self.request_change(pair, ["swack-tools"])
        self.assertEqual((status, json.loads(raw)["error"]), (503, "control_vm_unavailable"))

    def test_target_change_requires_oauth_session(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        status, _, _ = self.request("POST", "/api/auth/targets",
                                    headers={"Authorization": f"Bearer {self.LEGACY_TOKEN}"}, body={"targets": ["x"]})
        self.assertEqual(status, 403)

    def test_unchanged_targets_need_no_reauth(self):
        self.start()
        pair = self.login_cookie()
        calls = len(self.fake.calls)
        status, msg, raw = self.request_change(pair, [])
        self.assertEqual((status, json.loads(raw)["status"]), (200, "unchanged"))
        self.assertEqual(len(self.fake.calls), calls)  # no GitHub round trip
        self.assertIsNone(self.cookie_pair(msg, OAUTH_STATE_COOKIE_NAME)[0])

    def test_dynamic_target_list_is_capped(self):
        self.start()
        pair = self.login_cookie()
        status, _, raw = self.request_change(pair, [f"o{i}" for i in range(33)])
        self.assertEqual(status, 400)
        self.assertIn("At most 32", json.loads(raw)["error"])

    # -- ported hardening: session-size limits --

    def test_oversized_target_change_rejected_before_reauth(self):
        store = self.start(client_secret="")
        status, _, raw = self.request_change(self.login_cookie(), _long_repo_targets(32))
        self.assertEqual(status, 400, raw)
        self.assertIn("too long to fit in a session cookie", json.loads(raw)["error"])
        self.assertEqual(store.get_controlled_targets(), [])

    def test_oversized_stored_policy_never_issues_broken_cookie(self):
        store = self.start(client_secret="")
        big = _long_repo_targets(32)
        for name in big:
            self.fake.set_repo("tok-admin", name)
        store.set_controlled_targets(big)  # for example a policy written before the size check existed
        status, msg, raw = self.device_login("tok-admin")
        self.assertEqual(status, 400, raw)
        self.assertIn("too long", json.loads(raw)["error"])
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])

    def test_oversized_config_repo_targets_never_issue_broken_cookie(self):
        self.config_targets = _long_repo_targets(32)
        for name in self.config_targets:
            self.fake.set_repo("tok-admin", name)
        self.start()
        status, msg, _ = self.web_login("big", "tok-admin")
        self.assertEqual(status, 400)
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])

    def test_oversized_static_targets_refused_at_startup(self):
        mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=self.fake, clock=self.clock)
        with self.assertRaisesRegex(ValueError, "too long to fit in a session cookie"):
            create_dashboard_server(host="127.0.0.1", port=0, oauth_manager=mgr,
                                    controlled_targets=_long_repo_targets(32),
                                    config_targets_provider=lambda: [])

    # -- ported hardening: fail closed on unreadable state --

    def test_corrupt_state_fails_closed_and_reports_unhealthy(self):
        path = Path(self.tmp.name) / "state.json"
        corrupt = '{"controlled_targets": ["swack-tools/oxidex"'
        path.write_text(corrupt, encoding="utf-8")
        with self.assertLogs("spot_dashboard", "ERROR"):
            store = self.start(auth_token=self.LEGACY_TOKEN)
        status, _, raw = self.request("GET", "/api/health")
        health = json.loads(raw)
        # The control-VM controller and the Worker treat a non-200 / status != "ok" health as not ready.
        self.assertEqual((status, health["status"], health["hydrated"]), (503, "state_unavailable", False))
        self.assertIn("could not be loaded", health["reason"])
        self.assertIn("no Cloudflare KV snapshot source", health["reason"])
        self.assertEqual(Path(health["state_backup"]).read_text(encoding="utf-8"), corrupt)
        session = self.mgr.create_session_cookie_value("swackhamer", 600, user_id=ADMIN_ID)
        calls = len(self.fake.calls)
        for method, route, headers in (("GET", "/api/overview", {"Cookie": f"{SESSION_COOKIE_NAME}={session}"}),
                                       ("GET", "/api/overview", {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}),
                                       ("POST", "/auth/device/code", {}),
                                       ("GET", "/auth/github/login", {"Host": "spot.example"})):
            status, _, raw = self.request(method, route, headers=headers, body={} if method == "POST" else None)
            self.assertEqual((status, json.loads(raw)["error"]), (503, "control_vm_unavailable"), route)
        self.assertEqual(len(self.fake.calls), calls)  # GitHub never contacted
        self.assertFalse(path.exists())  # nothing written at the state path before recovery
        self.assertTrue(store.recovery_pending)

    def test_policy_unavailable_answers_500_not_503(self):
        # Defense in depth: if the policy error is ever set on a hydrated store, OAuth fails closed with 500
        # (a 503 would be turned into an endless control-VM wait by the edge Worker).
        store = self.start(auth_token=self.LEGACY_TOKEN)
        pair = self.login_cookie()
        store.controlled_targets_error = "simulated policy failure"
        status, _, raw = self.request("GET", "/api/overview", headers={"Cookie": pair})
        self.assertEqual((status, json.loads(raw)["code"]), (500, "controlled_targets_unavailable"))
        self.assertEqual(self.request("GET", "/", headers={"Cookie": pair})[0], 500)
        calls = len(self.fake.calls)
        self.assertEqual(self.request("POST", "/auth/device/code", body={})[0], 500)
        self.assertEqual(self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})[0], 500)
        self.assertEqual(len(self.fake.calls), calls)
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        status, _, raw = self.request("GET", "/api/auth/targets", headers=bearer)
        self.assertIn("simulated policy failure", json.loads(raw)["error"])

    # -- ported hardening: unauthenticated amplification --

    def test_rate_limit_returns_429(self):
        self.start()
        self.mgr.rate_limiters["start"] = RateLimiter(2, 100, 60, clock=self.clock)
        for _ in range(2):
            self.assertEqual(self.request("POST", "/auth/device/code", body={})[0], 200)
        status, msg, raw = self.request("POST", "/auth/device/code", body={})
        self.assertEqual(status, 429)
        self.assertEqual(msg["Retry-After"], "60")
        self.assertEqual(json.loads(raw)["retry_after"], 60)
        self.assertEqual(self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})[0], 429)
        self.assertEqual(sum(c["url"] == GITHUB_DEVICE_CODE_URL for c in self.fake.calls), 2)
        self.clock.now += 61
        self.assertEqual(self.request("POST", "/auth/device/code", body={})[0], 200)

    def test_default_rate_limit_caps_device_code_amplification(self):
        self.start()
        statuses = [self.request("POST", "/auth/device/code", body={})[0] for _ in range(20)]
        self.assertEqual((statuses.count(200), statuses.count(429)), (10, 10))
        self.assertEqual(sum(c["url"] == GITHUB_DEVICE_CODE_URL for c in self.fake.calls), 10)

    def test_rate_limit_buckets_by_edge_client_ip(self):
        self.start()
        self.mgr.rate_limiters["start"] = RateLimiter(1, 100, 60, clock=self.clock)
        edge = lambda ip: {"X-Spot-Client-Ip": ip, "Cf-Ray": "8abc-IAD"}  # noqa: E731
        self.assertEqual(self.request("POST", "/auth/device/code", headers=edge("203.0.113.1"), body={})[0], 200)
        self.assertEqual(self.request("POST", "/auth/device/code", headers=edge("203.0.113.1"), body={})[0], 429)
        self.assertEqual(self.request("POST", "/auth/device/code", headers=edge("203.0.113.2"), body={})[0], 200)
        # A malformed value falls back to the TCP peer's bucket.
        self.assertEqual(self.request("POST", "/auth/device/code", headers=edge("not-an-ip"), body={})[0], 200)
        self.assertEqual(self.request("POST", "/auth/device/code", body={})[0], 429)

    def test_edge_client_ip_header_disables_legacy_token(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        self.assertEqual(self.request("GET", "/api/overview", headers=bearer)[0], 200)
        status, _, _ = self.request("GET", "/api/overview", headers={**bearer, "X-Spot-Client-Ip": "203.0.113.9"})
        self.assertEqual(status, 401)

    def test_complete_endpoints_rate_limited(self):
        self.start()
        self.mgr.rate_limiters["complete"] = RateLimiter(1, 100, 60, clock=self.clock)
        self.assertEqual(self.request("POST", "/auth/device/token", body={"device_handle": "x"})[0], 400)
        self.assertEqual(self.request("POST", "/auth/device/token", body={"device_handle": "x"})[0], 429)
        self.assertEqual(self.request("GET", "/auth/github/callback?code=a&state=b")[0], 429)

    def test_device_poll_interval_enforced_by_server(self):
        self.start()
        _, _, raw = self.request("POST", "/auth/device/code", body={})
        handle = json.loads(raw)["device_handle"]
        status, _, raw = self.request("POST", "/auth/device/token", body={"device_handle": handle})
        self.assertEqual((status, json.loads(raw)), (200, {"status": "slow_down", "interval": 10}))
        self.assertEqual(self.fake.token_calls(), [])

    def test_transient_github_device_poll_failure_is_retryable(self):
        self.start(client_secret="")
        _, _, raw = self.request("POST", "/auth/device/code", body={})
        handle = json.loads(raw)["device_handle"]
        self.fake.device_poll_errors = 1
        self.clock.now += 5
        status, msg, raw = self.request("POST", "/auth/device/token", body={"device_handle": handle})
        body = json.loads(raw)
        self.assertEqual((status, body["status"], body["retryable"]), (503, "retry", True))
        self.assertEqual(msg.get("Retry-After"), "10")
        self.fake.device_polls = [{"access_token": "tok-admin"}]
        self.clock.now += 10
        status, _, raw = self.request("POST", "/auth/device/token", body={"device_handle": handle})
        self.assertEqual((status, json.loads(raw)["status"]), (200, "authorized"))  # same code still works

    def test_auth_posts_reject_cross_origin(self):
        self.start()
        hostile = ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"})
        for path in ("/auth/device/code", "/auth/device/token", "/auth/logout", "/api/auth/targets"):
            for headers in hostile:
                status, msg, _ = self.request("POST", path, headers=headers, body={"device_handle": "x"})
                self.assertEqual(status, 403, (path, headers))
                self.assertIsNone(msg.get("Set-Cookie"))
            self.assertEqual(self.request("POST", path, headers={"Content-Type": "text/plain"}, body={})[0], 415, path)
        self.assertEqual(self.fake.calls, [])

    # -- ported hardening: browser recovery (real page scripts under Node) --

    def _gate_script(self):
        _, _, raw = self.request("GET", "/")
        html = raw.decode()
        script = html[html.index("<script"):]
        return script[script.index(">") + 1:script.index("</script>")]

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the login gate script")
    def test_login_gate_tries_existing_token_before_discarding(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        script = self._gate_script()
        cases = {
            # name: (stored token, url hash, login response ok?, recent attempt?)
            "stored_valid": (self.LEGACY_TOKEN, "", True, False),
            "stored_rejected": ("stale-token", "", False, False),
            "fragment_valid": ("", "#token=" + self.LEGACY_TOKEN + "&tab=fleet", True, False),
            "none": ("", "", True, False),
            "recent_attempt": (self.LEGACY_TOKEN, "", True, True),
        }
        for name, (stored, hash_part, ok, recent) in cases.items():
            result = _run_gate_script(script, stored=stored, hash_part=hash_part, login_ok=ok, recent=recent)
            with self.subTest(name):
                if name in ("none", "recent_attempt"):
                    self.assertEqual(result["posts"], [])
                    self.assertIsNone(result["replaced"])
                else:
                    expected = self.LEGACY_TOKEN if name != "stored_rejected" else "stale-token"
                    self.assertEqual(result["posts"], [["/api/auth/login", {"token": expected}]])
                if name in ("stored_valid", "fragment_valid"):
                    self.assertEqual(result["replaced"], "/")
                    self.assertEqual(result["session"].get("dashboard_auth_token"), self.LEGACY_TOKEN)
                if name == "stored_rejected":
                    self.assertIsNone(result["replaced"])
                    self.assertNotIn("dashboard_auth_token", result["session"])
                    self.assertNotIn("dashboard_auth_token", result["local"])
                if name == "fragment_valid":
                    self.assertEqual(result["url"], "/#tab=fleet")  # token stripped from the address bar
        # Transient failures (network, 429, 5xx, the edge's control-VM wait) keep the credential.
        for login_status in (0, 429, 502, 503):
            for stored, hash_part in ((self.LEGACY_TOKEN, ""), ("", "#token=" + self.LEGACY_TOKEN)):
                result = _run_gate_script(script, stored=stored, hash_part=hash_part, login_ok=False,
                                          recent=False, login_status=login_status)
                with self.subTest(login_status=login_status, from_url=bool(hash_part)):
                    self.assertEqual(result["session"].get("dashboard_auth_token"), self.LEGACY_TOKEN)
                    self.assertNotIn("dashboard_gate_token_attempt", result["session"])
                    self.assertIn("reload to retry", result["msg"])
        rejected = _run_gate_script(script, stored=self.LEGACY_TOKEN, hash_part="", login_ok=False,
                                    recent=False, login_status=403)
        self.assertNotIn("dashboard_auth_token", rejected["session"])
        self.assertNotIn("dashboard_gate_token_attempt", rejected["session"])
        # A fresh #token= link inside the retry cooldown is still tried (and kept on success).
        fresh = _run_gate_script(script, stored="", hash_part="#token=" + self.LEGACY_TOKEN, login_ok=True,
                                 recent=True)
        self.assertEqual(fresh["posts"], [["/api/auth/login", {"token": self.LEGACY_TOKEN}]])
        self.assertEqual(fresh["session"].get("dashboard_auth_token"), self.LEGACY_TOKEN)

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the login gate script")
    def test_device_login_recovers_from_network_failures(self):
        self.start(client_secret="")
        script = self._gate_script()
        failed = _run_node(_DEVICE_HARNESS, script, {"code_fails": True, "poll_failures": 0})
        self.assertIn("Network error", failed["msg"])
        self.assertFalse(failed["disabled"])  # the user can retry without reloading
        waiting = _run_node(_DEVICE_HARNESS, script, {"code_wait": True, "poll_failures": 0})
        self.assertIn("restarting", waiting["msg"])
        self.assertFalse(waiting["disabled"])
        for cfg in ({"poll_failures": 2}, {"poll_failures": 0, "upstream_failures": 2},
                    {"poll_failures": 0, "edge_waits": 2}):
            recovered = _run_node(_DEVICE_HARNESS, script, {"code_fails": False, **cfg})
            with self.subTest(cfg):
                self.assertEqual(recovered["calls"].count("/auth/device/token"), 3)
                self.assertEqual(recovered["assigned"], "/")

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the dashboard fetch wrapper")
    def test_fetch_wrapper_sends_expired_or_invalidated_sessions_to_sign_in(self):
        oauth = _run_fetch_wrapper({"oauth": True})
        self.assertEqual((oauth["assigned"], oauth["prompted"]), ("/", False))
        legacy = _run_fetch_wrapper({"oauth": False})
        self.assertEqual((legacy["assigned"], legacy["prompted"], legacy["resolved"]), (None, True, 401))
        # A session that a target-policy change no longer covers gets 403 and must sign in again.
        policy = _run_fetch_wrapper({"oauth": True, "code": 403})
        self.assertEqual((policy["assigned"], policy["prompted"]), ("/", False))
        # A 403 for another reason (still authenticated) is returned to the caller unchanged.
        other = _run_fetch_wrapper({"oauth": True, "code": 403, "authenticated": True})
        self.assertEqual((other["assigned"], other["prompted"], other["resolved"]), (None, False, 403))
        # The sign-in check waits out a control VM outage instead of falling back to the token prompt.
        outage = _run_fetch_wrapper({"oauth": True, "status_waits": 2})
        self.assertEqual((outage["assigned"], outage["prompted"]), ("/", False))
        self.assertEqual([c[0] for c in outage["calls"]].count("/api/auth/status"), 3)

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the dashboard fetch wrapper")
    def test_stale_stored_bearer_token_does_not_mask_session(self):
        result = _run_fetch_wrapper({"oauth": True, "token": "stale-token", "cookie_ok": True,
                                     "path": "/api/budget", "method": "POST"})
        self.assertEqual(result["resolved"], 200)
        self.assertIsNone(result["assigned"])
        self.assertEqual(result["stored"], "")  # the stale token is dropped
        sent = [c for c in result["calls"] if c[0] == "/api/budget"]
        self.assertEqual([c[1] for c in sent], ["Bearer stale-token", None])
        self.assertEqual(sent[0][2], sent[1][2])  # the retry reuses the same Idempotency-Key


class StoreTargetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.kv_fake = FakeKvRequester()

    def seeded(self, targets=("swack-tools/oxidex",)):
        store = DashboardStore(state_path=self.path)
        store.set_controlled_targets(list(targets))
        return self.path.read_bytes()

    def open_store(self, *, kv=False, **kwargs):
        with self.assertLogs("spot_dashboard", "ERROR"):
            return DashboardStore(state_path=self.path, control_slot="a" if kv else kwargs.pop("control_slot", None),
                                  kv=make_kv(self.kv_fake) if kv else None, sleep=lambda _s: None, **kwargs)

    def backups(self):
        return sorted(p for p in self.path.parent.iterdir() if ".corrupt-" in p.name)

    def marker(self):
        return self.path.parent / "state.json.recovery-pending"

    def operator_repaired(self):
        """What an operator does after inspecting a backup: remove it and the recovery hold."""
        for old in self.backups():
            old.unlink()
        self.marker().unlink(missing_ok=True)

    def assert_fails_closed(self, store):
        self.assertTrue(store.recovery_pending)
        self.assertFalse(store.hydrated)
        self.assertIn("could not be loaded", store.controlled_targets_error)
        self.assertEqual(store.get_controlled_targets(), ["!unavailable-controlled-targets"])
        session = make_manager().create_session_cookie_value("swackhamer", 600, user_id=ADMIN_ID)
        self.assertEqual(make_manager().check_session(session, store.get_controlled_targets()).status, "forbidden")
        with self.assertRaises(Exception) as ctx:
            store.set_controlled_targets([])
        self.assertEqual(type(ctx.exception).__name__, "ControlledTargetsUnavailableError")
        health = store.health()
        self.assertEqual((health["status"], health["hydrated"]), ("state_unavailable", False))
        self.assertIn("could not be loaded", health["reason"])
        return health

    def kv_put_snapshot(self, version, targets):
        from lib.edge_state import KV_STATE_KEY, build_snapshot
        self.kv_fake.values[KV_STATE_KEY] = json.dumps(
            build_snapshot({"controlled_targets": list(targets)}, version, "a", now=1))

    def test_inaccessible_state_file_is_kept_aside_and_fails_closed(self):
        for exc in (PermissionError(13, "Permission denied"), IsADirectoryError(21, "Is a directory"),
                    OSError(5, "Input/output error")):
            self.operator_repaired()
            original = self.seeded()
            # Path.exists() also "lies" (False) when stat fails with EACCES; the store must not rely on it.
            with mock.patch.object(Path, "read_text", side_effect=exc), \
                    mock.patch.object(Path, "exists", return_value=False):
                store = self.open_store()
            with self.subTest(exc=type(exc).__name__):
                health = self.assert_fails_closed(store)
                [backup] = self.backups()
                self.assertEqual(backup.read_bytes(), original)  # kept, byte for byte
                self.assertEqual(health["state_backup"], str(backup))
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                self.assertFalse(self.path.exists())

    def test_unpreservable_state_is_never_replaced(self):
        DashboardStore(state_path=self.path).set_controlled_targets(["swack-tools/oxidex"])
        self.kv_put_snapshot(9, ["kv/repo"])
        # The file is unreadable AND cannot be renamed aside (the directory itself stays writable).
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")), \
                mock.patch("lib.dashboard_server.os.rename", side_effect=OSError(16, "Device or resource busy")):
            store = self.open_store(kv=True)
        health = self.assert_fails_closed(store)
        self.assertIsNone(health["state_backup"])
        self.assertIn("could not be moved aside", health["reason"])
        self.assertEqual(store.hydrate(), "unavailable")  # even a verified KV snapshot cannot replace it
        envelope = DashboardStore(state_path=None, control_slot="a").snapshot_envelope()
        with self.assertRaises(OSError):
            store.import_snapshot(envelope)
        self.assertTrue(store.recovery_pending)
        self.assertIn("swack-tools/oxidex", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.backups(), [])

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_unwritable_state_directory_fails_closed(self):
        locked = Path(self.tmp.name) / "locked"
        locked.mkdir()
        self.path = locked / "state.json"
        DashboardStore(state_path=self.path).set_controlled_targets(["swack-tools/oxidex"])
        locked.chmod(0o500)  # readable path components, but no rename inside the directory
        self.addCleanup(locked.chmod, 0o700)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            store = self.open_store(kv=True)
        self.assertIsNone(self.assert_fails_closed(store)["state_backup"])
        locked.chmod(0o700)
        self.assertIn("swack-tools/oxidex", self.path.read_text(encoding="utf-8"))

    def test_state_path_that_is_a_directory_is_moved_aside(self):
        self.path.mkdir()
        (self.path / "inside").write_text("x", encoding="utf-8")
        store = self.open_store()
        self.assert_fails_closed(store)
        [backup] = self.backups()
        self.assertTrue(backup.is_dir())
        self.assertEqual((backup / "inside").read_text(encoding="utf-8"), "x")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)

    def test_missing_state_file_starts_from_defaults(self):
        store = DashboardStore(state_path=Path(self.tmp.name) / "new" / "state.json")
        self.assertEqual((store.controlled_targets_error, store.get_controlled_targets()), ("", []))
        self.assertFalse(store.recovery_pending)

    def test_corrupt_state_file_is_kept_and_never_overwritten_before_recovery(self):
        self.seeded()
        for corrupt in (self.path.read_bytes()[:-5], b"\xff\xfe not utf-8", b"[1, 2]", b'"text"',
                        b'{"controlled_targets": "not-a-list"}'):
            self.operator_repaired()
            self.path.write_bytes(corrupt)
            store = self.open_store()
            with self.subTest(corrupt=corrupt[:20]):
                self.assert_fails_closed(store)
                with self.assertRaises(OSError):
                    store.update_budget({"target": "global", "daily_cap_usd": 5})
                from lib.dashboard_server import DashboardFrozenError

                with self.assertRaises(DashboardFrozenError):  # unhydrated: every change waits
                    store.run_request("k" * 16, "POST", "/api/budget",
                                      lambda: (200, store.update_budget({"target": "global"})))
                self.assertEqual(store.hydrate(), "unavailable")  # no KV: no verified snapshot to recover from
                self.assertFalse(self.path.exists())
                [backup] = self.backups()
                self.assertEqual(backup.read_bytes(), corrupt)

    def test_backup_names_are_never_reused(self):
        clock = FakeClock()
        stamp = "20270115T080000Z"  # FakeClock() default time
        existing = self.path.parent / f"state.json.corrupt-{stamp}"
        existing.write_text("older backup", encoding="utf-8")
        self.path.write_text("{broken", encoding="utf-8")
        store = self.open_store(clock=clock)
        self.assertEqual(existing.read_text(encoding="utf-8"), "older backup")
        self.assertEqual(store.state_backup_path, self.path.parent / f"state.json.corrupt-{stamp}.1")
        self.assertEqual(store.state_backup_path.read_text(encoding="utf-8"), "{broken")

    def test_corrupt_state_recovers_from_verified_kv_snapshot(self):
        self.path.write_text("{broken", encoding="utf-8")
        self.kv_put_snapshot(12, ["kv/repo"])
        store = self.open_store(kv=True)
        self.assert_fails_closed(store)
        with self.assertLogs("spot_dashboard", "WARNING"):
            self.assertEqual(store.hydrate(), "kv-recovery")
        self.assertTrue(store.hydrated)
        self.assertFalse(store.recovery_pending)
        self.assertEqual(store.controlled_targets_error, "")
        self.assertEqual(store.get_controlled_targets(), ["kv/repo"])
        self.assertEqual(store.state_version(), 12)  # the verified snapshot's version, no bump
        self.assertEqual(json.loads(self.path.read_text())["state_version"], 12)
        self.assertEqual(store.health()["status"], "ok")
        [backup] = self.backups()
        self.assertEqual(backup.read_text(encoding="utf-8"), "{broken")
        store.set_controlled_targets(["kv/repo", "o/r"])  # normal operation resumes
        self.assertEqual(store.state_version(), 13)

    def test_corrupt_state_with_kv_unavailable_or_invalid_stays_closed(self):
        from lib.edge_state import KV_STATE_KEY
        self.path.write_text("{broken", encoding="utf-8")
        store = self.open_store(kv=True)
        cases = (("unreachable", "unreachable"), ("missing", "holds no dashboard snapshot"),
                 ("invalid", "failed verification"), ("wrong-schema", "failed verification"))
        for case, reason in cases:
            self.kv_fake.fail = case == "unreachable"
            self.kv_fake.values.pop(KV_STATE_KEY, None)
            if case == "invalid":
                self.kv_fake.values[KV_STATE_KEY] = "{not json"
            if case == "wrong-schema":
                self.kv_fake.values[KV_STATE_KEY] = json.dumps({"schema": "other", "state_version": 3, "state": {}})
            with self.subTest(case), self.assertLogs("spot_dashboard", "WARNING"):
                self.assertEqual(store.hydrate(), "unavailable")
                health = self.assert_fails_closed(store)
                self.assertIn(reason, health["reason"])
                self.assertEqual(store.state["controlled_targets"], [])  # never served as a loosened policy
                self.assertFalse(self.path.exists())
        self.kv_fake.fail = False
        self.kv_put_snapshot(4, ["kv/repo"])
        with self.assertLogs("spot_dashboard", "WARNING"):
            self.assertEqual(store.hydrate(), "kv-recovery")
        self.assertEqual(store.get_controlled_targets(), ["kv/repo"])

    def test_snapshot_with_invalid_state_is_rejected_during_recovery(self):
        from lib.edge_state import KV_STATE_KEY, build_snapshot
        self.path.write_text("{broken", encoding="utf-8")
        self.kv_fake.values[KV_STATE_KEY] = json.dumps(build_snapshot({"instances": "not-a-list"}, 5, "a", now=1))
        store = self.open_store(kv=True)
        with self.assertLogs("spot_dashboard", "ERROR"):
            self.assertEqual(store.hydrate(), "unavailable")
        self.assertIn("failed validation", self.assert_fails_closed(store)["reason"])
        self.assertFalse(self.path.exists())

    def test_unreadable_state_is_never_snapshotted_or_frozen_but_import_recovers(self):
        self.seeded()
        self.path.write_bytes(b"{broken")
        store = self.open_store(control_slot="a")
        with self.assertRaises(ValueError):
            store.snapshot_envelope()
        with self.assertRaises(ValueError):
            store.freeze()
        self.assertFalse(store.frozen)
        self.assertFalse(os.path.lexists(store.freeze_marker_path))
        source = DashboardStore(state_path=None, control_slot="a")
        source.set_controlled_targets(["imported/repo"])
        self.assertEqual(store.import_snapshot(source.snapshot_envelope())["imported"], True)
        self.assertTrue(store.hydrated)
        self.assertFalse(store.recovery_pending)
        self.assertEqual(store.get_controlled_targets(), ["imported/repo"])
        self.assertEqual(json.loads(self.path.read_text())["controlled_targets"], ["imported/repo"])
        self.assertEqual(self.backups()[0].read_bytes(), b"{broken")

    # -- Codex round 7 P1: a durable recovery hold survives a restart between the rename and the recovery --

    def test_hold_is_durable_before_the_rename(self):
        self.path.write_text("{broken", encoding="utf-8")
        real_rename = os.rename
        seen = []

        def rename(src, dst):
            seen.append(os.path.lexists(self.marker()))  # the hold must already be on disk
            return real_rename(src, dst)

        with mock.patch("lib.dashboard_server.os.rename", side_effect=rename):
            store = self.open_store()
        self.assertEqual(seen, [True])
        self.assertEqual(self.marker().stat().st_mode & 0o777, 0o600)
        recorded = json.loads(self.marker().read_text(encoding="utf-8"))
        self.assertEqual(recorded["backup"], str(store.state_backup_path))
        self.assertIn("could not be loaded", recorded["reason"])

    def test_restart_after_rename_without_kv_stays_closed(self):
        self.seeded()
        self.path.write_text("{broken", encoding="utf-8")
        first = self.open_store()
        backup = first.state_backup_path
        self.assertFalse(self.path.exists())
        # The container restarts: the state path is now empty, which must NOT mean "start from defaults".
        for _ in range(2):
            with self.assertLogs("spot_dashboard", "ERROR"):
                again = DashboardStore(state_path=self.path)
            health = self.assert_fails_closed(again)
            self.assertIn("still pending", health["reason"])
            self.assertEqual(health["state_backup"], str(backup))
            again.start()  # no KV: nothing verified to recover from
            self.assertFalse(again.hydrated)
            self.assertFalse(self.path.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), "{broken")

    def test_restart_after_rename_with_kv_recovers_and_releases_the_hold(self):
        self.path.write_text("{broken", encoding="utf-8")
        self.open_store(kv=True)  # moved aside; KV had nothing yet
        with self.assertLogs("spot_dashboard", "ERROR"):
            restarted = DashboardStore(state_path=self.path, control_slot="a", kv=make_kv(self.kv_fake),
                                       sleep=lambda _s: None)
        self.assertEqual(restarted.hydrate(), "unavailable")  # KV still empty: closed, not defaults
        self.assertTrue(self.marker().exists())
        self.kv_put_snapshot(8, ["kv/repo"])
        with self.assertLogs("spot_dashboard", "WARNING"):
            self.assertEqual(restarted.hydrate(), "kv-recovery")
        self.assertFalse(self.marker().exists())  # released only after the recovered state is on disk
        self.assertEqual(json.loads(self.path.read_text())["controlled_targets"], ["kv/repo"])
        normal = DashboardStore(state_path=self.path)
        self.assertEqual((normal.recovery_pending, normal.get_controlled_targets()), (False, ["kv/repo"]))

    def test_hold_with_a_file_back_at_the_state_path_preserves_it(self):
        self.path.write_text("{broken", encoding="utf-8")
        self.open_store()
        self.path.write_text(json.dumps({"controlled_targets": ["hand/placed"]}), encoding="utf-8")
        with self.assertLogs("spot_dashboard", "ERROR"):
            store = DashboardStore(state_path=self.path)
        self.assert_fails_closed(store)  # the hold wins over a readable file
        self.assertEqual(len(self.backups()), 2)
        self.assertTrue(any("hand/placed" in b.read_text(encoding="utf-8") for b in self.backups()))
        self.assertFalse(self.path.exists())

    def test_no_rename_without_a_durable_hold(self):
        self.path.write_text("{broken", encoding="utf-8")
        with mock.patch.object(DashboardStore, "_write_private_durably", side_effect=OSError(28, "No space left")):
            store = self.open_store()
        health = self.assert_fails_closed(store)
        self.assertIn("could not record the recovery hold", health["reason"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")  # left in place
        self.assertEqual(self.backups(), [])
        with self.assertRaises(OSError):
            store.import_snapshot(DashboardStore(state_path=None, control_slot="a").snapshot_envelope())

    def test_failed_write_of_recovered_state_keeps_the_hold(self):
        self.path.write_text("{broken", encoding="utf-8")
        self.kv_put_snapshot(3, ["kv/repo"])
        store = self.open_store(kv=True)
        with mock.patch.object(DashboardStore, "_persist", side_effect=OSError(5, "I/O error")), \
                self.assertLogs("spot_dashboard", "ERROR"):
            self.assertEqual(store.hydrate(), "unavailable")
        self.assertTrue(store.recovery_pending)
        self.assertTrue(self.marker().exists())
        with self.assertLogs("spot_dashboard", "WARNING"):
            self.assertEqual(store.hydrate(), "kv-recovery")
        self.assertFalse(self.marker().exists())

    def test_import_during_a_hold_releases_it(self):
        self.path.write_text("{broken", encoding="utf-8")
        self.open_store(control_slot="a")
        with self.assertLogs("spot_dashboard", "ERROR"):
            store = DashboardStore(state_path=self.path, control_slot="a")
        source = DashboardStore(state_path=None, control_slot="a")
        source.set_controlled_targets(["imported/repo"])
        store.import_snapshot(source.snapshot_envelope())
        self.assertFalse(self.marker().exists())
        self.assertEqual(DashboardStore(state_path=self.path).get_controlled_targets(), ["imported/repo"])

    def test_history_is_bounded_and_cas(self):
        store = DashboardStore(state_path=None)
        for i in range(60):
            store.set_controlled_targets([f"org{i}"], actor="swackhamer", actor_uid=ADMIN_ID, method="github_oauth")
        history = store.get_controlled_targets_history()
        self.assertEqual(len(history), 50)
        self.assertEqual((history[-1]["previous"], history[-1]["targets"]), (["org58"], ["org59"]))
        self.assertEqual((history[-1]["actor"], history[-1]["actor_uid"]), ("swackhamer", ADMIN_ID))
        self.assertIsInstance(history[-1]["epoch"], int)
        with self.assertRaises(Exception) as ctx:
            store.set_controlled_targets([], expected_digest=target_list_digest(["org1"]))
        self.assertEqual(type(ctx.exception).__name__, "ControlledTargetsConflictError")
        store.set_controlled_targets([], expected_digest=target_list_digest(["org59"]))
        self.assertEqual(store.get_controlled_targets(), [])
        with self.assertRaises(ValueError):
            store.set_controlled_targets([f"o{i}" for i in range(33)])

    def test_history_survives_reload(self):
        store = DashboardStore(state_path=self.path)
        store.set_controlled_targets(["Swack-Tools", "swack-tools", "o/r"], actor="swackhamer", actor_uid=ADMIN_ID,
                                     method="github_oauth")
        reloaded = DashboardStore(state_path=self.path)
        self.assertEqual(reloaded.get_controlled_targets(), ["Swack-Tools", "o/r"])
        self.assertEqual(reloaded.get_controlled_targets_history()[-1]["method"], "github_oauth")


class GlobalAdminTests(unittest.TestCase):
    """Configured global administrators skip per-target admin checks (pinned to login and uid)."""

    def test_parse_and_environment_default(self):
        self.assertEqual(dashboard_auth.parse_global_admins(None), frozenset({("swackhamer", 619624)}))
        self.assertEqual(dashboard_auth.parse_global_admins(""), frozenset({("swackhamer", 619624)}))
        self.assertEqual(dashboard_auth.parse_global_admins("none"), frozenset())
        self.assertEqual(dashboard_auth.parse_global_admins("Swackhamer:619624"),
                         frozenset({("swackhamer", 619624)}))
        # Only the identity that can sign in at all may be listed (PR review).
        for bad in ("swackhamer", "swackhamer:abc", "swackhamer:0", "bad login:1", "alice:123",
                    "swackhamer:1", "swackhamer:619624, alice:123"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dashboard_auth.parse_global_admins(bad)
        env = {"GITHUB_OAUTH_CLIENT_ID": CLIENT_ID, "GITHUB_OAUTH_CLIENT_SECRET": "x", "DASHBOARD_SESSION_SECRET": SECRET}
        self.assertEqual(GitHubOAuthManager.from_environ(env).global_admins, frozenset({("swackhamer", 619624)}))
        env["DASHBOARD_GLOBAL_ADMINS"] = "none"
        self.assertEqual(GitHubOAuthManager.from_environ(env).global_admins, frozenset())

    def test_global_admin_skips_target_checks(self):
        fake = FakeGitHub()
        fake.add_user("tok", "swackhamer", 619624)
        fake.set_repo("tok", "xbmc4lyfe/newznab-tmux", admin=False)
        targets = ["xbmc4lyfe/newznab-tmux", "Appz4Fun/nzbget"]
        plain = make_manager(fake).authorize("tok", targets)
        self.assertFalse(plain.authorized)  # not a global admin: per-target checks apply
        fake2 = FakeGitHub()
        fake2.add_user("tok", "swackhamer", 619624)
        mgr = make_manager(fake2, global_admins=[("swackhamer", 619624)])
        decision = mgr.authorize("tok", targets)
        self.assertTrue(decision.authorized, decision.message)
        self.assertEqual(fake2.api_paths(), ["/user"])  # no per-target GitHub calls
        self.assertEqual(decision.verified_targets, [])  # skipped checks are never certified
        session = mgr.issue_session(decision)
        self.assertEqual(mgr.check_session(session, targets + ["swack-tools"]).status, "ok")
        # Policy changes to "none": the same session is now held to target coverage and refused.
        strict = make_manager(fake2, global_admins=[])
        self.assertEqual(strict.check_session(session, targets).status, "forbidden")

    def test_uid_pinning(self):
        fake = FakeGitHub()
        fake.add_user("tok", "swackhamer", 619624)
        fake.set_repo("tok", "xbmc4lyfe/newznab-tmux", admin=False)
        mgr = make_manager(fake, global_admins=[("swackhamer", 1)])  # wrong uid: no exception
        self.assertFalse(mgr.authorize("tok", ["xbmc4lyfe/newznab-tmux"]).authorized)



OWNER = ("swackhamer", 619624)


def _user_payload(login: str, uid: int, kind: str = "User") -> dict:
    return {"login": login, "id": uid, "type": kind}


class AddedAdminAuthTests(unittest.TestCase):
    """Added administrators (Admins page): sign-in by numeric ID, per-request membership, owner fallback."""

    def manager(self, fake, **kwargs):
        kwargs.setdefault("global_admins", [OWNER])
        return make_manager(fake, **kwargs)

    def test_added_admin_signs_in_and_skips_target_checks(self):
        fake = FakeGitHub()
        fake.add_user("tok", "alice", 4242)
        fake.set_repo("tok", "swack-tools/oxidex", admin=False)
        mgr = self.manager(fake)
        self.assertFalse(mgr.authorize("tok", ["swack-tools/oxidex"]).authorized)  # not listed
        self.assertFalse(mgr.authorize("tok", ["swack-tools/oxidex"], added_admins=None).authorized)
        decision = mgr.authorize("tok", ["swack-tools/oxidex"], added_admins=[4242])
        self.assertTrue(decision.authorized, decision.message)
        self.assertEqual((decision.user_login, decision.user_id, decision.verified_targets), ("alice", 4242, []))
        session = mgr.issue_session(decision)
        self.assertEqual(mgr.check_session(session, ["swack-tools/oxidex"], {4242}).status, "ok")
        self.assertEqual(mgr.verify_session_cookie_value(session, {4242}), (True, "alice"))

    def test_removed_admin_session_is_rejected_on_the_next_check(self):
        fake = FakeGitHub()
        fake.add_user("tok", "alice", 4242)
        mgr = self.manager(fake)
        session = mgr.issue_session(mgr.authorize("tok", [], added_admins={4242}))
        self.assertEqual(mgr.check_session(session, [], {4242}).status, "ok")
        for current in (frozenset(), None, [1, 2], "4242"):
            with self.subTest(current=current):
                check = mgr.check_session(session, [], current)
                self.assertEqual(check.status, "forbidden")
                self.assertIn("not an authorized administrator", check.message)
        self.assertEqual(mgr.verify_session_cookie_value(session), (False, ""))

    def test_uid_match_survives_a_rename_and_a_recycled_login_gets_nothing(self):
        fake = FakeGitHub()
        fake.add_user("renamed", "alice-renamed", 4242)  # same account, new login
        fake.add_user("recycled", "alice", 9999)  # someone else took the old login
        mgr = self.manager(fake)
        renamed = mgr.authorize("renamed", [], added_admins={4242})
        self.assertTrue(renamed.authorized, renamed.message)
        self.assertEqual(renamed.user_login, "alice-renamed")
        recycled = mgr.authorize("recycled", [], added_admins={4242})
        self.assertFalse(recycled.authorized)
        # An older session issued under the previous login still matches by ID.
        old = mgr.create_session_cookie_value("alice", 3600, user_id=4242)
        self.assertEqual(mgr.check_session(old, [], {4242}).status, "ok")
        self.assertEqual(mgr.check_session(mgr.create_session_cookie_value("alice", 3600, user_id=9999), [], {4242}).status,
                         "forbidden")

    def test_owner_is_unaffected_and_owner_id_never_works_as_an_added_id(self):
        fake = FakeGitHub()
        fake.add_user("tok-owner", "Swackhamer", ADMIN_ID)
        mgr = self.manager(fake)
        decision = mgr.authorize("tok-owner", [], added_admins=frozenset())
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.user_login, "swackhamer")  # canonical owner login
        forged = mgr.create_session_cookie_value("eve", 3600, user_id=ADMIN_ID)
        self.assertEqual(mgr.check_session(forged, [], {ADMIN_ID}).status, "forbidden")
        self.assertEqual(dashboard_auth.normalize_added_admin_uids([ADMIN_ID, 5, 0, -1, True, "7", 5.0]),
                         frozenset({5}))
        self.assertTrue(mgr.is_owner("SWACKHAMER", ADMIN_ID))
        self.assertFalse(mgr.is_owner("swackhamer", 4242))
        self.assertFalse(mgr.is_owner("alice", ADMIN_ID))

    def test_global_admins_none_holds_added_admins_to_target_checks(self):
        fake = FakeGitHub()
        fake.add_user("tok", "alice", 4242)
        fake.set_repo("tok", "swack-tools/oxidex", admin=False)
        strict = self.manager(fake, global_admins=[])
        self.assertFalse(strict.authorize("tok", ["swack-tools/oxidex"], added_admins={4242}).authorized)
        fake.set_repo("tok", "swack-tools/oxidex", admin=True)
        decision = strict.authorize("tok", ["swack-tools/oxidex"], added_admins={4242})
        self.assertTrue(decision.authorized, decision.message)
        self.assertEqual(decision.verified_targets, ["swack-tools/oxidex"])
        session = strict.issue_session(decision)
        self.assertEqual(strict.check_session(session, ["swack-tools/oxidex"], {4242}).status, "ok")
        self.assertEqual(strict.check_session(session, ["swack-tools/oxidex", "swack-tools/other"], {4242}).status,
                         "forbidden")

    def test_lookup_user(self):
        fake = FakeGitHub()
        fake.resources[("tok", "/users/alice")] = (200, _user_payload("Alice", 4242))
        fake.resources[("tok", "/users/acme")] = (200, _user_payload("acme", 77, "Organization"))
        fake.resources[("tok", "/users/robot")] = (200, _user_payload("robot", 78, "Bot"))
        fake.resources[("tok", "/users/broken")] = (500, {"message": "boom"})
        fake.resources[("tok", "/users/limited")] = (403, {"message": "rate limited"})
        fake.resources[("tok", "/users/weird")] = (200, {"login": "weird", "id": "1", "type": "User"})
        fake.resources[("tok", "/users/other")] = (200, _user_payload("someone-else", 5))
        fake.resources[("", "/users/anon")] = (200, _user_payload("anon", 6))
        mgr = make_manager(fake)
        self.assertEqual(mgr.lookup_user("@alice ", "tok"), {"login": "Alice", "uid": 4242, "type": "User"})
        self.assertEqual(fake.calls[-1]["headers"].get("Authorization"), "Bearer tok")
        self.assertEqual(mgr.lookup_user("anon")["uid"], 6)
        self.assertNotIn("Authorization", fake.calls[-1]["headers"])
        fake.resources[("tok", "/users/old--name-")] = (200, _user_payload("old--name-", 8))  # legacy login
        self.assertEqual(mgr.lookup_user("old--name-", "tok")["uid"], 8)
        for login, status, retryable in (("nobody", 404, False), ("acme", 400, False), ("robot", 400, False),
                                         ("broken", 424, True), ("limited", 424, True), ("weird", 424, True),
                                         ("other", 424, False)):
            with self.subTest(login=login), self.assertRaises(dashboard_auth.GitHubUserLookupError) as ctx:
                mgr.lookup_user(login, "tok")
            self.assertEqual((ctx.exception.status, ctx.exception.retryable), (status, retryable))
        calls = len(fake.calls)
        for bad in ("", "-x", "@", "@@x", "a b", "a/b", "a.b", "x" * 40, None, 42, "../user", "x\ny"):
            with self.subTest(bad=bad), self.assertRaises(dashboard_auth.GitHubUserLookupError) as ctx:
                mgr.lookup_user(bad, "tok")
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(fake.calls), calls)  # invalid logins never reach GitHub
        fake.raise_on_api = True
        with self.assertRaises(dashboard_auth.GitHubUserLookupError) as ctx:
            mgr.lookup_user("alice", "tok")
        self.assertEqual((ctx.exception.status, ctx.exception.retryable), (424, True))


class AdminStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def store(self):
        store = DashboardStore(state_path=self.path)
        store.hydrate()
        return store

    def test_add_remove_persist_and_history(self):
        store = self.store()
        with self.assertLogs("spot_dashboard.auth", "INFO") as logs:
            record = store.add_dashboard_admin("alice", 4242, actor="swackhamer", actor_uid=ADMIN_ID)
        self.assertEqual({k: record[k] for k in ("login", "uid", "added_by", "added_by_uid")},
                         {"login": "alice", "uid": 4242, "added_by": "swackhamer", "added_by_uid": ADMIN_ID})
        self.assertRegex(record["added_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("dashboard admin added login=alice uid=4242 by=swackhamer by_uid=619624", logs.output[0])
        reopened = self.store()  # survives a restart from the local state file
        self.assertEqual(reopened.dashboard_admin_uids(), frozenset({4242}))
        with self.assertRaises(dashboard_server_module().DashboardAdminError) as ctx:
            reopened.add_dashboard_admin("alice-renamed", 4242, actor="swackhamer", actor_uid=ADMIN_ID)
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(dashboard_server_module().DashboardAdminError) as ctx:
            reopened.add_dashboard_admin("swackhamer", ADMIN_ID, actor="swackhamer", actor_uid=ADMIN_ID)
        self.assertEqual(ctx.exception.status, 409)
        with self.assertLogs("spot_dashboard.auth", "INFO") as logs:
            reopened.remove_dashboard_admin(4242, actor="swackhamer", actor_uid=ADMIN_ID)
        self.assertIn("dashboard admin removed login=alice uid=4242", logs.output[0])
        self.assertEqual(self.store().dashboard_admin_uids(), frozenset())
        history = self.store().get_dashboard_admins_history()
        self.assertEqual([(h["action"], h["login"], h["uid"], h["actor"]) for h in history],
                         [("added", "alice", 4242, "swackhamer"), ("removed", "alice", 4242, "swackhamer")])
        for uid, status in ((4242, 404), (ADMIN_ID, 400), (0, 400), (True, 400), ("4242", 400)):
            with self.subTest(uid=uid), self.assertRaises(dashboard_server_module().DashboardAdminError) as ctx:
                reopened.remove_dashboard_admin(uid, actor="swackhamer", actor_uid=ADMIN_ID)
            self.assertEqual(ctx.exception.status, status)

    def test_cap_on_added_admins(self):
        store = self.store()
        for i in range(dashboard_auth.MAX_ADDED_ADMINS):
            store.add_dashboard_admin(f"user{i}", 10_000 + i, actor="swackhamer", actor_uid=ADMIN_ID)
        with self.assertRaises(dashboard_server_module().DashboardAdminError) as ctx:
            store.add_dashboard_admin("onemore", 99_999, actor="swackhamer", actor_uid=ADMIN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_malformed_stored_entries_never_grant_access(self):
        self.path.write_text(json.dumps({"state_version": 3, "dashboard_admins": [
            {"login": "bad login", "uid": 5}, {"login": "x", "uid": True}, {"login": "y", "uid": "6"},
            {"login": "eve", "uid": ADMIN_ID}, "alice", {"login": "ok-user", "uid": 7}, {"login": "dup", "uid": 7},
        ]}), encoding="utf-8")
        store = self.store()
        self.assertEqual(store.dashboard_admin_uids(), frozenset({7}))
        self.assertEqual([a["login"] for a in store.get_dashboard_admins()], ["ok-user"])

    def test_wrong_type_is_an_unreadable_state_file(self):
        self.path.write_text(json.dumps({"state_version": 3, "dashboard_admins": {"uid": 7}}), encoding="utf-8")
        with self.assertLogs("spot_dashboard", "ERROR"):
            store = DashboardStore(state_path=self.path)
        self.assertTrue(store.recovery_pending)
        self.assertEqual(store.dashboard_admin_uids(), frozenset())

    def test_unavailable_state_falls_back_to_owners_only(self):
        store = self.store()
        store.add_dashboard_admin("alice", 4242, actor="swackhamer", actor_uid=ADMIN_ID)
        fake = FakeGitHub()
        fake.add_user("tok-alice", "alice", 4242)
        fake.add_user("tok-owner", "swackhamer", ADMIN_ID)
        mgr = make_manager(fake, global_admins=[OWNER])
        self.assertTrue(mgr.authorize("tok-alice", [], added_admins=store.dashboard_admin_uids()).authorized)
        store.controlled_targets_error = "simulated unreadable state"
        self.assertEqual(store.dashboard_admin_uids(), frozenset())
        self.assertEqual(store.get_dashboard_admins(), [])
        self.assertFalse(mgr.authorize("tok-alice", [], added_admins=store.dashboard_admin_uids()).authorized)
        self.assertTrue(mgr.authorize("tok-owner", [], added_admins=store.dashboard_admin_uids()).authorized)
        with self.assertRaises(dashboard_server_module().ControlledTargetsUnavailableError):
            store.add_dashboard_admin("bob", 5, actor="swackhamer", actor_uid=ADMIN_ID)
        store.controlled_targets_error = ""
        store.recovery_pending = True
        self.assertEqual(store.dashboard_admin_uids(), frozenset())

    def test_frozen_store_refuses_changes(self):
        store = self.store()
        store.frozen = True
        with self.assertRaises(DashboardFrozenError):
            store.add_dashboard_admin("alice", 4242, actor="swackhamer", actor_uid=ADMIN_ID)
        self.assertEqual(store.dashboard_admin_uids(), frozenset())


def dashboard_server_module():
    from lib import dashboard_server
    return dashboard_server


class AdminsApiTests(unittest.TestCase):
    """HTTP: /api/admins is owner-only; added administrators sign in and lose access as soon as they're removed."""

    LOOKUP_TOKEN = "lookup-token"

    setUp = OAuthServerIntegrationTests.setUp
    config_lister = OAuthServerIntegrationTests.config_lister
    request = OAuthServerIntegrationTests.request
    cookie_pair = staticmethod(OAuthServerIntegrationTests.cookie_pair)
    complete_device = OAuthServerIntegrationTests.complete_device
    device_login = OAuthServerIntegrationTests.device_login
    login_cookie = OAuthServerIntegrationTests.login_cookie

    def start(self, *, controlled_targets=None, global_admins=(OWNER,), auth_token=None):
        self.fake.add_user("tok-alice", "alice", 4242)
        self.fake.add_user("tok-bob", "bob", 5151)
        self.fake.resources[(self.LOOKUP_TOKEN, "/users/alice")] = (200, _user_payload("alice", 4242))
        self.fake.resources[(self.LOOKUP_TOKEN, "/users/bob")] = (200, _user_payload("bob", 5151))
        self.fake.resources[(self.LOOKUP_TOKEN, "/users/acme")] = (200, _user_payload("acme", 77, "Organization"))
        self.mgr = GitHubOAuthManager(CLIENT_ID, "web-secret", None, SECRET, http_requester=self.fake,
                                      device_flow=True, clock=self.clock, global_admins=list(global_admins))
        self.provider = ConfigRepoTargets(lister=self.config_lister, ttl_seconds=0)
        server, store = create_dashboard_server(
            host="127.0.0.1", port=0, state_path=Path(self.tmp.name) / "state.json", auth_token=auth_token,
            oauth_manager=self.mgr, controlled_targets=controlled_targets, config_targets_provider=self.provider,
            github_token_provider=lambda: self.LOOKUP_TOKEN,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        self.port = server.server_address[1]
        self.store = store
        return store

    def post(self, path, body, *, cookie, key="admin-key-0001", headers=None):
        hdrs = {"Cookie": cookie, **({"Idempotency-Key": key} if key else {}), **(headers or {})}
        status, msg, raw = self.request("POST", path, headers=hdrs, body=body)
        return status, msg, json.loads(raw or b"{}")

    def get(self, path, cookie):
        status, _, raw = self.request("GET", path, headers={"Cookie": cookie})
        return status, json.loads(raw or b"{}")

    def lookups(self):
        return [p for p in self.fake.api_paths() if p.startswith("/users/")]

    def add_alice(self, owner):
        status, _, body = self.post("/api/admins/add", {"login": "alice", "confirm": True}, cookie=owner,
                                    key="add-alice-0001")
        self.assertEqual(status, 200, body)
        return body

    def test_owner_lists_adds_and_removes(self):
        self.start()
        owner = self.login_cookie("tok-admin")
        status, body = self.get("/api/admins", owner)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["owners"], [{"login": "swackhamer", "uid": ADMIN_ID, "global_admin": True}])
        self.assertEqual((body["admins"], body["added_admins_exempt"]), ([], True))
        self.assertTrue(self.get("/api/auth/status", owner)[1]["owner"])
        with self.assertLogs("spot_dashboard.auth", "INFO") as logs:
            body = self.add_alice(owner)
        self.assertTrue(any("dashboard admin added login=alice uid=4242 by=swackhamer" in line for line in logs.output))
        self.assertEqual(body["status"], "added")
        self.assertEqual({k: body["admin"][k] for k in ("login", "uid", "added_by", "added_by_uid")},
                         {"login": "alice", "uid": 4242, "added_by": "swackhamer", "added_by_uid": ADMIN_ID})
        self.assertEqual([a["uid"] for a in body["admins"]], [4242])
        self.assertEqual(self.fake.calls[-1]["headers"]["Authorization"], f"Bearer {self.LOOKUP_TOKEN}")
        self.assertEqual(self.store.dashboard_admin_uids(), frozenset({4242}))
        status, _, body = self.post("/api/admins/remove", {"uid": 4242, "confirm": True}, cookie=owner,
                                    key="remove-alice-0001")
        self.assertEqual((status, body["status"], body["admin"]), (200, "removed", {"login": "alice", "uid": 4242}))
        self.assertEqual(self.get("/api/admins", owner)[1]["admins"], [])
        history = self.get("/api/admins", owner)[1]["history"]
        self.assertEqual([h["action"] for h in history], ["added", "removed"])

    def test_added_admin_signs_in_without_target_admin_and_loses_access_on_removal(self):
        self.start(controlled_targets=["swack-tools/oxidex"])
        self.fake.set_repo("tok-alice", "swack-tools/oxidex", admin=False)
        status, _, raw = self.device_login("tok-alice")
        self.assertEqual(status, 403, raw)  # not listed yet
        owner = self.login_cookie("tok-admin")
        self.add_alice(owner)
        alice = self.login_cookie("tok-alice")
        self.assertEqual(self.get("/api/overview", alice)[0], 200)
        status, info = self.get("/api/auth/status", alice)
        self.assertEqual((info["authenticated"], info["user"], info["owner"]), (True, "alice", False))
        status, _, body = self.post("/api/admins/remove", {"uid": 4242, "confirm": True}, cookie=owner,
                                    key="remove-alice-0001")
        self.assertEqual(status, 200, body)
        # The very next request with the old cookie is refused: membership is checked on every request.
        status, body = self.get("/api/overview", alice)
        self.assertEqual(status, 403)
        self.assertIn("not an authorized administrator", body["error"])
        status, _, raw = self.request("GET", "/", headers={"Cookie": alice})
        self.assertEqual(status, 403)
        self.assertIn("Spot Dashboard sign-in", raw.decode())
        self.assertFalse(self.get("/api/auth/status", alice)[1]["authenticated"])
        status, _, raw = self.device_login("tok-alice")
        self.assertEqual(status, 403, raw)
        self.assertEqual(self.get("/api/overview", owner)[0], 200)  # the owner is unaffected

    def test_non_owners_get_403(self):
        self.start(auth_token="legacy-dashboard-token-123")
        owner = self.login_cookie("tok-admin")
        self.add_alice(owner)
        alice = self.login_cookie("tok-alice")
        status, body = self.get("/api/admins", alice)
        self.assertEqual((status, body.get("code")), (403, "owner_required"))
        for path, payload in (("/api/admins/add", {"login": "bob", "confirm": True}),
                              ("/api/admins/remove", {"uid": 4242, "confirm": True}),
                              ("/api/admins/remove", {"uid": ADMIN_ID, "confirm": True})):
            with self.subTest(path=path, payload=payload):
                status, _, body = self.post(path, payload, cookie=alice, key="nonowner-key-01")
                self.assertEqual((status, body.get("code")), (403, "owner_required"))
        self.assertEqual(self.store.dashboard_admin_uids(), frozenset({4242}))
        self.assertNotIn("/users/bob", self.lookups())
        # The loopback operator token is not a GitHub owner session either.
        status, _, raw = self.request("GET", "/api/admins",
                                      headers={"Authorization": "Bearer legacy-dashboard-token-123"})
        self.assertEqual(status, 403, raw)
        status, _, raw = self.request("GET", "/api/admins")
        self.assertEqual(status, 401, raw)

    def test_csrf_idempotency_and_confirmation(self):
        self.start()
        owner = self.login_cookie("tok-admin")
        add = {"login": "alice", "confirm": True}
        status, _, body = self.post("/api/admins/add", add, cookie=owner, key=None)
        self.assertEqual(status, 400)
        self.assertIn("Idempotency-Key", body["error"])
        status, _, body = self.post("/api/admins/add", add, cookie=owner, key="bad key!")
        self.assertEqual(status, 400)
        for missing in ({"login": "alice"}, {"login": "alice", "confirm": "yes"}, {"uid": 4242}):
            path = "/api/admins/remove" if "uid" in missing else "/api/admins/add"
            status, _, body = self.post(path, missing, cookie=owner, key="confirm-key-001")
            self.assertEqual(status, 400, missing)
            self.assertIn("confirm", body["error"])
        status, _, body = self.post("/api/admins/add", add, cookie=owner, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, body = self.post("/api/admins/add", add, cookie=owner, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _, _ = self.post("/api/admins/add", add, cookie=owner, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.lookups(), [])
        self.assertEqual(self.store.dashboard_admin_uids(), frozenset())
        # Lost response: the same key replays the stored result without a second GitHub lookup or change.
        status, _, first = self.post("/api/admins/add", add, cookie=owner, key="add-alice-0001")
        self.assertEqual(status, 200, first)
        status, msg, again = self.post("/api/admins/add", add, cookie=owner, key="add-alice-0001")
        self.assertEqual((status, msg.get("Idempotent-Replay")), (200, "true"))
        self.assertEqual(again["admin"], first["admin"])
        self.assertEqual(self.lookups(), ["/users/alice"])
        status, _, body = self.post("/api/admins/add", {"login": "bob", "confirm": True}, cookie=owner,
                                    key="add-alice-0001")
        self.assertEqual(status, 422, body)
        self.assertEqual(self.lookups(), ["/users/alice"])
        # A new key for the same user is a duplicate.
        status, _, body = self.post("/api/admins/add", {"login": "@alice", "confirm": True}, cookie=owner,
                                    key="add-alice-0002")
        self.assertEqual(status, 409, body)
        self.assertEqual(self.store.dashboard_admin_uids(), frozenset({4242}))

    def test_validation_and_github_failures(self):
        self.start()
        owner = self.login_cookie("tok-admin")
        for bad in ("", "a b", "-x", "x" * 40, "../etc", 7, None):
            status, _, body = self.post("/api/admins/add", {"login": bad, "confirm": True}, cookie=owner,
                                        key="bad-login-0001")
            self.assertEqual(status, 400, (bad, body))
        self.assertEqual(self.lookups(), [])
        status, _, body = self.post("/api/admins/add", {"login": "nobody", "confirm": True}, cookie=owner,
                                    key="nobody-key-0001")
        self.assertEqual(status, 404, body)
        status, _, body = self.post("/api/admins/add", {"login": "acme", "confirm": True}, cookie=owner,
                                    key="acme-key-00001")
        self.assertEqual(status, 400, body)
        self.assertIn("not a user", body["error"])
        status, _, body = self.post("/api/admins/add", {"login": "swackhamer", "confirm": True}, cookie=owner,
                                    key="owner-key-0001")
        self.assertEqual(status, 404, body)  # FakeGitHub has no /users/swackhamer
        self.fake.resources[(self.LOOKUP_TOKEN, "/users/swackhamer")] = (200, _user_payload("swackhamer", ADMIN_ID))
        status, _, body = self.post("/api/admins/add", {"login": "swackhamer", "confirm": True}, cookie=owner,
                                    key="owner-key-0002")
        self.assertEqual(status, 409, body)
        # GitHub outage: 424 with retry_same_key, nothing stored; the same key succeeds once GitHub answers.
        self.fake.raise_on_api = True
        status, _, body = self.post("/api/admins/add", {"login": "bob", "confirm": True}, cookie=owner,
                                    key="bob-key-000001")
        self.assertEqual((status, body.get("code"), body.get("retry_same_key")), (424, "upstream_error", True))
        self.fake.raise_on_api = False
        self.fake.resources[(self.LOOKUP_TOKEN, "/users/bob")] = (502, {"message": "bad gateway"})
        status, msg, body = self.post("/api/admins/add", {"login": "bob", "confirm": True}, cookie=owner,
                                      key="bob-key-000001")
        self.assertEqual((status, body.get("retry_same_key")), (424, True))
        self.assertEqual(msg.get("X-Spot-Origin"), "dashboard")
        self.assertEqual(self.store.dashboard_admin_uids(), frozenset())
        self.fake.resources[(self.LOOKUP_TOKEN, "/users/bob")] = (200, _user_payload("bob", 5151))
        status, msg, body = self.post("/api/admins/add", {"login": "bob", "confirm": True}, cookie=owner,
                                      key="bob-key-000001")
        self.assertEqual(status, 200, body)
        self.assertIsNone(msg.get("Idempotent-Replay"))
        for uid in (0, -1, True, "5151", None, 1.5):
            status, _, body = self.post("/api/admins/remove", {"uid": uid, "confirm": True}, cookie=owner,
                                        key="bad-uid-00001")
            self.assertEqual(status, 400, (uid, body))
        status, _, body = self.post("/api/admins/remove", {"uid": 999, "confirm": True}, cookie=owner,
                                    key="missing-uid-01")
        self.assertEqual(status, 404, body)
        status, _, body = self.post("/api/admins/remove", {"uid": ADMIN_ID, "confirm": True}, cookie=owner,
                                    key="owner-remove-1")
        self.assertEqual(status, 400, body)
        self.assertIn("can't be removed", body["error"])

    def test_frozen_dashboard_answers_the_wait_response(self):
        self.start()
        owner = self.login_cookie("tok-admin")
        self.store.frozen = True
        status, _, body = self.post("/api/admins/add", {"login": "alice", "confirm": True}, cookie=owner)
        self.assertEqual((status, body.get("error")), (503, "control_vm_unavailable"))
        self.assertEqual(self.store.dashboard_admin_uids(), frozenset())

    def test_unreadable_admin_list_means_owners_only(self):
        self.start()
        owner = self.login_cookie("tok-admin")
        self.add_alice(owner)
        alice = self.login_cookie("tok-alice")
        with mock.patch.object(self.store, "dashboard_admin_uids", side_effect=RuntimeError("boom")), \
                self.assertLogs("spot_dashboard", "ERROR"):
            self.assertEqual(self.get("/api/overview", alice)[0], 403)
            self.assertEqual(self.get("/api/overview", owner)[0], 200)
        self.assertEqual(self.get("/api/overview", alice)[0], 200)

    def test_global_admins_none_requires_target_admin_for_added_admins(self):
        self.start(controlled_targets=["swack-tools/oxidex"], global_admins=())
        self.fake.set_repo("tok-admin", "swack-tools/oxidex", admin=True)
        self.fake.set_repo("tok-alice", "swack-tools/oxidex", admin=False)
        owner = self.login_cookie("tok-admin")
        body = self.add_alice(owner)
        self.assertFalse(body["added_admins_exempt"])
        self.assertFalse(self.get("/api/admins", owner)[1]["owners"][0]["global_admin"])
        status, _, raw = self.device_login("tok-alice")
        self.assertEqual(status, 403, raw)
        self.assertIn("swack-tools/oxidex", json.loads(raw)["missing_targets"])
        self.fake.set_repo("tok-alice", "swack-tools/oxidex", admin=True)
        alice = self.login_cookie("tok-alice")
        self.assertEqual(self.get("/api/overview", alice)[0], 200)

    def test_admins_page_markup_is_owner_gated(self):
        from lib.dashboard_ui_assets import render_dashboard_html
        page = render_dashboard_html()
        self.assertRegex(page, r'<button class="tab-btn" data-tab="tab-admins" id="tab-btn-admins"[^>]*\bhidden>')
        self.assertRegex(page, r'<section id="tab-admins" class="tab-pane" hidden>')
        self.assertIn("[hidden] { display: none !important; }", page)
        self.assertIn('S.owner = Boolean(d && d.owner === true);', page)
        self.assertIn('"/api/admins/add", { login, confirm: true }', page)
        self.assertIn('"/api/admins/remove", { uid: admin.uid, confirm: true }', page)
        self.assertIn('"Idempotency-Key": S.pending.key', page)
        admins_js = page[page.index("const DashboardAdmins"):page.index("window.DashboardAdmins")]
        self.assertNotIn("innerHTML", admins_js)
        self.assertEqual(admins_js.count("window.confirm("), 2)



_ADMINS_PANEL_HARNESS = r"""
const [script, cfg] = [process.argv[1], JSON.parse(process.argv[2])];
const els = {};
function mk(tag) {
  return {tag, hidden: false, disabled: false, value: "", textContent: "", children: [], listeners: {}, className: "",
    type: "", colSpan: 1, appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); },
    get firstChild() { return this.children[0] || null; },
    addEventListener(t, fn) { this.listeners[t] = fn; }};
}
["adm-owners", "adm-admins", "adm-history", "adm-status", "adm-exempt", "adm-login", "adm-add", "adm-add-form",
 "adm-refresh", "badge-admins", "tab-btn-admins", "tab-admins"].forEach(id => { els[id] = mk("x"); });
els["tab-btn-admins"].hidden = true; els["tab-admins"].hidden = true;
global.document = {getElementById: id => els[id] || null, createElement: tag => mk(tag)};
const out = {calls: [], confirms: 0};
global.window = {confirm: () => { out.confirms += 1; return cfg.confirm !== false; }};
let keys = 0;
global.newIdempotencyKey = () => `key-${++keys}`;
global.formatLocalTime = v => String(v);
const listing = {owners: [{login: "swackhamer", uid: 619624, global_admin: true}], admins: [], history: [],
  added_admins_exempt: true};
let adds = 0;
global.fetch = async (url, init = {}) => {
  out.calls.push({url, method: init.method || "GET", body: init.body ? JSON.parse(init.body) : null,
    key: init.headers ? init.headers["Idempotency-Key"] || null : null});
  const reply = (status, body) => ({ok: status < 400, status, json: async () => body});
  if (url === "/api/auth/status") return reply(200, {authenticated: true, owner: cfg.owner});
  if (url === "/api/admins") return cfg.owner ? reply(200, listing) : reply(403, {error: "owner only"});
  if (url === "/api/admins/add") {
    adds += 1;
    if (adds <= (cfg.add_failures || 0)) return reply(424, {error: "GitHub down", retry_same_key: true});
    return reply(200, {...listing, status: "added", admin: {login: "alice", uid: 4242},
      admins: [{login: "alice", uid: 4242, added_by: "swackhamer", added_at: "2026-10-01T00:00:00Z"}]});
  }
  return reply(404, {});
};
new Function(script + "; globalThis.DashboardAdmins = DashboardAdmins;")();
(async () => {
  await DashboardAdmins.init();
  out.tab_hidden = els["tab-btn-admins"].hidden; out.pane_hidden = els["tab-admins"].hidden;
  for (const login of cfg.adds || []) {
    els["adm-login"].value = login;
    await els["adm-add-form"].listeners.submit({preventDefault() {}});
  }
  out.status = els["adm-status"].textContent;
  out.rows = els["adm-admins"].children.length;
  process.stdout.write(JSON.stringify(out));
})();
"""


class AdminsPanelScriptTests(unittest.TestCase):
    def run_panel(self, cfg):
        from lib.dashboard_ui_assets import render_dashboard_html
        page = render_dashboard_html()
        block = page[page.index("    const DashboardAdmins = (function () {"):page.index("    window.DashboardAdmins")]
        return _run_node(_ADMINS_PANEL_HARNESS, block, cfg)

    def test_tab_stays_hidden_for_non_owners(self):
        out = self.run_panel({"owner": False})
        self.assertEqual((out["tab_hidden"], out["pane_hidden"]), (True, True))
        self.assertEqual([c["url"] for c in out["calls"]], ["/api/auth/status"])  # never asks for the list

    def test_owner_adds_with_confirmation_and_reuses_the_key_for_a_retry(self):
        out = self.run_panel({"owner": True, "adds": ["@alice", "alice"], "add_failures": 1})
        self.assertEqual((out["tab_hidden"], out["pane_hidden"]), (False, False))
        posts = [c for c in out["calls"] if c["method"] == "POST"]
        self.assertEqual([p["body"] for p in posts], [{"login": "alice", "confirm": True}] * 2)
        self.assertEqual(posts[0]["key"], posts[1]["key"])  # same change after retry_same_key: same key
        self.assertEqual(out["confirms"], 2)
        self.assertIn("Added alice", out["status"])
        self.assertEqual(out["rows"], 1)

    def test_declined_confirmation_and_bad_login_send_nothing(self):
        out = self.run_panel({"owner": True, "adds": ["alice"], "confirm": False})
        self.assertEqual([c for c in out["calls"] if c["method"] == "POST"], [])
        out = self.run_panel({"owner": True, "adds": ["bad login", "-x", ""]})
        self.assertEqual([c for c in out["calls"] if c["method"] == "POST"], [])
        self.assertEqual(out["confirms"], 0)

if __name__ == "__main__":
    unittest.main()
