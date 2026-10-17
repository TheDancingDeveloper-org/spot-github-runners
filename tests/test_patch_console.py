"""Runner patch console (R2): config-repo manifests, validation, PR saves, rollout preview, hot-patch gating.

A fake GitHub REST transport and a fake hot-patch executor are injected; nothing reaches GitHub or a VM.
"""

from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse

from lib import patch_console as pc
from lib.dashboard_server import create_dashboard_server
from lib.runner_patch import merge_patch_dicts, validate_patch_dict

REPO = "swack-tools/spot-runners-config"
HEAD = "a" * 40
NEXT = "b" * 40
DEFAULTS = json.dumps({"schema": "spot-runner-patch-v1", "host": {"apt_packages": ["jq"]}})
OXIDEX_PATH = "repos/swack-tools/oxidex.yaml"
OXIDEX = json.dumps({"schema": "spot-runner-patch-v1", "repository": "swack-tools/oxidex",
                     "container": {"apt_packages": ["ripgrep"]}})


def blob(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode()).hexdigest()


class FakeGitHubRepo:
    """In-memory stand-in for the GitHub REST endpoints the console uses."""

    def __init__(self) -> None:
        self.default_branch = "main"
        self.branches = {"main": HEAD}
        self.files = {HEAD: {"defaults.yaml": DEFAULTS, OXIDEX_PATH: OXIDEX, "README.md": "hi"}}
        self.calls: list[tuple[str, str, object]] = []
        self.prs: list[dict] = []
        self.fail: dict[tuple[str, str], int] = {}
        self.commit_counter = 0

    def __call__(self, method, url, headers, body, timeout):
        assert url.startswith("https://api.github.com/"), url
        assert headers["Authorization"] == "Bearer test-token"
        path = url[len("https://api.github.com/"):]
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        route, _, query = path.partition("?")
        params = urllib.parse.parse_qs(query)
        prefix = f"repos/{REPO}"
        for (m, p), status in self.fail.items():
            if m == method and route.startswith(p):
                return status, json.dumps({"message": "simulated"}).encode()
        if method == "GET" and route == prefix:
            return 200, json.dumps({"default_branch": self.default_branch}).encode()
        if method == "GET" and route.startswith(prefix + "/git/ref/heads/"):
            name = urllib.parse.unquote(route[len(prefix + "/git/ref/heads/"):])
            if name not in self.branches:
                return 404, b"{}"
            return 200, json.dumps({"object": {"sha": self.branches[name]}}).encode()
        if method == "GET" and route.startswith(prefix + "/git/trees/"):
            sha = route.rsplit("/", 1)[1]
            return 200, json.dumps({"tree": [{"path": p, "type": "blob"} for p in self.files[sha]],
                                    "truncated": False}).encode()
        if method == "GET" and route.startswith(prefix + "/contents/"):
            file_path = route[len(prefix + "/contents/"):]
            ref = params["ref"][0]
            sha = self.branches.get(ref, ref)
            text = self.files.get(sha, {}).get(file_path)
            if text is None:
                return 404, b'{"message": "Not Found"}'
            return 200, json.dumps({"encoding": "base64", "sha": blob(text),
                                    "content": base64.b64encode(text.encode()).decode()}).encode()
        if method == "POST" and route == prefix + "/git/refs":
            name = payload["ref"][len("refs/heads/"):]
            if name in self.branches:
                return 422, b'{"message": "Reference already exists"}'
            self.branches[name] = payload["sha"]
            return 201, json.dumps({"ref": payload["ref"]}).encode()
        if method == "DELETE" and route.startswith(prefix + "/git/refs/heads/"):
            self.branches.pop(urllib.parse.unquote(route[len(prefix + "/git/refs/heads/"):]), None)
            return 204, b""
        if method == "PUT" and route.startswith(prefix + "/contents/"):
            file_path = route[len(prefix + "/contents/"):]
            branch = payload["branch"]
            assert branch != self.default_branch, "must never write to the default branch"
            base = self.branches[branch]
            current = self.files[base].get(file_path)
            if (current is None) != ("sha" not in payload) or (current is not None and payload["sha"] != blob(current)):
                return 409, b'{"message": "sha does not match"}'
            self.commit_counter += 1
            new_sha = f"{self.commit_counter:040x}"
            self.files[new_sha] = {**self.files[base], file_path: base64.b64decode(payload["content"]).decode()}
            self.branches[branch] = new_sha
            return 200, json.dumps({"commit": {"sha": new_sha}}).encode()
        if method == "POST" and route == prefix + "/pulls":
            pr = {"number": len(self.prs) + 1, "html_url": f"https://github.com/{REPO}/pull/{len(self.prs) + 1}",
                  "state": "open", **payload}
            self.prs.append(pr)
            return 201, json.dumps(pr).encode()
        return 404, b'{"message": "unexpected"}'

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def effective(manifest: dict) -> dict:
    return validate_patch_dict(merge_patch_dicts(json.loads(DEFAULTS), manifest)).to_canonical_dict()


