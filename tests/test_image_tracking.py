"""Dashboard image rollout (SPOT_DASHBOARD_IMAGE_TRACK=main) and the control VM restart policy.

Registry, GitHub, gcloud, KV, and HTTP all go through in-memory fakes; nothing touches GHCR, GitHub, GCP, or
Cloudflare.
"""
from __future__ import annotations

import base64
import hashlib
import json
import unittest
from types import SimpleNamespace

import test_control_vm as tcv
from lib import control_vm as cv
from lib import image_tracking as it
from lib.https_transport import HttpResponse, TransportError

REPO = cv.DEFAULT_DASHBOARD_IMAGE_REPO
D_A, D_B, D_C = ("sha256:" + c * 64 for c in "abc")
IMG_A, IMG_B, IMG_C = (f"{REPO}@{d}" for d in (D_A, D_B, D_C))
SHA_A, SHA_B, SHA_C = (c * 40 for c in "123")


def tearDownModule():
    tcv._CONTROL_STATE_DIR.cleanup()  # imported for its harness; idempotent with its own tearDownModule


# --------------------------------------------------------------------------------------------
# Pure decision
# --------------------------------------------------------------------------------------------

def facts(**overrides):
    values = dict(transition_active=False, active_ready=True, cooling=False, active_digest=D_A,
                  active_revision=SHA_A, candidate_digest=D_B, candidate_revision=SHA_B, failed_digests=(),
                  workflow="success", ancestry="ahead", repository=REPO)
    values.update(overrides)
    return values


class DecisionTests(unittest.TestCase):
    def test_newer_digest_rolls(self):
        decision = it.decide(**facts())
        self.assertTrue(decision.roll)
        self.assertEqual((decision.image, decision.revision, decision.from_revision), (IMG_B, SHA_B, SHA_A))

    def test_every_guard_refuses(self):
        cases = {
            "a transition is in progress": dict(transition_active=True),
            "not healthy": dict(active_ready=False),
            "cooldown": dict(cooling=True),
            "could not be resolved": dict(candidate_digest=None),
            "active image is main": dict(candidate_digest=D_A),
            "aborted earlier": dict(failed_digests=[D_B]),
            "commit is unknown": dict(active_revision=None),
            "has no org.opencontainers.image.revision": dict(candidate_revision=None),
            "is failure": dict(workflow="failure"),
            "is pending": dict(workflow="pending"),
            "is missing": dict(workflow="missing"),
            "older than the active image": dict(ancestry="behind"),
            "the same commit": dict(ancestry="identical"),
            "not a descendant": dict(ancestry="diverged"),
            "of unknown order": dict(ancestry=None),
        }
        for reason, override in cases.items():
            decision = it.decide(**facts(**override))
            self.assertFalse(decision.roll, override)
            self.assertIn(reason, decision.reason, override)
            self.assertIsNone(decision.image)

    def test_guard_order_puts_gates_first(self):
        # A closed gate wins over everything else, so no network fact is needed while it is closed.
        decision = it.decide(**facts(transition_active=True, cooling=True, candidate_digest=None))
        self.assertIn("transition", decision.reason)

    def test_parse_image(self):
        self.assertEqual(it.parse_image(IMG_A, REPO), (D_A, None))
        self.assertEqual(it.parse_image(f"{REPO}:{SHA_A}", REPO), (None, SHA_A))
        for other in (f"{REPO}:main", "ghcr.io/other/image@" + D_A, f"docker.io/x/y@{D_A}", ""):
            self.assertEqual(it.parse_image(other, REPO), (None, None), other)

    def test_record_failed_is_bounded(self):
        cache = {}
        for i in range(it.FAILED_DIGESTS_KEPT + 5):
            it.record_failed(cache, f"sha256:{i:064d}")
        it.record_failed(cache, f"sha256:{0:064d}")
        self.assertEqual(len(cache["failed"]), it.FAILED_DIGESTS_KEPT)
        self.assertEqual(cache["failed"][-1], f"sha256:{0:064d}")


# --------------------------------------------------------------------------------------------
# Registry and GitHub clients
# --------------------------------------------------------------------------------------------

