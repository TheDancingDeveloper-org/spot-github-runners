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
        if method == "GET" and route == prefix + "/pulls":
            owner_branch = params["head"][0]
            branch = owner_branch.split(":", 1)[1]
            return 200, json.dumps([pr for pr in self.prs if pr["head"] == branch]).encode()
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
            "runners_count": 2, "runner_label": "oxidex-spot-ci", "runner_region": "us-central1",
            "registration_target": "swack-tools", "runner_group_id": 10, **extra}


def console(fake=None, executor=None):
    return pc.PatchConsole(REPO, token="test-token", requester=fake or FakeGitHubRepo(), executor=executor)


class LossyGitHub:
    """Wraps FakeGitHubRepo and loses responses: "after" applies the call then times out, "before" fails it
    without applying, "status" answers 502 without applying. Each rule fires once."""

    def __init__(self, fake):
        self.fake = fake
        self.rules: list[tuple[str, str, str]] = []

    def lose(self, method, route_suffix, mode):
        self.rules.append((method, route_suffix, mode))

    def __call__(self, method, url, headers, body, timeout):
        route = url.split("?", 1)[0]
        for rule in list(self.rules):
            m, suffix, mode = rule
            if m == method and route.endswith(suffix):
                self.rules.remove(rule)
                if mode == "before":
                    raise TimeoutError("simulated timeout before GitHub applied the call")
                if mode == "status":
                    self.fake.calls.append((method, url, None))
                    return 502, b'{"message": "Server Error"}'
                self.fake(method, url, headers, body, timeout)
                raise TimeoutError("simulated lost response after GitHub applied the call")
        return self.fake(method, url, headers, body, timeout)

    def count(self, method, suffix):
        return len([c for c in self.fake.calls if c[0] == method and c[1].split("?", 1)[0].endswith(suffix)])


