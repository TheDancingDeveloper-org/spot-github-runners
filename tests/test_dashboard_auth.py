"""Unit and HTTP integration tests for GitHub OAuth login and admin verification (dashboard_auth)."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse

from lib import dashboard_auth
from lib.dashboard_auth import (
    DEFAULT_ALLOWED_ADMINS,
    GITHUB_ACCESS_TOKEN_URL,
    GITHUB_AUTHORIZE_URL,
    GITHUB_DEVICE_CODE_URL,
    MAX_GITHUB_RESPONSE_BYTES,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthCapacityError,
    GitHubApiError,
    GitHubOAuthManager,
    RateLimiter,
    TargetChange,
    default_http_request,
    derive_purpose_key,
    format_session_cookie,
    format_state_cookie,
    parse_allowed_admins,
    parse_controlled_target,
    parse_controlled_targets,
    parse_cookie_header,
    safe_return_path,
    sign_token,
    target_list_digest,
)
from lib.dashboard_server import DashboardStore, build_oauth_config, create_dashboard_server

SECRET = "test-session-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
CLIENT_ID = "Iv1.testclientid"
ADMIN_UID = 167585258


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
        # code -> PKCE code_challenge that GitHub bound to the code at /login/oauth/authorize.
        self.code_challenges: dict[str, str] = {}
        self.calls: list[dict] = []
        self.raise_on_api = False
        self.on_api_call = None  # optional hook(path) run before answering a REST call

    def add_user(self, token: str, login: str, uid: int = 1000) -> None:
        self.users[token] = (login, uid)

    def set_repo(self, token: str, full_name: str, status: int = 200, admin: object = True) -> None:
        payload = {"full_name": full_name, "permissions": {"admin": admin, "push": True, "pull": True}}
        self.resources[(token, "/repos/" + full_name)] = (status, payload if status == 200 else {"message": "x"})

    def set_org(self, token: str, org: str, status: int = 200, role: str = "admin", state: str = "active") -> None:
        payload = {"role": role, "state": state, "organization": {"login": org}}
        self.resources[(token, "/user/memberships/orgs/" + org)] = (status, payload if status == 200 else {"message": "x"})

    def issue_code(self, code: str, token: str, authorize_url: str) -> None:
        """Simulate GitHub issuing ``code`` for an authorize request (binding its PKCE challenge)."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)
        assert query.get("code_challenge_method") == ["S256"], query
        self.code_tokens[code] = token
        self.code_challenges[code] = query["code_challenge"][0]

    def api_paths(self) -> list[str]:
        return [urllib.parse.urlsplit(c["url"]).path for c in self.calls if "api.github.com" in c["url"]]

    def token_calls(self) -> list[dict]:
        return [c for c in self.calls if c["url"] == GITHUB_ACCESS_TOKEN_URL]

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        if url == GITHUB_DEVICE_CODE_URL:
            return self.device_code_status, json.dumps(self.device_code_response).encode()
        if url == GITHUB_ACCESS_TOKEN_URL:
            form = urllib.parse.parse_qs((body or b"").decode())
            if form.get("grant_type") == ["urn:ietf:params:oauth:grant-type:device_code"]:
                resp = self.device_polls.pop(0) if self.device_polls else {"error": "authorization_pending"}
                return 200, json.dumps(resp).encode()
            code = form.get("code", [""])[0]
            if code in self.code_challenges:
                verifier = form.get("code_verifier", [""])[0]
                digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if digest != self.code_challenges[code]:
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
global.document = {title: "t", getElementById: () => null};
global.sessionStorage = session; global.localStorage = local;
global.fetch = async (url, init) => { out.posts.push([url, JSON.parse(init.body)]);
  return {ok: cfg.login_ok, status: cfg.login_ok ? 200 : 401, json: async () => ({})}; };