class FakeRegistryHttp:
    def __init__(self):
        self.calls = []
        config_a = json.dumps({"config": {"Labels": {it.REVISION_LABEL: SHA_A}}}).encode()
        self.blobs = {"sha256:" + hashlib.sha256(config_a).hexdigest(): config_a}
        config_digest = next(iter(self.blobs))
        manifest = json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json",
                               "config": {"digest": config_digest}}).encode()
        self.manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        index = json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
            {"digest": "sha256:" + "f" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": self.manifest_digest, "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()
        self.index_digest = "sha256:" + hashlib.sha256(index).hexdigest()
        self.manifests = {self.manifest_digest: manifest, self.index_digest: index, "main": index}
        self.lie_about_digest = False

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((url, dict(headers)))
        if url.startswith("https://ghcr.io/token?"):
            expected = base64.b64encode(b"x-access-token:pat").decode()
            assert headers["Authorization"] == f"Basic {expected}"
            assert "scope=repository%3Aswack-tools%2Fspot-dashboard%3Apull" in url
            return HttpResponse(200, {}, b'{"token": "registry-token"}')
        if url.startswith("https://ghcr.io/v2/swack-tools/spot-dashboard/manifests/"):
            assert headers["Authorization"] == "Bearer registry-token"
            ref = url.rsplit("/", 1)[1]
            body = self.manifests[ref]
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            return HttpResponse(200, {"docker-content-digest": ("sha256:" + "0" * 64) if self.lie_about_digest
                                      else digest}, body)
        if url.startswith("https://ghcr.io/v2/swack-tools/spot-dashboard/blobs/"):
            digest = url.rsplit("/", 1)[1]
            return HttpResponse(307, {"location": f"https://pkg-containers.githubusercontent.com/blob/{digest}"}, b"")
        if url.startswith("https://pkg-containers.githubusercontent.com/blob/"):
            assert "Authorization" not in headers, "the registry token must never follow a redirect"
            return HttpResponse(200, {}, self.blobs[url.rsplit("/", 1)[1]])
        raise AssertionError(url)


class RegistryClientTests(unittest.TestCase):
    def test_resolve_and_revision_through_an_index_and_a_redirect(self):
        http = FakeRegistryHttp()
        client = it.RegistryClient(REPO, "pat", transport=http)
        self.assertEqual(client.resolve("main"), http.index_digest)
        self.assertEqual(client.revision(http.index_digest), SHA_A)
        self.assertEqual(sum(1 for url, _ in http.calls if "/token?" in url), 1)  # token cached

    def test_digests_are_verified(self):
        http = FakeRegistryHttp()
        http.lie_about_digest = True
        with self.assertRaisesRegex(it.TrackingError, "differs from its bytes"):
            it.RegistryClient(REPO, "pat", transport=http).resolve("main")
        http = FakeRegistryHttp()
        http.manifests[D_C] = b"{}"  # bytes that don't hash to the requested digest
        with self.assertRaisesRegex(it.TrackingError, "does not hash"):
            it.RegistryClient(REPO, "pat", transport=http).manifest(D_C)

    def test_missing_label_and_failures(self):
        http = FakeRegistryHttp()
        unlabeled = json.dumps({"config": {"Labels": {}}}).encode()
        config_digest = "sha256:" + hashlib.sha256(unlabeled).hexdigest()
        http.blobs[config_digest] = unlabeled
        manifest = json.dumps({"config": {"digest": config_digest}}).encode()
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        http.manifests[digest] = manifest
        self.assertIsNone(it.RegistryClient(REPO, "pat", transport=http).revision(digest))

        def down(*a):
            raise TransportError("boom")
        with self.assertRaises(it.TrackingError):
            it.RegistryClient(REPO, "pat", transport=down).resolve("main")
        with self.assertRaises(ValueError):
            it.RegistryClient("docker.io/x/y", "pat")


class GitHubClientTests(unittest.TestCase):
    def client(self, runs=None, compare=None, status=200):
        calls = []

        def http(method, url, headers, body, timeout):
            calls.append(url)
            assert headers["Authorization"] == "Bearer pat"
            if "/actions/workflows/dashboard-image.yml/runs?" in url:
                return HttpResponse(status, {}, json.dumps({"workflow_runs": runs or []}).encode())
            if "/compare/" in url:
                return HttpResponse(status, {}, json.dumps({"status": compare}).encode())
            raise AssertionError(url)
        return it.GitHubClient("swack-tools/spot-github-runners", "pat", transport=http), calls

    def run(self, conclusion, status="completed", branch="main", sha=SHA_B):
        return {"head_sha": sha, "head_branch": branch, "status": status, "conclusion": conclusion}

    def test_workflow_verdicts(self):
        cases = [([self.run("success")], "success"), ([self.run("failure"), self.run("success")], "success"),
                 ([self.run(None, status="in_progress")], "pending"), ([self.run("failure")], "failure"),
                 ([], "missing"), ([self.run("success", branch="feature")], "missing"),
                 ([self.run("success", sha=SHA_C)], "missing")]
        for runs, verdict in cases:
            client, calls = self.client(runs=runs)
            self.assertEqual(client.workflow_verdict(SHA_B), verdict, runs)
            self.assertIn(f"head_sha={SHA_B}", calls[0])
            self.assertIn("branch=main", calls[0])
        client, _ = self.client(status=500)
        with self.assertRaises(it.TrackingError):
            client.workflow_verdict(SHA_B)
        with self.assertRaises(it.TrackingError):
            client.workflow_verdict("main")

    def test_compare(self):
        for status in ("ahead", "behind", "identical", "diverged"):
            client, calls = self.client(compare=status)
            self.assertEqual(client.compare(SHA_A, SHA_B), status)
            self.assertIn(f"/compare/{SHA_A}...{SHA_B}", calls[0])
        client, _ = self.client(compare="weird")
        with self.assertRaises(it.TrackingError):
            client.compare(SHA_A, SHA_B)


# --------------------------------------------------------------------------------------------
# Manager integration
# --------------------------------------------------------------------------------------------

class FakeRegistry:
    def __init__(self):
        self.tags = {"main": D_B}
        self.revisions = {D_A: SHA_A, D_B: SHA_B, D_C: SHA_C}
        self.calls = []

    def resolve(self, tag):
        self.calls.append(("resolve", tag))
        return self.tags[tag]

    def revision(self, digest):
        self.calls.append(("revision", digest))
        return self.revisions.get(digest)


class FakeGitHub:
    def __init__(self):
        self.verdicts = {SHA_B: "success", SHA_C: "success"}
        self.order = {(SHA_A, SHA_B): "ahead", (SHA_A, SHA_C): "ahead", (SHA_B, SHA_C): "ahead"}
        self.calls = []

    def workflow_verdict(self, sha):
        self.calls.append(("workflow", sha))
        return self.verdicts.get(sha, "missing")

    def compare(self, base, head):
        self.calls.append(("compare", base, head))
        return self.order.get((base, head), "behind")


class WatchdogGcloud(tcv.FakeGcloud):
    """FakeGcloud plus instance metadata (dashboard-image) and set-scheduling."""

    def __init__(self, events):
        super().__init__(events)
        self.images: dict[str, str] = {}

    def __call__(self, argv):
        verb = argv[2:4]
        if verb == ["instances", "set-scheduling"]:
            self.calls.append(list(argv))
            self.events.append(("set-scheduling", argv[4]))
            assert "--no-restart-on-failure" in argv and "--restart-on-failure" not in argv
            self.vms[argv[4]].setdefault("scheduling", {})["automaticRestart"] = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if verb == ["instances", "describe"] and "--format=json(id,metadata.items)" in argv:
            self.calls.append(list(argv))
            row = self.vms.get(argv[4])
            if row is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="was not found")
            items = [{"key": "dashboard-image", "value": self.images.get(argv[4], "")}]
            return SimpleNamespace(returncode=0, stdout=json.dumps({"id": row["id"], "metadata": {"items": items}}),
                                   stderr="")
        result = super().__call__(argv)
        if verb == ["instances", "create"] and result.returncode == 0:
            metadata = next(a for a in argv if a.startswith("--metadata="))[len("--metadata="):]
            self.images[argv[4]] = dict(item.split("=", 1) for item in metadata.split(","))["dashboard-image"]
        return result

    def create_image(self):
        create = [c for c in self.calls if c[2:4] == ["instances", "create"]][-1]
        metadata = next(a for a in create if a.startswith("--metadata="))[len("--metadata="):]
        return dict(item.split("=", 1) for item in metadata.split(","))["dashboard-image"]