OLD = effective({"schema": "spot-runner-patch-v1", "repository": "swack-tools/oxidex"})
TARGET_SHA = validate_patch_dict(merge_patch_dicts(json.loads(DEFAULTS), json.loads(OXIDEX))).patch_sha256


class FakeExecutor:
    available = True
    reason = ""

    def __init__(self, manifests=None):
        self.manifests = manifests or {}
        self.applied: list[tuple[str, str]] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def read_manifest(self, host):
        value = self.manifests.get(host["name"], OLD)
        if isinstance(value, Exception):
            raise value
        return value

    def apply(self, host, patch):
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        self.applied.append((host["name"], patch.patch_sha256))
        return {"result": "applied"}


def host(name, **extra):
    return {"name": name, "zone": "us-central1-a", "role": "cicd_runner", "status": "RUNNING",
            "patch_target": "swack-tools/oxidex", "machine_type": "n2-standard-8", "runner_prefix": "oxidex",
            "runners_count": 2, **extra}


def console(fake=None, executor=None):
    return pc.PatchConsole(REPO, token="test-token", requester=fake or FakeGitHubRepo(), executor=executor)


class ValidationTests(unittest.TestCase):
    def test_valid_repo_manifest_is_checked_merged_with_defaults(self):
        patch, errors, _ = pc.validate_manifest(OXIDEX, path=OXIDEX_PATH, defaults=json.loads(DEFAULTS))
        self.assertEqual(errors, [])
        self.assertEqual(patch.patch_sha256, TARGET_SHA)
        self.assertEqual(patch.host.apt_packages, ("jq",))

    def test_field_errors_are_precise_and_collected_per_section(self):
        text = json.dumps({
            "schema": "spot-runner-patch-v1",
            "repository": "someone/else",
            "hardware": {"arch": "x86_64", "isa": ["sve"], "colour": 1},
            "host": {"apt_packages": ["Bad Name"]},
            "container": {"mounts": [{"source": "/var/run/docker.sock", "target": "/x"}]},
            "extra": True,
        })
        patch, errors, _ = pc.validate_manifest(text, path=OXIDEX_PATH)
        self.assertIsNone(patch)
        fields = {e["field"] for e in errors}
        self.assertTrue({"extra", "repository", "hardware.colour", "hardware.isa", "host.apt_packages",
                         "container.mounts"} <= fields, errors)
        self.assertTrue(any("docker.sock" in e["message"] for e in errors))

    def test_parse_and_shape_errors(self):
        for text, field in (("", "content"), ("{not json", "content"), (json.dumps([1, 2]), None),
                            (json.dumps({"host": []}), "host"), (json.dumps({"schema": "v0"}), "schema")):
            patch, errors, _ = pc.validate_manifest(text, path=OXIDEX_PATH)
            self.assertIsNone(patch, text)
            self.assertTrue(errors, text)
            if field:
                self.assertIn(field, {e["field"] for e in errors}, text)

    def test_merged_conflict_is_reported(self):
        defaults = {"hardware": {"arch": "x86_64", "isa": ["avx2"]}}
        patch, errors, _ = pc.validate_manifest(json.dumps({"hardware": {"arch": "arm64"}}), path=OXIDEX_PATH,
                                                defaults=defaults)
        self.assertIsNone(patch)
        self.assertEqual(errors[0]["field"], "effective")

    def test_paths_are_restricted(self):
        for bad in ("../x.yaml", "repos/a/b/c.yaml", "repos/a/...yaml", "README.md", "repos/a/b.yml", ""):
            with self.assertRaises(pc.InvalidRequestError, msg=bad):
                pc.validate_manifest(OXIDEX, path=bad)
        self.assertIsNone(pc.repository_for_path("defaults.yaml"))
        self.assertEqual(pc.repository_for_path(OXIDEX_PATH), "swack-tools/oxidex")
        _, errors, _ = pc.validate_manifest(OXIDEX, path="defaults.yaml")
        self.assertEqual(errors[0]["field"], "repository")

    def test_yaml_manifest(self):
        text = "schema: spot-runner-patch-v1\nhost:\n  apt_packages:\n    - jq\n"
        patch, errors, _ = pc.validate_manifest(text, path="defaults.yaml")
        self.assertEqual((errors, patch.host.apt_packages), ([], ("jq",)))


