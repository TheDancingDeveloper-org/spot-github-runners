"""Unit and HTTP integration tests for GitHub OAuth login and admin verification (dashboard_auth)."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.parse

from lib.dashboard_auth import (
    ALLOWED_ADMIN_ID,
    ALLOWED_ADMIN_LOGIN,
    DEFAULT_ALLOWED_ADMINS,
    GITHUB_ACCESS_TOKEN_URL,
    GITHUB_AUTHORIZE_URL,
    GITHUB_DEVICE_CODE_URL,
    MAX_GITHUB_RESPONSE_BYTES,
    ConfigRepoTargets,
    ConfigTargetsError,
    GitHubApiError,
    GitHubOAuthManager,
    default_http_request,
    format_session_cookie,
    normalize_admin_logins,
    parse_controlled_target,
    parse_controlled_targets,
    parse_cookie_header,
    safe_return_path,
)
from lib.dashboard_server import build_oauth_config, create_dashboard_server

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
        self.calls: list[dict] = []
        self.raise_on_api = False

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
                resp = self.device_polls.pop(0) if self.device_polls else {"error": "authorization_pending"}
                return 200, json.dumps(resp).encode()
            code = form.get("code", [""])[0]
            verifier = form.get("code_verifier", [""])[0]
            self.code_verifiers.append(verifier)
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
        token = headers.get("Authorization", "")[len("Bearer "):]
        if parsed.path == "/user":
            if token not in self.users:
                return 401, b'{"message": "Bad credentials"}'
            login, uid = self.users[token]
            return 200, json.dumps({"login": login, "id": uid}).encode()
        status, payload = self.resources.get((token, parsed.path), (404, {"message": "Not Found"}))
        return status, json.dumps(payload).encode()


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

    def test_cookie_attributes(self):
        header = format_session_cookie("abc.def", 600)
        for attr in ("spot_session=abc.def", "HttpOnly", "Secure", "SameSite=Lax", "Max-Age=600", "Path=/"):
            self.assertIn(attr, header)
        self.assertEqual(parse_cookie_header('a=1; spot_session="x.y"; a=2'), {"a": "1", "spot_session": "x.y"})


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

    def test_used_nonce_map_is_bounded(self):
        self.mgr.max_used_state_nonces = 5
        states = [self.mgr.create_state("/") for _ in range(12)]
        for state in states:
            self.assertTrue(self.mgr.validate_state(state, state)[0])
        self.assertEqual(len(self.mgr._used_state_nonces), 5)
        # The most recent nonces are still remembered, so their replay is refused.
        self.assertEqual(self.mgr.validate_state(states[-1], states[-1]), (False, "/"))

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

    def test_device_handle_seal_tamper_expiry(self):
        handle = self.mgr.seal_device_code("abc123", 100)
        self.assertEqual(self.mgr.open_device_handle(handle), "abc123")
        self.assertIsNone(self.mgr.open_device_handle(handle[:-1] + ("0" if handle[-1] != "0" else "1")))
        self.assertIsNone(self.mgr.open_device_handle(self.mgr.create_state("/")))
        self.clock.now += 101
        self.assertIsNone(self.mgr.open_device_handle(handle))


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

    def config_lister(self):
        self.config_calls += 1
        if self.config_error is not None:
            raise self.config_error
        return list(self.config_targets)

    def start(self, *, client_secret="web-secret", auth_token=None, controlled_targets=None, device_flow=True):
        mgr = GitHubOAuthManager(CLIENT_ID, client_secret, None, SECRET, http_requester=self.fake,
                                 device_flow=device_flow)
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

    def device_login(self, access_token):
        status, _, raw = self.request("POST", "/auth/device/code", body={})
        self.assertEqual(status, 200)
        started = json.loads(raw)
        self.assertNotIn("device_code", started)
        self.fake.device_polls = [{"error": "authorization_pending"}, {"access_token": access_token}]
        status, _, raw = self.request("POST", "/auth/device/token", body={"device_handle": started["device_handle"]})
        self.assertEqual((status, json.loads(raw)["status"]), (200, "pending"))
        return self.request("POST", "/auth/device/token", body={"device_handle": started["device_handle"]})

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
        pair, header = self.cookie_pair(msg, "spot_session")
        self.assertIsNotNone(pair)
        for attr in ("HttpOnly", "Secure", "SameSite=Lax"):
            self.assertIn(attr, header)
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
        self.assertTrue(any(h.startswith("spot_session=;") and "Max-Age=0" in h for h in msg.get_all("Set-Cookie")))

    def test_non_admin_login_is_forbidden(self):
        self.start()
        status, msg, raw = self.device_login("tok-eve")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["status"], "forbidden")
        self.assertIsNone(self.cookie_pair(msg, "spot_session")[0])

    def test_missing_target_admin_is_forbidden(self):
        self.fake.set_org("tok-admin", "swack-tools", role="member")
        self.start(controlled_targets=["swack-tools"])
        status, _, raw = self.device_login("tok-admin")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["missing_targets"], ["swack-tools"])

    def test_new_target_forbids_existing_session(self):
        self.start(auth_token=self.LEGACY_TOKEN)
        _, msg, _ = self.device_login("tok-admin")
        pair, _ = self.cookie_pair(msg, "spot_session")
        bearer = {"Authorization": f"Bearer {self.LEGACY_TOKEN}"}
        # The legacy token can no longer change controlled targets; only an OAuth session can.
        status, _, _ = self.request("POST", "/api/auth/targets", headers=bearer, body={"targets": ["swack-tools"]})
        self.assertEqual(status, 403)
        cookie = {"Cookie": pair}
        status, _, raw = self.request("POST", "/api/auth/targets", headers=cookie, body={"targets": ["swack-tools"]})
        self.assertEqual((status, json.loads(raw)["controlled_targets"]), (200, ["swack-tools"]))
        status, _, raw = self.request("GET", "/api/overview", headers=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["missing_targets"], ["swack-tools"])
        status, _, raw = self.request("GET", "/", headers=cookie)
        self.assertEqual(status, 403)
        self.assertIn("Sign out", raw.decode())
        self.fake.set_org("tok-admin", "swack-tools")
        _, msg, _ = self.device_login("tok-admin")
        fresh = {"Cookie": self.cookie_pair(msg, "spot_session")[0]}
        status, _, _ = self.request("POST", "/api/auth/targets", headers=fresh, body={"targets": ["bad/t/x"]})
        self.assertEqual(status, 400)

    def test_config_repo_targets_are_required_and_locked(self):
        self.config_targets = ["swack-tools/oxidex"]
        self.fake.set_repo("tok-admin", "swack-tools/oxidex", admin=True)
        self.start()
        status, msg, _ = self.device_login("tok-admin")
        self.assertEqual(status, 200)
        cookie = {"Cookie": self.cookie_pair(msg, "spot_session")[0]}
        status, _, raw = self.request("GET", "/api/auth/targets", headers=cookie)
        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(body["config_targets"], ["swack-tools/oxidex"])
        self.assertIn("swack-tools/oxidex", body["locked_targets"])
        # Saving an empty dynamic list cannot drop a config-repo target.
        status, _, raw = self.request("POST", "/api/auth/targets", headers=cookie, body={"targets": []})
        self.assertEqual((status, json.loads(raw)["controlled_targets"]), (200, ["swack-tools/oxidex"]))
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
        cookie = {"Cookie": self.cookie_pair(msg, "spot_session")[0]}
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
        forged = other.create_session_cookie_value("swackhamer", 3600)
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": f"spot_session={forged}"})
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
        state_pair, state_header = self.cookie_pair(msg, "spot_oauth_state")
        self.assertEqual(state_pair, f"spot_oauth_state={state}")
        self.assertIn("Path=/auth/github", state_header)

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
            headers={"Host": "spot.example", "Cookie": f"spot_oauth_state={state2}"},
        )
        self.assertEqual(status, 302)
        self.assertEqual(msg["Location"], "/")
        self.assertTrue(self.fake.code_verifiers[-1])
        pair, _ = self.cookie_pair(msg, "spot_session")
        payload = decode_payload(pair.split("=", 1)[1])
        self.assertEqual((payload["sub"], payload["uid"]), ("swackhamer", ADMIN_ID))
        status, _, _ = self.request("GET", "/api/overview", headers={"Cookie": pair})
        self.assertEqual(status, 200)

        status, _, _ = self.request(
            "GET", f"/auth/github/callback?{qs}",
            headers={"Host": "spot.example", "Cookie": f"spot_oauth_state={state2}"},
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
            headers={"Host": "spot.example", "Cookie": f"spot_oauth_state={state}"},
        )
        self.assertEqual(status, 403)
        self.assertIn("not an authorized administrator", raw.decode())
        self.assertIsNone(self.cookie_pair(msg, "spot_session")[0])

    def test_web_code_flow_rejects_wrong_pkce_verifier(self):
        self.start(device_flow=False)
        self.fake.code_tokens["goodcode"] = "tok-admin"
        self.fake.expected_challenges = {"not-the-real-challenge"}
        _, msg, _ = self.request("GET", "/auth/github/login", headers={"Host": "spot.example"})
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(msg["Location"]).query)["state"][0]
        qs = urllib.parse.urlencode({"code": "goodcode", "state": state})
        status, msg, _ = self.request("GET", f"/auth/github/callback?{qs}",
                                      headers={"Host": "spot.example", "Cookie": f"spot_oauth_state={state}"})
        self.assertEqual(status, 403)
        self.assertIsNone(self.cookie_pair(msg, "spot_session")[0])

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


if __name__ == "__main__":
    unittest.main()