class TrackingHarness(tcv.Harness):
    def __init__(self, **overrides):
        values = dict(offers=tcv.STANDARD_OFFERS, provisioning="standard", regions=("us-central1",),
                      image_track="main", image_track_token="pat")
        values.update(overrides)
        super().__init__(**values)
        self.gcloud = WatchdogGcloud(self.events)
        self.registry = FakeRegistry()
        self.github = FakeGitHub()

    def manager(self):
        tracker = it.ImageTracker(REPO, self.registry, self.github, lambda: self.clock[0])
        return cv.ControlVmManager(self.config, self.state, self.save, gcloud=self.gcloud, kv=self.kv,
                                   requester=self.http, offer_source=lambda: list(self.offers),
                                   clock=lambda: self.clock[0], image_tracker=tracker)

    def active(self, image=IMG_A, slot="a"):
        self.gcloud.add(slot, machine="e2-micro", provisioning="STANDARD")
        self.gcloud.images[f"spot-control-{slot}"] = image
        self.kv.set_origin(slot)
        self.http.healthy(slot, version=7)

    @property
    def image(self):
        return self.state.get(cv.STATE_KEY, {}).get("image", {})

    def finish(self):
        """Drive a started rollout to completion."""
        self.gcloud.status("b", "RUNNING")
        self.http.healthy("b")
        self.tick()
        self.clock[0] += cv.PROPAGATION_SECONDS + 1
        self.tick()