class ConsoleTests(unittest.TestCase):
    def test_list_and_load_resolve_default_branch_and_head(self):
        fake = FakeGitHubRepo()
        c = console(fake)
        listing = c.list_manifests()
        self.assertEqual((listing["default_branch"], listing["head_sha"]), ("main", HEAD))
        self.assertTrue(listing["defaults"]["present"])
        self.assertEqual(listing["repositories"], [{"repository": "swack-tools/oxidex", "path": OXIDEX_PATH}])
        self.assertFalse(listing["executor"]["available"])
        loaded = c.load_manifest(OXIDEX_PATH)
        self.assertEqual((loaded["content"], loaded["base_sha"], loaded["blob_sha"]), (OXIDEX, HEAD, blob(OXIDEX)))
        self.assertEqual(loaded["patch_sha256"], TARGET_SHA)
        self.assertEqual(loaded["errors"], [])
        new = c.load_manifest("repos/swack-tools/new-repo.yaml")
        self.assertEqual((new["exists"], new["content"], new["blob_sha"]), (False, "", None))
        self.assertEqual(fake.writes(), [])

    def test_missing_token_or_github_errors_are_clear(self):
        with self.assertRaisesRegex(pc.GitHubRestError, "SPOT_CONFIG_GH_TOKEN"):
            pc.PatchConsole(REPO, token="", requester=FakeGitHubRepo()).list_manifests()
        fake = FakeGitHubRepo()
        fake.fail[("GET", f"repos/{REPO}")] = 403
        with self.assertRaisesRegex(pc.GitHubRestError, "HTTP 403"):
            console(fake).list_manifests()
        self.assertEqual(pc.config_repo_token({"GH_TOKEN": "g", "SPOT_CONFIG_GH_TOKEN": "s"}), "s")

    def test_save_opens_pull_request_from_new_branch(self):
        fake = FakeGitHubRepo()
        new_text = OXIDEX.replace("ripgrep", "fd-find")
        result = console(fake).save(OXIDEX_PATH, new_text, HEAD, "Add fd")
        self.assertEqual(result["status"], "pull_request_opened")
        self.assertTrue(result["branch"].startswith("spot-dashboard/swack-tools-oxidex-aaaaaaa-"))
        self.assertEqual(fake.branches["main"], HEAD)  # default branch untouched
        self.assertEqual(fake.files[fake.branches[result["branch"]]][OXIDEX_PATH], new_text)
        [pr] = fake.prs
        self.assertEqual((pr["head"], pr["base"], pr["title"]), (result["branch"], "main", "Add fd"))
        put = [c for c in fake.calls if c[0] == "PUT"][0]
        self.assertEqual(put[2]["sha"], blob(OXIDEX))
        self.assertIn(result["patch_sha256"], pr["body"])
        new_file = console(fake).save("repos/swack-tools/new-repo.yaml", json.dumps({"host": {"apt_packages": ["jq"]}}),
                                      HEAD)
        self.assertEqual(new_file["pull_request"]["number"], 2)

    def test_save_conflicts_when_the_base_moved(self):
        fake = FakeGitHubRepo()
        fake.branches["main"] = NEXT
        fake.files[NEXT] = dict(fake.files[HEAD])
        with self.assertRaises(pc.ConflictError) as ctx:
            console(fake).save(OXIDEX_PATH, OXIDEX.replace("ripgrep", "fd-find"), HEAD)
        self.assertEqual(ctx.exception.body["head_sha"], NEXT)
        self.assertEqual(fake.writes(), [])

    def test_save_conflict_from_github_cleans_up_the_branch(self):
        fake = FakeGitHubRepo()
        real = fake.__call__

        def racing(method, url, headers, body, timeout):
            if method == "PUT":
                return 409, b'{"message": "sha mismatch"}'
            return real(method, url, headers, body, timeout)

        with self.assertRaises(pc.ConflictError):
            console(racing).save(OXIDEX_PATH, OXIDEX.replace("ripgrep", "fd-find"), HEAD)
        self.assertEqual(set(fake.branches), {"main"})
        self.assertEqual(fake.prs, [])

    def test_save_refuses_invalid_unchanged_or_malformed_requests(self):
        fake = FakeGitHubRepo()
        c = console(fake)
        with self.assertRaises(pc.InvalidRequestError) as ctx:
            c.save(OXIDEX_PATH, json.dumps({"host": {"apt_packages": ["Bad Name"]}}), HEAD)
        self.assertEqual(ctx.exception.body["errors"][0]["field"], "host.apt_packages")
        with self.assertRaisesRegex(pc.InvalidRequestError, "unchanged"):
            c.save(OXIDEX_PATH, OXIDEX, HEAD)
        for bad_sha in ("", "abc", None, "A" * 40):
            with self.assertRaises(pc.InvalidRequestError):
                c.save(OXIDEX_PATH, OXIDEX, bad_sha)
        with self.assertRaises(pc.InvalidRequestError):
            c.save("../../etc/passwd", OXIDEX, HEAD)
        self.assertEqual(fake.writes(), [])

    def test_preview_classifies_each_recorded_host(self):
        mounts = effective({"container": {"mounts": [{"source": "/srv/cache", "target": "/cache"}]}})
        arm = validate_patch_dict({"hardware": {"arch": "arm64"}}).to_canonical_dict()
        executor = FakeExecutor({"h-old": OLD, "h-same": effective(json.loads(OXIDEX)), "h-mounts": mounts,
                                 "h-arm": arm, "h-fresh": None, "h-broken": RuntimeError("ssh failed")})
        instances = [host(n) for n in ("h-old", "h-same", "h-mounts", "h-arm", "h-fresh", "h-broken")] + [
            host("other", patch_target="someone/else"), host("stopped", status="TERMINATED"),
            {**host("builder"), "role": "rust_builder"}]
        result = console(executor=executor).preview("swack-tools/oxidex", instances)
        got = {h["name"]: h["classification"] for h in result["hosts"]}
        self.assertEqual(got, {"h-old": "hot_patch", "h-same": "noop", "h-mounts": "recreate_containers",
                               "h-arm": "replace_vm", "h-fresh": "hot_patch", "h-broken": "unknown"})
        self.assertEqual(result["patch_sha256"], TARGET_SHA)
        self.assertFalse(result["hot_patch_allowed"])
        ok = console(executor=FakeExecutor()).preview("swack-tools/oxidex", [host("h1"), host("h2")])
        self.assertTrue(ok["hot_patch_allowed"])
        draft = console(executor=FakeExecutor()).preview("swack-tools/oxidex", [host("h1")],
                                                          OXIDEX.replace("ripgrep", "fd-find"))
        self.assertTrue(draft["source"]["draft"])
        self.assertFalse(draft["hot_patch_allowed"])  # only the saved, merged patch can be hot-patched

    def test_preview_without_executor_uses_recorded_manifests_or_unknown(self):
        result = console().preview("swack-tools/oxidex", [host("rec", patch_manifest=OLD), host("blind")])
        got = {h["name"]: h["classification"] for h in result["hosts"]}
        self.assertEqual(got, {"rec": "hot_patch", "blind": "unknown"})
        self.assertFalse(result["hot_patch_allowed"])
        self.assertIn("no gcloud", result["executor"]["reason"])

    def test_hot_patch_gates(self):
        executor = FakeExecutor()
        c = console(executor=executor)
        with self.assertRaises(pc.ConflictError) as ctx:
            c.hot_patch("swack-tools/oxidex", "0" * 64, [host("h1")])
        self.assertEqual(ctx.exception.body["status"], "stale_patch")
        with self.assertRaises(pc.ConflictError):
            c.hot_patch("swack-tools/oxidex", TARGET_SHA, [])
        executor.manifests["h2"] = validate_patch_dict({"hardware": {"arch": "arm64"}}).to_canonical_dict()
        with self.assertRaises(pc.ConflictError) as ctx:
            c.hot_patch("swack-tools/oxidex", TARGET_SHA, [host("h1"), host("h2")])
        self.assertEqual(ctx.exception.body["status"], "refused")
        self.assertEqual(executor.applied, [])  # never partially applied
        status, body = c.hot_patch("swack-tools/oxidex", TARGET_SHA, [host("h1"), host("h3")])
        self.assertEqual((status, body["status"]), (200, "applied"))
        self.assertEqual(executor.applied, [("h1", TARGET_SHA), ("h3", TARGET_SHA)])
        with self.assertRaises(pc.HotPatchUnavailable):
            console().hot_patch("swack-tools/oxidex", TARGET_SHA, [host("h1")])

    def test_hot_patch_reports_per_host_failures(self):
        executor = FakeExecutor({"same": effective(json.loads(OXIDEX))})
        calls = []

        def apply(h, patch):
            calls.append(h["name"])
            if h["name"] == "bad":
                raise RuntimeError("live patch did not confirm")
            return {"result": "applied"}

        executor.apply = apply
        status, body = console(executor=executor).hot_patch(
            "swack-tools/oxidex", TARGET_SHA, [host("good"), host("bad"), host("same")])
        results = {h["name"]: h["result"] for h in body["hosts"]}
        self.assertEqual((body["status"], results), ("partial", {"good": "applied", "bad": "failed", "same": "noop"}))
        self.assertEqual(calls, ["good", "bad"])