new Function(script)();
setTimeout(() => { out.session = session.dump(); out.local = local.dump();
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
    return {ok: true, status: 200, headers: new Headers(), json: async () => ({
      user_code: "ABCD-1234", verification_uri: "https://github.com/login/device", interval: 5,
      expires_in: 900, device_handle: "h"})};
  }
  polls += 1;
  if (polls <= cfg.poll_failures) throw new TypeError("network reset");
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

_FETCH_WRAPPER_HARNESS = r"""
const [source, cfg] = [process.argv[1], JSON.parse(process.argv[2])];
const out = {calls: [], assigned: null, prompted: false, resolved: null};
const NATIVE_FETCH = async (url, init) => {
  out.calls.push(url);
  if (url === "/api/auth/status") {
    return {ok: true, status: 200, json: async () => ({oauth: {enabled: cfg.oauth}})};
  }
  return {ok: false, status: 401, json: async () => ({error: "Unauthorized"})};
};
function getDashboardAuthToken() { return ""; }
function storeDashboardAuthToken() { return ""; }
async function authenticateDashboardToken() { return false; }
global.window = {location: {assign: u => { out.assigned = u; }}, prompt: () => { out.prompted = true; return null; }};
new Function("NATIVE_FETCH", "getDashboardAuthToken", "storeDashboardAuthToken",
  "authenticateDashboardToken", source)(NATIVE_FETCH, getDashboardAuthToken, storeDashboardAuthToken,
  authenticateDashboardToken);
window.fetch("/api/overview").then(r => { out.resolved = r.status; });
setTimeout(() => process.stdout.write(JSON.stringify(out)), 50);
"""


def _run_node(harness: str, source: str, cfg: dict) -> dict:
    proc = subprocess.run(["node", "-e", harness, source, json.dumps(cfg)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout)


def _run_gate_script(script: str, *, stored: str, hash_part: str, login_ok: bool, recent: bool) -> dict:
    import time as _time
    session = {"dashboard_auth_token": stored} if stored else {}
    if recent:
        session["dashboard_gate_token_attempt"] = str(int(_time.time() * 1000))
    cfg = {"session": session, "local": {}, "hash": hash_part, "login_ok": login_ok}
    proc = subprocess.run(["node", "-e", _GATE_HARNESS, script, json.dumps(cfg)],
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
        self.assertEqual(DEFAULT_ALLOWED_ADMINS, frozenset({"swackhamer", "xbmc4lyfe"}))
        mgr = make_manager()
        self.assertEqual(mgr.allowed_admins, frozenset({"swackhamer", "xbmc4lyfe"}))
        manager, targets = build_oauth_config(environ={"GITHUB_OAUTH_CLIENT_ID": CLIENT_ID, "DASHBOARD_SESSION_SECRET": SECRET})
        self.assertIsNotNone(manager)
        self.assertEqual(targets, [])
        self.assertEqual(manager.allowed_admins, DEFAULT_ALLOWED_ADMINS)
        self.assertEqual(manager.session_ttl_seconds, 43_200)
        self.assertEqual(manager.session_not_before, 0)

    def test_env_and_cli_overrides(self):
        env = {
            "GITHUB_OAUTH_CLIENT_ID": CLIENT_ID,
            "DASHBOARD_SESSION_SECRET": SECRET,
            "DASHBOARD_ALLOWED_ADMINS": "Alice, bob",
            "DASHBOARD_CONTROLLED_TARGETS": "swack-tools, xbmc4lyfe/newznab-tmux",
        }
        manager, targets = build_oauth_config(environ=env)
        self.assertEqual(manager.allowed_admins, frozenset({"alice", "bob"}))
        self.assertEqual(targets, ["swack-tools", "xbmc4lyfe/newznab-tmux"])
        manager, targets = build_oauth_config(environ=env, allowed_admins="carol", controlled_targets="Appz4Fun")
        self.assertEqual(manager.allowed_admins, frozenset({"carol"}))
        self.assertEqual(targets, ["Appz4Fun"])
        self.assertFalse(manager.web_flow_enabled)
        self.assertTrue(manager.device_flow_enabled)

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
        mgr = GitHubOAuthManager.from_environ({**base, "DASHBOARD_SESSION_NOT_BEFORE": str(int(clock.now) - 5)}, clock=clock)
        self.assertEqual(mgr.session_not_before, int(clock.now) - 5)
        for bad in ("soon", "-1", str(int(clock.now) + 3600)):
            with self.assertRaises(ValueError, msg=bad):
                GitHubOAuthManager.from_environ({**base, "DASHBOARD_SESSION_NOT_BEFORE": bad}, clock=clock)

    def test_invalid_allowlist_and_targets_rejected(self):
        with self.assertRaises(ValueError):
            parse_allowed_admins("good, bad/login")
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
        token = self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=42, verified_targets=["Org"])
        self.assertEqual(self.mgr.verify_session_cookie_value(token), (True, "swackhamer"))
        payload = decode_payload(token)
        self.assertEqual(set(payload), {"v", "sub", "uid", "iat", "exp", "tgt"})
        self.assertEqual(payload["tgt"], ["org"])
        self.assertEqual(payload["uid"], 42)
        self.assertNotIn("access_token", json.dumps(payload))

    def test_verifiable_by_another_instance_with_same_secret(self):
        token = self.mgr.create_session_cookie_value("xbmc4lyfe", 3600, user_id=2)
        other = make_manager(clock=self.clock)
        self.assertEqual(other.verify_session_cookie_value(token), (True, "xbmc4lyfe"))
        different = GitHubOAuthManager(CLIENT_ID, "", None, SECRET + "-other", clock=self.clock)
        self.assertEqual(different.verify_session_cookie_value(token), (False, ""))

    def test_tampered_cookie_rejected(self):
        token = self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=1)
        body, sig = token.split(".")
        forged_payload = decode_payload(token)
        forged_payload["sub"] = "xbmc4lyfe"
        forged_body = base64.urlsafe_b64encode(json.dumps(forged_payload).encode()).decode().rstrip("=")
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        for bad in (f"{forged_body}.{sig}", f"{body}.{flipped}", body, f"{token}.x", "", "é.é", "x" * 5000, None, 12):
            self.assertEqual(self.mgr.verify_session_cookie_value(bad), (False, ""), bad)

    def test_expired_cookie_rejected(self):
        token = self.mgr.create_session_cookie_value("swackhamer", 120, user_id=1)
        self.clock.now += 119
        self.assertTrue(self.mgr.verify_session_cookie_value(token)[0])
        self.clock.now += 2
        self.assertEqual(self.mgr.verify_session_cookie_value(token), (False, ""))

    def test_state_token_is_not_a_session(self):
        state = self.mgr.create_state("/")
        self.assertEqual(self.mgr.verify_session_cookie_value(state), (False, ""))

    def test_uid_is_required(self):
        with self.assertRaises(TypeError):
            self.mgr.create_session_cookie_value("swackhamer", 3600)  # noqa: missing user_id
        for bad_uid in (None, 0, -1, "42", True):
            with self.assertRaises(ValueError):
                self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=bad_uid)
        key = derive_purpose_key(SECRET, "session")
        now = int(self.clock.now)
        base = {"v": 1, "sub": "swackhamer", "iat": now, "exp": now + 600, "tgt": []}
        for payload in (base, {**base, "uid": None}, {**base, "uid": "42"}, {**base, "uid": 0}):
            forged = sign_token(key, payload)  # correctly signed, but no usable GitHub numeric id
            self.assertEqual(self.mgr.verify_session_cookie_value(forged), (False, ""), payload)
            self.assertEqual(self.mgr.check_session(forged, []).status, "none")
        self.assertTrue(self.mgr.verify_session_cookie_value(sign_token(key, {**base, "uid": 42}))[0])

    def test_not_before_revokes_older_sessions(self):
        old = self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=1)
        self.clock.now += 100
        revoking = make_manager(clock=self.clock, session_not_before=int(self.clock.now))
        self.assertEqual(revoking.verify_session_cookie_value(old), (False, ""))
        self.assertEqual(revoking.check_session(old, []).status, "none")
        fresh = revoking.create_session_cookie_value("swackhamer", 3600, user_id=1)
        self.assertTrue(revoking.verify_session_cookie_value(fresh)[0])
        self.assertTrue(self.mgr.verify_session_cookie_value(old)[0])  # without the cut-off it is still valid

    def test_removed_admin_and_new_target_forbid_session(self):
        token = self.mgr.create_session_cookie_value("eve", 3600, user_id=7)
        self.assertEqual(self.mgr.verify_session_cookie_value(token), (False, ""))
        self.assertEqual(self.mgr.check_session(token, []).status, "forbidden")
        token = self.mgr.create_session_cookie_value("swackhamer", 3600, user_id=1, verified_targets=["swack-tools"])
        self.assertEqual(self.mgr.check_session(token, ["Swack-Tools"]).status, "ok")
        check = self.mgr.check_session(token, ["swack-tools", "o/r"])
        self.assertEqual(check.status, "forbidden")
        self.assertEqual(check.missing_targets, ("o/r",))
        self.assertEqual(self.mgr.check_session(token, ["bad/target/name"]).status, "forbidden")
        self.assertEqual(self.mgr.check_session("garbage", []).status, "none")

    def test_cookie_attributes_use_host_prefix(self):
        self.assertEqual(SESSION_COOKIE_NAME, "__Host-spot_session")
        self.assertEqual(OAUTH_STATE_COOKIE_NAME, "__Host-spot_oauth_state")
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
        result = self.mgr.validate_state(state, state)
        self.assertEqual((result.ok, result.return_to, result.target_change), (True, "/dashboard?tab=1", None))
        self.assertGreaterEqual(len(result.code_verifier), 43)
        self.assertFalse(self.mgr.validate_state(state, state).ok)  # replay

    def test_tampered_state_rejected(self):
        state = self.mgr.create_state("/")
        body, sig = state.split(".")
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        tampered = f"{body}.{flipped}"
        self.assertFalse(self.mgr.validate_state(tampered, tampered).ok)
        other = GitHubOAuthManager(CLIENT_ID, "", None, SECRET + "-x", clock=self.clock).create_state("/")
        self.assertFalse(self.mgr.validate_state(other, other).ok)

    def test_state_must_match_browser_cookie(self):
        state = self.mgr.create_state("/")
        self.assertFalse(self.mgr.validate_state(state, "").ok)
        self.assertFalse(self.mgr.validate_state(state, self.mgr.create_state("/")).ok)

    def test_expired_state_rejected(self):
        state = self.mgr.create_state("/")
        self.clock.now += 601
        self.assertFalse(self.mgr.validate_state(state, state).ok)

    def test_open_redirects_sanitized(self):
        for bad in ("https://evil.example", "//evil.example", "/\\evil", "javascript:alert(1)", "/a\nb", "", "/a b"):
            self.assertEqual(safe_return_path(bad), "/")
        state = self.mgr.create_state("//evil.example/x")
        self.assertEqual(self.mgr.validate_state(state, state).return_to, "/")

    def test_non_ascii_return_path_rejected(self):
        for bad in ("/中", "/café", "/ ", "/ ", "/ok\x7f"):
            self.assertEqual(safe_return_path(bad), "/", repr(bad))
        self.assertEqual(safe_return_path("/%E4%B8%AD?a=b~"), "/%E4%B8%AD?a=b~")

    def test_target_change_bound_into_state(self):
        change = TargetChange(("swack-tools",), target_list_digest([]), ADMIN_UID, "swackhamer")
        state = self.mgr.create_state("/", target_change=change)
        result = self.mgr.validate_state(state, state)
        self.assertTrue(result.ok)
        self.assertEqual(result.target_change, change)
        too_big = TargetChange(tuple(f"org{i:03d}/{'r' * 90}" for i in range(32)), "0" * 64, 1, "swackhamer")
        with self.assertRaises(ValueError):
            self.mgr.create_state("/", target_change=too_big)

    def test_used_nonce_table_is_bounded(self):
        with mock.patch.object(dashboard_auth, "MAX_USED_STATE_NONCES", 2):
            states = [self.mgr.create_state("/") for _ in range(3)]
            self.assertTrue(self.mgr.validate_state(states[0], states[0]).ok)
            self.assertTrue(self.mgr.validate_state(states[1], states[1]).ok)
            # Full: new logins are refused rather than evicting live nonces...
            with self.assertRaises(AuthCapacityError) as ctx:
                self.mgr.create_state("/")
            self.assertGreaterEqual(ctx.exception.retry_after, 1)
            self.assertFalse(self.mgr.validate_state(states[2], states[2]).ok)
            # ...and the recorded nonces still block replay.
            self.assertFalse(self.mgr.validate_state(states[0], states[0]).ok)
            self.clock.now += 601
            fresh = self.mgr.create_state("/")
            self.assertTrue(self.mgr.validate_state(fresh, fresh).ok)


class DeviceFlowTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.clock = FakeClock()
        self.mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=self.fake, clock=self.clock)

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
            self.mgr.begin_device_login()

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

            mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=broken)
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

    def test_authorize_url_carries_pkce_challenge(self):
        state = self.mgr.create_state("/")
        url = self.mgr.create_authorize_url("https://spot.example/auth/github/callback", state)
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", GITHUB_AUTHORIZE_URL)
        q = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(q["client_id"], [CLIENT_ID])
        self.assertEqual(q["state"], [state])
        self.assertEqual(q["redirect_uri"], ["https://spot.example/auth/github/callback"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        verifier = self.mgr.validate_state(state, state).code_verifier
        self.assertNotIn(verifier, url)
        self.assertNotIn(verifier, json.dumps(decode_payload(state)))
        self.assertEqual(q["code_challenge"], [GitHubOAuthManager.pkce_challenge(verifier)])
        with self.assertRaises(ValueError):
            self.mgr.create_authorize_url("https://x/cb", "st.ate")

    def test_exchange_sends_verifier(self):
        state = self.mgr.create_state("/")
        self.fake.issue_code("code123", "gho_web", self.mgr.create_authorize_url("https://x/cb", state))
        verifier = self.mgr.validate_state(state, state).code_verifier
        self.assertEqual(self.mgr.exchange_code_for_token("code123", "https://x/cb", verifier)["access_token"], "gho_web")
        form = urllib.parse.parse_qs(self.fake.calls[-1]["body"].decode())
        self.assertEqual(form["client_secret"], ["web-secret"])
        self.assertEqual(form["redirect_uri"], ["https://x/cb"])
        self.assertEqual(form["code_verifier"], [verifier])
        other_state = self.mgr.create_state("/")
        other_verifier = self.mgr.validate_state(other_state, other_state).code_verifier
        with self.assertRaises(GitHubApiError):
            self.mgr.exchange_code_for_token("code123", "https://x/cb", other_verifier)
        with self.assertRaises(GitHubApiError):
            self.mgr.exchange_code_for_token("wrong", "https://x/cb", verifier)
        with self.assertRaises(GitHubApiError):
            self.mgr.exchange_code_for_token("", "https://x/cb", verifier)
        with self.assertRaises(GitHubApiError):
            self.mgr.exchange_code_for_token("code123", "https://x/cb", "short")

    def test_web_flow_requires_secret(self):
        mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=self.fake)
        self.assertFalse(mgr.web_flow_enabled)
        with self.assertRaises(ValueError):
            mgr.create_authorize_url("https://x/cb", mgr.create_state("/"))
        with self.assertRaises(GitHubApiError):
            mgr.exchange_code_for_token("code123", "https://x/cb", "v" * 43)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.fake.add_user("tok-admin", "Swackhamer", ADMIN_UID)
        self.fake.add_user("tok-eve", "eve", 7)
        self.mgr = make_manager(self.fake)

    def test_allowlisted_user_with_empty_targets(self):
        ok, msg, data = self.mgr.verify_and_authorize_token("tok-admin", [])
        self.assertTrue(ok, msg)
        self.assertEqual(data["user"], "Swackhamer")
        self.assertEqual(data["user_id"], ADMIN_UID)
        self.assertEqual(self.fake.api_paths(), ["/user"])

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
        # API errors on a fresh check also drop the grant.
        self.fake.set_org("tok-admin", "swack-tools")
        self.assertTrue(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized)
        self.fake.set_org("tok-admin", "swack-tools", status=500)
        self.assertFalse(self.mgr.authorize("tok-admin", ["swack-tools"], use_cache=False).authorized)
        self.assertFalse(self.mgr.verification_cache.is_verified(ADMIN_UID, "swackhamer", "swack-tools"))

    def test_session_fits_rejects_oversized_target_lists(self):
        self.assertTrue(self.mgr.session_fits([]))
        self.assertTrue(self.mgr.session_fits(["swack-tools", "xbmc4lyfe/newznab-tmux"]))
        self.assertFalse(self.mgr.session_fits(_long_repo_targets(32)))

    def test_cache_is_pinned_to_github_uid(self):
        self.fake.set_org("tok-admin", "swack-tools")
        self.assertTrue(self.mgr.authorize("tok-admin", ["swack-tools"]).authorized)
        # Same login, different GitHub account id (renamed/recreated account): no cached verification.
        self.fake.add_user("tok-impostor", "swackhamer", 999)
        decision = self.mgr.authorize("tok-impostor", ["swack-tools"])
        self.assertFalse(decision.authorized)
        self.assertFalse(decision.checks[0].cached)
        self.assertTrue(self.mgr.verification_cache.is_verified(ADMIN_UID, "swackhamer", "swack-tools"))
        self.assertFalse(self.mgr.verification_cache.is_verified(999, "swackhamer", "swack-tools"))

    def test_expected_uid_mismatch_denied(self):
        self.assertTrue(self.mgr.authorize("tok-admin", [], expected_uid=ADMIN_UID).authorized)
        decision = self.mgr.authorize("tok-admin", [], expected_uid=ADMIN_UID + 1)
        self.assertFalse(decision.authorized)
        self.assertIn("not the account that requested", decision.message)

    def test_default_transport_refuses_non_github_hosts(self):
        with self.assertRaises(GitHubApiError):
            default_http_request("GET", "http://api.github.com/user", {}, None, 1.0)
        with self.assertRaises(GitHubApiError):
            default_http_request("GET", "https://evil.example/user", {}, None, 1.0)


class StoreTargetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_inaccessible_state_file_fails_closed(self):
        DashboardStore(state_path=self.path).set_controlled_targets(["swack-tools/oxidex"])
        original = self.path.read_bytes()
        for exc in (PermissionError(13, "Permission denied"), IsADirectoryError(21, "Is a directory"),
                    OSError(5, "Input/output error")):
            with mock.patch.object(Path, "read_text", side_effect=exc), \
                    mock.patch.object(Path, "exists", return_value=False):
                with self.assertLogs("spot_runner_autoscaler.dashboard_auth", "ERROR"):
                    store = DashboardStore(state_path=self.path)
            self.assertIn("could not be loaded", store.controlled_targets_error, exc)
            self.assertEqual(store.get_controlled_targets(), ["!unavailable-controlled-targets"])
            self.assertEqual(self.path.read_bytes(), original)  # never rewritten

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_unreadable_state_directory_fails_closed(self):
        locked = Path(self.tmp.name) / "locked"
        locked.mkdir()
        path = locked / "state.json"
        DashboardStore(state_path=path).set_controlled_targets(["swack-tools/oxidex"])
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertLogs("spot_runner_autoscaler.dashboard_auth", "ERROR"):
            store = DashboardStore(state_path=path)
        self.assertEqual(store.get_controlled_targets(), ["!unavailable-controlled-targets"])

    def test_missing_state_file_starts_from_defaults(self):
        store = DashboardStore(state_path=Path(self.tmp.name) / "new" / "state.json")
        self.assertEqual((store.controlled_targets_error, store.get_controlled_targets()), ("", []))

    def test_corrupt_state_file_fails_closed_and_is_preserved(self):
        DashboardStore(state_path=self.path).set_controlled_targets(["swack-tools/oxidex"])
        for corrupt in (self.path.read_bytes()[:-5], b"\xff\xfe not utf-8", b"[1, 2]", b'"text"'):
            self.path.write_bytes(corrupt)
            with self.assertLogs("spot_runner_autoscaler.dashboard_auth", "ERROR"):
                store = DashboardStore(state_path=self.path)
            self.assertIn("could not be loaded", store.controlled_targets_error)
            self.assertEqual(store.get_controlled_targets(), ["!unavailable-controlled-targets"])
            mgr = make_manager()
            session = mgr.create_session_cookie_value("swackhamer", 600, user_id=ADMIN_UID)
            self.assertEqual(mgr.check_session(session, store.get_controlled_targets()).status, "forbidden")
            with self.assertRaises(Exception) as ctx:
                store.set_controlled_targets([])
            self.assertEqual(type(ctx.exception).__name__, "ControlledTargetsUnavailableError")
            store.update_budget({"target": "global", "daily_cap_usd": 5})  # other features keep working
            self.assertEqual(self.path.read_bytes(), corrupt)  # never rewritten

    def test_missing_and_valid_state(self):
        store = DashboardStore(state_path=self.path)
        self.assertEqual((store.controlled_targets_error, store.get_controlled_targets()), ("", []))
        store.set_controlled_targets(["Swack-Tools", "swack-tools", "o/r"], actor="legacy-token", method="token")
        reloaded = DashboardStore(state_path=self.path)
        self.assertEqual(reloaded.get_controlled_targets(), ["Swack-Tools", "o/r"])
        self.assertEqual(reloaded.get_controlled_targets_history()[-1]["actor"], "legacy-token")

    def test_history_is_bounded_and_cas(self):
        store = DashboardStore(state_path=None)
        for i in range(60):
            store.set_controlled_targets([f"org{i}"], actor="swackhamer", actor_uid=1, method="github_oauth")
        history = store.get_controlled_targets_history()
        self.assertEqual(len(history), 50)
        self.assertEqual((history[-1]["previous"], history[-1]["targets"]), (["org58"], ["org59"]))
        self.assertFalse(history[-1]["persistent"])
        with self.assertRaises(Exception) as ctx:
            store.set_controlled_targets([], expected_digest=target_list_digest(["org1"]))
        self.assertEqual(type(ctx.exception).__name__, "ControlledTargetsConflictError")
        store.set_controlled_targets([], expected_digest=target_list_digest(["org59"]))
        self.assertEqual(store.get_controlled_targets(), [])
        with self.assertRaises(ValueError):
            store.set_controlled_targets([f"o{i}" for i in range(33)])


class OAuthServerIntegrationTests(unittest.TestCase):
    LEGACY_TOKEN = "legacy-dashboard-token-123"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeGitHub()
        self.fake.add_user("tok-admin", "swackhamer", ADMIN_UID)
        self.fake.add_user("tok-eve", "eve", 7)
        self.clock = FakeClock()

    def start(self, *, client_secret="web-secret", auth_token=None, controlled_targets=None, persistent=True):
        mgr = GitHubOAuthManager(CLIENT_ID, client_secret, None, SECRET, http_requester=self.fake, clock=self.clock)
        self.mgr = mgr
        server, store = create_dashboard_server(
            host="127.0.0.1",
            port=0,
            state_path=Path(self.tmp.name) / "state.json" if persistent else None,
            auth_token=auth_token,
            oauth_manager=mgr,
            controlled_targets=controlled_targets,
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
        """Poll a device handle (respecting the interval) until GitHub returns ``access_token``."""
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
        return self.complete_device(started["device_handle"], access_token)

    def login_cookie(self, access_token="tok-admin"):
        status, msg, raw = self.device_login(access_token)
        self.assertEqual(status, 200, raw)
        return self.cookie_pair(msg, SESSION_COOKIE_NAME)[0]

    def web_login(self, code, access_token, *, next_path="/", extra_cookie=""):
        _, msg, _ = self.request("GET", f"/auth/github/login?next={urllib.parse.quote(next_path)}",
                                 headers={"Host": "spot.example"})
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(msg["Location"]).query)["state"][0]
        self.fake.issue_code(code, access_token, msg["Location"])
        cookie = f"{OAUTH_STATE_COOKIE_NAME}={state}" + (f"; {extra_cookie}" if extra_cookie else "")
        qs = urllib.parse.urlencode({"code": code, "state": state})
        return self.request("GET", f"/auth/github/callback?{qs}", headers={"Host": "spot.example", "Cookie": cookie})

    # -- baseline behaviour --

    def test_health_401_and_login_gate(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        status, _, raw = self.request("GET", "/api/health")
        self.assertEqual((status, json.loads(raw)["status"]), (200, "healthy"))
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
        self.assertEqual(decode_payload(pair.split("=", 1)[1])["uid"], ADMIN_UID)
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

    def test_unprefixed_cookie_name_is_ignored(self):
        self.start()
        pair = self.login_cookie()
        shadow = "spot_session=" + pair.split("=", 1)[1]
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": shadow})
        self.assertEqual(status, 401)

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
        pair = self.login_cookie()
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        status, _, raw = self.request("POST", "/api/auth/targets", headers=bearer, body={"targets": ["swack-tools"]})
        self.assertEqual((status, json.loads(raw)["controlled_targets"]), (200, ["swack-tools"]))
        status, _, raw = self.request("GET", "/api/overview", headers={"Cookie": pair})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["missing_targets"], ["swack-tools"])
        status, _, raw = self.request("GET", "/", headers={"Cookie": pair})
        self.assertEqual(status, 403)
        self.assertIn("Sign out", raw.decode())
        status, _, _ = self.request("POST", "/api/auth/targets", headers=bearer, body={"targets": ["bad/t/x"]})
        self.assertEqual(status, 400)

    def test_forged_session_is_unauthenticated(self):
        self.start()
        other = GitHubOAuthManager(CLIENT_ID, "", None, SECRET + "-attacker")
        forged = other.create_session_cookie_value("swackhamer", 3600, user_id=ADMIN_UID)
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": f"{SESSION_COOKIE_NAME}={forged}"})
        self.assertEqual(status, 401)

    def test_session_without_uid_is_unauthenticated(self):
        self.start()
        now = int(self.clock.now)
        no_uid = sign_token(derive_purpose_key(SECRET, "session"),
                            {"v": 1, "sub": "swackhamer", "iat": now, "exp": now + 600, "tgt": []})
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": f"{SESSION_COOKIE_NAME}={no_uid}"})
        self.assertEqual(status, 401)

    def test_not_before_rejects_existing_session_on_new_backend(self):
        self.start()
        pair = self.login_cookie()
        self.clock.now += 60
        revoking = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, clock=self.clock,
                                      session_not_before=int(self.clock.now))
        self.assertEqual(revoking.check_session(pair.split("=", 1)[1], []).status, "none")
        self.assertEqual(self.mgr.check_session(pair.split("=", 1)[1], []).status, "ok")

    # -- authorization code flow --

    def test_web_code_flow_with_pkce(self):
        self.start()
        status, msg, _ = self.request("GET", "/auth/github/login?next=/index.html", headers={"Host": "spot.example"})
        self.assertEqual(status, 302)
        location = urllib.parse.urlsplit(msg["Location"])
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", GITHUB_AUTHORIZE_URL)
        query = urllib.parse.parse_qs(location.query)
        self.assertEqual(query["redirect_uri"], ["http://spot.example/auth/github/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(len(query["code_challenge"][0]), 43)
        state = query["state"][0]
        state_pair, state_header = self.cookie_pair(msg, OAUTH_STATE_COOKIE_NAME)
        self.assertEqual(state_pair, f"{OAUTH_STATE_COOKIE_NAME}={state}")
        for attr in ("Path=/;", "Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(attr, state_header)
        self.assertNotIn("Domain", state_header)
        self.fake.issue_code("goodcode", "tok-admin", msg["Location"])

        qs = urllib.parse.urlencode({"code": "goodcode", "state": state})
        status, _, _ = self.request("GET", f"/auth/github/callback?{qs}", headers={"Host": "spot.example"})
        self.assertEqual(status, 400)  # no browser-bound state cookie
        self.assertEqual(self.fake.token_calls(), [])

        status, msg, _ = self.request(
            "GET", f"/auth/github/callback?{qs}",
            headers={"Host": "spot.example", "Cookie": f"{OAUTH_STATE_COOKIE_NAME}={state}"},
        )
        self.assertEqual(status, 302)
        self.assertEqual(msg["Location"], "/index.html")
        form = urllib.parse.parse_qs(self.fake.token_calls()[-1]["body"].decode())
        self.assertEqual(len(form["code_verifier"][0]), 43)
        pair, _ = self.cookie_pair(msg, SESSION_COOKIE_NAME)
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": pair})
        self.assertEqual(status, 200)

        status, _, _ = self.request(
            "GET", f"/auth/github/callback?{qs}",
            headers={"Host": "spot.example", "Cookie": f"{OAUTH_STATE_COOKIE_NAME}={state}"},
        )
        self.assertEqual(status, 400)  # replayed state

    def test_code_replayed_with_other_browser_state_fails(self):
        self.start()
        # Victim browser starts a login; GitHub issues a code bound to the victim's PKCE challenge.
        _, victim, _ = self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})
        self.fake.issue_code("victimcode", "tok-admin", victim["Location"])
        # Attacker obtains the code and replays it with their own state and state cookie.
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

    def test_web_code_flow_non_admin_forbidden(self):
        self.start()
        status, msg, raw = self.web_login("evecode", "tok-eve")
        self.assertEqual(status, 403)
        self.assertIn("not an authorized administrator", raw.decode())
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])

    def test_web_flow_disabled_without_secret(self):
        self.start(client_secret="")
        status, _, _ = self.request("GET", "/auth/github/login")
        self.assertEqual(status, 404)
        status, _, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn('href="/auth/github/login"', raw.decode())
        self.assertIn("/auth/device/code", raw.decode())

    # -- legacy token --

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

    def test_legacy_token_target_change_is_audited(self):
        store = self.start(auth_token=self.LEGACY_TOKEN)
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        with self.assertLogs("spot_runner_autoscaler.dashboard_auth", "WARNING") as logs:
            status, _, raw = self.request("POST", "/api/auth/targets", headers=bearer, body={"targets": ["swack-tools"]})
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual((body["status"], body["actor"]), ("updated", "legacy-token"))
        self.assertNotIn("warning", body)
        self.assertTrue(any("legacy-token" in line and "swack-tools" in line for line in logs.output))
        entry = store.get_controlled_targets_history()[-1]
        self.assertEqual((entry["actor"], entry["method"], entry["previous"], entry["targets"]),
                         ("legacy-token", "token", [], ["swack-tools"]))
        self.assertIsInstance(entry["epoch"], int)
        status, _, raw = self.request("GET", "/api/auth/targets", headers=bearer)
        self.assertEqual(json.loads(raw)["history"][-1]["actor"], "legacy-token")

    def test_target_change_without_state_file_warns(self):
        self.start(auth_token=self.LEGACY_TOKEN, persistent=False)
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        status, _, raw = self.request("POST", "/api/auth/targets", headers=bearer, body={"targets": ["swack-tools"]})
        self.assertEqual(status, 200)
        self.assertIn("in memory only", json.loads(raw)["warning"])
        self.assertFalse(json.loads(raw)["persistent"])

    # -- OAuth target changes need a fresh, bound GitHub verification --

    def test_session_cannot_loosen_targets_without_fresh_verification(self):
        store = self.start()
        self.fake.set_repo("tok-admin", "swack-tools/oxidex")
        store.set_controlled_targets(["swack-tools/oxidex"])
        pair = self.login_cookie()
        status, msg, raw = self.request("POST", "/api/auth/targets", headers={"Cookie": pair}, body={"targets": []})
        body = json.loads(raw)
        self.assertEqual((status, body["status"], body["flow"]), (202, "reauth_required", "web"))
        self.assertEqual(body["verify_targets"], ["swack-tools/oxidex"])
        self.assertIn("code_challenge=", body["authorize_url"])
        self.assertIsNotNone(self.cookie_pair(msg, OAUTH_STATE_COOKIE_NAME)[0])
        self.assertEqual(store.get_controlled_targets(), ["swack-tools/oxidex"])  # not committed
        # The non-admin second allowlisted user is still locked out.
        self.fake.add_user("tok-x", "xbmc4lyfe", 2)
        status, _, _ = self.device_login("tok-x")
        self.assertEqual(status, 403)

    def test_web_reauth_commits_change_and_reissues_session(self):
        store = self.start()
        self.fake.set_repo("tok-admin", "swack-tools/oxidex")
        self.fake.set_org("tok-admin", "swack-tools")
        store.set_controlled_targets(["swack-tools/oxidex"])
        pair = self.login_cookie()
        status, msg, raw = self.request(
            "POST", "/api/auth/targets", headers={"Cookie": pair, "Host": "spot.example"},
            body={"targets": ["swack-tools"], "next": "/?tab=auth"},
        )
        self.assertEqual(status, 202, raw)
        authorize_url = json.loads(raw)["authorize_url"]
        state_pair, _ = self.cookie_pair(msg, OAUTH_STATE_COOKIE_NAME)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)["state"][0]
        self.fake.issue_code("chg1", "tok-admin", authorize_url)
        api_calls = len(self.fake.api_paths())
        with self.assertLogs("spot_runner_autoscaler.dashboard_auth", "INFO") as logs:
            status, msg, _ = self.request(
                "GET", "/auth/github/callback?" + urllib.parse.urlencode({"code": "chg1", "state": state}),
                headers={"Host": "spot.example", "Cookie": state_pair},
            )
        self.assertEqual(status, 302)
        self.assertEqual(msg["Location"], "/?tab=auth")
        # Fresh (uncached) verification of both the removed and the added target.
        self.assertEqual(sorted(self.fake.api_paths()[api_calls:]),
                         ["/repos/swack-tools/oxidex", "/user", "/user/memberships/orgs/swack-tools"])
        self.assertEqual(store.get_controlled_targets(), ["swack-tools"])
        entry = store.get_controlled_targets_history()[-1]
        self.assertEqual((entry["actor"], entry["actor_uid"], entry["method"]), ("swackhamer", ADMIN_UID, "github_oauth"))
        self.assertEqual((entry["previous"], entry["targets"]), (["swack-tools/oxidex"], ["swack-tools"]))
        self.assertTrue(any("swackhamer" in line for line in logs.output))
        new_pair, _ = self.cookie_pair(msg, SESSION_COOKIE_NAME)
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": new_pair})
        self.assertEqual(status, 200)

    def test_lockout_is_impossible(self):
        store = self.start(client_secret="")
        pair = self.login_cookie()
        status, _, raw = self.request("POST", "/api/auth/targets", headers={"Cookie": pair}, body={"targets": ["github"]})
        body = json.loads(raw)
        self.assertEqual((status, body["flow"]), (202, "device"))
        self.assertNotIn("device_code", json.dumps(body))
        status, msg, raw = self.complete_device(body["device"]["device_handle"], "tok-admin")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["missing_targets"], ["github"])
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])
        self.assertEqual(store.get_controlled_targets(), [])  # nothing committed
        self.assertEqual(store.get_controlled_targets_history(), [])
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": pair})
        self.assertEqual(status, 200)  # the existing session still works

    def test_device_reauth_commits_change(self):
        store = self.start(client_secret="")
        self.fake.set_org("tok-admin", "swack-tools")
        pair = self.login_cookie()
        _, _, raw = self.request("POST", "/api/auth/targets", headers={"Cookie": pair}, body={"targets": ["swack-tools"]})
        handle = json.loads(raw)["device"]["device_handle"]
        status, msg, raw = self.complete_device(handle, "tok-admin")
        body = json.loads(raw)
        self.assertEqual((status, body["status"], body["target_change"]["committed"]), (200, "authorized", True))
        self.assertEqual(store.get_controlled_targets(), ["swack-tools"])
        new_pair, _ = self.cookie_pair(msg, SESSION_COOKIE_NAME)
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": new_pair})
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": pair})
        self.assertEqual(status, 403)  # the old session was never verified for the new target

    def test_removing_target_requires_current_admin(self):
        store = self.start(client_secret="")
        self.fake.set_repo("tok-admin", "swack-tools/oxidex")
        store.set_controlled_targets(["swack-tools/oxidex"])
        pair = self.login_cookie()
        self.fake.set_repo("tok-admin", "swack-tools/oxidex", admin=False)  # revoked on GitHub after sign-in
        _, _, raw = self.request("POST", "/api/auth/targets", headers={"Cookie": pair}, body={"targets": []})
        status, _, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], "tok-admin")
        self.assertEqual(status, 403)  # cached positive result is not reused
        self.assertEqual(store.get_controlled_targets(), ["swack-tools/oxidex"])

    def test_change_completed_by_other_account_rejected(self):
        store = self.start(client_secret="")
        self.fake.add_user("tok-x", "xbmc4lyfe", 2)
        pair = self.login_cookie()
        _, _, raw = self.request("POST", "/api/auth/targets", headers={"Cookie": pair}, body={"targets": ["swack-tools"]})
        self.fake.set_org("tok-x", "swack-tools")
        status, _, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], "tok-x")
        self.assertEqual(status, 403)
        self.assertIn("not the account that requested", json.loads(raw)["error"])
        self.assertEqual(store.get_controlled_targets(), [])

    def test_stale_change_conflicts(self):
        store = self.start(client_secret="", auth_token=self.LEGACY_TOKEN)
        self.fake.set_org("tok-admin", "swack-tools")
        pair = self.login_cookie()
        _, _, raw = self.request("POST", "/api/auth/targets", headers={"Cookie": pair}, body={"targets": ["swack-tools"]})
        store.set_controlled_targets(["o/r"], actor="legacy-token", method="token")
        status, _, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], "tok-admin")
        self.assertEqual((status, json.loads(raw)["status"]), (409, "conflict"))
        self.assertEqual(store.get_controlled_targets(), ["o/r"])

    def test_change_racing_concurrent_update_conflicts(self):
        store = self.start(client_secret="")
        self.fake.set_org("tok-admin", "swack-tools")
        pair = self.login_cookie()
        _, _, raw = self.request("POST", "/api/auth/targets", headers={"Cookie": pair}, body={"targets": ["swack-tools"]})

        def concurrent_operator_change(path):
            if path.startswith("/user/memberships/") and store.get_controlled_targets() == []:
                store.set_controlled_targets(["o/r"], actor="legacy-token", method="token")

        self.fake.on_api_call = concurrent_operator_change  # list changes while GitHub verifies
        status, msg, raw = self.complete_device(json.loads(raw)["device"]["device_handle"], "tok-admin")
        self.assertEqual((status, json.loads(raw)["status"]), (409, "conflict"))
        self.assertIsNone(self.cookie_pair(msg, SESSION_COOKIE_NAME)[0])
        self.assertEqual(store.get_controlled_targets(), ["o/r"])

    def test_oversized_target_change_rejected_before_commit(self):
        store = self.start(auth_token=self.LEGACY_TOKEN, client_secret="")
        big = _long_repo_targets(32)
        for headers in ({"Authorization": f"Bearer {self.LEGACY_TOKEN}"}, {"Cookie": self.login_cookie()}):
            status, _, raw = self.request("POST", "/api/auth/targets", headers=headers, body={"targets": big})
            self.assertEqual(status, 400, raw)
            self.assertIn("too long to fit in a session cookie", json.loads(raw)["error"])
            self.assertEqual(store.get_controlled_targets(), [])

    def test_oversized_stored_policy_never_issues_broken_cookie(self):
        store = self.start(client_secret="")
        big = _long_repo_targets(32)
        for name in big:
            self.fake.set_repo("tok-admin", name)
        store.set_controlled_targets(big)  # e.g. a policy written before the size check existed
        status, msg, raw = self.device_login("tok-admin")
        self.assertEqual(status, 400, raw)
        pair, _ = self.cookie_pair(msg, SESSION_COOKIE_NAME)
        self.assertIsNone(pair)

    def test_oversized_static_targets_refused_at_startup(self):
        mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=self.fake, clock=self.clock)
        with self.assertRaisesRegex(ValueError, "too long to fit in a session cookie"):
            create_dashboard_server(host="127.0.0.1", port=0, oauth_manager=mgr,
                                    controlled_targets=_long_repo_targets(32))

    def test_stale_stored_bearer_token_does_not_mask_session(self):
        from lib.dashboard_ui_assets import render_dashboard_html
        page = render_dashboard_html()
        self.assertIn('storeDashboardAuthToken("");', page)
        self.assertIn('res.status === 401 && token && !new Headers(init.headers || {}).has("Authorization")', page)

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the login gate script")
    def test_login_gate_tries_existing_token_before_discarding(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        _, _, raw = self.request("GET", "/")
        html = raw.decode()
        script = html[html.index("<script"):]
        script = script[script.index(">") + 1:script.index("</script>")]
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

    def _gate_script(self):
        _, _, raw = self.request("GET", "/")
        html = raw.decode()
        script = html[html.index("<script"):]
        return script[script.index(">") + 1:script.index("</script>")]

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the login gate script")
    def test_device_login_recovers_from_network_failures(self):
        self.start(client_secret="")
        script = self._gate_script()
        failed = _run_node(_DEVICE_HARNESS, script, {"code_fails": True, "poll_failures": 0})
        self.assertIn("Network error", failed["msg"])
        self.assertFalse(failed["disabled"])  # the user can retry without reloading
        recovered = _run_node(_DEVICE_HARNESS, script, {"code_fails": False, "poll_failures": 2})
        self.assertEqual(recovered["calls"].count("/auth/device/token"), 3)
        self.assertEqual(recovered["assigned"], "/")

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the dashboard fetch wrapper")
    def test_expired_oauth_session_returns_to_login_gate(self):
        from lib.dashboard_ui_assets import render_dashboard_html
        page = render_dashboard_html()
        start = page.index("window.fetch = async function dashboardAuthorizedFetch")
        source = page[start:page.index("\n    };", start) + len("\n    };")]
        oauth = _run_node(_FETCH_WRAPPER_HARNESS, source, {"oauth": True})
        self.assertEqual((oauth["assigned"], oauth["prompted"], oauth["resolved"]), ("/", False, None))
        legacy = _run_node(_FETCH_WRAPPER_HARNESS, source, {"oauth": False})
        self.assertEqual((legacy["assigned"], legacy["prompted"], legacy["resolved"]), (None, True, 401))

    def test_unchanged_targets_need_no_reauth(self):
        self.start()
        pair = self.login_cookie()
        status, _, raw = self.request("POST", "/api/auth/targets", headers={"Cookie": pair}, body={"targets": []})
        self.assertEqual((status, json.loads(raw)["status"]), (200, "unchanged"))

    # -- fail closed on unreadable state --

    def test_corrupt_state_fails_closed(self):
        path = Path(self.tmp.name) / "state.json"
        path.write_text('{"controlled_targets": ["swack-tools/oxidex"', encoding="utf-8")
        with self.assertLogs("spot_runner_autoscaler.dashboard_auth", "ERROR"):
            self.start(auth_token=self.LEGACY_TOKEN)
        session = self.mgr.create_session_cookie_value("swackhamer", 600, user_id=ADMIN_UID)
        cookie = {"Cookie": f"{SESSION_COOKIE_NAME}={session}"}
        status, _, raw = self.request("GET", "/api/overview", headers=cookie)
        self.assertEqual((status, json.loads(raw)["status"]), (503, "unavailable"))
        status, _, _ = self.request("GET", "/", headers=cookie)
        self.assertEqual(status, 503)
        calls = len(self.fake.calls)
        status, _, _ = self.request("POST", "/auth/device/code", body={})
        self.assertEqual(status, 503)
        status, _, _ = self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})
        self.assertEqual(status, 503)
        self.assertEqual(len(self.fake.calls), calls)
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        status, _, _ = self.request("GET", "/api/overview", headers=bearer)
        self.assertEqual(status, 200)  # break-glass operator access is unaffected
        status, _, _ = self.request("POST", "/api/auth/targets", headers=bearer, body={"targets": []})
        self.assertEqual(status, 503)
        status, _, raw = self.request("GET", "/api/auth/targets", headers=bearer)
        self.assertIn("could not be loaded", json.loads(raw)["error"])
        self.assertEqual(path.read_text(encoding="utf-8"), '{"controlled_targets": ["swack-tools/oxidex"')

    # -- unauthenticated amplification --

    def test_rate_limit_returns_429(self):
        self.start()
        self.mgr.rate_limiters["start"] = RateLimiter(2, 100, 60, clock=self.clock)
        for _ in range(2):
            self.assertEqual(self.request("POST", "/auth/device/code", body={})[0], 200)
        status, msg, raw = self.request("POST", "/auth/device/code", body={})
        self.assertEqual(status, 429)
        self.assertEqual(msg["Retry-After"], "60")
        self.assertEqual(json.loads(raw)["retry_after"], 60)
        status, msg, _ = self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})
        self.assertEqual(status, 429)
        self.assertEqual(sum(c["url"] == GITHUB_DEVICE_CODE_URL for c in self.fake.calls), 2)
        self.clock.now += 61
        self.assertEqual(self.request("POST", "/auth/device/code", body={})[0], 200)

    def test_default_rate_limit_caps_device_code_amplification(self):
        self.start()
        statuses = [self.request("POST", "/auth/device/code", body={})[0] for _ in range(20)]
        self.assertEqual(statuses.count(200), 10)
        self.assertEqual(statuses.count(429), 10)
        self.assertEqual(sum(c["url"] == GITHUB_DEVICE_CODE_URL for c in self.fake.calls), 10)

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

    def test_device_token_rejects_forged_handle(self):
        self.start()
        status, _, raw = self.request("POST", "/auth/device/token", body={"device_handle": "abc.def"})
        self.assertEqual((status, json.loads(raw)["status"]), (400, "invalid"))
        self.assertFalse(any(c["url"] == GITHUB_ACCESS_TOKEN_URL for c in self.fake.calls))

    # -- request hygiene --

    def test_head_and_options_return_no_data(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        for method in ("HEAD", "OPTIONS"):
            for path in ("/", "/api/overview", "/api/auth/targets", "/auth/status", "/auth/device/code"):
                status, msg, raw = self.request(method, path, headers={**bearer, "Origin": "https://evil.example"})
                self.assertGreaterEqual(status, 400, (method, path))
                self.assertIsNone(msg.get("Access-Control-Allow-Origin"))
                self.assertIsNone(msg.get("Set-Cookie"))
                for marker in (b"profiles", b"controlled_targets", b"device_handle", b"swack"):
                    self.assertNotIn(marker, raw, (method, path))

    def test_auth_posts_reject_cross_origin(self):
        self.start()
        hostile = (
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        )
        for path in ("/auth/device/code", "/auth/device/token", "/auth/logout", "/api/auth/targets"):
            for headers in hostile:
                status, msg, _ = self.request("POST", path, headers=headers, body={"device_handle": "x"})
                self.assertEqual(status, 403, (path, headers))
                self.assertIsNone(msg.get("Set-Cookie"))
            status, _, _ = self.request("POST", path, headers={"Content-Type": "text/plain"}, body={})
            self.assertEqual(status, 415, path)
        self.assertEqual(self.fake.calls, [])

    def test_non_loopback_bind_requires_some_auth(self):
        with self.assertRaisesRegex(ValueError, "without authentication"):
            create_dashboard_server(host="0.0.0.0", port=0, state_path=Path(self.tmp.name) / "s.json")
        mgr = GitHubOAuthManager(CLIENT_ID, "", None, SECRET, http_requester=self.fake)
        with self.assertRaisesRegex(ValueError, "without TLS"):
            create_dashboard_server(host="0.0.0.0", port=0, state_path=Path(self.tmp.name) / "s.json", oauth_manager=mgr)


if __name__ == "__main__":
    unittest.main()