class RolloutTests(unittest.TestCase):
    def test_newer_main_digest_rolls_out_through_make_before_break(self):
        h = TrackingHarness()
        h.active()
        plan = h.tick()
        self.assertEqual((plan.action, plan.slot, plan.target_slot), ("rotate", "a", "b"))
        self.assertIn("image rollout", plan.reason)
        t = h.transition
        self.assertEqual((t["mode"], t["dashboard_image"]), (cv.MODE_PRICE, IMG_B))
        self.assertEqual(t["image_rollout"], {"image": IMG_B, "digest": D_B, "revision": SHA_B,
                                              "from_revision": SHA_A})
        self.assertEqual(h.gcloud.create_image(), IMG_B)
        self.assertEqual(h.image["active"]["image"], IMG_A)  # recorded from a's metadata
        h.finish()
        self.assertIsNone(h.transition)
        self.assertEqual(h.mutations(), [("create", "spot-control-b", "us-central1-a"), ("freeze", "a"),
                                         ("import", "b"), ("origin", "b"), ("delete", "spot-control-a")])
        self.assertEqual((h.image["active"]["image"], h.image["active"]["slot"]), (IMG_B, "b"))
        # Up to date afterwards, and the cooldown keeps the registry quiet meanwhile.
        h.registry.calls.clear()
        self.assertIn("cooldown", h.tick().reason)
        self.assertEqual(h.registry.calls, [])
        h.clock[0] += cv.ROTATION_COOLDOWN_SECONDS
        plan = h.tick()
        self.assertEqual(plan.action, "noop")
        self.assertIn("active image is main", plan.reason)

    def test_same_digest_needs_no_github_call(self):
        h = TrackingHarness()
        h.active()
        h.registry.tags["main"] = D_A
        plan = h.tick()
        self.assertEqual(plan.action, "noop")
        self.assertIn("active image is main", plan.reason)
        self.assertEqual(h.github.calls, [])
        self.assertEqual(h.mutations(), [])

    def test_older_or_identical_commit_never_rolls(self):
        for order, words in (("behind", "older than the active image"), ("identical", "the same commit"),
                             ("diverged", "not a descendant")):
            h = TrackingHarness()
            h.active()
            h.github.order[(SHA_A, SHA_B)] = order
            plan = h.tick()
            self.assertEqual(plan.action, "noop", order)
            self.assertIn(words, plan.reason)
            self.assertIsNone(h.transition)
            self.assertEqual(h.mutations(), [])
            # The order of two commits is final: no second compare call on the next check.
            before = len(h.github.calls)
            h.tick()
            self.assertEqual(len(h.github.calls), before)

    def test_failed_or_pending_workflow_waits_and_is_checked_again(self):
        h = TrackingHarness()
        h.active()
        h.github.verdicts[SHA_B] = "pending"
        self.assertIn("is pending", h.tick().reason)
        h.github.verdicts[SHA_B] = "failure"
        self.assertIn("is pending", h.tick().reason)  # cached inside the recheck interval
        h.clock[0] += it.VERDICT_RECHECK_SECONDS
        self.assertIn("is failure", h.tick().reason)
        self.assertIsNone(h.transition)
        h.github.verdicts[SHA_B] = "success"  # a re-run succeeded
        h.clock[0] += it.VERDICT_RECHECK_SECONDS
        self.assertEqual(h.tick().action, "rotate")

    def test_aborted_rollout_backs_off_that_digest_until_a_new_one(self):
        h = TrackingHarness()
        h.active()
        h.tick()
        h.http.unhealthy("b")
        h.gcloud.status("b", "RUNNING")
        h.clock[0] += cv.HEALTH_TIMEOUT_SECONDS
        h.tick()  # never healthy: abort, a keeps serving
        self.assertIsNone(h.transition)
        self.assertEqual(h.image["failed"], [D_B])
        self.assertNotIn("spot-control-b", h.gcloud.vms)
        self.assertEqual(h.kv.origin_slot(), "a")
        h.clock[0] += cv.ROTATION_COOLDOWN_SECONDS
        plan = h.tick()
        self.assertEqual(plan.action, "noop")
        self.assertIn("aborted earlier", plan.reason)
        h.registry.tags["main"] = D_C
        plan = h.tick()
        self.assertEqual(plan.action, "rotate")
        self.assertEqual(h.transition["dashboard_image"], IMG_C)

    def test_no_rollout_while_a_transition_runs_or_the_other_slot_exists(self):
        h = TrackingHarness()
        h.active()
        h.state[cv.STATE_KEY] = {"transition": {"id": "t", "mode": cv.MODE_RECOVER, "step": "wait_health",
                                                "old_slot": "a", "new_slot": "b", "new_vm": {"name": "spot-control-b"},
                                                "old_vm": {"name": "spot-control-a", "zone": "us-central1-a",
                                                           "instance_id": "id-a"}}}
        self.assertTrue(h.tick().resume)
        self.assertEqual(h.registry.calls, [])
        h = TrackingHarness()
        h.active()
        h.gcloud.add("b", machine="e2-micro", provisioning="STANDARD")
        plan = h.tick()
        self.assertEqual(plan.action, "noop")
        self.assertIn("not healthy, or the other slot is occupied", plan.reason)
        self.assertEqual(h.registry.calls, [])

    def test_unhealthy_active_vm_never_rolls(self):
        h = TrackingHarness()
        h.active()
        h.http.unhealthy("a")
        self.assertEqual(h.tick().action, "wait")
        self.assertEqual(h.registry.calls, [])

    def test_active_image_of_unknown_commit_never_rolls(self):
        h = TrackingHarness()
        h.active(image="ghcr.io/other/dashboard@" + D_A)
        plan = h.tick()
        self.assertIn("commit is unknown", plan.reason)
        self.assertIsNone(h.transition)

    def test_recovery_recreates_the_recorded_image_not_the_configured_one(self):
        h = TrackingHarness()
        h.active(image=IMG_B)
        h.registry.tags["main"] = D_B
        h.tick()  # records a's image (main already)
        self.assertEqual(h.image["active"]["image"], IMG_B)
        h.gcloud.vms.pop("spot-control-a")
        h.tick()
        h.clock[0] += cv.START_GRACE_SECONDS
        plan = h.tick()
        self.assertEqual(plan.action, "recreate_other_slot")
        self.assertEqual(h.transition["dashboard_image"], IMG_B)
        self.assertNotIn("image_rollout", h.transition)
        self.assertEqual(h.gcloud.create_image(), IMG_B)  # SPOT_DASHBOARD_IMAGE is IMG_A

    def recover_from(self, h, image):
        h.active(image=image)
        h.registry.tags.update({"main": D_B, SHA_B: D_B})
        h.tick()  # records a's image
        self.assertEqual(h.image["active"]["image"], image)
        h.gcloud.vms.pop("spot-control-a")
        h.tick()
        h.clock[0] += cv.START_GRACE_SECONDS
        return h.tick()

    def test_recovery_keeps_a_recorded_commit_tag_on_the_standard_vm(self):
        h = TrackingHarness()
        tag = f"{REPO}:{SHA_B}"
        self.assertEqual(self.recover_from(h, tag).action, "recreate_other_slot")
        self.assertEqual(h.transition["dashboard_image"], tag)
        self.assertEqual(h.gcloud.create_image(), tag)  # never the configured IMG_A

    def test_recovery_resolves_a_recorded_commit_tag_for_spot(self):
        h = TrackingHarness(provisioning="spot", offers=tcv.OFFERS)
        h.gcloud = WatchdogGcloud(h.events)
        tag = f"{REPO}:{SHA_B}"
        h.active = lambda image=IMG_A, slot="a": (h.gcloud.add(slot, provisioning="SPOT"),
                                                  h.gcloud.images.__setitem__(f"spot-control-{slot}", image),
                                                  h.kv.set_origin(slot), h.http.healthy(slot, version=7))
        self.assertEqual(self.recover_from(h, tag).action, "recreate_other_slot")
        self.assertEqual(h.transition["dashboard_image"], IMG_B)
        self.assertEqual(h.image["active"]["resolved_image"], IMG_B)
        self.assertEqual(h.gcloud.create_image(), IMG_B)

    def test_unresolvable_commit_tag_on_spot_waits_instead_of_downgrading(self):
        h = TrackingHarness(provisioning="spot", offers=tcv.OFFERS)
        tag = f"{REPO}:{SHA_B}"
        h.gcloud.add("a", provisioning="SPOT")
        h.gcloud.images["spot-control-a"] = tag
        h.kv.set_origin("a")
        h.http.healthy("a", version=7)
        h.state[cv.STATE_KEY] = {"image": {"active": {"slot": "a", "instance_id": "id-a", "image": tag}}}

        def down(tag):
            raise it.TrackingError("ghcr down")
        h.registry.resolve = down
        h.gcloud.vms.pop("spot-control-a")
        h.tick()
        h.clock[0] += cv.START_GRACE_SECONDS
        h.tick()
        # The transition keeps the commit tag, which a Spot create refuses: it waits, never creating IMG_A.
        self.assertEqual(h.transition["dashboard_image"], tag)
        self.assertIn("pinned by digest", h.transition["error"])
        self.assertFalse([c for c in h.gcloud.calls if c[2:4] == ["instances", "create"]])

    def test_no_rollout_from_a_standard_vm_when_spot_is_configured(self):
        h = TrackingHarness(provisioning="spot", offers=tcv.OFFERS)
        h.active()  # standard e2-micro serving; :main is newer
        with self.assertLogs("spot_runner_autoscaler.control_vm", "WARNING") as logs:
            plan = h.tick()
        self.assertEqual(plan.action, "noop")
        self.assertIn("run rotate to move back to Spot", plan.reason)  # the planner's explicit-rotate reason stays
        self.assertIn("image rollout held", plan.reason)
        self.assertIn("an explicit rotate is required", "\n".join(logs.output))
        self.assertIsNone(h.transition)
        self.assertEqual(h.registry.calls, [])
        self.assertEqual(h.mutations(), [])

    def test_no_rollout_from_a_spot_vm_when_standard_is_configured(self):
        h = TrackingHarness()
        h.gcloud.add("a", machine="e2-small", provisioning="SPOT")
        h.gcloud.images["spot-control-a"] = IMG_A
        h.kv.set_origin("a")
        h.http.healthy("a", version=7)
        h.state[cv.STATE_KEY] = {"last_transition_end_at": h.clock[0] - 10}  # inside the cooldown: a noop
        plan = h.tick()
        self.assertEqual(plan.action, "noop")
        self.assertIn("image rollout held", plan.reason)
        self.assertIsNone(h.transition)
        self.assertEqual(h.registry.calls, [])
        # After the cooldown the planner's own mode switch runs, never an image rollout.
        h.clock[0] += cv.ROTATION_COOLDOWN_SECONDS
        plan = h.tick()
        self.assertEqual(plan.action, "rotate")
        self.assertIn("SPOT_CONTROL_PROVISIONING=standard", plan.reason)
        self.assertNotIn("image_rollout", h.transition)
        self.assertNotEqual(h.transition["dashboard_image"], IMG_B)

    def test_unknown_model_holds_a_standard_rollout(self):
        h = TrackingHarness()
        h.gcloud.add("a", machine="e2-micro")  # no scheduling in the inventory row
        h.gcloud.images["spot-control-a"] = IMG_A
        h.kv.set_origin("a")
        h.http.healthy("a", version=7)
        plan = h.tick()
        self.assertEqual(plan.action, "noop")
        self.assertIn("image rollout held", plan.reason)
        self.assertIsNone(h.transition)

    def test_a_resumed_rollout_creates_its_pinned_image_on_any_controller(self):
        h = tcv.Harness(offers=tcv.STANDARD_OFFERS, provisioning="standard", regions=("us-central1",))
        h.gcloud.add("a", machine="e2-micro", provisioning="STANDARD")
        h.kv.set_origin("a")
        h.http.healthy("a", version=7)
        h.state[cv.STATE_KEY] = {"transition": {
            "id": "t1", "mode": cv.MODE_PRICE, "old_slot": "a", "new_slot": "b", "step": "create",
            "old_vm": {"name": "spot-control-a", "zone": "us-central1-a", "instance_id": "id-a"},
            "new_vm": {"name": "spot-control-b", "zone": "", "machine_type": ""}, "offer": None,
            "provisioning": "standard", "excluded_zones": [], "failed_offers": [], "failed_offers_at": {},
            "started_at": h.clock[0], "step_started_at": h.clock[0], "updated_at": h.clock[0], "attempts": 0,
            "create_failures": 0, "freeze_requested": False, "frozen_version": None, "import_skipped": False,
            "cutover_attempted": False, "cutover_at": None, "abort_reason": None, "error": None, "reason": "test",
            "dashboard_image": IMG_C, "image_rollout": {"image": IMG_C, "digest": D_C}}}
        h.tick()  # tracking is off on this controller, and its SPOT_DASHBOARD_IMAGE is IMG_A
        create = [c for c in h.gcloud.calls if c[2:4] == ["instances", "create"]][-1]
        self.assertIn(f"dashboard-image={IMG_C}", next(a for a in create if a.startswith("--metadata=")))

    def test_missing_token_or_bad_settings(self):
        h = TrackingHarness(image_track_token="")
        h.active()
        manager = cv.ControlVmManager(h.config, h.state, h.save, gcloud=h.gcloud, kv=h.kv, requester=h.http,
                                      offer_source=lambda: list(h.offers), clock=lambda: h.clock[0])
        plan = manager.tick(apply=True)
        self.assertEqual(plan.action, "noop")
        self.assertIn("SPOT_DASHBOARD_IMAGE_TRACK_TOKEN is not set", plan.reason)
        self.assertIn("must be empty or main", " ".join(cv.ControlConfig(project="p", image_track="latest")
                                                         .runtime_problems()))
        self.assertIn("only swack-tools/spot-github-runners publishes", " ".join(
            cv.ControlConfig(project="p", image_track="main", dashboard_repo="me/fork").runtime_problems()))
        env_config = cv.ControlConfig.from_env("p", {"SPOT_DASHBOARD_IMAGE_TRACK": " Main ",
                                                     "SPOT_DASHBOARD_IMAGE_TRACK_TOKEN": "pat"})
        self.assertTrue(env_config.tracking)
        self.assertNotIn("pat", repr(env_config))

    def test_registry_failure_changes_nothing(self):
        h2 = TrackingHarness()
        h2.active()
        h2.registry.resolve = lambda tag: (_ for _ in ()).throw(it.TrackingError("ghcr down"))
        plan = h2.tick()
        self.assertEqual(plan.action, "noop")
        self.assertIn("ghcr down", plan.reason)
        self.assertEqual(h2.mutations(), [])

    def test_preview_rolls_nothing_and_saves_nothing(self):
        h = TrackingHarness()
        h.active()
        plan = h.tick(apply=False)
        self.assertEqual(plan.action, "rotate")
        self.assertEqual(h.mutations(), [])
        self.assertEqual(h.state, {})
        self.assertEqual(h.saves, 0)