class ExecutorTests(unittest.TestCase):
    def test_default_executor_refuses_clearly(self):
        ex = pc.executor_from_env("proj", {})
        self.assertFalse(ex.available)
        with self.assertRaisesRegex(pc.HotPatchUnavailable, "spot_runner_manager.py"):
            ex.apply(host("h"), validate_patch_dict({}))
        with mock.patch("shutil.which", return_value=None):
            missing = pc.executor_from_env("proj", {"DASHBOARD_HOT_PATCH_EXECUTOR": "gcloud"})
        self.assertFalse(missing.available)
        self.assertIn("gcloud is not installed", missing.reason)

    def test_gcloud_executor_wraps_apply_live_patch(self):
        from lib import runner_manager

        ex = pc.GcloudHotPatchExecutor("proj", which=lambda _n: "/usr/bin/gcloud")
        patch = validate_patch_dict(json.loads(OXIDEX))
        before = runner_manager.PROJECT
        seen = []
        with mock.patch.object(runner_manager, "apply_live_patch",
                               side_effect=lambda *_a: seen.append(runner_manager.PROJECT) or "hot_patch") as apply:
            self.assertEqual(ex.apply(host("h1"), patch), {"result": "applied"})
        self.assertEqual((seen, runner_manager.PROJECT), (["proj"], before))  # scoped, then restored
        args, zone = apply.call_args[0]
        self.assertEqual((args.instance_name, args.zone, args.instance_type, args.runner_prefix, args.runner_count,
                          args.target, zone), ("h1", "us-central1-a", "n2-standard-8", "oxidex", 2,
                                               "swack-tools/oxidex", "us-central1-a"))
        self.assertIs(args.runner_patch, patch)
        with self.assertRaisesRegex(pc.PatchConsoleError, "runner_prefix"):
            ex.apply({"name": "h", "zone": "z", "machine_type": "m", "runners_count": 1}, patch)

    def test_gcloud_executor_reads_host_receipt(self):
        receipts = {"h1": json.dumps({"manifest": OLD}), "h2": ""}

        def run(cmd, **_kw):
            self.assertEqual(cmd[:4], ["gcloud", "compute", "ssh", cmd[3]])
            self.assertIn("--project=proj", cmd)
            return subprocess.CompletedProcess(cmd, 0, receipts[cmd[3]], "")

        ex = pc.GcloudHotPatchExecutor("proj", which=lambda _n: "/usr/bin/gcloud", run=run)
        self.assertEqual(ex.read_manifest(host("h1")), json.loads(json.dumps(OLD)))
        self.assertIsNone(ex.read_manifest(host("h2")))
        bad = pc.GcloudHotPatchExecutor("proj", which=lambda _n: "/x",
                                        run=lambda cmd, **_k: subprocess.CompletedProcess(cmd, 255, "", "denied"))
        with self.assertRaises(pc.PatchConsoleError):
            bad.read_manifest(host("h1"))