class LostResponseSaveTests(unittest.TestCase):
    """Codex round 4: an ambiguous GitHub failure never deletes the branch; it reconciles instead."""

    NEW = OXIDEX.replace("ripgrep", "fd-find")

    def save(self, lossy, key="lost-0001"):
        return console(lossy).save(OXIDEX_PATH, self.NEW, HEAD, "Add fd", idempotency_key=key)

    def test_pull_request_created_but_response_lost(self):
        fake = FakeGitHubRepo()
        lossy = LossyGitHub(fake)
        lossy.lose("POST", "/pulls", "after")
        first = self.save(lossy)  # reconciles by looking the PR up instead of failing
        self.assertEqual(first["pull_request"]["number"], 1)
        again = self.save(lossy)
        self.assertEqual((again["reconciled"], again["pull_request"]["number"]), (True, 1))
        self.assertEqual(len(fake.prs), 1)
        self.assertEqual(lossy.count("DELETE", first["branch"].split("/", 1)[1]), 0)

    def test_pull_request_not_created_keeps_the_branch_for_a_retry(self):
        for mode in ("before", "status"):
            fake = FakeGitHubRepo()
            lossy = LossyGitHub(fake)
            lossy.lose("POST", "/pulls", mode)
            with self.subTest(mode), self.assertRaises(pc.GitHubRestError) as ctx:
                self.save(lossy)
            self.assertTrue(ctx.exception.body["retry_same_key"])
            branch = pc.PatchConsole.branch_for(OXIDEX_PATH, "lost-0001")
            self.assertIn(branch, fake.branches)  # never deleted
            self.assertEqual([c for c in fake.calls if c[0] == "DELETE"], [])
            result = self.save(lossy)  # the retry opens the PR exactly once
            self.assertEqual((len(fake.prs), result["branch"]), (1, branch))
            self.assertEqual(lossy.count("PUT", OXIDEX_PATH), 1)  # and does not commit again

    def test_commit_applied_but_response_lost(self):
        fake = FakeGitHubRepo()
        lossy = LossyGitHub(fake)
        lossy.lose("PUT", OXIDEX_PATH, "after")
        result = self.save(lossy)
        self.assertEqual(fake.files[fake.branches[result["branch"]]][OXIDEX_PATH], self.NEW)
        self.assertEqual((len(fake.prs), lossy.count("PUT", OXIDEX_PATH)), (1, 1))

    def test_commit_not_applied_keeps_the_branch_and_retry_finishes(self):
        fake = FakeGitHubRepo()
        lossy = LossyGitHub(fake)
        lossy.lose("PUT", OXIDEX_PATH, "before")
        with self.assertRaises(pc.GitHubRestError):
            self.save(lossy)
        branch = pc.PatchConsole.branch_for(OXIDEX_PATH, "lost-0001")
        self.assertEqual(fake.branches[branch], HEAD)
        self.assertEqual([c for c in fake.calls if c[0] == "DELETE"], [])
        self.assertEqual(self.save(lossy)["pull_request"]["number"], 1)
        self.assertEqual(len(fake.prs), 1)

    def test_branch_created_but_response_lost(self):
        fake = FakeGitHubRepo()
        lossy = LossyGitHub(fake)
        lossy.lose("POST", "/git/refs", "after")
        self.assertEqual(self.save(lossy)["pull_request"]["number"], 1)
        self.assertEqual(lossy.count("POST", "/git/refs"), 1)

    def test_failed_lookup_never_cleans_up(self):
        fake = FakeGitHubRepo()
        lossy = LossyGitHub(fake)
        lossy.lose("POST", "/pulls", "after")
        lossy.lose("GET", "/pulls", "before")  # the reconcile lookup itself fails
        with self.assertRaises(pc.GitHubRestError):
            self.save(lossy)
        self.assertEqual([c for c in fake.calls if c[0] == "DELETE"], [])
        again = self.save(lossy)
        self.assertEqual((again["reconciled"], len(fake.prs)), (True, 1))

    def test_definite_rejections_still_clean_up_an_untouched_branch(self):
        fake = FakeGitHubRepo()
        real = fake.__call__

        def forbidden(method, url, headers, body, timeout):
            if method == "PUT":
                return 403, b'{"message": "Resource not accessible by integration"}'
            return real(method, url, headers, body, timeout)

        with self.assertRaises(pc.GitHubRestError):
            console(forbidden).save(OXIDEX_PATH, self.NEW, HEAD, idempotency_key="lost-0002")
        self.assertEqual(set(fake.branches), {"main"})  # GitHub confirmed nothing was committed


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

    def test_isa_inherits_the_default_architecture(self):
        defaults = {"schema": "spot-runner-patch-v1", "hardware": {"arch": "arm64"}}
        override = json.dumps({"schema": "spot-runner-patch-v1", "hardware": {"isa": ["neon"]},
                               "container": {"apt_packages": ["jq"]}})
        patch, errors, _ = pc.validate_manifest(override, path=OXIDEX_PATH, defaults=defaults)
        self.assertEqual(errors, [])
        self.assertEqual((patch.hardware.arch, patch.hardware.isa), ("arm64", ("neon",)))
        # Without the inherited architecture the same override is (correctly) invalid.
        _, errors, _ = pc.validate_manifest(override, path=OXIDEX_PATH, defaults={})
        self.assertEqual(errors[0]["field"], "hardware.isa")
        # An explicit override architecture still wins over the inherited one.
        explicit = json.dumps({"hardware": {"arch": "x86_64", "isa": ["neon"]}})
        _, errors, _ = pc.validate_manifest(explicit, path=OXIDEX_PATH, defaults=defaults)
        self.assertEqual(errors[0]["field"], "hardware.isa")

    def test_override_invalid_only_after_merging_is_rejected(self):
        defaults = {"hardware": {"arch": "x86_64", "isa": ["avx512-vbmi2"]}}
        override = json.dumps({"hardware": {"arch": "arm64", "extra_labels": ["arm"]}})
        patch, errors, _ = pc.validate_manifest(override, path=OXIDEX_PATH, defaults=defaults)
        self.assertIsNone(patch)
        self.assertEqual(errors[0]["field"], "effective")
        self.assertIn("requires arch", errors[0]["message"])
        self.assertIsNotNone(pc.validate_manifest(override, path=OXIDEX_PATH, defaults={})[0])  # fine on its own

    def test_inherited_arm64_save_and_validate_over_http_paths(self):
        fake = FakeGitHubRepo()
        fake.files[HEAD]["defaults.yaml"] = json.dumps({"schema": "spot-runner-patch-v1",
                                                        "hardware": {"arch": "arm64"}})
        override = json.dumps({"schema": "spot-runner-patch-v1", "repository": "swack-tools/oxidex",
                               "hardware": {"isa": ["neon", "sve"]}})
        c = console(fake)
        self.assertTrue(c.validate(OXIDEX_PATH, override)["valid"])
        result = c.save(OXIDEX_PATH, override, HEAD, idempotency_key="arm-key-0001")
        self.assertEqual(result["effective"]["hardware"]["arch"], "arm64")

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
        result = console(fake).save(OXIDEX_PATH, new_text, HEAD, "Add fd", idempotency_key="save-key-0001")
        self.assertEqual(result["status"], "pull_request_opened")
        self.assertEqual(result["branch"], pc.PatchConsole.branch_for(OXIDEX_PATH, "save-key-0001"))
        self.assertTrue(result["branch"].startswith("spot-dashboard/swack-tools-oxidex-"))
        self.assertNotEqual(result["branch"], pc.PatchConsole.branch_for(OXIDEX_PATH, "save-key-0002"))
        self.assertNotEqual(result["branch"], pc.PatchConsole.branch_for("defaults.yaml", "save-key-0001"))
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

    def test_save_retry_after_lost_response_reconciles_instead_of_opening_a_second_pr(self):
        fake = FakeGitHubRepo()
        new_text = OXIDEX.replace("ripgrep", "fd-find")
        first = console(fake).save(OXIDEX_PATH, new_text, HEAD, "Add fd", idempotency_key="lost-key-0001")
        # The process died before persisting the result; even if main moved on, the retry finds the same PR.
        fake.branches["main"] = NEXT
        fake.files[NEXT] = dict(fake.files[HEAD])
        again = console(fake).save(OXIDEX_PATH, new_text, HEAD, "Add fd", idempotency_key="lost-key-0001")
        self.assertEqual((again["reconciled"], again["pull_request"], again["branch"]),
                         (True, first["pull_request"], first["branch"]))
        self.assertEqual(len(fake.prs), 1)
        self.assertEqual(len([c for c in fake.calls if c[0] == "PUT"]), 1)
        with self.assertRaises(pc.IdempotencyMismatchError):  # same key, different change
            console(fake).save(OXIDEX_PATH, OXIDEX.replace("ripgrep", "bat"), HEAD, idempotency_key="lost-key-0001")
        self.assertEqual(len(fake.prs), 1)

    def test_save_retry_completes_a_partial_attempt(self):
        new_text = OXIDEX.replace("ripgrep", "fd-find")
        branch = pc.PatchConsole.branch_for(OXIDEX_PATH, "part-key-0001")
        # Crashed after creating the branch, before the commit: the retry commits and opens the PR once.
        fake = FakeGitHubRepo()
        fake.branches[branch] = HEAD
        result = console(fake).save(OXIDEX_PATH, new_text, HEAD, idempotency_key="part-key-0001")
        self.assertEqual((len(fake.prs), result["branch"]), (1, branch))
        self.assertNotIn("reconciled", result)
        # Crashed after the commit, before the PR: the retry opens the PR without committing again.
        fake = FakeGitHubRepo()
        console(fake).save(OXIDEX_PATH, new_text, HEAD, idempotency_key="part-key-0002")
        pr_branch = fake.prs.pop()["head"]
        puts = len([c for c in fake.calls if c[0] == "PUT"])
        result = console(fake).save(OXIDEX_PATH, new_text, HEAD, idempotency_key="part-key-0002")
        self.assertEqual((result["reconciled"], result["branch"], len(fake.prs)), (True, pr_branch, 1))
        self.assertEqual(len([c for c in fake.calls if c[0] == "PUT"]), puts)

    def test_hot_patch_retry_rederives_results_from_the_hosts(self):
        executor = FakeExecutor()
        real_apply = executor.apply

        def apply_and_record(h, patch):
            executor.manifests[h["name"]] = patch.to_canonical_dict()  # the host's receipt now shows the patch
            return real_apply(h, patch)

        executor.apply = apply_and_record
        c = console(executor=executor)
        c.hot_patch("swack-tools/oxidex", TARGET_SHA, [host("h1"), host("h2")])
        self.assertEqual(len(executor.applied), 2)
        status, body = c.hot_patch("swack-tools/oxidex", TARGET_SHA, [host("h1"), host("h2")])  # a lost response
        self.assertEqual((status, body["status"]), (200, "noop"))
        self.assertTrue(all(h["result"] == "noop" and "already at this patch" in h["detail"] for h in body["hosts"]))
        self.assertEqual(len(executor.applied), 2)  # nothing ran twice

    def test_github_errors_use_424_not_a_gateway_status(self):
        fake = FakeGitHubRepo()
        fake.fail[("GET", f"repos/{REPO}")] = 403
        with self.assertRaises(pc.GitHubRestError) as ctx:
            console(fake).list_manifests()
        self.assertEqual((ctx.exception.status, ctx.exception.body["code"], ctx.exception.body["upstream_status"]),
                         (424, "upstream_error", 403))

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
        self.assertEqual((args.instance_name, args.zone, args.region, args.instance_type, args.runner_prefix,
                          args.runner_count, args.runner_label, args.target, args.runner_group_id,
                          args.heavy_runner_pool, zone),
                         ("h1", "us-central1-a", "us-central1", "n2-standard-8", "oxidex", 2, "oxidex-spot-ci",
                          "swack-tools", 10, None, "us-central1-a"))  # the org scope, not the patch repo
        # Never the runner_manager defaults (asia-south2, oxidex-spot-7x27), which sync_runner_labels would apply.
        defaults = runner_manager.args_parser().parse_args(["--max-cost", "0"])
        self.assertNotEqual((args.region, args.runner_label), (defaults.region, defaults.runner_label))
        self.assertIs(args.runner_patch, patch)
        with mock.patch.object(runner_manager, "apply_live_patch", return_value="hot_patch") as apply:
            ex.apply(host("h2", heavy_runner_pool=3), patch)
            ex.apply(host("h3", registration_target="swack-tools/oxidex", runner_group_id=None), patch)
        self.assertEqual(apply.call_args_list[0][0][0].heavy_runner_pool, 3)
        self.assertEqual(apply.call_args_list[1][0][0].target, "swack-tools/oxidex")  # repo-registered runners

    def test_gcloud_executor_refuses_hosts_without_recorded_deployment_details(self):
        from lib import runner_manager

        ex = pc.GcloudHotPatchExecutor("proj", which=lambda _n: "/usr/bin/gcloud")
        patch = validate_patch_dict(json.loads(OXIDEX))
        with mock.patch.object(runner_manager, "apply_live_patch") as apply:
            for key in ("runner_label", "runner_region", "patch_target", "runners_count", "runner_prefix", "zone",
                        "registration_target", "runner_group_id"):
                bad = host("h1")
                bad.pop(key)
                with self.subTest(missing=key), self.assertRaisesRegex(pc.HostRefusedError, key):
                    ex.apply(bad, patch)
            with self.assertRaisesRegex(pc.HostRefusedError, "does not contain zone"):
                ex.apply(host("h1", runner_region="asia-south2"), patch)
            with self.assertRaisesRegex(pc.HostRefusedError, "belongs to"):
                ex.apply(host("h1", patch_target="someone/else"), patch)
        apply.assert_not_called()

    def test_hot_patch_reports_refused_hosts(self):
        executor = FakeExecutor()

        def apply(h, patch):
            if h["name"] == "legacy":
                raise pc.HostRefusedError("refused: the host record does not carry runner_label")
            return {"result": "applied"}

        executor.apply = apply
        status, body = console(executor=executor).hot_patch("swack-tools/oxidex", TARGET_SHA,
                                                            [host("new"), host("legacy")])
        self.assertEqual({h["name"]: h["result"] for h in body["hosts"]}, {"new": "applied", "legacy": "refused"})
        self.assertEqual(body["status"], "partial")

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
        status, msg, body = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key="race-key-0001")
        self.assertEqual((status, body["status"], body["pending_retry"]), (202, "pending", True))
        self.assertEqual(msg["Retry-After"], str(body["retry_after_seconds"]))
        # The same key with a different body is still rejected, while running and afterwards.
        other = self.hot_patch_body(repository="swack-tools/other")
        self.assertEqual(self.request("POST", "/api/patches/hot-patch", other, key="race-key-0001")[0], 422)
        self.executor.gate.set()
        worker.join(10)
        self.assertEqual(results[0][0], 200)
        self.assertEqual(len(self.executor.applied), 2)
        status, msg, replay = self.request("POST", "/api/patches/hot-patch", self.hot_patch_body(), key="race-key-0001")
        self.assertEqual((status, replay, msg.get("Idempotent-Replay")), (200, results[0][2], "true"))
        self.assertEqual(self.request("POST", "/api/patches/hot-patch", other, key="race-key-0001")[0], 422)
        self.assertEqual(len(self.executor.applied), 2)

    def test_save_survives_losing_the_stored_result(self):
        store = self.start()
        body = {"path": OXIDEX_PATH, "content": OXIDEX.replace("ripgrep", "fd-find"), "base_sha": HEAD}
        status, _, first = self.request("POST", "/api/patches/save", body, key="loss-key-0001")
        self.assertEqual(status, 200)
        with store._lock:  # simulate a replacement VM that never received the idempotency record
            store.state["idempotency"] = {}
        status, msg, again = self.request("POST", "/api/patches/save", body, key="loss-key-0001")
        self.assertEqual((status, again["reconciled"], again["pull_request"]), (200, True, first["pull_request"]))
        self.assertIsNone(msg.get("Idempotent-Replay"))
        self.assertEqual(len(self.fake.prs), 1)

    def test_upstream_errors_are_424_and_marked_as_dashboard_responses(self):
        self.start()
        self.fake.fail[("GET", f"repos/{REPO}")] = 403
        status, msg, body = self.request("GET", "/api/patches")
        self.assertEqual((status, body["code"], msg["X-Spot-Origin"]), (424, "upstream_error", "dashboard"))


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

    def test_upstream_errors_are_shown_not_retried(self):
        message = "GitHub PUT repos/x/contents/y returned HTTP 403: Resource not accessible by integration"
        out = self.run_ui(self.replies(save={"status": 424, "body": {"error": message, "code": "upstream_error"}}))
        self.assertIn(message, out["status"])
        self.assertEqual(len([p for p in out["posts"] if p[0] == "/api/patches/save"]), 1)

    def test_fetch_wrapper_polls_a_pending_change_with_the_same_key(self):
        from lib.dashboard_ui_assets import render_dashboard_html

        html = render_dashboard_html()
        script = html[html.index("<script>") + len("<script>"):html.rindex("</script>")]
        block = script[script.index("    function getDashboardAuthToken()"):script.index("    function showToast(msg)")]
        program = r"""
const AUTH_STORAGE_KEY = "dashboard_auth_token";
const out = {keys: [], waits: []};
globalThis.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
globalThis.localStorage = globalThis.sessionStorage;
globalThis.setTimeout = (fn, ms) => { out.waits.push(ms); fn(); return 0; };
globalThis.document = {getElementById: () => null, createElement: () => ({setAttribute() {}, style: {}}),
  body: {appendChild() {}}};
globalThis.window = {crypto: globalThis.crypto, location: {assign() {}}, prompt: undefined};
const json = (status, body) => new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
let calls = 0;
const NATIVE_FETCH = async (url, init) => {
  out.keys.push(init.headers.get("Idempotency-Key"));
  calls += 1;
  if (calls > 100) return json(202, {status: "reauth_required"});
  if (calls <= 2) return json(202, {status: "pending", pending_retry: true, retry_after_seconds: 3});
  return json(200, {status: "pull_request_opened"});
};
""" + block + r"""
(async () => {
  const res = await window.fetch("/api/patches/save", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  out.final = [res.status, (await res.json()).status];
  // A 202 that is not a pending external change (for example reauth_required) is returned as is.
  calls = 100;
  const other = await window.fetch("/api/auth/targets", {method: "POST", body: "{}"});
  out.other = [other.status, (await other.json()).status, calls];
  process.stdout.write(JSON.stringify(out));
})();
"""
        proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30, check=True)
        out = json.loads(proc.stdout)
        self.assertEqual(out["final"], [200, "pull_request_opened"])
        self.assertEqual(len(out["keys"]), 4)
        self.assertEqual(len(set(out["keys"][:3])), 1)  # the same Idempotency-Key for every poll of the save
        self.assertEqual(out["waits"], [3000, 3000])
        self.assertEqual(out["other"], [202, "reauth_required", 101])  # not a pending change: returned once

    def test_save_conflict_and_untrusted_links(self):
        out = self.run_ui(self.replies(save={"status": 409, "body": {"error": "main moved; reload"}}))
        self.assertIn("main moved; reload", out["status"])
        out = self.run_ui(self.replies(pr_url="javascript:alert(1)"))
        self.assertEqual(out["links"], [])


if __name__ == "__main__":
    unittest.main()
