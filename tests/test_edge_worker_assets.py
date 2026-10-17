"""Static checks for the Cloudflare Worker, its wrangler config, the dashboard image, and its workflow.

When ``node`` is on PATH, also runs the Worker's own ``node:test`` suite (no network; fake KV/fetch).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from lib import dashboard_auth, dashboard_server, edge_state, patch_console

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare" / "worker.js"
WORKER_TEST = ROOT / "cloudflare" / "worker.test.mjs"
WRANGLER = ROOT / "cloudflare" / "wrangler.toml"
DOCKERFILE = ROOT / "dashboard" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
WORKFLOW = ROOT / ".github" / "workflows" / "dashboard-image.yml"
VECTOR = ROOT / "tests" / "fixtures" / "session_vector.json"


def _js_int_constant(source: str, name: str) -> int:
    """Read a simple `export const NAME = 262_144;` integer constant from worker.js."""
    match = re.search(rf"export const {name} = ([0-9_]+);\n", source)
    if not match:
        raise AssertionError(f"{name} not found in worker.js")
    return int(match.group(1).replace("_", ""))


def _js_string_constant(source: str, name: str) -> str:
    """Evaluate a simple `export const NAME = "a" + "b";` string constant from worker.js."""
    match = re.search(rf"export const {name} =(.*?);\n", source, re.S)
    if not match:
        raise AssertionError(f"{name} not found in worker.js")
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))
    return "".join(json.loads(f'"{p}"') for p in parts)


class WorkerSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WORKER.read_text(encoding="utf-8")

    def test_wait_contract_strings_match_edge_state(self) -> None:
        self.assertEqual(_js_string_constant(self.source, "UNAVAILABLE_ERROR"), edge_state.UNAVAILABLE_ERROR)
        self.assertEqual(_js_string_constant(self.source, "UNAVAILABLE_MESSAGE"), edge_state.UNAVAILABLE_MESSAGE)
        self.assertIn(f'"{edge_state.UNAVAILABLE_ERROR}"', self.source)
        self.assertRegex(self.source, rf"RETRY_AFTER_SECONDS = {edge_state.DEFAULT_RETRY_AFTER_SECONDS};")
        self.assertIn('"Retry-After"', self.source)
        self.assertIn('"Cache-Control": "no-store"', self.source)

    def test_session_and_kv_constants(self) -> None:
        vector = json.loads(VECTOR.read_text(encoding="utf-8"))
        self.assertEqual(_js_string_constant(self.source, "SESSION_PURPOSE_LABEL"), vector["purpose_label"])
        self.assertEqual(_js_string_constant(self.source, "SESSION_PURPOSE_LABEL"), "spot-dashboard/session/v1")
        self.assertEqual(_js_string_constant(self.source, "SESSION_COOKIE_NAME"), vector["cookie_name"])
        self.assertEqual(_js_string_constant(self.source, "SESSION_COOKIE_NAME"), dashboard_auth.SESSION_COOKIE_NAME)
        self.assertEqual(_js_string_constant(self.source, "CLIENT_IP_HEADER"), dashboard_server.CLIENT_IP_HEADER)
        self.assertEqual(_js_string_constant(self.source, "ORIGIN_MARKER_HEADER"), dashboard_server.ORIGIN_MARKER_HEADER)
        self.assertEqual(_js_string_constant(self.source, "ORIGIN_MARKER_VALUE"), dashboard_server.ORIGIN_MARKER_VALUE)
        self.assertEqual(_js_string_constant(self.source, "KV_ORIGIN_KEY"), edge_state.KV_ORIGIN_KEY)
        self.assertEqual(_js_string_constant(self.source, "DEFAULT_ORIGIN_HOST_SUFFIX"),
                         "." + edge_state.DEFAULT_ORIGIN_DOMAIN)

    def test_request_body_limits_match_the_backend(self) -> None:
        """Codex round 17 P2: the Worker's body caps track the backend's largest legitimate request bodies."""
        dashboard = _js_int_constant(self.source, "MAX_DASHBOARD_BODY_BYTES")
        auth = _js_int_constant(self.source, "MAX_AUTH_BODY_BYTES")
        self.assertEqual(dashboard, dashboard_server.MAX_JSON_BODY_BYTES)
        self.assertGreaterEqual(dashboard, patch_console.MAX_MANIFEST_BYTES)  # a patch save's manifest
        self.assertGreaterEqual(auth, dashboard_server.MAX_LOGIN_BODY_BYTES)
        self.assertLess(auth, dashboard)
        # Every body the backend reads through the Worker is bounded by one of these; /api/control (the larger
        # MAX_CONTROL_BODY_BYTES) is blocked at the edge.
        server = Path(dashboard_server.__file__).read_text(encoding="utf-8")
        limits = set(re.findall(r"_read_json\(max_bytes=([A-Z_]+)\)", server))
        self.assertEqual(limits, {"MAX_JSON_BODY_BYTES", "MAX_LOGIN_BODY_BYTES", "MAX_CONTROL_BODY_BYTES"})
        self.assertRegex(server, r"def _read_json\(self, \*, max_bytes: int = MAX_JSON_BODY_BYTES\)")
        self.assertIn("body = await readBoundedBody(request, limit)", self.source)
        self.assertNotIn("request.arrayBuffer()", self.source)

    def test_control_paths_blocked(self) -> None:
        self.assertIn(r"/^\/api\/control(?![a-z0-9_-])/", self.source)
        self.assertRegex(self.source, r'route === "control"\) return jsonResponse\(404')

    def test_strips_client_access_headers_and_adds_worker_ones(self) -> None:
        self.assertIn('lower.startsWith("cf-access-")', self.source)
        self.assertIn('lower.startsWith("x-spot-")', self.source)
        self.assertIn('headers.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID)', self.source)
        self.assertIn('headers.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET)', self.source)

    def test_no_dependencies_or_remote_scripts(self) -> None:
        self.assertNotRegex(self.source, r"^\s*import\s", "worker.js must not import modules")
        self.assertNotRegex(self.source, r"\brequire\(")
        self.assertNotRegex(self.source, r"<script[^>]+src=")
        self.assertIn("/api/health", self.source)
        self.assertIn("5000", self.source)

    def test_worker_test_uses_only_node_builtins(self) -> None:
        test_source = WORKER_TEST.read_text(encoding="utf-8")
        for spec in re.findall(r'^import .*? from "([^"]+)";', test_source, re.M | re.S):
            self.assertTrue(spec.startswith("node:") or spec == "./worker.js", spec)


class WranglerConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WRANGLER.read_text(encoding="utf-8")

    def test_no_account_ids_or_secrets(self) -> None:
        self.assertIsNone(re.search(r"\b[0-9a-f]{32}\b", self.text), "wrangler.toml must not contain 32-hex ids")
        self.assertNotRegex(self.text, r"(?m)^\s*account_id\s*=")
        for secret in ("SESSION_SECRET", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"):
            self.assertNotRegex(self.text, rf"(?m)^\s*{secret}\s*=", f"{secret} must be a wrangler secret")
            self.assertIn(f"wrangler secret put {secret}", self.text)

    def test_route_kv_and_vars(self) -> None:
        self.assertRegex(self.text, r'(?m)^name = "spot-dashboard-edge"$')
        self.assertRegex(self.text, r'(?m)^main = "worker\.js"$')
        self.assertIn('{ pattern = "spot.swacktech.com", custom_domain = true }', self.text)
        self.assertIn('binding = "SPOT_KV", id = "REPLACE_WITH_SPOT_KV_NAMESPACE_ID"', self.text)
        # The Worker only authenticates; the control VM decides which accounts may sign in (owner plus the
        # administrators added on the Admins page), so no identity is pinned here.
        self.assertNotRegex(self.text, r'(?m)^ALLOWED_ADMIN_(LOGIN|ID)\s*=')
        # Optional bulk sign-out cut-off: empty (none) or Unix seconds, read by worker.js sessionNotBefore().
        self.assertRegex(self.text, r'(?m)^SESSION_NOT_BEFORE = "([0-9]{1,15})?"$')
        date = re.search(r'(?m)^compatibility_date = "(\d{4}-\d{2}-\d{2})"$', self.text)
        self.assertIsNotNone(date)
        self.assertLessEqual(date.group(1), "2026-08-01")


class DockerfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = DOCKERFILE.read_text(encoding="utf-8")

    def test_pinned_base_and_non_root(self) -> None:
        self.assertRegex(self.text, r"(?m)^FROM python:3\.13\.\d+-slim-bookworm(@sha256:[0-9a-f]{64})?$")
        self.assertRegex(self.text, r"(?m)^USER 10001:10001$")
        self.assertIn("--uid 10001", self.text)
        self.assertIn("--gid 10001", self.text)
        self.assertNotRegex(self.text, r"(?m)^USER (root|0)\b")

    def test_copied_files_are_normalized_for_the_runtime_user(self) -> None:
        # A checkout under umask 077 must still produce readable, root-owned, non-writable app files.
        normalize = "RUN chown -R root:root /app \\\n    && chmod -R u=rwX,go=rX /app"
        self.assertIn(normalize, self.text)
        self.assertLess(self.text.rindex("COPY --chown=root:root"), self.text.index(normalize))
        self.assertLess(self.text.index(normalize), self.text.index("USER 10001:10001"))

    def test_runtime_settings(self) -> None:
        self.assertRegex(self.text, r"(?m)^WORKDIR /app$")
        self.assertIn("PYTHONPATH=/app/src", self.text)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", self.text)
        self.assertIn('ENTRYPOINT ["python3", "-B", "/app/src/web_dashboard.py"]', self.text)
        self.assertNotRegex(self.text, r"(?m)^CMD\b")
        self.assertIn("http://127.0.0.1:8787/api/health", self.text)
        self.assertRegex(self.text, r"(?m)^HEALTHCHECK ")

    def test_binds_nothing_publicly_and_carries_no_secrets(self) -> None:
        self.assertNotIn("0.0.0.0", self.text)
        self.assertNotRegex(self.text, r"(?m)^EXPOSE\b")
        self.assertNotRegex(self.text, r"(?im)^(ENV|ARG)\b.*(SECRET|TOKEN|PASSWORD)")

    def test_dockerignore_excludes_sensitive_paths(self) -> None:
        lines = set(DOCKERIGNORE.read_text(encoding="utf-8").split())
        self.assertIn("*", lines)
        for pattern in ("**/.git", "**/.env", "**/*.pem", "**/.wrangler", "**/__pycache__", "**/.pytest_cache"):
            self.assertIn(pattern, lines)
        for allowed in ("!src/", "!cloud-init/", "!runner/", "!builder/"):
            self.assertIn(allowed, lines)


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_least_privilege_permissions(self) -> None:
        block = re.search(r"(?m)^permissions:\n((?:  .+\n)+)", self.text)
        self.assertIsNotNone(block)
        perms = dict(line.strip().split(": ", 1) for line in block.group(1).splitlines())
        self.assertEqual(perms, {"contents": "read", "packages": "write"})
        self.assertNotIn("write-all", self.text)
        self.assertNotIn("pull_request_target", self.text)
        self.assertIn("persist-credentials: false", self.text)

    def test_triggers_and_tags(self) -> None:
        self.assertIn("branches: [main]", self.text)
        self.assertIn("workflow_dispatch:", self.text)
        for path in ("src/**", "dashboard/**", "cloud-init/**", "runner/**", "builder/**"):
            self.assertIn(f'"{path}"', self.text)
        self.assertIn("IMAGE: ghcr.io/swack-tools/spot-dashboard", self.text)
        self.assertIn('"${IMAGE}:${GITHUB_SHA}"', self.text)
        self.assertIn('"${IMAGE}:main"', self.text)
        self.assertIn("GITHUB_STEP_SUMMARY", self.text)
        self.assertIn("--password-stdin", self.text)

    def test_third_party_actions_are_pinned(self) -> None:
        for ref in re.findall(r"uses:\s*(\S+)", self.text):
            owner = ref.split("/", 1)[0]
            if owner == "actions":
                continue  # First-party GitHub actions; tag pin flagged for review in the workflow.
            self.assertRegex(ref, r"@[0-9a-f]{40}$", f"third-party action {ref} must be pinned by SHA")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class WorkerNodeSuiteTests(unittest.TestCase):
    def test_node_suite_passes(self) -> None:
        proc = subprocess.run(
            [shutil.which("node"), "--test", str(WORKER_TEST)],
            cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:] + proc.stderr[-4000:])
        self.assertRegex(proc.stdout, r"(?m)^(ℹ|#) fail 0$")


if __name__ == "__main__":
    unittest.main()