class PatchApiTests(unittest.TestCase):
    TOKEN = "legacy-dashboard-token-123"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeGitHubRepo()
        self.executor = FakeExecutor()

    def start(self, executor="fake"):
        ex = self.executor if executor == "fake" else executor
        server, store = create_dashboard_server(
            host="127.0.0.1", port=0, state_path=Path(self.tmp.name) / "state.json", auth_token=self.TOKEN,
            patch_console=console(self.fake, ex),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        self.port = server.server_address[1]
        with store._lock:
            store.state["instances"] = [host("h1"), host("h2")]
        self.store = store
        return store

    def request(self, method, path, body=None, *, key=None, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = dict(headers or {})
        if auth:
            hdrs["Authorization"] = f"Bearer {self.TOKEN}"
        if key:
            hdrs["Idempotency-Key"] = key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, resp.msg, json.loads(raw) if raw else None
        finally:
            conn.close()

    def hot_patch_body(self, **extra):
        return {"repository": "swack-tools/oxidex", "patch_sha256": TARGET_SHA, "confirm": True, **extra}

    def test_every_route_requires_auth_and_same_origin(self):
        self.start()
        for method, path in (("GET", "/api/patches"), ("GET", f"/api/patches/manifest?path={OXIDEX_PATH}"),
                             ("POST", "/api/patches/validate"), ("POST", "/api/patches/preview"),
                             ("POST", "/api/patches/save"), ("POST", "/api/patches/hot-patch")):
            status, _, _ = self.request(method, path, {} if method == "POST" else None, auth=False, key="k" * 16)
            self.assertEqual(status, 401, path)
            if method == "POST":
                status, _, _ = self.request(method, path, {}, key="k" * 16, headers={"Origin": "https://evil.example"})
                self.assertEqual(status, 403, path)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.executor.applied, [])

    def test_read_validate_and_preview(self):
        self.start()
        status, _, body = self.request("GET", "/api/patches")
        self.assertEqual((status, body["head_sha"]), (200, HEAD))
        status, _, body = self.request("GET", f"/api/patches/manifest?path={urllib.parse.quote(OXIDEX_PATH)}")
        self.assertEqual((status, body["patch_sha256"]), (200, TARGET_SHA))
        status, _, body = self.request("GET", "/api/patches/manifest?path=../secrets")
        self.assertEqual(status, 400)
        status, _, body = self.request("POST", "/api/patches/validate",
                                       {"path": OXIDEX_PATH, "content": json.dumps({"hardware": {"min_vcpu": 0}})})
        self.assertEqual((status, body["valid"], body["errors"][0]["field"]), (200, False, "hardware.min_vcpu"))
        status, _, body = self.request("POST", "/api/patches/preview", {"repository": "swack-tools/oxidex"})
        self.assertEqual((status, body["hot_patch_allowed"]), (200, True))
        self.assertEqual([h["classification"] for h in body["hosts"]], ["hot_patch", "hot_patch"])
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.executor.applied, [])

    def test_save_needs_key_and_replays_without_a_second_pull_request(self):
        self.start()
        body = {"path": OXIDEX_PATH, "content": OXIDEX.replace("ripgrep", "fd-find"), "base_sha": HEAD}
        status, _, _ = self.request("POST", "/api/patches/save", body)
        self.assertEqual(status, 400)
        status, msg, first = self.request("POST", "/api/patches/save", body, key="save-key-0001")
        self.assertEqual((status, first["status"]), (200, "pull_request_opened"))
        status, msg, again = self.request("POST", "/api/patches/save", body, key="save-key-0001")
        self.assertEqual((status, again, msg.get("Idempotent-Replay")), (200, first, "true"))
        self.assertEqual(len(self.fake.prs), 1)
        status, _, conflict = self.request("POST", "/api/patches/save", {**body, "base_sha": NEXT}, key="save-key-0002")
        self.assertEqual(status, 409)
        self.assertEqual(conflict["head_sha"], HEAD)
        status, _, bad = self.request("POST", "/api/patches/save",
                                      {**body, "content": json.dumps({"host": {"sysctl": {"BAD": 1}}})},
                                      key="save-key-0003")
        self.assertEqual((status, bad["errors"][0]["field"]), (400, "host.sysctl"))
        self.assertEqual(len(self.fake.prs), 1)

    def test_hot_patch_requires_confirm_and_key_and_replays(self):
        self.start()
        status, _, _ = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key=None)
        self.assertEqual(status, 400)
        for confirm in (None, False, "true", 1):
            status, _, _ = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(confirm=confirm),
                                        key="hot-key-0001")
            self.assertEqual(status, 400, confirm)
        self.assertEqual(self.executor.applied, [])
        status, msg, first = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key="hot-key-0002")
        self.assertEqual((status, first["status"]), (200, "applied"))
        self.assertEqual([r["result"] for r in first["hosts"]], ["applied", "applied"])
        status, msg, again = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key="hot-key-0002")
        self.assertEqual((status, again, msg.get("Idempotent-Replay")), (200, first, "true"))
        self.assertEqual(len(self.executor.applied), 2)  # the replay did not touch the hosts again
        status, _, reused = self.request("POST", "/api/patches/save", {}, key="hot-key-0002")
        self.assertEqual(status, 422)

    def test_hot_patch_refused_unless_every_host_is_hot_patch(self):
        self.executor.manifests["h2"] = effective({"container": {"mounts": [{"source": "/srv", "target": "/srv"}]}})
        self.start()
        status, _, body = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key="hot-key-0003")
        self.assertEqual((status, body["status"]), (409, "refused"))
        self.assertEqual({h["name"]: h["classification"] for h in body["hosts"]},
                         {"h1": "hot_patch", "h2": "recreate_containers"})
        self.assertEqual(self.executor.applied, [])
        status, _, body = self.request("POST", "/api/patches/hot-patch",
                                       self.hot_patch_body(patch_sha256="f" * 64), key="hot-key-0004")
        self.assertEqual((status, body["status"]), (409, "stale_patch"))

    def test_hot_patch_without_a_host_path_fails_clearly(self):
        self.start(executor=None)
        status, _, body = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key="hot-key-0005")
        self.assertEqual((status, body["code"]), (501, "hot_patch_unavailable"))
        self.assertIn("no gcloud", body["error"])

    def test_hot_patch_refused_while_frozen_unhydrated_or_inactive(self):
        store = self.start()
        for setup, undo in ((lambda: setattr(store, "frozen", True), lambda: setattr(store, "frozen", False)),
                            (lambda: setattr(store, "hydrated", False), lambda: setattr(store, "hydrated", True)),
                            (lambda: setattr(store, "known_inactive", True),
                             lambda: setattr(store, "known_inactive", False))):
            setup()
            for path, body in (("/api/patches/hot-patch", self.hot_patch_body()),
                               ("/api/patches/save", {"path": OXIDEX_PATH, "content": "{}", "base_sha": HEAD})):
                status, _, reply = self.request("POST", path, body, key="gate-key-0001")
                self.assertEqual((status, reply["error"]), (503, "control_vm_unavailable"), path)
            undo()
        self.assertEqual(self.executor.applied, [])
        self.assertEqual(self.fake.writes(), [])
        status, _, _ = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key="gate-key-0001")
        self.assertEqual(status, 200)  # the refused attempts stored nothing under the key

    def test_concurrent_request_with_the_same_key_does_not_run_twice(self):
        self.start()
        self.executor.gate = threading.Event()
        results = []
        worker = threading.Thread(target=lambda: results.append(
            self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key="race-key-0001")))
        worker.start()
        self.assertTrue(self.executor.started.wait(5))
        status, _, body = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key="race-key-0001")
        self.assertEqual(status, 409)
        self.assertIn("still running", body["error"])
        self.executor.gate.set()
        worker.join(10)
        self.assertEqual(results[0][0], 200)
        self.assertEqual(len(self.executor.applied), 2)