class RestartPolicyTests(unittest.TestCase):
    def test_create_command_keeps_automatic_restart_off(self):
        config = tcv.config(provisioning="standard", regions=("us-central1",))
        command = cv.create_command(config, "a", tcv.micro(), "/tmp/s.sh")
        self.assertIn("--no-restart-on-failure", command)
        self.assertNotIn("--restart-on-failure", command)
        spot = cv.create_command(tcv.config(), "a", tcv.offer(), "/tmp/s.sh")
        self.assertFalse([a for a in spot if "restart-on-failure" in a])  # Spot VMs never restart by themselves

    def test_inventory_reads_automatic_restart(self):
        self.assertIn("scheduling.automaticRestart", " ".join(cv.list_command("p")))
        base = {"name": "spot-control-a", "zone": "z/us-central1-a", "status": "RUNNING",
                "labels": {"spot-role": "control", "spot-control-slot": "a"}}
        for scheduling, expected in (({"automaticRestart": True}, True), ({"automaticRestart": False}, False),
                                     ({}, None), (None, None), ({"automaticRestart": "true"}, None)):
            row = dict(base, scheduling=scheduling) if scheduling is not None else dict(base)
            self.assertIs(cv.parse_control_vms([row])[0].automatic_restart, expected, scheduling)

    def harness_with_auto_restart(self):
        h = TrackingHarness(image_track="")
        h.gcloud.add("a", machine="e2-micro", provisioning="STANDARD")
        h.gcloud.vms["spot-control-a"]["scheduling"]["automaticRestart"] = True
        h.kv.set_origin("a")
        h.http.healthy("a")
        return h

    def test_automatic_restart_is_turned_off_and_a_preview_is_not_idle(self):
        h = self.harness_with_auto_restart()
        h.gcloud.vms["spot-control-a"]["scheduling"]["automaticRestart"] = False
        h.tick()  # settle the state: from here on a preview of a healthy VM is idle
        manager = h.manager()
        self.assertTrue(manager.preview_is_idle(manager.tick(apply=False), h.state))
        h.gcloud.vms["spot-control-a"]["scheduling"]["automaticRestart"] = True
        h.events.clear()
        manager = h.manager()
        plan = manager.tick(apply=False)
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(manager.preview_actions, ["turn off automatic restart on spot-control-a"])
        self.assertFalse(manager.preview_is_idle(plan, h.state), "a pending set-scheduling is not idle")
        h.tick()
        self.assertEqual(h.events, [("set-scheduling", "spot-control-a")])
        self.assertFalse(h.gcloud.vms["spot-control-a"]["scheduling"]["automaticRestart"])
        manager = h.manager()
        plan = manager.tick(apply=False)
        self.assertEqual(manager.preview_actions, [])
        h.tick()
        self.assertEqual(h.events.count(("set-scheduling", "spot-control-a")), 1)

    def test_recovery_fence_waits_while_a_stopped_old_vm_could_restart_by_itself(self):
        h = tcv.ResumeFromPersistedStepTests.setup_rotation(None, "cutover", mode=cv.MODE_RECOVER, kv_caught_up=True,
                                                            freeze_requested=True, frozen_version=7)
        h.gcloud = WatchdogGcloud(h.events)
        h.gcloud.add("a", status="TERMINATED", machine="e2-micro", provisioning="STANDARD")
        h.gcloud.vms["spot-control-a"]["scheduling"]["automaticRestart"] = True
        tcv.ResumeFromPersistedStepTests.with_new_vm(None, h)
        h.tick()
        self.assertNotIn(("origin", "b"), h.events, "a crash-TERMINATED VM with automatic restart is no fence")
        self.assertIn(("set-scheduling", "spot-control-a"), h.events)
        self.assertIn("automatic restart is on", h.transition["error"])
        self.assertEqual(h.transition["step"], "cutover")
        h.tick()  # the inventory now confirms automaticRestart false while it is still TERMINATED
        self.assertIn(("origin", "b"), h.events)

    def test_rollback_fence_waits_for_a_stopped_replacement_to_lose_automatic_restart(self):
        h = tcv.ResumeFromPersistedStepTests.rollback_setup(tcv.RollbackFenceTests())
        h.gcloud = WatchdogGcloud(h.events)
        h.gcloud.add("a", machine="e2-medium")
        tcv.ResumeFromPersistedStepTests.with_new_vm(None, h, status="TERMINATED", healthy=False)
        h.gcloud.vms["spot-control-b"]["scheduling"] = {"provisioningModel": "STANDARD", "automaticRestart": True}
        h.http.unhealthy("b")
        h.clock[0] += cv.REPLACEMENT_READY_GRACE_SECONDS
        h.state[cv.STATE_KEY]["transition"].update(step="abort", abort_reason="test")
        h.tick()
        self.assertNotIn(("unfreeze", "a"), h.events)
        self.assertNotIn(("origin", "a"), h.events)
        self.assertIn(("set-scheduling", "spot-control-b"), h.events)
        h.tick()
        self.assertIn(("origin", "a"), h.events)


if __name__ == "__main__":
    unittest.main()