_UI_HARNESS = r"""
const cfg = JSON.parse(process.argv[1]);
const out = {posts: [], gets: []};
const nodes = {};
function makeNode(tag) {
  return {tag, children: [], textContent: "", value: "", disabled: false, checked: false, href: "", target: "",
    rel: "", className: "", listeners: {},
    get firstChild() { return this.children[0] || null; },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children.splice(this.children.indexOf(c), 1); },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    set innerHTML(_v) { throw new Error("innerHTML must not be used"); }};
}
for (const id of ["rp-refresh", "rp-source", "rp-status", "rp-list", "rp-path", "rp-editor", "rp-errors",
                  "rp-message", "rp-validate", "rp-preview", "rp-save", "rp-effective-sha", "rp-effective",
                  "rp-hosts", "rp-confirm", "rp-hotpatch", "rp-executor"]) nodes[id] = makeNode("x");
globalThis.document = {getElementById: id => nodes[id] || null, createElement: makeNode};
globalThis.window = {};
const replies = cfg.replies;
globalThis.fetch = async (url, init) => {
  const method = (init && init.method) || "GET";
  const key = url.split("?")[0];
  if (method === "POST") out.posts.push([key, JSON.parse(init.body)]); else out.gets.push(url);
  const r = replies[key] || {status: 404, body: {error: "missing"}};
  return {ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body};
};
""" + "\n"

_UI_DRIVER = r"""
function texts(n) { return [n.textContent].concat(n.children.flatMap(texts)).filter(Boolean); }
(async () => {
  const RP = window.RunnerPatches;
  RP.init();
  await RP.refresh();
  out.list = nodes["rp-list"].children.map(li => li.children[0].textContent);
  await RP.load(cfg.path);
  out.editor = nodes["rp-editor"].value;
  out.errors = nodes["rp-errors"].children.map(li => li.textContent);
  nodes["rp-hotpatch"].disabled = false;
  await RP.hotPatch();  // nothing previewed: must not post
  await RP.preview();
  out.disabled_before_confirm = nodes["rp-hotpatch"].disabled;
  nodes["rp-confirm"].checked = true;
  nodes["rp-confirm"].listeners.change();
  out.disabled_after_confirm = nodes["rp-hotpatch"].disabled;
  out.hosts = nodes["rp-hosts"].children.map(tr => tr.children.map(td => td.textContent));
  if (cfg.edit) { nodes["rp-editor"].value = "edited"; nodes["rp-editor"].listeners.input(); }
  out.disabled_after_edit = nodes["rp-hotpatch"].disabled;
  await RP.hotPatch();
  await RP.save();
  out.status = texts(nodes["rp-status"]);
  out.links = nodes["rp-status"].children.filter(c => c.href).map(c => c.href);
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stdout.write(JSON.stringify({crash: String(e)})); });
"""


def _ui_block() -> str:
    from lib.dashboard_ui_assets import render_dashboard_html

    html = render_dashboard_html()
    start = html.index("    // --- runner patches panel ---")
    return html[start:html.index("    // --- end runner patches panel ---", start)]


@unittest.skipUnless(shutil.which("node"), "node is required to execute the patch panel script")
class PatchPanelUiTests(unittest.TestCase):
    EVIL = '<img src=x onerror="alert(1)">'

    def run_ui(self, replies, **cfg):
        program = _UI_HARNESS + _ui_block() + _UI_DRIVER
        proc = subprocess.run(["node", "-e", program, json.dumps({"replies": replies, "path": OXIDEX_PATH, **cfg})],
                              capture_output=True, text=True, timeout=30, check=True)
        out = json.loads(proc.stdout)
        self.assertNotIn("crash", out, out)
        return out

    def replies(self, *, allowed=True, save=None, pr_url=f"https://github.com/{REPO}/pull/7"):
        return {
            "/api/patches": {"status": 200, "body": {
                "config_repo": REPO, "default_branch": "main", "head_sha": HEAD, "defaults": {"present": True},
                "repositories": [{"repository": self.EVIL, "path": OXIDEX_PATH}],
                "executor": {"available": True, "reason": ""}}},
            "/api/patches/manifest": {"status": 200, "body": {
                "path": OXIDEX_PATH, "repository": "swack-tools/oxidex", "base_sha": HEAD, "exists": True,
                "content": OXIDEX, "errors": [{"field": "host", "message": self.EVIL}], "patch_sha256": TARGET_SHA}},
            "/api/patches/preview": {"status": 200, "body": {
                "repository": "swack-tools/oxidex", "patch_sha256": TARGET_SHA, "source": {"draft": False},
                "hot_patch_allowed": allowed, "executor": {"available": True},
                "hosts": [{"name": self.EVIL, "zone": "z", "classification": "hot_patch",
                           "current_patch_sha256": "c" * 64, "detail": "d"}]}},
            "/api/patches/hot-patch": {"status": 200, "body": {"status": "applied", "hosts": [
                {"name": "h1", "zone": "z", "classification": "hot_patch", "result": "applied"}]}},
            "/api/patches/save": save or {"status": 200, "body": {
                "status": "pull_request_opened", "branch": "spot-dashboard/x",
                "pull_request": {"number": 7, "url": pr_url}}},
        }

    def test_panel_static_rules(self):
        block = _ui_block()
        self.assertNotIn("innerHTML", block)
        self.assertNotIn("insertAdjacentHTML", block)
        self.assertIn("textContent", block)

    def test_hot_patch_needs_preview_and_confirm_and_sends_confirm_true(self):
        out = self.run_ui(self.replies())
        self.assertEqual(out["list"], ["defaults.yaml", self.EVIL])  # rendered as text, not markup
        self.assertEqual(out["errors"], [f"host: {self.EVIL}"])
        self.assertEqual(out["editor"], OXIDEX)
        self.assertEqual((out["disabled_before_confirm"], out["disabled_after_confirm"]), (True, False))
        posts = [p for p in out["posts"] if p[0] == "/api/patches/hot-patch"]
        self.assertEqual(posts, [["/api/patches/hot-patch",
                                  {"repository": "swack-tools/oxidex", "patch_sha256": TARGET_SHA, "confirm": True}]])
        self.assertEqual(out["hosts"][0][0], self.EVIL)
        save = [p for p in out["posts"] if p[0] == "/api/patches/save"][0][1]
        self.assertEqual((save["path"], save["base_sha"]), (OXIDEX_PATH, HEAD))
        self.assertEqual(out["links"], [f"https://github.com/{REPO}/pull/7"])

    def test_hot_patch_stays_disabled_when_not_allowed_or_edited(self):
        out = self.run_ui(self.replies(allowed=False))
        self.assertTrue(out["disabled_after_confirm"])
        self.assertEqual([p for p in out["posts"] if p[0] == "/api/patches/hot-patch"], [])
        edited = self.run_ui(self.replies(), edit=True)
        self.assertTrue(edited["disabled_after_edit"])
        self.assertEqual(len([p for p in edited["posts"] if p[0] == "/api/patches/hot-patch"]), 0)

    def test_save_conflict_and_untrusted_links(self):
        out = self.run_ui(self.replies(save={"status": 409, "body": {"error": "main moved; reload"}}))
        self.assertIn("main moved; reload", out["status"])
        out = self.run_ui(self.replies(pr_url="javascript:alert(1)"))
        self.assertEqual(out["links"], [])


if __name__ == "__main__":
    unittest.main()
