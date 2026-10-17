"""Control VM lifecycle: planner decision table, make-before-break executor, startup script checks.

Every provider, KV, and HTTP interaction goes through in-memory fakes; nothing touches GCP,
Cloudflare, or GitHub.
"""
from __future__ import annotations

import copy
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib import control_vm as cv
from lib import edge_state
from lib.edge_state import (
    KV_ORIGIN_KEY,
    KV_STATE_KEY,
    OriginRecord,
    build_snapshot,
    verify_control_token,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cloud-init" / "control-vm-startup.sh"
_CONTROL_STATE_DIR = tempfile.TemporaryDirectory()
_CONTROL_STATE_ENV = patch.dict("os.environ", {cv.ENV_CONTROL_STATE_DIR: _CONTROL_STATE_DIR.name})


def setUpModule():
    # Never let a test read or write the operator's real project-scoped control VM state.
    _CONTROL_STATE_ENV.start()


def tearDownModule():
    _CONTROL_STATE_ENV.stop()
    _CONTROL_STATE_DIR.cleanup()
IMAGE = "ghcr.io/swack-tools/spot-dashboard@sha256:" + "a" * 64
SECRET = "control-secret-for-tests"


def config(**overrides):
    values = dict(project="proj", dashboard_image=IMAGE, cloudflare_account_id="acct123",
                  cloudflare_kv_namespace_id="ns456", access_client_id="access-id",
                  access_client_secret="access-secret", control_secret=SECRET)
    values.update(overrides)
    return cv.ControlConfig(**values)


def offer(machine="e2-small", zone="us-central1-a", daily="0.10"):
    return cv.ControlOffer(machine, zone.rsplit("-", 1)[0], zone, Decimal(daily))


OFFERS = [offer("e2-small", "us-central1-a", "0.10"), offer("e2-small", "us-east1-b", "0.12"),
          offer("e2-medium", "us-central1-a", "0.40")]


class FakeGcloud:
    def __init__(self, events):
        self.events = events
        self.vms: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.fail_create_zones: set[str] = set()
        self.fail_create_offers: set[tuple[str, str]] = set()  # (machine type, zone) stockouts
        self.create_attempts: list[tuple[str, str]] = []  # (machine type, zone) of every create call
        self.start_rc = 0
        self.stop_rc = 0
        self.delete_rc = 0
        self.scripts: list[str] = []
        self.land_but_fail_zones: set[str] = set()  # create succeeds in GCE but gcloud exits nonzero
        self.describe_error: str | None = None  # ambiguous describe failure (not a 404)
        self.extra_rows: list[dict] = []  # additional inventory rows (for duplicate-name tests)
        self.list_stderr = ""  # diagnostics gcloud printed while still exiting 0 (partial results)
        self.create_ids: list[str] = []  # instance ids for the next creates (default id-<slot>)
        self.create_returns_row = True  # create prints the new instance (with its id) as JSON

    def add(self, slot, status="RUNNING", zone="us-central1-a", machine="e2-small", instance_id=None,
            provisioning=None):
        name = f"spot-control-{slot}"
        self.vms[name] = {"name": name, "zone": f"projects/proj/zones/{zone}", "status": status,
                          "labels": {"spot-role": "control", "spot-control-slot": slot},
                          "id": instance_id or f"id-{slot}", "machineType": f"zones/{zone}/machineTypes/{machine}"}
        if provisioning:  # None: an inventory row without scheduling (model unknown)
            self.vms[name]["scheduling"] = {"provisioningModel": provisioning,
                                            "preemptible": False}

    def recreate(self, slot, instance_id):
        """Simulate an operator deleting and recreating a slot VM under the same name."""
        self.vms[f"spot-control-{slot}"]["id"] = instance_id

    def status(self, slot, status):
        self.vms[f"spot-control-{slot}"]["status"] = status

    def __call__(self, argv):
        self.calls.append(list(argv))
        verb = argv[2:4]
        if verb == ["instances", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(list(self.vms.values()) + self.extra_rows),
                                   stderr=self.list_stderr)
        if verb == ["machine-types", "list"]:
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        name = argv[4]
        zone = next(a.split("=", 1)[1] for a in argv if a.startswith("--zone="))
        if verb == ["instances", "create"]:
            script = next(a.split("=", 2)[2] for a in argv if a.startswith("--metadata-from-file="))
            self.scripts.append(Path(script).read_text(encoding="utf-8"))
            machine = next(a.split("=", 1)[1] for a in argv if a.startswith("--machine-type="))
            self.create_attempts.append((machine, zone))
            if zone in self.fail_create_zones or (machine, zone) in self.fail_create_offers:
                self.events.append(("create_failed", name, zone))
                return SimpleNamespace(returncode=1, stdout="", stderr="ZONE_RESOURCE_POOL_EXHAUSTED")
            slot = name.rsplit("-", 1)[1]
            model = next((a.split("=", 1)[1] for a in argv if a.startswith("--provisioning-model=")), None)
            self.add(slot, status="PROVISIONING", zone=zone, machine=machine,
                     instance_id=self.create_ids.pop(0) if self.create_ids else None, provisioning=model)
            self.events.append(("create", name, zone))
            if zone in self.land_but_fail_zones:
                return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: operation polling failed")
            rows = [dict(self.vms[name])] if self.create_returns_row else []
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        if verb == ["instances", "describe"]:
            self.events.append(("describe", name, zone))
            if self.describe_error:
                return SimpleNamespace(returncode=1, stdout="", stderr=self.describe_error)
            row = self.vms.get(name)
            if row and row["zone"].rsplit("/", 1)[-1] == zone:
                return SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr="")
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr=f"ERROR: The resource 'projects/proj/zones/{zone}/instances/{name}' "
                                          "was not found")
        if verb == ["instances", "start"]:
            self.events.append(("start", name))
            if self.start_rc == 0:
                self.vms[name]["status"] = "STAGING"
            return SimpleNamespace(returncode=self.start_rc, stdout="", stderr="stockout" if self.start_rc else "")
        if verb == ["instances", "stop"]:
            self.events.append(("stop", name))
            if self.stop_rc == 0:
                self.vms[name]["status"] = "STOPPING"
            return SimpleNamespace(returncode=self.stop_rc, stdout="", stderr="")
        if verb == ["instances", "delete"]:
            self.events.append(("delete", name))
            if self.delete_rc == 0:
                self.vms.pop(name, None)
            return SimpleNamespace(returncode=self.delete_rc, stdout="", stderr="")
        raise AssertionError(f"unexpected gcloud call {argv}")

    def mutating(self):
        return [c for c in self.calls if c[2:4] not in (["instances", "list"], ["machine-types", "list"])]


class FakeKv:
    def __init__(self, events):
        self.events = events
        self.store: dict[str, str] = {}
        self.fail_origin_verify = False
        self.land_failed_write = False
        self.restore_fails = False
        self.get_error = False
        self.puts: list[tuple[str, str]] = []

    def get(self, key):
        if self.get_error:
            raise cv.KvError("KV down")
        return self.store.get(key)

    def put(self, key, value):
        self.puts.append((key, value))
        self.store[key] = value

    def put_verified(self, key, value):
        """First write after ``fail_origin_verify`` is unverified (optionally landing anyway)."""
        self.events.append(("origin", json.loads(value)["slot"]))
        self.puts.append((key, value))
        if self.fail_origin_verify is True:
            self.fail_origin_verify = "done"
            if self.land_failed_write:
                self.store[key] = value
            return False
        if self.fail_origin_verify == "done" and self.restore_fails:
            return False
        self.store[key] = value
        return True

    def origin_slot(self):
        record = OriginRecord.from_json(self.store.get(KV_ORIGIN_KEY))
        return record.slot if record else None

    def set_origin(self, slot):
        self.store[KV_ORIGIN_KEY] = OriginRecord.for_slot(slot, now=1).to_json()


class FakeHttp:
    def __init__(self, events, clock):
        self.events = events
        self.clock = clock
        self.health: dict[str, tuple[int, dict]] = {}
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.versions = {"a": 7, "b": 0}
        self.freeze_status = 200
        self.import_status = 200
        self.unfreeze_status = 200
        self.snapshot_status = 200

    def healthy(self, slot, version=0):
        self.health[slot] = (200, {"status": "ok", "hydrated": True, "frozen": False, "state_version": version,
                                   "slot": slot, "kv_writer": False})

    def unhealthy(self, slot):
        self.health[slot] = (503, {"error": "control_vm_unavailable", "status": "starting", "hydrated": False,
                                   "frozen": False, "state_version": 0, "slot": slot, "kv_writer": False})

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        match = re.match(r"https://spot-origin-([ab])\.swacktech\.com(/.*)$", url)
        assert match, url
        slot, path = match.groups()
        assert headers["CF-Access-Client-Id"] == "access-id"
        assert headers["CF-Access-Client-Secret"] == "access-secret"
        if path == "/api/health":
            if slot not in self.health:
                raise cv.KvError("unreachable")
            status, data = self.health[slot]
            data = dict(data, state_version=self.versions.get(slot, data.get("state_version")))  # live version
            return status, json.dumps(data).encode()
        op = path.rsplit("/", 1)[-1]
        token = headers["Authorization"].removeprefix("Bearer ")
        assert verify_control_token(SECRET, token, op, slot, now=self.clock[0]), (op, slot)
        self.events.append((op, slot))
        if op == "freeze":
            snapshot = build_snapshot({"targets": ["x"]}, self.versions[slot], slot, now=5)
            return self.freeze_status, json.dumps({"state_version": self.versions[slot], "snapshot": snapshot}).encode()
        if op == "snapshot":
            return self.snapshot_status, json.dumps(build_snapshot({}, self.versions[slot], slot, now=5)).encode()
        if op == "import":
            envelope = json.loads(body)
            if self.import_status == 200:
                self.versions[slot] = max(self.versions[slot], envelope["state_version"])
            return self.import_status, json.dumps({"state_version": self.versions[slot], "imported": True}).encode()
        if op == "unfreeze":
            return self.unfreeze_status, b'{"frozen": false}'
        raise AssertionError(path)


class Harness:
    def __init__(self, offers=OFFERS, **config_overrides):
        self.events: list[tuple] = []
        self.gcloud = FakeGcloud(self.events)
        self.kv = FakeKv(self.events)
        self.clock = [100000.0]
        self.http = FakeHttp(self.events, self.clock)
        self.state: dict = {}
        self.saves = 0
        self.offers = list(offers)
        self.config = config(**config_overrides)

    def save(self):
        self.saves += 1

    def manager(self):
        return cv.ControlVmManager(self.config, self.state, self.save, gcloud=self.gcloud, kv=self.kv,
                                   requester=self.http, offer_source=lambda: list(self.offers),
                                   clock=lambda: self.clock[0])

    def tick(self, apply=True, **kw):
        return self.manager().tick(apply=apply, **kw)

    @property
    def transition(self):
        return self.state.get(cv.STATE_KEY, {}).get("transition")

    def mutations(self):
        return [e for e in self.events if e[0] in ("create", "start", "stop", "delete", "origin", "freeze", "import",
                                                    "unfreeze")]


def vm(slot, status="RUNNING", zone="us-central1-a", machine="e2-small"):
    return cv.ControlVm(f"spot-control-{slot}", zone, status, slot, machine)


def ok():
    return cv.HealthResult(True, 200, 3, False, True, "healthy")


class PlannerDecisionTableTests(unittest.TestCase):
    def plan(self, vms=(), origin_slot="a", health=None, offers=None, cstate=None, now=10000.0, **kw):
        origin = OriginRecord.for_slot(origin_slot, now=1) if origin_slot else None
        return cv.plan_next(vms=list(vms), origin=origin, origin_known=kw.pop("origin_known", True),
                            health=health or {}, offers=offers, control_state=cstate or {}, now=now,
                            max_daily_usd=Decimal("0.50"), **kw)

    def test_healthy_active_without_price_check_is_noop(self):
        plan = self.plan([vm("a")], health={"a": ok()})
        self.assertEqual(plan.action, "noop")

    def test_preempted_active_is_started(self):
        plan = self.plan([vm("a", "TERMINATED")], cstate={"down_since": {"a": 9900.0}})
        self.assertEqual((plan.action, plan.slot), ("start", "a"))

    def test_recent_start_request_waits(self):
        plan = self.plan([vm("a", "TERMINATED")], cstate={"down_since": {"a": 9900.0}, "last_start_at": {"a": 9990.0}})
        self.assertEqual(plan.action, "wait")

    def test_start_failing_past_grace_recreates_other_slot_in_another_zone(self):
        plan = self.plan([vm("a", "TERMINATED", zone="us-central1-a")], offers=OFFERS,
                         cstate={"down_since": {"a": 10000.0 - cv.START_GRACE_SECONDS}})
        self.assertEqual((plan.action, plan.slot, plan.target_slot), ("recreate_other_slot", "a", "b"))
        self.assertEqual(plan.offer.zone, "us-east1-b")

    def test_missing_active_waits_for_grace_then_recreates(self):
        self.assertEqual(self.plan([], cstate={"down_since": {"a": 9990.0}}, offers=OFFERS).action, "wait")
        plan = self.plan([], cstate={"down_since": {"a": 1.0}}, offers=OFFERS)
        self.assertEqual((plan.action, plan.target_slot), ("recreate_other_slot", "b"))

    def test_recreate_adopts_existing_other_slot_without_offer(self):
        plan = self.plan([vm("a", "TERMINATED"), vm("b", "TERMINATED")], cstate={"down_since": {"a": 1.0}})
        self.assertEqual((plan.action, plan.target_slot, plan.offer), ("recreate_other_slot", "b", None))

    def test_no_vm_and_no_origin_bootstraps_slot_a(self):
        plan = self.plan([], origin_slot=None, offers=OFFERS)
        self.assertEqual((plan.action, plan.target_slot, plan.offer.zone), ("bootstrap", "a", "us-central1-a"))

    def test_no_origin_without_offer_waits(self):
        self.assertEqual(self.plan([], origin_slot=None, offers=[]).action, "wait")

    def test_no_origin_with_existing_vm_starts_or_adopts(self):
        self.assertEqual(self.plan([vm("b", "TERMINATED")], origin_slot=None).action, "start")
        plan = self.plan([vm("b")], origin_slot=None)
        self.assertEqual((plan.action, plan.target_slot), ("bootstrap", "b"))

    def test_unreadable_origin_never_acts(self):
        plan = self.plan([], origin_slot=None, origin_known=False, offers=OFFERS)
        self.assertEqual(plan.action, "wait")

    def test_running_unhealthy_waits_then_recreates(self):
        bad = cv.HealthResult(False, 503, detail="starting")
        self.assertEqual(self.plan([vm("a")], health={"a": bad}, cstate={"down_since": {"a": 9900.0}}).action, "wait")
        plan = self.plan([vm("a")], health={"a": bad}, offers=OFFERS,
                         cstate={"down_since": {"a": 10000.0 - cv.UNHEALTHY_GRACE_SECONDS}})
        self.assertEqual(plan.action, "recreate_other_slot")

    def test_price_rotation_when_savings_are_worthwhile(self):
        plan = self.plan([vm("a", machine="e2-medium")], health={"a": ok()}, offers=OFFERS)
        self.assertEqual((plan.action, plan.slot, plan.target_slot), ("rotate", "a", "b"))
        self.assertEqual(plan.offer.machine_type, "e2-small")

    def test_small_savings_do_not_rotate(self):
        offers = [offer("e2-small", "us-east1-b", "0.10"), offer("e2-small", "us-central1-a", "0.11")]
        plan = self.plan([vm("a")], health={"a": ok()}, offers=offers)
        self.assertEqual(plan.action, "noop")

    def test_rotation_cooldown_and_cap(self):
        cstate = {"last_transition_end_at": 9000.0}
        self.assertEqual(self.plan([vm("a", machine="e2-medium")], health={"a": ok()}, offers=OFFERS,
                                   cstate=cstate).action, "noop")
        offers = [offer("e2-small", "us-east1-b", "0.55"), offer("e2-medium", "us-central1-a", "0.60")]
        self.assertEqual(self.plan([vm("a", machine="e2-medium")], health={"a": ok()}, offers=offers).action, "noop")
        offers = [offer("e2-small", "us-east1-b", "0.45"), offer("e2-medium", "us-central1-a", "0.60")]
        self.assertEqual(self.plan([vm("a", machine="e2-medium")], health={"a": ok()}, offers=offers).action, "rotate")

    def test_force_rotate_ignores_savings(self):
        offers = [offer("e2-small", "us-central1-a", "0.10")]
        plan = self.plan([vm("a")], health={"a": ok()}, offers=offers, force_rotate=True)
        self.assertEqual(plan.action, "rotate")

    def test_stray_other_slot_is_left_alone(self):
        plan = self.plan([vm("a"), vm("b")], health={"a": ok()}, offers=OFFERS)
        self.assertEqual(plan.action, "noop")

    def test_transition_in_progress_resumes(self):
        for mode, action in cv.MODE_ACTION.items():
            plan = self.plan([], cstate={"transition": {"mode": mode, "step": "cutover", "old_slot": "a",
                                                        "new_slot": "b"}})
            self.assertEqual((plan.action, plan.resume), (action, True))

    def test_update_tracking_sets_and_clears_down_since(self):
        cstate = {}
        origin = OriginRecord.for_slot("a", now=1)
        cv.update_tracking(cstate, origin, [vm("a", "TERMINATED")], {}, 50.0)
        cv.update_tracking(cstate, origin, [vm("a", "TERMINATED")], {}, 80.0)
        self.assertEqual(cstate["down_since"], {"a": 50.0})
        cv.update_tracking(cstate, origin, [vm("a")], {"a": ok()}, 90.0)
        self.assertEqual(cstate["down_since"], {})


class ExecutorTests(unittest.TestCase):
    def healthy_active(self, h, machine="e2-medium"):
        h.gcloud.add("a", machine=machine)
        h.kv.set_origin("a")
        h.http.healthy("a", version=7)

    def test_bootstrap_creates_waits_for_health_and_writes_origin(self):
        h = Harness()
        plan = h.tick()
        self.assertEqual(plan.action, "bootstrap")
        self.assertEqual(h.transition["step"], "wait_health")
        self.assertEqual(h.events, [("create", "spot-control-a", "us-central1-a")])
        self.assertIsNone(h.kv.origin_slot())
        h.gcloud.status("a", "RUNNING")
        h.http.healthy("a")
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertIsNone(h.transition)
        self.assertEqual(h.gcloud.scripts[0], cv.render_control_startup())

    def test_make_before_break_ordering(self):
        h = Harness()
        self.healthy_active(h)
        plan = h.tick()
        self.assertEqual(plan.action, "rotate")
        self.assertEqual(h.transition["step"], "wait_health")
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.tick()
        self.assertEqual(h.transition["step"], "propagate")
        self.assertIn("spot-control-a", h.gcloud.vms)
        h.clock[0] += cv.PROPAGATION_SECONDS - 1
        h.tick()
        self.assertIn("spot-control-a", h.gcloud.vms)
        h.clock[0] += 2
        h.tick()
        self.assertEqual(h.mutations(), [("create", "spot-control-b", "us-central1-a"), ("freeze", "a"),
                                         ("import", "b"), ("origin", "b"), ("delete", "spot-control-a")])
        self.assertIsNone(h.transition)
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertEqual(h.http.versions["b"], 7)
        self.assertNotIn("spot-control-a", h.gcloud.vms)

    def test_write_origin_failure_unfreezes_old_deletes_new_and_keeps_old(self):
        h = Harness()
        self.healthy_active(h)
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.kv.fail_origin_verify = True
        h.tick()
        self.assertIsNone(h.transition)
        self.assertEqual(h.mutations()[-5:], [("import", "b"), ("origin", "b"), ("freeze", "b"), ("unfreeze", "a"),
                                              ("delete", "spot-control-b")])
        self.assertIn("--delete-disks=all", delete_call(h, "spot-control-b"))
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertIn("spot-control-a", h.gcloud.vms)
        self.assertNotIn("spot-control-b", h.gcloud.vms)
        self.assertIsNotNone(h.state[cv.STATE_KEY]["last_abort_at"])
        self.assertEqual(h.state[cv.STATE_KEY]["history"][-1]["result"], "aborted")

    def test_unverified_write_that_landed_is_rolled_back_before_deleting_new(self):
        h = Harness()
        self.healthy_active(h)
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.kv.fail_origin_verify = True
        h.kv.land_failed_write = True
        h.tick()
        self.assertEqual([e for e in h.events if e[0] == "origin"], [("origin", "b"), ("origin", "a")])
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertNotIn("spot-control-b", h.gcloud.vms)

    def test_abort_keeps_new_vm_while_origin_cannot_be_restored(self):
        h = Harness()
        self.healthy_active(h)
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.kv.fail_origin_verify = True
        h.kv.land_failed_write = True
        h.kv.restore_fails = True
        h.tick()
        self.assertEqual(h.transition["step"], "abort")
        self.assertIn("spot-control-b", h.gcloud.vms)
        self.assertIn("spot-control-a", h.gcloud.vms)

    def test_preemption_start_then_recreate_other_slot(self):
        h = Harness()
        h.gcloud.add("a", status="TERMINATED")
        h.kv.set_origin("a")
        h.gcloud.start_rc = 1
        self.assertEqual(h.tick().action, "start")
        self.assertEqual(h.events, [("start", "spot-control-a")])
        h.clock[0] += 30
        self.assertEqual(h.tick().action, "wait")
        h.clock[0] += cv.START_GRACE_SECONDS
        plan = h.tick()
        self.assertEqual(plan.action, "recreate_other_slot")
        self.assertEqual(h.events[-1], ("create", "spot-control-b", "us-east1-b"))
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.tick()
        self.assertTrue(h.transition["import_skipped"])
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertNotIn(("freeze", "a"), h.events)
        h.clock[0] += cv.PROPAGATION_SECONDS + 1
        h.tick()
        self.assertEqual(h.events[-1], ("delete", "spot-control-a"))
        self.assertIsNone(h.transition)

    def test_recovery_aborts_when_old_vm_returns_healthy(self):
        h = Harness()
        h.gcloud.add("a", status="TERMINATED")
        h.kv.set_origin("a")
        h.state[cv.STATE_KEY] = {"down_since": {"a": 1.0}}
        h.tick()
        self.assertEqual(h.transition["mode"], cv.MODE_RECOVER)
        h.gcloud.status("a", "RUNNING")
        h.http.healthy("a")
        h.tick()
        self.assertIsNone(h.transition)
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertNotIn("spot-control-b", h.gcloud.vms)

    def test_recovery_cutover_failure_retries_instead_of_deleting_new(self):
        h = Harness()
        h.gcloud.add("a", status="TERMINATED")
        h.kv.set_origin("a")
        h.state[cv.STATE_KEY] = {"down_since": {"a": 1.0}}
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.kv.fail_origin_verify = True
        h.tick()
        self.assertEqual(h.transition["step"], "cutover")
        self.assertIn("spot-control-b", h.gcloud.vms)
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "b")

    def test_old_preempted_during_price_rotation_finishes_as_recovery(self):
        h = Harness()
        self.healthy_active(h)
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.gcloud.status("a", "TERMINATED")
        h.tick()
        self.assertEqual(h.transition["mode"], cv.MODE_RECOVER)
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertIn("spot-control-b", h.gcloud.vms)

    def test_new_vm_never_healthy_aborts_after_timeout(self):
        h = Harness()
        self.healthy_active(h)
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.unhealthy("b")
        h.tick()
        self.assertEqual(h.transition["step"], "wait_health")
        h.clock[0] += cv.HEALTH_TIMEOUT_SECONDS
        h.tick()
        self.assertIsNone(h.transition)
        self.assertNotIn("spot-control-b", h.gcloud.vms)
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertNotIn(("unfreeze", "a"), h.events)

    def test_freeze_failure_aborts_price_rotation_and_unfreezes(self):
        h = Harness()
        self.healthy_active(h)
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.http.freeze_status = 500
        for _ in range(cv.STEP_RETRY_LIMIT):
            h.tick()
        self.assertIsNone(h.transition)
        self.assertIn(("unfreeze", "a"), h.events)
        self.assertNotIn("spot-control-b", h.gcloud.vms)

    def test_create_stockout_tries_other_zones(self):
        h = Harness()
        h.gcloud.fail_create_zones = {"us-central1-a"}
        h.tick()
        self.assertEqual([e[0] for e in h.events], ["create_failed", "create"])
        self.assertEqual(h.transition["new_vm"]["zone"], "us-east1-b")

    def test_ambiguous_create_failure_adopts_vm_that_landed(self):
        h = Harness()
        self.healthy_active(h)
        h.gcloud.land_but_fail_zones = {"us-central1-a"}
        h.tick()
        creates = [e for e in h.events if e[0] == "create"]
        self.assertEqual(creates, [("create", "spot-control-b", "us-central1-a")])  # no second zone
        self.assertIn(("describe", "spot-control-b", "us-central1-a"), h.events)
        self.assertEqual(h.transition["step"], "create")
        h.tick()  # the inventory now lists it
        self.assertEqual(h.transition["step"], "wait_health")
        self.assertNotIn("pending_create", h.transition)
        self.assertEqual([e for e in h.events if e[0] == "create"], creates)

    def test_ambiguous_create_failure_confirmed_absent_tries_next_zone(self):
        h = Harness()
        self.healthy_active(h)
        real = h.gcloud.__call__

        def polling_failure_without_vm(argv):
            if argv[2:4] == ["instances", "create"] and "--zone=us-central1-a" in argv:
                h.gcloud.calls.append(list(argv))
                h.events.append(("create_ambiguous", argv[4], "us-central1-a"))
                return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: deadline exceeded")
            return real(argv)

        manager = cv.ControlVmManager(h.config, h.state, h.save, gcloud=polling_failure_without_vm, kv=h.kv,
                                      requester=h.http, offer_source=lambda: list(h.offers),
                                      clock=lambda: h.clock[0])
        manager.tick(apply=True)
        self.assertIn(("describe", "spot-control-b", "us-central1-a"), h.events)
        self.assertIn(("create", "spot-control-b", "us-east1-b"), h.events)
        self.assertEqual(h.transition["step"], "wait_health")

    def test_describe_failure_keeps_pending_attempt_and_waits(self):
        h = Harness()
        self.healthy_active(h)
        h.gcloud.land_but_fail_zones = {"us-central1-a"}
        h.gcloud.describe_error = "ERROR: backend error"
        h.tick()
        h.gcloud.vms.pop("spot-control-b")  # GCE later reports it absent
        h.tick()  # describe still ambiguous: wait, no other zone
        self.assertEqual([e[2] for e in h.events if e[0] == "create"], ["us-central1-a"])
        self.assertEqual(h.transition["pending_create"]["zone"], "us-central1-a")
        self.assertIn("cannot confirm", h.transition["error"])
        h.gcloud.describe_error = None
        h.gcloud.land_but_fail_zones = set()
        h.tick()  # confirmed absent: now another zone may be tried
        self.assertEqual([e[2] for e in h.events if e[0] == "create"], ["us-central1-a", "us-east1-b"])
        self.assertEqual(h.transition["step"], "wait_health")

    def test_two_vms_with_one_slot_never_act(self):
        h = Harness()
        self.healthy_active(h)
        h.gcloud.add("b", zone="us-east1-b")
        row = dict(h.gcloud.vms["spot-control-b"], zone="projects/proj/zones/us-west1-a", id="id-b2")
        h.gcloud.extra_rows.append(row)
        h.http.healthy("b")
        h.kv.set_origin("b")
        plan = h.tick()
        self.assertEqual(plan.action, "wait")
        self.assertIn("more than one control VM for slot b", plan.reason)
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(h.mutations(), [])

    def test_failed_zones_reset_when_every_available_zone_failed(self):
        h = Harness()
        h.gcloud.fail_create_zones = {"us-central1-a", "us-east1-b"}  # only two zones exist
        h.tick()  # bootstrap: all three placements fail
        self.assertEqual(sorted(h.transition["failed_offers"]),
                         ["e2-medium@us-central1-a", "e2-small@us-central1-a", "e2-small@us-east1-b"])
        h.tick()  # candidates exhausted: the list resets instead of filtering every placement forever
        self.assertEqual(h.transition["failed_offers"], [])
        h.gcloud.fail_create_zones = set()
        h.tick()
        self.assertEqual(h.transition["step"], "wait_health")

    # Codex round 22 P1: the cheapest shape exhausted in 3+ zones must not hide other shapes in those zones.
    WIDE_OFFERS = [offer("e2-small", "us-central1-a", "0.10"), offer("e2-small", "us-central1-b", "0.10"),
                   offer("e2-small", "us-central1-c", "0.10"), offer("e2-small", "us-east1-b", "0.12"),
                   offer("e2-medium", "us-central1-a", "0.40")]

    def test_exhausted_cheapest_shape_tries_another_shape_in_the_same_zone(self):
        h = Harness(offers=self.WIDE_OFFERS)
        h.gcloud.fail_create_offers = {("e2-small", o.zone) for o in self.WIDE_OFFERS}
        h.tick()  # bootstrap: the per-tick limit spends every attempt on the cheapest shape
        self.assertEqual(h.gcloud.create_attempts, [("e2-small", "us-central1-a"), ("e2-small", "us-central1-b"),
                                                    ("e2-small", "us-central1-c")])
        self.assertEqual(h.transition["step"], "create")
        h.tick()  # next tick: the remaining cheap zone, then the other shape in a zone that already failed
        self.assertEqual(h.gcloud.create_attempts[3:], [("e2-small", "us-east1-b"), ("e2-medium", "us-central1-a")])
        self.assertEqual(h.transition["step"], "wait_health")
        self.assertEqual(h.transition["new_vm"]["machine_type"], "e2-medium")
        self.assertEqual(h.transition["new_vm"]["zone"], "us-central1-a")

    def test_search_restarts_only_after_every_shape_and_zone_failed(self):
        h = Harness(offers=self.WIDE_OFFERS)
        every = {(o.machine_type, o.zone) for o in self.WIDE_OFFERS}
        h.gcloud.fail_create_offers = set(every)
        h.tick()
        h.tick()
        # Each placement was attempted exactly once before anything was retried.
        self.assertEqual(sorted(h.gcloud.create_attempts), sorted(every))
        self.assertEqual(sorted(h.transition["failed_offers"]), sorted(cv._offer_key(m, z) for m, z in every))
        self.assertEqual(set(h.transition["failed_offers_at"]), set(h.transition["failed_offers"]))
        h.tick()  # nothing left: reset, without another create this tick
        self.assertEqual(len(h.gcloud.create_attempts), len(every))
        self.assertEqual((h.transition["failed_offers"], h.transition["failed_offers_at"]), ([], {}))
        self.assertIn("every affordable machine type and zone failed", h.transition["error"])
        h.tick()  # the search starts over from the cheapest placement
        self.assertEqual(h.gcloud.create_attempts[len(every)], ("e2-small", "us-central1-a"))

    def test_definitive_failure_marks_only_that_shape_and_zone(self):
        h = Harness()
        h.gcloud.fail_create_offers = {("e2-small", "us-central1-a")}
        h.tick()
        self.assertEqual(h.transition["failed_offers"], ["e2-small@us-central1-a"])
        self.assertNotIn("failed_zones", h.transition)
        self.assertEqual(h.gcloud.create_attempts, [("e2-small", "us-central1-a"), ("e2-small", "us-east1-b")])

    def test_legacy_zone_only_failures_still_exclude_the_whole_zone_until_they_expire(self):
        h = Harness(offers=self.WIDE_OFFERS)
        h.gcloud.fail_create_offers = {(o.machine_type, o.zone) for o in self.WIDE_OFFERS}
        h.tick()  # leaves a bootstrap transition at the create step
        t = h.transition
        # A record persisted before per-shape tracking: zone-only, one with and one without a timestamp.
        t["failed_offers"], t["failed_offers_at"] = [], {}
        t["failed_zones"] = ["us-central1-a", "us-central1-b"]
        t["failed_zones_at"] = {"us-central1-a": h.clock[0]}
        h.gcloud.fail_create_offers = set()
        h.gcloud.create_attempts.clear()
        h.tick()
        # Neither shape is tried in a legacy-failed zone: the first create lands in the next zone.
        self.assertEqual(h.gcloud.create_attempts, [("e2-small", "us-central1-c")])
        self.assertEqual(h.transition["step"], "wait_health")

    def test_legacy_zone_records_expire_and_reset_with_the_search(self):
        h = Harness()
        manager = h.manager()
        t = {"failed_zones": ["us-east1-b"]}  # recorded before timestamps existed
        self.assertEqual(manager._failed_offers(t), ([], ["us-east1-b"]))
        h.clock[0] += cv.FAILED_ZONE_TTL_SECONDS
        self.assertEqual(manager._failed_offers(t), ([], []))
        # A pending create persisted without its machine type fails the whole zone (conservative).
        manager._mark_offer_failed(t, "", "us-central1-a")
        self.assertEqual(manager._failed_offers(t), ([], ["us-central1-a"]))
        manager._mark_offer_failed(t, "e2-small", "us-east1-b")
        self.assertEqual(manager._failed_offers(t), (["e2-small@us-east1-b"], ["us-central1-a"]))
        h.clock[0] += cv.FAILED_ZONE_TTL_SECONDS
        self.assertEqual(manager._failed_offers(t), ([], []))

    def test_failed_offer_records_expire(self):
        h = Harness()
        manager = h.manager()
        t = {"failed_offers": []}
        manager._mark_offer_failed(t, "e2-small", "us-central1-a")
        self.assertEqual(manager._failed_offers(t), (["e2-small@us-central1-a"], []))
        h.clock[0] += cv.FAILED_ZONE_TTL_SECONDS
        self.assertEqual(manager._failed_offers(t), ([], []))
        untimed = {"failed_offers": ["e2-small@us-east1-b"]}
        self.assertEqual(manager._failed_offers(untimed), (["e2-small@us-east1-b"], []))
        h.clock[0] += cv.FAILED_ZONE_TTL_SECONDS
        self.assertEqual(manager._failed_offers(untimed), ([], []))

    def test_price_create_failures_abort_without_touching_old(self):
        h = Harness()
        self.healthy_active(h)
        h.gcloud.fail_create_zones = {"us-central1-a", "us-east1-b"}
        for _ in range(cv.PRICE_CREATE_MAX_FAILURES):
            h.tick()
        self.assertIsNone(h.transition)
        self.assertIn("spot-control-a", h.gcloud.vms)
        self.assertEqual(h.kv.origin_slot(), "a")

    def test_dry_run_reads_only_and_saves_nothing(self):
        for setup in ("empty", "rotation", "preempted"):
            h = Harness()
            if setup == "rotation":
                self.healthy_active(h)
            elif setup == "preempted":
                h.gcloud.add("a", status="TERMINATED")
                h.kv.set_origin("a")
            before = copy.deepcopy(h.state)
            plan = h.tick(apply=False)
            self.assertNotIn(plan.action, ("wait",), setup)
            self.assertEqual(h.gcloud.mutating(), [], setup)
            self.assertEqual(h.kv.puts, [], setup)
            self.assertTrue(all(c[0] == "GET" and c[1].endswith("/api/health") for c in h.http.calls), setup)
            self.assertEqual(h.state, before, setup)
            self.assertEqual(h.saves, 0, setup)

    def test_missing_access_credentials_or_kv_never_mutate(self):
        h = Harness(access_client_id="")
        plan = h.tick()
        self.assertEqual(plan.action, "wait")
        self.assertEqual(h.gcloud.mutating(), [])
        h = Harness()
        h.kv.get_error = True
        self.assertEqual(h.tick().action, "wait")
        self.assertEqual(h.gcloud.mutating(), [])
        h = Harness()
        manager = cv.ControlVmManager(h.config, h.state, h.save, gcloud=h.gcloud, kv=None, requester=h.http,
                                      offer_source=lambda: OFFERS, clock=lambda: 1.0)
        self.assertEqual(manager.tick(apply=True).action, "wait")
        self.assertEqual(h.gcloud.mutating(), [])


class ResumeFromPersistedStepTests(unittest.TestCase):
    """A fresh manager (no in-memory snapshot) resumes every persisted step safely."""

    def setup_rotation(self, step, **fields):
        h = Harness()
        h.gcloud.add("a", machine="e2-medium")
        h.http.healthy("a", version=7)
        h.kv.set_origin("a")
        transition = {"id": "t1", "mode": cv.MODE_PRICE, "old_slot": "a", "new_slot": "b",
                      "old_vm": {"name": "spot-control-a", "zone": "us-central1-a"},
                      "new_vm": {"name": "spot-control-b", "zone": "us-east1-b", "machine_type": "e2-small"},
                      "offer": offer("e2-small", "us-east1-b").to_dict(), "excluded_zones": [], "failed_zones": [],
                      "step": step, "started_at": h.clock[0], "step_started_at": h.clock[0],
                      "updated_at": h.clock[0], "attempts": 0, "create_failures": 0, "freeze_requested": False,
                      "frozen_version": None, "import_skipped": False, "cutover_attempted": False,
                      "cutover_at": None, "abort_reason": None, "error": None, "reason": "test"}
        transition.update(fields)
        h.state[cv.STATE_KEY] = {"transition": transition}
        return h

    def with_new_vm(self, h, status="RUNNING", healthy=True):
        h.gcloud.add("b", status=status, zone="us-east1-b")
        t = h.transition
        if t["step"] in ("cutover", "propagate", "delete_old", "abort") and not t.get("import_skipped"):
            # Resuming after the import step: the replacement already holds the frozen state.
            h.http.versions["b"] = max(h.http.versions["b"], t.get("frozen_version") or 0)
        if healthy:
            h.http.healthy("b")

    def test_resume_create_adopts_vm_created_before_crash(self):
        h = self.setup_rotation("create")
        self.with_new_vm(h, status="PROVISIONING", healthy=False)
        h.tick()
        self.assertNotIn("create", [e[0] for e in h.events])
        self.assertEqual(h.transition["step"], "wait_health")

    def test_resume_create_recreates_when_vm_never_appeared(self):
        h = self.setup_rotation("create")
        h.tick()
        self.assertEqual(h.events[0], ("create", "spot-control-b", "us-east1-b"))

    def test_resume_wait_health(self):
        h = self.setup_rotation("wait_health")
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.transition["step"], "propagate")
        self.assertEqual(h.mutations(), [("freeze", "a"), ("import", "b"), ("origin", "b")])

    def test_resume_freeze_after_crash_mid_request(self):
        h = self.setup_rotation("freeze", freeze_requested=True)
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.mutations(), [("freeze", "a"), ("import", "b"), ("origin", "b")])

    def test_resume_import_fetches_frozen_snapshot_again(self):
        h = self.setup_rotation("import", freeze_requested=True, frozen_version=7)
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.mutations(), [("import", "b"), ("origin", "b")])
        self.assertIn(("snapshot", "a"), h.events)
        self.assertEqual(h.http.versions["b"], 7)

    def test_resume_import_falls_back_to_kv_snapshot(self):
        h = self.setup_rotation("import", freeze_requested=True, frozen_version=7)
        self.with_new_vm(h)
        h.http.snapshot_status = 503
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({"k": 1}, 7, "a", now=1))
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "b")

    def test_resume_import_rejects_stale_snapshot(self):
        h = self.setup_rotation("import", freeze_requested=True, frozen_version=9)
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.transition["step"], "import")
        self.assertNotIn(("import", "b"), h.events)

    def test_resume_cutover(self):
        h = self.setup_rotation("cutover", freeze_requested=True, frozen_version=7)
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertEqual(h.transition["step"], "propagate")

    def test_price_cutover_aborts_when_replacement_unhealthy(self):
        """Codex M4 round 2: recheck the replacement right before switching the origin."""
        for status, healthy in (("TERMINATED", False), ("RUNNING", False)):
            with self.subTest(status=status):
                h = self.setup_rotation("cutover", freeze_requested=True, frozen_version=7)
                self.with_new_vm(h, status=status, healthy=healthy)
                if status == "RUNNING":
                    h.http.unhealthy("b")
                h.tick()
                self.assertEqual(h.kv.origin_slot(), "a")  # users stay on the old VM
                self.assertNotIn(("delete", "spot-control-a"), h.events)

    def test_recovery_cutover_waits_for_preempted_replacement(self):
        h = self.setup_rotation("cutover", mode=cv.MODE_RECOVER, freeze_requested=True, frozen_version=7)
        h.gcloud.status("a", "TERMINATED")
        h.http.unhealthy("a")
        self.with_new_vm(h, status="TERMINATED", healthy=False)
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "a")  # never switched to a dead VM
        self.assertIsNotNone(h.transition)
        self.assertNotEqual(h.transition["step"], "propagate")

    def test_resume_propagate_waits_full_interval(self):
        h = self.setup_rotation("propagate", cutover_attempted=True, cutover_at=h_now() - 30)
        h.clock[0] = h_now()
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.transition["step"], "propagate")
        h.clock[0] += 61
        h.tick()
        self.assertEqual(h.events[-1], ("delete", "spot-control-a"))

    def test_resume_delete_old(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0)
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.events, [("delete", "spot-control-a")])
        self.assertIsNone(h.transition)

    def test_delete_old_waits_for_unhealthy_replacement_then_falls_back(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0, freeze_requested=True,
                                frozen_version=7)
        h.kv.set_origin("b")
        self.with_new_vm(h, healthy=False)
        h.http.unhealthy("b")
        h.tick()
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(h.transition["step"], "delete_old")
        self.assertIn("not healthy", h.transition["error"])
        self.assertEqual(h.kv.origin_slot(), "b")
        h.clock[0] += cv.REPLACEMENT_READY_GRACE_SECONDS - 1
        h.tick()
        self.assertEqual(h.gcloud.mutating(), [])
        h.clock[0] += 2
        h.tick()
        self.assertEqual(h.mutations(), [("freeze", "b"), ("unfreeze", "a"), ("origin", "a"),
                                         ("delete", "spot-control-b")])
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertIn("spot-control-a", h.gcloud.vms)
        self.assertIsNone(h.transition)
        self.assertEqual(h.state[cv.STATE_KEY]["history"][-1]["result"], "aborted")

    def test_fallback_carries_newer_kv_state_back_to_old_vm(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0, freeze_requested=True,
                                frozen_version=7, replacement_unready_since=1.0)
        h.kv.set_origin("b")
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({"targets": ["after-cutover"]}, 9, "b", now=1))
        self.with_new_vm(h, healthy=False)
        h.http.unhealthy("b")
        h.tick()
        self.assertEqual(h.mutations(), [("freeze", "b"), ("import", "a"), ("unfreeze", "a"), ("origin", "a"),
                                         ("delete", "spot-control-b")])
        self.assertEqual(h.http.versions["a"], 9)

    def test_delete_old_restores_origin_when_replacement_is_gone(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0, freeze_requested=True,
                                frozen_version=7)
        h.kv.set_origin("b")
        h.tick()
        self.assertEqual(h.mutations(), [("unfreeze", "a"), ("origin", "a")])
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertIn("spot-control-a", h.gcloud.vms)
        self.assertIsNone(h.transition)

    def test_delete_old_starts_stopped_replacement_and_keeps_old(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0, freeze_requested=True)
        h.kv.set_origin("b")
        self.with_new_vm(h, status="TERMINATED", healthy=False)
        h.tick()
        self.assertEqual(h.mutations(), [("start", "spot-control-b")])
        self.assertIn("spot-control-a", h.gcloud.vms)
        self.assertEqual(h.transition["step"], "delete_old")

    def test_delete_old_requires_replacement_to_answer_as_its_own_slot(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0)
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.http.health["b"][1]["slot"] = "a"  # tunnel hostname routed to the wrong VM
        h.tick()
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertIn("answered as slot 'a'", h.transition["error"])

    def test_no_fallback_without_a_running_old_vm(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0, import_skipped=True,
                                mode=cv.MODE_RECOVER, replacement_unready_since=1.0)
        h.gcloud.status("a", "TERMINATED")
        h.kv.set_origin("b")
        self.with_new_vm(h, healthy=False)
        h.http.unhealthy("b")
        h.tick()
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(h.mutations(), [])
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertEqual(h.transition["step"], "delete_old")

    def test_abort_after_replacement_vanishes_carries_back_its_newer_kv_state(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0, freeze_requested=True,
                                frozen_version=7)
        h.kv.set_origin("b")
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({"targets": ["after-cutover"]}, 9, "b", now=1))
        h.tick()  # spot-control-b is gone
        self.assertEqual(h.mutations(), [("import", "a"), ("unfreeze", "a"), ("origin", "a")])
        self.assertEqual(h.http.versions["a"], 9)
        self.assertIsNone(h.transition)

    def frozen_old(self, h):
        h.http.health["a"] = (503, {"error": "control_vm_unavailable", "status": "frozen", "hydrated": True,
                                    "frozen": True, "state_version": 7, "slot": "a", "kv_writer": True})

    def test_recovery_imports_from_reachable_frozen_old_vm(self):
        h = self.setup_rotation("freeze", mode=cv.MODE_RECOVER, freeze_requested=True)
        self.frozen_old(h)
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.mutations(), [("freeze", "a"), ("import", "b"), ("origin", "b")])
        self.assertEqual(h.http.versions["b"], 7)
        self.assertFalse(h.transition["import_skipped"])

    def test_recovery_falls_back_to_snapshot_when_frozen_old_vm_will_not_freeze(self):
        h = self.setup_rotation("freeze", mode=cv.MODE_RECOVER, freeze_requested=True)
        self.frozen_old(h)
        self.with_new_vm(h)
        h.http.freeze_status = 500
        for _ in range(cv.STEP_RETRY_LIMIT):
            h.tick()
        self.assertIn(("snapshot", "a"), h.events)
        # The old VM is running but did not freeze: it must be stopped before the replacement writes.
        self.assertEqual(h.mutations()[-2:], [("import", "b"), ("stop", "spot-control-a")])
        self.assertEqual(h.kv.origin_slot(), "a")
        h.gcloud.status("a", "TERMINATED")
        h.tick()
        self.assertEqual(h.mutations()[-1], ("origin", "b"))
        self.assertEqual(h.http.versions["b"], 7)

    def test_recovery_never_cuts_over_while_unfrozen_old_vm_runs(self):
        """Codex M4 round 11: an unreachable old writer is fenced (stopped) before cutover."""
        h = self.setup_rotation("cutover", mode=cv.MODE_RECOVER, import_skipped=True, kv_caught_up=True)
        h.http.unhealthy("a")  # running but unreachable
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertIn(("stop", "spot-control-a"), h.events)
        h.tick()  # still STOPPING: keep waiting, do not stop again yet
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertEqual(h.events.count(("stop", "spot-control-a")), 1)
        h.gcloud.status("a", "TERMINATED")
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "b")

    def test_frozen_old_vm_needs_no_stop(self):
        h = self.setup_rotation("cutover", mode=cv.MODE_RECOVER, freeze_requested=True, frozen_version=7,
                                old_frozen=True)
        self.with_new_vm(h)
        h.tick()
        self.assertNotIn(("stop", "spot-control-a"), h.events)
        self.assertEqual(h.kv.origin_slot(), "b")

    def test_recovery_cutover_imports_newer_kv_state_first(self):
        h = self.setup_rotation("freeze", mode=cv.MODE_RECOVER)
        h.gcloud.status("a", "TERMINATED")  # preempted before any freeze
        self.with_new_vm(h)
        h.http.versions["b"] = 7  # replacement hydrated at v7
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({"k": 8}, 8, "a", now=1))  # old wrote v8, died
        h.tick()
        self.assertEqual(h.mutations(), [("import", "b"), ("origin", "b")])
        self.assertEqual(h.http.versions["b"], 8)

    def test_recovery_cutover_waits_until_replacement_confirms_kv_version(self):
        h = self.setup_rotation("freeze", mode=cv.MODE_RECOVER)
        h.gcloud.status("a", "TERMINATED")
        self.with_new_vm(h)
        h.http.versions["b"] = 7
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({"k": 8}, 8, "a", now=1))
        h.http.import_status = 500
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertEqual(h.transition["step"], "cutover")
        self.assertFalse(h.transition["cutover_attempted"])
        h.http.import_status = 200
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertEqual(h.http.versions["b"], 8)

    def test_recovery_cutover_waits_while_kv_snapshot_unreadable(self):
        h = self.setup_rotation("cutover", mode=cv.MODE_RECOVER, import_skipped=True)
        h.gcloud.status("a", "TERMINATED")
        self.with_new_vm(h)
        real_get = h.kv.get
        h.kv.get = lambda key: (_ for _ in ()).throw(cv.KvError("down")) if key == KV_STATE_KEY else real_get(key)
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertIn("unreadable", h.transition["error"])

    def test_recovery_still_skips_import_when_old_vm_is_down(self):
        h = self.setup_rotation("freeze", mode=cv.MODE_RECOVER)
        h.gcloud.status("a", "TERMINATED")
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.mutations(), [("origin", "b")])
        self.assertTrue(h.transition["import_skipped"])

    def rollback_setup(self, **fields):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0, freeze_requested=True,
                                frozen_version=7, replacement_unready_since=1.0, **fields)
        h.kv.set_origin("b")
        return h

    def test_rollback_imports_replacement_snapshot_newer_than_kv(self):
        h = self.rollback_setup()
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({}, 9, "b", now=1))
        self.with_new_vm(h, healthy=False)
        h.http.unhealthy("b")
        h.http.versions["b"] = 12  # write-behind had not published 10..12 yet
        h.tick()
        self.assertEqual(h.mutations(), [("freeze", "b"), ("import", "a"), ("unfreeze", "a"), ("origin", "a"),
                                         ("delete", "spot-control-b")])
        self.assertEqual(h.http.versions["a"], 12)
        self.assertIn("--delete-disks=all", delete_call(h, "spot-control-b"))
        self.assertEqual(h.state[cv.STATE_KEY].get("retained_disks", []), [])

    def test_rollback_keeps_disk_of_unreachable_replacement(self):
        h = self.rollback_setup()
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({}, 9, "b", now=1))
        self.with_new_vm(h, healthy=False)
        h.http.unhealthy("b")
        h.http.freeze_status = 503  # the replacement cannot be frozen
        h.tick()
        # Not frozen and still RUNNING: it is stopped, and the old slot is not reactivated until it is.
        self.assertEqual(h.mutations(), [("freeze", "b"), ("stop", "spot-control-b")])
        self.assertEqual(h.kv.origin_slot(), "b")
        h.gcloud.status("b", "TERMINATED")
        h.tick()
        self.assertEqual(h.http.versions["a"], 9)  # KV state still carried back
        self.assertEqual(h.kv.origin_slot(), "a")
        call = delete_call(h, "spot-control-b")
        self.assertIn("--keep-disks=boot", call)
        self.assertNotIn("--delete-disks=all", call)
        retained = h.state[cv.STATE_KEY]["retained_disks"]
        self.assertEqual([(d["disk"], d["zone"], d["slot"]) for d in retained],
                         [("spot-control-b", "us-east1-b", "b")])
        self.assertIsNone(h.transition)
        manager = h.manager()
        self.assertEqual(manager._retained_zones("b"), ["us-east1-b"])

    def test_rollback_keeps_disk_of_stopped_replacement(self):
        h = self.rollback_setup()
        self.with_new_vm(h, status="TERMINATED", healthy=False)
        h.clock[0] += cv.START_RETRY_SECONDS
        h.state[cv.STATE_KEY]["last_start_at"] = {"b": h.clock[0]}  # just asked to start it
        h.tick()
        self.assertNotIn(("freeze", "b"), h.events)
        self.assertIn("--keep-disks=boot", delete_call(h, "spot-control-b"))

    def test_rollback_keeps_replacement_disk_when_old_vm_import_is_not_confirmed(self):
        h = self.rollback_setup()
        self.with_new_vm(h, healthy=False)
        h.http.unhealthy("b")
        h.http.versions["b"] = 12
        h.http.import_status = 500  # the old VM cannot persist the import
        for _ in range(cv.STEP_RETRY_LIMIT):
            h.tick()
        self.assertEqual(h.http.versions["a"], 7)
        self.assertIn("spot-control-a", h.gcloud.vms)
        self.assertIn("--keep-disks=boot", delete_call(h, "spot-control-b"))
        self.assertIsNone(h.transition)

    def test_recovery_without_import_keeps_old_boot_disk(self):
        h = self.setup_rotation("delete_old", mode=cv.MODE_RECOVER, cutover_attempted=True, cutover_at=1.0,
                                import_skipped=True)
        h.gcloud.status("a", "TERMINATED")
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.tick()
        self.assertIn("--keep-disks=boot", delete_call(h, "spot-control-a"))
        self.assertEqual(h.state[cv.STATE_KEY]["retained_disks"][0]["disk"], "spot-control-a")
        self.assertIsNone(h.transition)

    def add_duplicate_b(self, h):
        row = dict(h.gcloud.vms["spot-control-b"], zone="projects/proj/zones/us-west1-a", id="id-b2")
        h.gcloud.extra_rows.append(row)

    def test_duplicate_appearing_mid_transition_pauses_before_next_step(self):
        h = self.setup_rotation("wait_health")
        self.with_new_vm(h)
        real = h.gcloud.__call__
        lists = []

        def gcloud(argv):
            if argv[2:4] == ["instances", "list"]:
                lists.append(1)
                if len(lists) == 2:  # the re-observation after wait_health advanced
                    self.add_duplicate_b(h)
            return real(argv)

        manager = cv.ControlVmManager(h.config, h.state, h.save, gcloud=gcloud, kv=h.kv, requester=h.http,
                                      offer_source=lambda: list(h.offers), clock=lambda: h.clock[0])
        manager.tick(apply=True)
        self.assertEqual(h.transition["step"], "freeze")
        self.assertEqual(h.mutations(), [])  # no freeze, import, or origin write
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertTrue(h.transition["error"].startswith("paused: more than one control VM for slot b"))

    def test_abort_with_duplicate_keeps_everything_frozen(self):
        h = self.setup_rotation("abort", freeze_requested=True, cutover_attempted=True, abort_reason="x")
        self.with_new_vm(h)
        self.add_duplicate_b(h)
        h.kv.set_origin("b")
        manager = h.manager()
        manager.advance(manager.observe(want_offers=False))
        self.assertEqual(h.mutations(), [])
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertEqual(h.transition["step"], "abort")

    def test_delete_old_refuses_while_origin_points_at_old(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0)
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(h.transition["step"], "delete_old")
        self.assertIn("refusing", h.transition["error"])

    def test_resume_abort(self):
        h = self.setup_rotation("abort", freeze_requested=True, abort_reason="crashed")
        self.with_new_vm(h)
        h.tick()
        self.assertEqual(h.mutations(), [("unfreeze", "a"), ("delete", "spot-control-b")])
        self.assertIsNone(h.transition)

    def test_abort_refuses_to_delete_vm_the_origin_names(self):
        h = self.setup_rotation("abort", abort_reason="crashed")
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.tick()
        self.assertIn("spot-control-b", h.gcloud.vms)
        self.assertEqual(h.transition["step"], "abort")


class RollbackFenceTests(unittest.TestCase):
    """Codex P1: a rollback never leaves the replacement and the old slot writable at the same time."""

    setup_rotation = ResumeFromPersistedStepTests.setup_rotation
    with_new_vm = ResumeFromPersistedStepTests.with_new_vm
    rollback_setup = ResumeFromPersistedStepTests.rollback_setup

    def old_reactivated(self, h):
        return ("unfreeze", "a") in h.events or ("origin", "a") in h.events or ("import", "a") in h.events

    def test_unfreezable_running_replacement_is_stopped_before_old_slot_serves_again(self):
        h = self.rollback_setup()
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({}, 9, "b", now=1))
        self.with_new_vm(h, healthy=False)
        h.http.unhealthy("b")
        h.http.freeze_status = 503  # the replacement cannot be frozen ...
        h.gcloud.stop_rc = 1  # ... and the first stop request does not take effect
        h.tick()
        self.assertEqual(h.transition["step"], "abort")
        self.assertEqual(h.mutations(), [("freeze", "b"), ("stop", "spot-control-b")])
        self.assertFalse(self.old_reactivated(h))
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertIn("to freeze or stop", h.transition["error"])
        h.clock[0] += cv.STOP_RETRY_SECONDS - 1
        h.tick()  # still RUNNING: freeze is retried, the stop is not repeated inside the interval
        self.assertEqual(h.events.count(("stop", "spot-control-b")), 1)
        self.assertFalse(self.old_reactivated(h))
        h.gcloud.stop_rc = 0
        h.clock[0] += 1
        h.tick()  # interval elapsed: the stop is requested again
        self.assertEqual(h.events.count(("stop", "spot-control-b")), 2)
        self.assertEqual(h.gcloud.vms["spot-control-b"]["status"], "STOPPING")
        self.assertFalse(self.old_reactivated(h))
        h.tick()  # STOPPING is not stopped yet
        self.assertEqual(h.events.count(("stop", "spot-control-b")), 2)
        self.assertFalse(self.old_reactivated(h))
        self.assertEqual(h.kv.origin_slot(), "b")
        h.gcloud.status("b", "TERMINATED")
        h.gcloud.delete_rc = 1  # the replacement delete fails after the old slot is restored
        h.tick()
        self.assertEqual(h.mutations()[-4:], [("import", "a"), ("unfreeze", "a"), ("origin", "a"),
                                              ("delete", "spot-control-b")])
        self.assertEqual(h.http.versions["a"], 9)
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertEqual(h.gcloud.vms["spot-control-b"]["status"], "TERMINATED")  # still not a writer
        self.assertEqual(h.transition["step"], "abort")
        h.gcloud.delete_rc = 0
        h.tick()
        self.assertIsNone(h.transition)
        self.assertNotIn("spot-control-b", h.gcloud.vms)
        self.assertIn("--keep-disks=boot", [c for c in h.gcloud.calls if c[2:4] == ["instances", "delete"]][-1])
        self.assertNotIn(("start", "spot-control-b"), h.events)

    def test_confirmed_freeze_fences_replacement_without_a_stop(self):
        h = self.rollback_setup()
        self.with_new_vm(h, healthy=False)
        h.http.unhealthy("b")
        h.gcloud.delete_rc = 1
        h.tick()
        self.assertEqual(h.mutations(), [("freeze", "b"), ("unfreeze", "a"), ("origin", "a"),
                                         ("delete", "spot-control-b")])
        self.assertTrue(h.transition["new_frozen"])  # delete failed: the frozen replacement still cannot write
        h.gcloud.delete_rc = 0
        h.tick()
        self.assertEqual(h.mutations()[-1], ("delete", "spot-control-b"))
        self.assertEqual(h.events.count(("freeze", "b")), 1)  # the recorded freeze still fences it
        self.assertNotIn(("stop", "spot-control-b"), h.events)

    def test_freeze_reply_without_state_version_is_not_a_fence(self):
        h = self.rollback_setup()
        self.with_new_vm(h, healthy=False)
        h.http.unhealthy("b")
        real = h.http.__call__

        def no_version(method, url, headers, body, timeout):
            status, raw = real(method, url, headers, body, timeout)
            if url.endswith("/api/control/freeze") and "spot-origin-b" in url:
                return 200, b'{"frozen": true}'
            return status, raw

        manager = cv.ControlVmManager(h.config, h.state, h.save, gcloud=h.gcloud, kv=h.kv, requester=no_version,
                                      offer_source=lambda: list(h.offers), clock=lambda: h.clock[0])
        manager.tick(apply=True)
        self.assertEqual(h.gcloud.mutating()[-1][2:5], ["instances", "stop", "spot-control-b"])
        self.assertFalse(self.old_reactivated(h))

    def test_rollback_without_cutover_attempt_needs_no_fence(self):
        h = self.setup_rotation("abort", freeze_requested=True, abort_reason="import failed")
        self.with_new_vm(h)
        h.http.freeze_status = 503
        h.tick()
        self.assertEqual(h.mutations(), [("unfreeze", "a"), ("delete", "spot-control-b")])


class ProvisionalOldStateTests(unittest.TestCase):
    """Codex P1 round 13: state taken from an old VM that never confirmed a freeze is provisional."""

    setup_rotation = ResumeFromPersistedStepTests.setup_rotation
    with_new_vm = ResumeFromPersistedStepTests.with_new_vm

    def old_delete(self, h):
        return delete_call(h, "spot-control-a")

    def finish_recovery(self, h):
        h.clock[0] += cv.PROPAGATION_SECONDS + 1
        h.tick()

    def test_unfrozen_snapshot_import_keeps_old_disk_with_its_later_changes(self):
        h = self.setup_rotation("freeze", mode=cv.MODE_RECOVER)
        self.with_new_vm(h)
        h.http.freeze_status = 500  # reachable, healthy, but never freezes
        with self.assertLogs(cv.LOG, level="ERROR") as marked:
            for _ in range(cv.STEP_RETRY_LIMIT):
                h.tick()
        self.assertTrue(any("will be KEPT" in line for line in marked.output))
        self.assertEqual(h.http.versions["b"], 7)  # the unfrozen snapshot (v7) was imported
        self.assertTrue(h.transition["old_state_unconfirmed"])
        self.assertIn("did not freeze", h.transition["old_state_unconfirmed_reason"])
        manager = h.manager()
        self.assertIn("did not freeze", manager.status(manager.observe(want_offers=False))["old_state_unconfirmed"])
        h.http.versions["a"] = 8  # the still-unfrozen old dashboard accepts v8 before it is stopped
        self.assertIn(("stop", "spot-control-a"), h.events)
        h.gcloud.status("a", "TERMINATED")
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "b")
        with self.assertLogs(cv.LOG, level="ERROR") as kept:
            self.finish_recovery(h)
        self.assertTrue(any("KEPT its boot disk" in line for line in kept.output))
        self.assertIsNone(h.transition)
        call = self.old_delete(h)
        self.assertIn("--keep-disks=boot", call)  # v8 survives on the retained disk
        self.assertNotIn("--delete-disks=all", call)
        retained = h.state[cv.STATE_KEY]["retained_disks"]
        self.assertEqual([(d["disk"], d["slot"]) for d in retained], [("spot-control-a", "a")])
        self.assertIn("did not freeze", retained[0]["reason"])

    def test_confirmed_freeze_deletes_old_disk(self):
        h = self.setup_rotation("freeze", mode=cv.MODE_RECOVER)
        self.with_new_vm(h)
        h.tick()
        self.assertTrue(h.transition["old_frozen"])
        self.assertNotIn("old_state_unconfirmed", h.transition)
        self.finish_recovery(h)
        self.assertIsNone(h.transition)
        self.assertIn("--delete-disks=all", self.old_delete(h))
        self.assertEqual(h.state[cv.STATE_KEY].get("retained_disks", []), [])
        self.assertNotIn(("stop", "spot-control-a"), h.events)

    def test_old_vm_fenced_by_stop_keeps_its_disk(self):
        # import_skipped was cleared by hand to isolate the stop fence as the only reason to keep the disk.
        h = self.setup_rotation("cutover", mode=cv.MODE_RECOVER, kv_caught_up=True, freeze_requested=True,
                                frozen_version=7)
        h.http.unhealthy("a")
        self.with_new_vm(h)
        h.tick()
        self.assertIn(("stop", "spot-control-a"), h.events)
        self.assertIn("fenced by stopping", h.transition["old_state_unconfirmed_reason"])
        h.gcloud.status("a", "TERMINATED")
        h.tick()
        self.finish_recovery(h)
        self.assertIn("--keep-disks=boot", self.old_delete(h))
        self.assertIn("fenced by stopping", h.state[cv.STATE_KEY]["retained_disks"][0]["reason"])

    def test_recovery_without_a_recorded_freeze_keeps_old_disk(self):
        # A recovery transition saved before freeze confirmation was recorded: no proof, so keep the disk.
        h = self.setup_rotation("delete_old", mode=cv.MODE_RECOVER, cutover_attempted=True, cutover_at=1.0,
                                freeze_requested=True, frozen_version=7)
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.tick()
        self.assertIn("--keep-disks=boot", self.old_delete(h))
        self.assertIn("never confirmed frozen", h.state[cv.STATE_KEY]["retained_disks"][0]["reason"])


class TransferredVersionFloorTests(unittest.TestCase):
    """Codex P1 round 16: a replacement holding less than the transferred state is never accepted."""

    setup_rotation = ResumeFromPersistedStepTests.setup_rotation
    with_new_vm = ResumeFromPersistedStepTests.with_new_vm

    def restarted_replacement_at_delete_old(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0, freeze_requested=True,
                                frozen_version=10, old_frozen=True)
        h.kv.set_origin("b")
        h.http.versions["a"] = 10  # the frozen old VM holds v10
        self.with_new_vm(h)
        h.http.versions["b"] = 8  # restarted after the import and recovered an older KV snapshot
        return h

    def old_deletes(self, h):
        return [c for c in h.gcloud.calls if c[2:5] == ["instances", "delete", "spot-control-a"]]

    def test_delete_old_never_deletes_old_vm_for_replacement_below_floor(self):
        h = self.restarted_replacement_at_delete_old()
        h.http.import_status = 500  # the re-import does not take
        h.tick()
        self.assertEqual(self.old_deletes(h), [])
        self.assertEqual(h.transition["step"], "delete_old")
        self.assertIn("below the transferred state_version 10", h.transition["error"])
        h.clock[0] += cv.REPLACEMENT_READY_GRACE_SECONDS
        h.tick()  # still below the floor after the grace: the regular rollback to the old VM
        self.assertIsNone(h.transition)
        self.assertEqual(self.old_deletes(h), [])
        self.assertIn("spot-control-a", h.gcloud.vms)
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertEqual(h.http.versions["a"], 10)
        self.assertIn(("unfreeze", "a"), h.events)
        self.assertEqual(h.state[cv.STATE_KEY]["history"][-1]["result"], "aborted")

    def test_reimport_brings_replacement_back_to_floor_before_deleting(self):
        h = self.restarted_replacement_at_delete_old()
        h.tick()
        self.assertEqual(h.mutations(), [("import", "b")])  # re-import from the frozen old VM's snapshot
        self.assertIn(("snapshot", "a"), h.events)
        self.assertEqual(self.old_deletes(h), [])  # fresh health must confirm it first
        self.assertEqual(h.http.versions["b"], 10)
        h.tick()
        self.assertIn("--delete-disks=all", self.old_deletes(h)[0])  # confirmed freeze and floor reached
        self.assertIsNone(h.transition)

    def test_reimport_attempts_are_bounded(self):
        h = self.restarted_replacement_at_delete_old()
        h.http.import_status = 500
        for _ in range(cv.STEP_RETRY_LIMIT + 2):
            h.tick()
        self.assertEqual(h.events.count(("import", "b")), cv.STEP_RETRY_LIMIT)
        self.assertEqual(self.old_deletes(h), [])

    def test_missing_state_version_is_below_floor(self):
        h = self.restarted_replacement_at_delete_old()
        h.http.versions["b"] = 10
        real = h.http.__call__

        def no_version(method, url, headers, body, timeout):
            status, raw = real(method, url, headers, body, timeout)
            if url.endswith("/api/health") and "spot-origin-b" in url:
                data = json.loads(raw)
                data.pop("state_version")
                raw = json.dumps(data).encode()
            return status, raw

        manager = cv.ControlVmManager(h.config, h.state, h.save, gcloud=h.gcloud, kv=h.kv, requester=no_version,
                                      offer_source=lambda: list(h.offers), clock=lambda: h.clock[0])
        manager.tick(apply=True)
        self.assertEqual(self.old_deletes(h), [])
        self.assertIn("reports state_version None", h.transition["error"])

    def test_import_response_version_is_the_floor(self):
        h = self.setup_rotation("import", freeze_requested=True, frozen_version=7, old_frozen=True)
        self.with_new_vm(h)
        h.http.versions["b"] = 9  # the import response confirms more than the frozen version
        h.tick()
        self.assertEqual(h.transition["required_version"], 9)
        self.assertEqual(h.kv.origin_slot(), "b")
        h.http.versions["b"] = 8  # above frozen_version 7, below what the replacement confirmed
        h.http.import_status = 500
        h.clock[0] += cv.PROPAGATION_SECONDS + 1
        h.tick()
        self.assertEqual(self.old_deletes(h), [])
        self.assertIn("below the transferred state_version 9", h.transition["error"])

    def test_kv_catch_up_version_is_the_floor(self):
        h = self.setup_rotation("cutover", mode=cv.MODE_RECOVER, import_skipped=True)
        h.gcloud.status("a", "TERMINATED")
        self.with_new_vm(h)
        h.http.versions["b"] = 7
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({"k": 8}, 8, "a", now=1))
        h.tick()
        self.assertEqual(h.transition["required_version"], 8)
        h.http.versions["b"] = 7  # restarted and lost the caught-up version
        h.http.import_status = 500
        h.clock[0] += cv.PROPAGATION_SECONDS + 1
        h.tick()
        self.assertEqual(self.old_deletes(h), [])
        self.assertIn("below the transferred state_version 8", h.transition["error"])

    def test_cutover_gate_rolls_back_when_replacement_stays_below_floor(self):
        h = self.setup_rotation("cutover", freeze_requested=True, frozen_version=7, old_frozen=True)
        self.with_new_vm(h)
        h.http.versions["b"] = 5
        h.http.import_status = 500
        h.tick()
        self.assertNotIn(("origin", "b"), h.events)  # users never pointed at the older state
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertIsNone(h.transition)  # price rotation: the frozen old VM is unfrozen and kept
        self.assertIn(("unfreeze", "a"), h.events)
        self.assertIn("spot-control-a", h.gcloud.vms)

    def test_reimport_reply_below_floor_is_not_a_success(self):
        h = self.setup_rotation("cutover", freeze_requested=True, frozen_version=7, old_frozen=True)
        self.with_new_vm(h)
        h.http.versions["b"] = 5
        real = h.http.__call__

        def short_import(method, url, headers, body, timeout):
            status, raw = real(method, url, headers, body, timeout)
            if url.endswith("/api/control/import") and "spot-origin-b" in url:
                h.http.versions["b"] = 5
                return 200, b'{"state_version": 5, "imported": true}'
            return status, raw

        manager = cv.ControlVmManager(h.config, h.state, h.save, gcloud=h.gcloud, kv=h.kv, requester=short_import,
                                      offer_source=lambda: list(h.offers), clock=lambda: h.clock[0])
        manager.tick(apply=True)
        self.assertIsNone(h.transition)  # not treated as re-imported: the rollback runs at once
        self.assertEqual(h.kv.origin_slot(), "a")

    def test_reimport_skips_a_cached_snapshot_below_the_floor(self):
        h = self.restarted_replacement_at_delete_old()
        manager = h.manager()
        manager._snapshot = build_snapshot({}, 5, "a", now=1)
        snapshot = manager._snapshot_for_import(h.transition, 10)
        self.assertEqual(snapshot["state_version"], 10)  # the frozen old VM's snapshot, not the stale cache

    def test_cutover_reimports_and_waits_for_health_to_confirm(self):
        h = self.setup_rotation("cutover", freeze_requested=True, frozen_version=7, old_frozen=True)
        self.with_new_vm(h)
        h.http.versions["b"] = 5
        h.tick()
        self.assertEqual(h.mutations(), [("import", "b")])
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertEqual(h.transition["step"], "cutover")
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertEqual(h.transition["step"], "propagate")


class CarryBackCurrentVersionTests(unittest.TestCase):
    """Codex P1 round 23: the rollback compares against the outgoing VM's current state, not frozen_version."""

    setup_rotation = ResumeFromPersistedStepTests.setup_rotation
    with_new_vm = ResumeFromPersistedStepTests.with_new_vm
    rollback_setup = ResumeFromPersistedStepTests.rollback_setup

    def setup(self, old_version):
        h = self.rollback_setup()  # frozen_version 7, cutover attempted, replacement unready past the grace
        self.with_new_vm(h, healthy=False)  # the replacement holds v7
        h.http.unhealthy("b")
        h.http.versions["a"] = old_version
        return h

    def test_outgoing_vm_below_frozen_version_gets_the_replacement_state_before_deletion(self):
        h = self.setup(old_version=5)  # it restarted mid-rotation and recovered an older KV snapshot
        h.tick()
        self.assertEqual(h.mutations(), [("freeze", "b"), ("import", "a"), ("unfreeze", "a"), ("origin", "a"),
                                         ("delete", "spot-control-b")])
        self.assertEqual(h.http.versions["a"], 7)
        self.assertIn("--delete-disks=all", delete_call(h, "spot-control-b"))
        self.assertEqual(h.state[cv.STATE_KEY].get("retained_disks", []), [])

    def test_unconfirmable_outgoing_vm_keeps_replacement_disk(self):
        h = self.setup(old_version=5)
        h.http.health.pop("a")  # health unreachable: the import cannot be confirmed
        h.gcloud.delete_rc = 1
        with self.assertLogs(cv.LOG, level="ERROR"):
            for _ in range(cv.STEP_RETRY_LIMIT):
                h.tick()
        self.assertEqual(h.events.count(("import", "a")), cv.STEP_RETRY_LIMIT)
        self.assertIn("--keep-disks=boot", delete_call(h, "spot-control-b"))
        manager = h.manager()
        status = manager.status(manager.observe(want_offers=False))
        self.assertIn("state_version None", status["carry_back_unconfirmed"])
        h.gcloud.delete_rc = 0
        h.tick()
        self.assertIsNone(h.transition)
        retained = h.state[cv.STATE_KEY]["retained_disks"]
        self.assertEqual([d["disk"] for d in retained], ["spot-control-b"])
        self.assertIn("state_version None", retained[0]["reason"])

    def test_outgoing_vm_below_floor_without_importable_snapshot_keeps_replacement_disk(self):
        h = self.setup(old_version=5)
        h.gcloud.status("b", "TERMINATED")  # no replacement snapshot; KV only has v6 (below the floor 7)
        h.kv.store[KV_STATE_KEY] = json.dumps(build_snapshot({}, 6, "b", now=1))
        h.state[cv.STATE_KEY]["last_start_at"] = {"b": h.clock[0]}  # just asked to start it: no restart now
        h.tick()
        self.assertNotIn(("import", "a"), h.events)
        self.assertIn("--keep-disks=boot", delete_call(h, "spot-control-b"))
        self.assertIn("no snapshot at or above", h.state[cv.STATE_KEY]["retained_disks"][0]["reason"])
        self.assertIsNone(h.transition)

    def test_outgoing_vm_at_frozen_version_needs_no_import(self):
        h = self.setup(old_version=7)
        h.tick()
        self.assertEqual(h.mutations(), [("freeze", "b"), ("unfreeze", "a"), ("origin", "a"),
                                         ("delete", "spot-control-b")])
        self.assertIn("--delete-disks=all", delete_call(h, "spot-control-b"))

    def test_misrouted_outgoing_health_is_not_its_version(self):
        h = self.setup(old_version=7)
        h.http.health["a"][1]["slot"] = "b"  # the old slot's hostname answered as another VM
        h.tick()
        self.assertNotIn(("delete", "spot-control-b"), h.events)
        self.assertIn("state_version None", h.transition["error"])

    def test_import_reply_alone_does_not_confirm(self):
        h = self.setup(old_version=5)
        real = h.http.__call__

        def lying_import(method, url, headers, body, timeout):
            status, raw = real(method, url, headers, body, timeout)
            if url.endswith("/api/control/import") and "spot-origin-a" in url:
                h.http.versions["a"] = 5  # claims success, still holds v5
                return 200, b'{"state_version": 7, "imported": true}'
            return status, raw

        manager = cv.ControlVmManager(h.config, h.state, h.save, gcloud=h.gcloud, kv=h.kv, requester=lying_import,
                                      offer_source=lambda: list(h.offers), clock=lambda: h.clock[0])
        manager.tick(apply=True)
        self.assertEqual(h.transition["step"], "abort")
        self.assertNotIn(("delete", "spot-control-b"), h.events)
        self.assertIn("state_version 5", h.transition["error"])


class InstanceIdentityTests(unittest.TestCase):
    """Codex P1: a VM recreated under a transition's slot name is never stopped, deleted, or reactivated."""

    setup_rotation = ResumeFromPersistedStepTests.setup_rotation
    with_new_vm = ResumeFromPersistedStepTests.with_new_vm

    def tracked(self, step, **fields):
        h = self.setup_rotation(step, **fields)
        if h.transition["old_vm"] is not None:
            h.transition["old_vm"]["instance_id"] = "id-a"
        h.state[cv.STATE_KEY]["transition"]["new_vm"]["instance_id"] = "id-b"
        return h

    def test_begin_and_create_record_instance_ids(self):
        h = Harness()
        ExecutorTests.healthy_active(None, h)
        h.gcloud.create_ids = ["id-b-created"]
        h.tick()
        self.assertEqual(h.transition["old_vm"]["instance_id"], "id-a")
        self.assertEqual(h.transition["new_vm"]["instance_id"], "id-b-created")

    def test_begin_pins_the_observed_instances_before_any_step(self):
        h = Harness()
        ExecutorTests.healthy_active(None, h)
        h.gcloud.add("b", status="TERMINATED", zone="us-east1-b", instance_id="id-b-existing")
        manager = h.manager()
        obs = manager.observe(want_offers=False)
        manager._begin(cv.MODE_RECOVER, cv.Plan("recreate_other_slot", "test", slot="a", target_slot="b"), obs)
        self.assertEqual(h.transition["old_vm"]["instance_id"], "id-a")
        self.assertEqual(h.transition["new_vm"]["instance_id"], "id-b-existing")
        self.assertEqual(h.gcloud.mutating(), [])

    def test_replacement_lost_before_health_adopts_the_slot_vm_found_by_create(self):
        h = Harness()
        ExecutorTests.healthy_active(None, h)
        h.tick()
        self.assertEqual(h.transition["new_vm"]["instance_id"], "id-b")
        h.gcloud.vms.pop("spot-control-b")
        h.tick()  # wait_health: gone, back to create
        self.assertEqual(h.transition["step"], "create")
        # The create step adopts whatever VM it finds in the slot (for example a create that landed late).
        h.gcloud.add("b", status="PROVISIONING", instance_id="id-b-late")
        h.tick()
        self.assertEqual(h.transition["step"], "wait_health")
        self.assertEqual(h.transition["new_vm"]["instance_id"], "id-b-late")
        self.assertNotIn("held", h.transition.get("error") or "")

    def test_replacement_swapped_right_after_create_is_held(self):
        h = Harness()
        ExecutorTests.healthy_active(None, h)
        real = h.gcloud.__call__

        def gcloud(argv):
            result = real(argv)
            if argv[2:4] == ["instances", "create"]:
                h.gcloud.recreate("b", "id-b-other")  # replaced before the first inventory listing
            return result

        manager = cv.ControlVmManager(h.config, h.state, h.save, gcloud=gcloud, kv=h.kv, requester=h.http,
                                      offer_source=lambda: list(h.offers), clock=lambda: h.clock[0])
        manager.tick(apply=True)
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.tick()
        self.assertEqual(h.mutations(), [("create", "spot-control-b", "us-central1-a")])
        self.assertIn("id-b-other", h.transition["error"])

    def test_delete_old_never_deletes_a_recreated_outgoing_vm(self):
        h = self.tracked("delete_old", cutover_attempted=True, cutover_at=1.0)
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.gcloud.recreate("a", "id-a2")
        h.tick()
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(h.transition["step"], "delete_old")
        self.assertTrue(h.transition["error"].startswith("held: outgoing spot-control-a"))
        self.assertIn("id-a2", h.transition["error"])
        self.assertIn("spot-control-a", h.gcloud.vms)
        h.gcloud.recreate("a", "id-a")  # the recorded instance: deletion proceeds
        h.tick()
        self.assertEqual(h.events, [("delete", "spot-control-a")])
        self.assertIsNone(h.transition)

    def test_abort_never_touches_a_recreated_replacement(self):
        for cutover in (False, True):
            with self.subTest(cutover_attempted=cutover):
                h = self.tracked("abort", freeze_requested=True, abort_reason="x", cutover_attempted=cutover)
                if cutover:
                    h.kv.set_origin("b")
                self.with_new_vm(h)
                h.gcloud.recreate("b", "id-b2")
                h.tick()
                self.assertEqual(h.mutations(), [])  # no freeze/stop/delete of it, no unfreeze or origin write
                self.assertEqual(h.gcloud.mutating(), [])
                self.assertIn("spot-control-b", h.gcloud.vms)
                self.assertEqual(h.transition["step"], "abort")
                self.assertIn("id-b2", h.transition["error"])
                h.gcloud.recreate("b", "id-b")
                h.tick()
                self.assertIsNone(h.transition)
                self.assertNotIn("spot-control-b", h.gcloud.vms)

    def test_cutover_never_points_origin_at_a_recreated_replacement(self):
        h = self.tracked("cutover", freeze_requested=True, frozen_version=7)
        self.with_new_vm(h)
        h.gcloud.recreate("b", "id-b2")
        h.tick()
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertEqual(h.mutations(), [])

    def test_recovery_fence_never_stops_a_recreated_outgoing_vm(self):
        h = self.tracked("cutover", mode=cv.MODE_RECOVER, import_skipped=True, kv_caught_up=True)
        h.http.unhealthy("a")
        self.with_new_vm(h)
        h.gcloud.recreate("a", "id-a2")
        h.tick()
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(h.kv.origin_slot(), "a")

    def test_outgoing_slot_without_a_vm_at_begin_never_adopts_a_new_one(self):
        h = self.tracked("delete_old", mode=cv.MODE_RECOVER, cutover_attempted=True, cutover_at=1.0,
                         import_skipped=True, old_vm=None)
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.tick()  # spot-control-a exists, but the transition began without one
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertIn("had no VM when this transition began", h.transition["error"])

    def test_replacement_lost_before_health_is_tracked_again_when_recreated(self):
        h = Harness()
        ExecutorTests.healthy_active(None, h)
        h.gcloud.create_returns_row = False  # ids come from the inventory, not the create output
        h.gcloud.create_ids = ["id-b1", "id-b2"]
        h.tick()
        h.tick()  # first observation records id-b1
        self.assertEqual(h.transition["new_vm"]["instance_id"], "id-b1")
        h.gcloud.vms.pop("spot-control-b")  # preempted and deleted before it became healthy
        h.tick()
        self.assertEqual(h.transition["step"], "create")
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.tick()
        self.assertEqual(h.transition["new_vm"]["instance_id"], "id-b2")
        self.assertEqual(h.transition["step"], "propagate")
        self.assertEqual(h.kv.origin_slot(), "b")

    def test_direct_delete_and_stop_guards_refuse_unconfirmed_instances(self):
        h = self.tracked("delete_old", cutover_attempted=True, cutover_at=1.0)
        self.with_new_vm(h)
        h.gcloud.recreate("a", "id-a2")
        manager = h.manager()
        obs = manager.observe(want_offers=False)
        t = h.transition
        self.assertFalse(manager._delete_vm(t, obs.vm("a"), role="old", keep_boot_disk=False, reason="x"))
        self.assertFalse(manager._request_stop(t, "old", obs.vm("a"), "old_stop_requested_at", "test"))
        t["new_vm"]["instance_id"] = None  # never recorded: deletes and stops do not adopt
        self.assertFalse(manager._delete_vm(t, obs.vm("b"), role="new", keep_boot_disk=False, reason="x"))
        self.assertFalse(manager._request_stop(t, "new", obs.vm("b"), "new_stop_requested_at", "test"))
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertIn("refusing to stop", t["error"])

    def test_legacy_transition_adopts_ids_on_resume_then_enforces_them(self):
        h = self.setup_rotation("propagate", cutover_attempted=True, cutover_at=h_now() - 30)
        self.assertNotIn("instance_id", h.transition["old_vm"])  # saved before ids were recorded
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.tick()
        self.assertEqual((h.transition["old_vm"]["instance_id"], h.transition["new_vm"]["instance_id"]),
                         ("id-a", "id-b"))
        h.gcloud.recreate("a", "id-a2")
        h.clock[0] += cv.PROPAGATION_SECONDS
        h.tick()
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertIn("spot-control-a", h.gcloud.vms)

    def test_legacy_transition_holds_when_outgoing_vm_moved_zones(self):
        h = self.setup_rotation("delete_old", cutover_attempted=True, cutover_at=1.0)
        h.kv.set_origin("b")
        self.with_new_vm(h)
        h.gcloud.vms["spot-control-a"]["zone"] = "projects/proj/zones/us-west1-a"  # recreated elsewhere
        h.tick()
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertIsNone(h.transition["old_vm"].get("instance_id"))
        self.assertIn("recorded zone us-central1-a", h.transition["error"])


def delete_call(h, name):
    calls = [c for c in h.gcloud.calls if c[2:4] == ["instances", "delete"] and c[4] == name]
    assert len(calls) == 1, calls
    return calls[0]


def h_now():
    return 100000.0


class CreateCommandTests(unittest.TestCase):
    def test_delete_command_disk_modes(self):
        self.assertIn("--delete-disks=all", cv.delete_command(vm("a"), "proj"))
        kept = cv.delete_command(vm("a"), "proj", keep_boot_disk=True)
        self.assertIn("--keep-disks=boot", kept)
        self.assertNotIn("--delete-disks=all", kept)

    def test_flags(self):
        command = cv.create_command(config(), "b", offer("t2d-standard-1", "us-east1-b"), "/tmp/s.sh")
        self.assertEqual(command[:5], ["gcloud", "compute", "instances", "create", "spot-control-b"])
        for flag in ("--project=proj", "--zone=us-east1-b", "--machine-type=t2d-standard-1",
                     "--provisioning-model=SPOT", "--instance-termination-action=STOP",
                     "--boot-disk-size=20GB", "--boot-disk-auto-delete",
                     "--labels=spot-role=control,spot-control-slot=b", "--scopes=cloud-platform",
                     "--metadata-from-file=startup-script=/tmp/s.sh"):
            self.assertIn(flag, command)
        self.assertNotIn("--no-address", command)
        metadata = next(a for a in command if a.startswith("--metadata="))
        items = dict(pair.split("=", 1) for pair in metadata.removeprefix("--metadata=").split(","))
        self.assertEqual(items["spot-control-slot"], "b")
        self.assertEqual(items["dashboard-image"], IMAGE)
        self.assertEqual(set(items) - set(cv.METADATA_KEYS), set())
        joined = " ".join(command)
        for secret in ("access-secret", SECRET, "access-id"):
            self.assertNotIn(secret, joined)

    def test_no_external_ip_and_service_account(self):
        command = cv.create_command(config(external_ip=False, service_account="sa@proj.iam.gserviceaccount.com"),
                                    "a", offer(), "/tmp/s.sh")
        self.assertIn("--no-address", command)
        self.assertIn("--service-account=sa@proj.iam.gserviceaccount.com", command)

    def test_source_build_metadata(self):
        command = cv.create_command(config(dashboard_image="", dashboard_source_ref="b" * 40), "a", offer(), "/s")
        metadata = next(a for a in command if a.startswith("--metadata="))
        self.assertIn("dashboard-source-ref=" + "b" * 40, metadata)
        self.assertNotIn("dashboard-image=", metadata)

    def test_invalid_settings_are_rejected(self):
        for bad in (dict(dashboard_image="", dashboard_source_ref=""), dict(dashboard_image="ghcr.io/x:latest"),
                    dict(cloudflare_account_id=""), dict(cloudflared_image="cloudflare/cloudflared:latest"),
                    dict(public_url="http://spot.example")):
            with self.assertRaises(ValueError, msg=bad):
                cv.create_command(config(**bad), "a", offer(), "/s")
        with self.assertRaises(ValueError):
            cv.create_command(config(), "a", offer("n2-standard-2"), "/s")
        with self.assertRaises(ValueError):
            cv.metadata_items(config(public_url="https://a,b"), "a")

    def test_session_not_before_travels_as_metadata(self):
        self.assertNotIn("dashboard-session-not-before", cv.metadata_items(config(), "a"))
        now = int(time.time())
        items = cv.metadata_items(config(session_not_before=str(now - 10)), "a")
        self.assertEqual(items["dashboard-session-not-before"], str(now - 10))
        command = cv.create_command(config(session_not_before=str(now - 10)), "a", offer(), "/s")
        self.assertIn(f"dashboard-session-not-before={now - 10}", next(a for a in command if a.startswith("--metadata=")))
        cfg = cv.ControlConfig.from_env("proj", {"DASHBOARD_SESSION_NOT_BEFORE": f" {now} "}, dashboard_image=IMAGE)
        self.assertEqual(cfg.session_not_before, str(now))
        for bad in ("soon", "-5", "1.5", str(now + 3600), "1" * 16):
            with self.assertRaises(ValueError, msg=bad):
                cv.create_command(config(session_not_before=bad), "a", offer(), "/s")
        self.assertEqual(cv.session_not_before_problems(str(now + 30), now=now), [])

    def test_config_from_env(self):
        env = {"SPOT_CONTROL_REGIONS": "us-west1, us-east4", "SPOT_CONTROL_EXTERNAL_IP": "0",
               "SPOT_CONTROL_MAX_DAILY_USD": "0.25", "CLOUDFLARE_ACCOUNT_ID": "acc", "CF_ACCESS_CLIENT_ID": "i",
               "DASHBOARD_CONTROL_SECRET": "s"}
        cfg = cv.ControlConfig.from_env("proj", env, dashboard_image=IMAGE, regions=None)
        self.assertEqual(cfg.regions, ("us-west1", "us-east4"))
        self.assertFalse(cfg.external_ip)
        self.assertEqual(cfg.max_daily_usd, Decimal("0.25"))
        self.assertEqual(cfg.dashboard_image, IMAGE)
        self.assertNotIn("control_secret='s'", repr(cfg))


class DashboardTargetPolicyTests(unittest.TestCase):
    """SPOT_CONFIG_REPO and DASHBOARD_CONTROLLED_TARGETS reach the control VM's dashboard."""

    ENV = {"SPOT_CONFIG_REPO": " swack-tools/runner-config ",
           "DASHBOARD_CONTROLLED_TARGETS": "swack-tools, xbmc4lyfe/newznab-tmux,Swack-Tools"}

    def test_from_env_carries_both_settings(self):
        cfg = cv.ControlConfig.from_env("proj", self.ENV, dashboard_image=IMAGE)
        self.assertEqual(cfg.config_repo, "swack-tools/runner-config")
        self.assertEqual(cfg.controlled_targets, "swack-tools, xbmc4lyfe/newznab-tmux,Swack-Tools")
        unset = cv.ControlConfig.from_env("proj", {}, dashboard_image=IMAGE)
        self.assertEqual((unset.config_repo, unset.controlled_targets), ("", ""))
        override = cv.ControlConfig.from_env("proj", self.ENV, dashboard_image=IMAGE, config_repo="a/b")
        self.assertEqual(override.config_repo, "a/b")

    def test_metadata_and_create_command_include_both(self):
        cfg = config(config_repo="swack-tools/runner-config",
                     controlled_targets="swack-tools, xbmc4lyfe/newznab-tmux,Swack-Tools")
        items = cv.metadata_items(cfg, "a")
        self.assertEqual(items["dashboard-config-repo"], "swack-tools/runner-config")
        # Normalized as the dashboard parses it (case-insensitive de-duplication), ;-separated for gcloud.
        self.assertEqual(items["dashboard-controlled-targets"], "swack-tools;xbmc4lyfe/newznab-tmux")
        metadata = next(a for a in cv.create_command(cfg, "a", offer(), "/s") if a.startswith("--metadata="))
        self.assertIn("dashboard-config-repo=swack-tools/runner-config", metadata.split(","))
        self.assertIn("dashboard-controlled-targets=swack-tools;xbmc4lyfe/newznab-tmux", metadata.split(","))
        for key in ("dashboard-config-repo", "dashboard-controlled-targets"):
            self.assertIn(key, cv.METADATA_KEYS)
            self.assertNotIn(key, cv.metadata_items(config(), "a"))

    def test_invalid_values_are_refused(self):
        for bad in ("swack-tools", "a/b/c", "-bad/repo", "owner/..", "owner/.", "owner/re po", "owner/a,b",
                    "o" * 40 + "/repo", "owner/" + "r" * 101):
            self.assertTrue(cv.config_repo_problems(bad), bad)
            with self.assertRaises(ValueError, msg=bad):
                cv.create_command(config(config_repo=bad), "a", offer(), "/s")
            with self.assertRaises(ValueError, msg=bad):
                cv.metadata_items(config(config_repo=bad), "a")
        for bad in ("swack-tools;other", "a/b/c", "-org", "owner/..", "owner/re=po", "https://github.com/a/b"):
            self.assertTrue(cv.dashboard_target_problems("", bad), bad)
            with self.assertRaises(ValueError, msg=bad):
                cv.create_command(config(controlled_targets=bad), "a", offer(), "/s")
        self.assertEqual(cv.dashboard_target_problems("swack-tools/x", "swack-tools owner/repo"), [])
        # The planner reports both before it plans a create, not only when the command is built.
        problems = config(config_repo="nope", controlled_targets="a/b/c").create_problems()
        self.assertTrue(any(p.startswith("SPOT_CONFIG_REPO") for p in problems), problems)
        self.assertTrue(any(p.startswith("DASHBOARD_CONTROLLED_TARGETS") for p in problems), problems)
        self.assertEqual(config(config_repo="a/b", controlled_targets="org,a/c").create_problems(), [])

    def test_autoscaler_config_repo_reaches_the_control_vm(self):
        captured = {}

        def factory(project, state, save, **overrides):
            captured.update(overrides)
            raise RuntimeError("stop")
        with patch.object(cv, "build_manager", side_effect=factory):
            cv.autoscaler_tick(SimpleNamespace(dry_run=True, project="p", config_repo="swack-tools/cfg"), {},
                               lambda: None)
            self.assertEqual(captured["config_repo"], "swack-tools/cfg")
            captured.clear()
            cv.autoscaler_tick(SimpleNamespace(dry_run=True, project="p"), {}, lambda: None)
            self.assertIsNone(captured["config_repo"])

    def test_cli_overrides_include_both(self):
        args = cv.args_parser().parse_args(["plan", "--config-repo", "a/b", "--controlled-targets", "org,a/c"])
        overrides = cv._overrides(args)
        self.assertEqual((overrides["config_repo"], overrides["controlled_targets"]), ("a/b", "org,a/c"))


class PricingTests(unittest.TestCase):
    def test_shapes_ranked_with_fractional_e2_cores_and_boot_disk(self):
        rates = {("us-central1", "e2"): (Decimal("0.01"), Decimal("0.001")),
                 ("us-central1", "t2d"): (Decimal("0.012"), Decimal("0.0012"))}
        zones = {"e2-small": ["us-central1-a", "us-east1-b"], "e2-medium": ["us-central1-a"],
                 "t2d-standard-1": ["us-central1-b"]}
        offers = cv.price_control_offers(
            ["us-central1"], zones, rates=lambda region, family: rates[(region, family)],
            storage=lambda region: {"pd-balanced:capacity": Decimal("0.10")})
        self.assertEqual([o.machine_type for o in offers], ["e2-small", "e2-medium", "t2d-standard-1"])
        disk_daily = Decimal("0.10") * 20 * 12 / Decimal("365")
        expected = (Decimal("0.01") * Decimal("0.5") + Decimal("0.001") * 2) * 24
        self.assertAlmostEqual(float(offers[0].daily_usd), float(expected + disk_daily), places=4)
        self.assertTrue(all(o.region == "us-central1" for o in offers))

    def test_choose_offer_respects_cap_and_exclusions(self):
        self.assertEqual(cv.choose_offer(OFFERS, Decimal("0.5"), ["us-central1-a"]).zone, "us-east1-b")
        self.assertEqual(cv.choose_offer(OFFERS[:1], Decimal("0.5"), ["us-central1-a"]).zone, "us-central1-a")
        self.assertIsNone(cv.choose_offer(OFFERS, Decimal("0.05")))


class AutoscalerHookTests(unittest.TestCase):
    def test_flag_defaults_off_and_parses(self):
        from lib.spot_runner_autoscaler import args_parser
        self.assertFalse(args_parser().parse_args([]).manage_control_vm)
        self.assertTrue(args_parser().parse_args(["--manage-control-vm"]).manage_control_vm)

    def test_dry_run_prints_plan_and_never_saves(self):
        h = Harness()
        saved = []
        manager = cv.ControlVmManager(h.config, h.state, lambda: saved.append(1), gcloud=h.gcloud, kv=h.kv,
                                      requester=h.http, offer_source=lambda: OFFERS, clock=lambda: 1.0)
        out = io.StringIO()
        with redirect_stdout(out):
            plan = cv.autoscaler_tick(SimpleNamespace(dry_run=True, project="proj"), h.state, lambda: None,
                                      manager=manager)
        self.assertEqual(plan.action, "bootstrap")
        self.assertIn("Control VM plan (dry run): bootstrap", out.getvalue())
        self.assertEqual(saved, [])
        self.assertEqual(h.gcloud.mutating(), [])

    def test_failures_never_raise_into_the_runner_loop(self):
        class Broken:
            def tick(self, apply):
                raise RuntimeError("gcloud exploded")
        self.assertIsNone(cv.autoscaler_tick(SimpleNamespace(dry_run=False, project="p"), {}, lambda: None,
                                             manager=Broken()))

    def test_autoscaler_dry_run_passes_a_copy(self):
        from lib import spot_runner_autoscaler as autoscaler
        state = {"control_vm": {"transition": {"id": "t", "step": "wait_health"}}}
        with tempfile.TemporaryDirectory() as tmp, patch("lib.control_vm.autoscaler_tick") as tick:
            autoscaler.manage_control_vm(SimpleNamespace(dry_run=True, project="proj", control_state_dir=tmp),
                                         state, Path("/nonexistent/state.json"))
            passed_state, save = tick.call_args[0][1], tick.call_args[0][2]
            self.assertIsNot(passed_state, state)
            # The plan sees the unmigrated transition, but nothing moves and nothing is written.
            self.assertEqual(passed_state["control_vm"]["transition"]["id"], "t")
            self.assertEqual(state, {"control_vm": {"transition": {"id": "t", "step": "wait_health"}}})
            save()  # No-op: must not try to write the nonexistent path.
            self.assertFalse(cv.project_control_paths("proj", tmp)[0].exists())


class ProjectControlStateTests(unittest.TestCase):
    """PR #44 review: one project-wide lock and state for every controller that manages the control VM."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def args(self, **extra):
        return SimpleNamespace(dry_run=False, project="proj", control_state_dir=str(self.dir), **extra)

    def tick_with(self, state, state_path, record):
        from lib import spot_runner_autoscaler as autoscaler

        def tick(args, cstate, save):
            record.append(copy.deepcopy(cstate))
            cstate.setdefault("control_vm", {})["transition"] = {"id": f"t{len(record)}", "step": "wait_health"}
            save()
            return "planned"
        with patch("lib.control_vm.autoscaler_tick", side_effect=tick):
            return autoscaler.manage_control_vm(self.args(), state, state_path)

    def test_second_controller_resumes_the_same_transition(self):
        record = []
        first, second = {}, {}
        self.assertEqual(self.tick_with(first, self.dir / "a.json", record), "planned")
        # A crash of the first controller leaves the transition in the project file, not in a.json.
        self.assertNotIn("control_vm", first)
        self.assertEqual(self.tick_with(second, self.dir / "b.json", record), "planned")
        self.assertEqual(record[1]["control_vm"]["transition"]["id"], "t1")
        project_file = cv.project_control_paths("proj", self.dir)[0]
        self.assertEqual(json.loads(project_file.read_text())["control_vm"]["transition"]["id"], "t2")

    def test_held_lock_skips_the_check(self):
        import fcntl
        from lib import spot_runner_autoscaler as autoscaler
        _, lock_file = cv.project_control_paths("proj", self.dir)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with lock_file.open("a") as held, patch("lib.control_vm.autoscaler_tick") as tick, \
                self.assertLogs("spot_runner_autoscaler", "INFO") as logs:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsNone(autoscaler.manage_control_vm(self.args(), {}, self.dir / "a.json"))
        tick.assert_not_called()
        self.assertIn("another controller holds the proj control VM lock", "\n".join(logs.output))
        # Released: the next check runs.
        record = []
        self.assertEqual(self.tick_with({}, self.dir / "a.json", record), "planned")

    def test_lock_is_held_while_the_tick_runs(self):
        import fcntl
        from lib import spot_runner_autoscaler as autoscaler
        _, lock_file = cv.project_control_paths("proj", self.dir)
        seen = []

        def tick(args, cstate, save):
            with lock_file.open("a") as other:
                try:
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append("acquired")
                except BlockingIOError:
                    seen.append("held")
        with patch("lib.control_vm.autoscaler_tick", side_effect=tick):
            autoscaler.manage_control_vm(self.args(), {}, self.dir / "a.json")
        self.assertEqual(seen, ["held"])

    def test_unmigrated_transition_moves_to_the_project_state(self):
        from lib import spot_runner_autoscaler as autoscaler
        state_path = self.dir / "a.json"
        legacy = {"idle_loops": 0, "control_vm": {"transition": {"id": "old", "step": "freeze"}}}
        autoscaler.write_scale_state(state_path, legacy)
        state = autoscaler.read_scale_state(state_path)
        record = []
        self.tick_with(state, state_path, record)
        self.assertEqual(record[0]["control_vm"]["transition"]["id"], "old")  # resumed, not restarted
        self.assertNotIn("control_vm", state)
        self.assertNotIn("control_vm", json.loads(state_path.read_text()))

    def test_conflicting_transitions_refuse_without_ticking(self):
        from lib import spot_runner_autoscaler as autoscaler
        project = cv.ProjectControlState(cv.project_control_paths("proj", self.dir)[0])
        project.state = {"control_vm": {"transition": {"id": "theirs"}}}
        project.save()
        state = {"control_vm": {"transition": {"id": "mine"}}}
        with patch("lib.control_vm.autoscaler_tick") as tick, \
                self.assertLogs("spot_runner_autoscaler", "ERROR") as logs:
            self.assertIsNone(autoscaler.manage_control_vm(self.args(), state, self.dir / "a.json"))
        tick.assert_not_called()
        self.assertIn("both hold an in-progress control VM transition", "\n".join(logs.output))
        self.assertEqual(state["control_vm"]["transition"]["id"], "mine")  # nothing dropped

    def test_adopt_rules(self):
        project = cv.ProjectControlState(self.dir / "p.json")
        project.state = {"control_vm": {"transition": {"id": "x"}}}
        same = {"control_vm": {"transition": {"id": "x"}}}
        self.assertTrue(project.adopt(same, "a", persist=False))
        self.assertNotIn("control_vm", same)
        idle = {"control_vm": {"down_since": {"a": 1}, "transition": None}}
        self.assertTrue(project.adopt(idle, "a", persist=False))
        self.assertEqual(project.state["control_vm"]["transition"]["id"], "x")  # idle copies never overwrite
        self.assertFalse(project.adopt({"idle_loops": 1}, "a", persist=False))
        empty = cv.ProjectControlState(self.dir / "q.json")
        disks = {"control_vm": {"retained_disks": [{"disk": "spot-control-a", "zone": "z", "slot": "a"}]}}
        self.assertTrue(empty.adopt(disks, "a", persist=True))
        self.assertEqual(json.loads((self.dir / "q.json").read_text())["control_vm"]["retained_disks"][0]["disk"],
                         "spot-control-a")

    def test_unreadable_project_state_fails_closed(self):
        from lib import spot_runner_autoscaler as autoscaler
        path = cv.project_control_paths("proj", self.dir)[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1]")
        with patch("lib.control_vm.autoscaler_tick") as tick, self.assertLogs("spot_runner_autoscaler", "ERROR"):
            self.assertIsNone(autoscaler.manage_control_vm(self.args(), {}, self.dir / "a.json"))
        tick.assert_not_called()
        self.assertEqual(path.read_text(), "[1]")

    def test_paths_are_project_scoped_and_validated(self):
        a, lock = cv.project_control_paths("homelab-424523", self.dir)
        self.assertEqual((a.name, lock.name), ("control-vm-homelab-424523.json", "control-vm-homelab-424523.lock"))
        self.assertNotEqual(cv.project_control_paths("other-project", self.dir)[0], a)
        self.assertEqual(cv.project_control_paths("example.com:proj", self.dir)[0].name,
                         "control-vm-example.com__proj.json")
        for bad in ("", "../etc", "Proj", "a/b", "p" * 64):
            with self.assertRaises(ValueError, msg=bad):
                cv.project_control_paths(bad, self.dir)
        self.assertEqual(cv.default_control_state_dir({}), cv.DEFAULT_STATE_FILE.parent)
        self.assertEqual(cv.default_control_state_dir({cv.ENV_CONTROL_STATE_DIR: str(self.dir)}), self.dir)


class CliTests(unittest.TestCase):
    def test_render_startup_writes_the_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "startup.sh"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cv.main(["render-startup", "--output", str(target)]), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), SCRIPT.read_text(encoding="utf-8"))

    def run_cli(self, argv, harness):
        def factory(project, state, save, **overrides):
            harness.state = state
            return cv.ControlVmManager(harness.config, state, save, gcloud=harness.gcloud, kv=harness.kv,
                                       requester=harness.http, offer_source=lambda: OFFERS,
                                       clock=lambda: harness.clock[0])
        out = io.StringIO()
        argv = [*argv, "--control-state-dir", str(self.control_dir)] if "--control-state-dir" not in argv else argv
        with redirect_stdout(out), patch("lib.environment.load_env"):
            code = cv.main(argv, manager_factory=factory)
        return code, out.getvalue()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.control_dir = Path(directory.name)

    def test_plan_ensure_rotate_without_apply_change_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            outputs = {}
            for command in ("plan", "ensure", "rotate", "status"):
                h = Harness()
                h.gcloud.add("a", machine="e2-medium")
                h.kv.set_origin("a")
                h.http.healthy("a")
                code, outputs[command] = self.run_cli([command, "--state-file", str(state_file)], h)
                self.assertEqual(code, 0)
                self.assertEqual(h.gcloud.mutating(), [], command)
                self.assertEqual(h.kv.puts, [], command)
                self.assertFalse(state_file.exists(), command)
            for command in ("plan", "ensure", "rotate"):
                self.assertIn("Plan (nothing changed): rotate", outputs[command])
                self.assertIn("Re-run with --apply", outputs[command])
            self.assertEqual(json.loads(outputs["status"])["origin"]["slot"], "a")

    def test_ensure_apply_persists_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            h = Harness()
            code, output = self.run_cli(["ensure", "--apply", "--state-file", str(state_file)], h)
            self.assertEqual(code, 0)
            self.assertIn("Executed: bootstrap", output)
            self.assertFalse(state_file.exists())  # the controller's own state file is not the control VM's
            saved = json.loads(cv.project_control_paths("homelab-424523", self.control_dir)[0].read_text())
            self.assertEqual(saved["control_vm"]["transition"]["step"], "wait_health")

    def test_apply_moves_unmigrated_controller_state_and_refuses_a_held_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            disk = {"disk": "spot-control-b", "zone": "us-east1-b", "slot": "b", "transition": "t0", "reason": "r", "at": 1}
            state_file.write_text(json.dumps({"idle_loops": 3, "control_vm": {"retained_disks": [disk]}}))
            h = Harness()
            code, _ = self.run_cli(["ensure", "--apply", "--state-file", str(state_file)], h)
            self.assertEqual(code, 0)
            self.assertNotIn("control_vm", json.loads(state_file.read_text()))
            self.assertEqual(json.loads(state_file.read_text())["idle_loops"], 3)
            project_file, lock_file = cv.project_control_paths("homelab-424523", self.control_dir)
            self.assertEqual(json.loads(project_file.read_text())["control_vm"]["retained_disks"], [disk])
            with lock_file.open("a") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "Another controller is managing the control VM"):
                    self.run_cli(["ensure", "--apply", "--state-file", str(state_file)], Harness())


class StartupScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_render_returns_file_content(self):
        self.assertEqual(cv.render_control_startup(), self.text)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_syntax(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True, timeout=30)

    def test_strict_mode_and_no_tracing(self):
        self.assertIn("set -euo pipefail", self.text)
        self.assertIn("umask 077", self.text)
        code = "\n".join(line for line in self.text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"set\s+-[a-z]*x|xtrace|bash\s+-x")

    def test_reads_all_metadata_keys_and_secrets(self):
        for key in cv.METADATA_KEYS:
            self.assertIn(key, self.text)
        for name in cv.SECRET_NAMES:
            if name.startswith("spot-dashboard-tunnel-token-"):
                continue
            self.assertIn(f"fetch_secret {name}", self.text)
        self.assertIn('fetch_secret "spot-dashboard-tunnel-token-$SLOT"', self.text)
        self.assertIn("secretmanager.googleapis.com/v1/projects/$PROJECT/secrets/$name/versions/latest:access",
                      self.text)
        self.assertIn("instance/service-accounts/default/token", self.text)

    def test_env_file_contents_and_permissions(self):
        for variable in ("GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET", "GITHUB_OAUTH_REDIRECT_URI",
                         "DASHBOARD_SESSION_SECRET", "DASHBOARD_CONTROL_SECRET", "CLOUDFLARE_ACCOUNT_ID",
                         "CLOUDFLARE_KV_NAMESPACE_ID", "CLOUDFLARE_KV_API_TOKEN", "SPOT_CONFIG_GH_TOKEN",
                         "GH_TOKEN", "SPOT_GCP_TOKEN_SOURCE=metadata"):
            self.assertIn(variable, self.text)
        self.assertIn('/auth/github/callback', self.text)
        self.assertIn("ENV_FILE=\"$CONFIG_DIR/dashboard.env\"", self.text)
        self.assertIn("chmod 0600", self.text)
        self.assertIn('install -d -m 0700 -o root -g root "$CONFIG_DIR"', self.text)

    def test_secrets_only_travel_through_files_stdin_or_environment(self):
        self.assertIn("--password-stdin", self.text)
        self.assertIn("http.extraHeader", self.text)
        self.assertIn("-H @<(printf 'Authorization: Bearer %s\\n' \"$TOKEN\")", self.text)
        self.assertNotIn("x-access-token:${", self.text)
        self.assertNotRegex(self.text, r"https://[^\s\"]*@github\.com")
        self.assertNotIn("--token", self.text)
        for variable in ("TOKEN", "GITHUB_TOKEN", "oauth_secret", "session_secret", "control_secret",
                         "kv_token", "tunnel_token"):
            self.assertNotRegex(self.text, rf"(echo|log)\s[^\n]*\${variable}\b")
            self.assertNotRegex(self.text, rf"(echo|log)\s[^\n]*\${{{variable}}}")
        self.assertNotIn("metadata-from", self.text)

    def test_containers_match_contract(self):
        self.assertIn('docker rm -f "$DASHBOARD_CONTAINER"', self.text)
        self.assertIn('docker rm -f "$TUNNEL_CONTAINER"', self.text)
        self.assertIn("readonly DASHBOARD_CONTAINER=spot-dashboard", self.text)
        self.assertIn("readonly TUNNEL_CONTAINER=spot-cloudflared", self.text)
        self.assertEqual(self.text.count("--restart unless-stopped --network host"), 2)
        self.assertIn('--env-file "$ENV_FILE" -v "$STATE_DIR:/state"', self.text)
        self.assertIn('--host 127.0.0.1 --port 8787 --state-file /state/state.json --control-slot "$SLOT" \\\n'
                      '    --project "$PROJECT"', self.text)
        self.assertIn('(umask 022 && git -C "$workdir" -c advice.detachedHead=false checkout -q FETCH_HEAD)',
                      self.text)
        self.assertIn("tunnel --no-autoupdate run", self.text)
        self.assertIn("readonly CONTAINER_UID=10001", self.text)
        self.assertIn('install -d -m 0750 -o "$CONTAINER_UID" -g "$CONTAINER_UID" "$STATE_DIR"', self.text)

    def _bash_function(self, name):
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", self.text, re.S | re.M)
        self.assertIsNotNone(match, name)
        return match.group(0)

    def _run_function(self, name, value):
        return subprocess.run(["bash", "-c", self._bash_function(name) + f'\n{name} "$1"', "check", value],
                              capture_output=True, text=True, timeout=30)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_session_not_before_check_matches_create_problems(self):
        self.assertIn('SESSION_NOT_BEFORE="$(metadata_attr dashboard-session-not-before)"', self.text)
        self.assertIn('session_not_before_ok "$SESSION_NOT_BEFORE"', self.text)
        self.assertIn("printf 'DASHBOARD_SESSION_NOT_BEFORE=%s\\n' \"$SESSION_NOT_BEFORE\"", self.text)
        now = int(time.time())
        for value in ("", str(now - 100), str(now), "0", "0099", "soon", "-1", "1.5", str(now + 3600), "1" * 16, "1 2"):
            expected = not cv.session_not_before_problems(value)
            self.assertEqual(self._run_function("session_not_before_ok", value).returncode == 0, expected, value)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_config_repo_check_matches_create_problems(self):
        self.assertIn('CONFIG_REPO="$(metadata_attr dashboard-config-repo)"', self.text)
        self.assertIn('config_repo_ok "$CONFIG_REPO" || fail', self.text)
        for value in ("", "swack-tools/spot-runners-config", "a/b", "a-1/x.y_z", "swack-tools", "a/b/c",
                      "-a/b", "a/..", "a/.", "a/b c", "a/b,c", "a/b;c", "o" * 39 + "/r", "o" * 40 + "/r",
                      "a/" + "r" * 100, "a/" + "r" * 101, "a/b\nc"):
            expected = not cv.config_repo_problems(value)
            self.assertEqual(self._run_function("config_repo_ok", value).returncode == 0, expected, value)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_controlled_targets_round_trip_through_metadata(self):
        from lib.dashboard_auth import parse_controlled_targets_env
        self.assertIn('CONTROLLED_TARGETS_METADATA="$(metadata_attr dashboard-controlled-targets)"', self.text)
        self.assertIn('CONTROLLED_TARGETS="$(controlled_targets_csv "$CONTROLLED_TARGETS_METADATA")" \\\n'
                      '    || fail', self.text)
        for raw in ("", "swack-tools", "swack-tools, xbmc4lyfe/newznab-tmux", "a/b c/d Org"):
            metadata = cv.metadata_items(config(controlled_targets=raw), "a").get("dashboard-controlled-targets", "")
            result = self._run_function("controlled_targets_csv", metadata)
            self.assertEqual(result.returncode, 0, raw)
            self.assertEqual([t.name for t in parse_controlled_targets_env(result.stdout)],
                             cv.controlled_target_names(raw), raw)
            self.assertNotIn(";", result.stdout)
        # Hand-edited metadata is held to the same rules: every entry must be an org or owner/repo.
        for bad in ("a;;b", ";a", "a;", "a b", "a,b", "a/b/c", "-a", "a/..", "a/.", "a/b=c", "o" * 40):
            self.assertNotEqual(self._run_function("controlled_targets_csv", bad).returncode, 0, bad)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_env_file_gets_target_policy(self):
        stubs = "fetch_secret() { printf 'secret-%s' \"$1\"; }\nwrite_private_file() { cat; }\n"
        body = self._bash_function("write_dashboard_env")

        def env_file(config_repo, targets):
            script = (stubs + body + '\nPUBLIC_URL=https://spot.example SLOT=a PUBLIC_ORIGIN=https://spot.example '
                      'SESSION_NOT_BEFORE= CLOUDFLARE_ACCOUNT_ID=a CLOUDFLARE_KV_NAMESPACE_ID=n GITHUB_TOKEN=t '
                      'CONFIG_REPO="$1" CONTROLLED_TARGETS="$2"\nwrite_dashboard_env\n')
            result = subprocess.run(["bash", "-c", script, "check", config_repo, targets],
                                    capture_output=True, text=True, timeout=30, check=True)
            return result.stdout.splitlines()
        lines = env_file("swack-tools/runner-config", "swack-tools,a/b")
        self.assertIn("SPOT_CONFIG_REPO=swack-tools/runner-config", lines)
        self.assertIn("DASHBOARD_CONTROLLED_TARGETS=swack-tools,a/b", lines)
        lines = env_file("", "")
        self.assertFalse(any(line.startswith(("SPOT_CONFIG_REPO=", "DASHBOARD_CONTROLLED_TARGETS=")) for line in lines))

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_global_admins_round_trip_through_metadata(self):
        """PR review: DASHBOARD_GLOBAL_ADMINS (including "none") reaches the managed dashboard."""
        from lib.dashboard_auth import parse_global_admins
        self.assertIn('GLOBAL_ADMINS_METADATA="$(metadata_attr dashboard-global-admins)"', self.text)
        self.assertIn("printf 'DASHBOARD_GLOBAL_ADMINS=%s\\n' \"$GLOBAL_ADMINS\"", self.text)
        for raw in ("", "none", "swackhamer:619624", "Swackhamer:619624"):
            metadata = cv.metadata_items(config(global_admins=raw), "a").get("dashboard-global-admins", "")
            result = self._run_function("global_admins_csv", metadata)
            self.assertEqual(result.returncode, 0, raw)
            self.assertEqual(parse_global_admins(result.stdout), parse_global_admins(raw), raw)
        self.assertEqual(cv.metadata_items(config(global_admins="none"), "a")["dashboard-global-admins"], "none")
        self.assertNotIn("dashboard-global-admins", cv.metadata_items(config(), "a"))
        for bad in ("swackhamer", "a:0", "a:x", "a:1;", "a:1;;b:2", "bad login:1", "alice:123",
                    "swackhamer:1", "swackhamer:619624;alice:123"):
            self.assertNotEqual(self._run_function("global_admins_csv", bad).returncode, 0, bad)
        problems = config(global_admins="swackhamer").create_problems()
        self.assertTrue(any("DASHBOARD_GLOBAL_ADMINS" in p for p in problems), problems)
        env = {"DASHBOARD_GLOBAL_ADMINS": "none"}
        self.assertEqual(cv.ControlConfig.from_env("proj", env).global_admins, "none")

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_cloudflared_pin_check_accepts_what_create_problems_accepts(self):
        self.assertIn('image_ref_pinned "$CLOUDFLARED_IMAGE" || fail', self.text)
        digest = "@sha256:" + "0" * 64
        accepted = [cv.DEFAULT_CLOUDFLARED_IMAGE, "cloudflare/cloudflared:2026.9.3",
                    "cloudflare/cloudflared" + digest, "cloudflare/cloudflared:latest" + digest]
        rejected = ["cloudflare/cloudflared", "cloudflare/cloudflared:latest", "Cloudflare/cloudflared:1",
                    "cloudflare/cloudflared:1 x", "cloudflare/cloudflared@sha256:abc", "registry:5000/cf:1", ""]
        for image in accepted + rejected:
            expected = image in accepted
            config_ok = not any("cloudflared" in problem for problem in config(cloudflared_image=image).create_problems())
            self.assertEqual(config_ok, expected, image)
            self.assertEqual(self._run_function("image_ref_pinned", image).returncode == 0, expected, image)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_public_origin_written_to_env_file(self):
        self.assertIn("printf 'DASHBOARD_PUBLIC_ORIGIN=%s\\n' \"$PUBLIC_ORIGIN\"", self.text)
        self.assertIn('PUBLIC_ORIGIN="$(public_origin_of "$PUBLIC_URL")" || fail', self.text)
        for url, origin in (("https://spot.swacktech.com", "https://spot.swacktech.com"),
                            ("https://spot.swacktech.com/", "https://spot.swacktech.com"),
                            ("https://spot.swacktech.com:8443/", "https://spot.swacktech.com:8443")):
            result = self._run_function("public_origin_of", url)
            self.assertEqual((result.returncode, result.stdout), (0, origin), url)
        # A path is refused too: the OAuth callback and the Worker serve the dashboard at the origin root.
        for url in ("https://bad host", "https://user@spot.swacktech.com", "https://",
                    "https://spot.swacktech.com:8443/app"):
            self.assertNotEqual(self._run_function("public_origin_of", url).returncode, 0, url)

    PUBLIC_URLS = (
        "https://spot.swacktech.com", "https://spot.swacktech.com/", "https://Spot.SwackTech.com",
        "https://localhost", "https://spot.example:1", "https://spot.example:8443", "https://spot.example:65535/",
        "https://a-b.example", "https://" + "a" * 63 + ".example",
        # Refused by both:
        "https://spot.example?bad", "https://spot.example/?q=1", "https://spot.example#frag",
        "https://spot.example/#", "https://spot.example/path", "https://spot.example//",
        "https://user@spot.example", "https://user:pw@spot.example", "https://spot.example:0", "https://spot.example:08443",
        "https://spot.example:65536", "https://spot.example:123456", "https://spot.example:", "https://:8443",
        "https://", "http://spot.example", "HTTPS://spot.example", "https:/spot.example", "spot.example",
        "https://spot..example", "https://.example", "https://example.", "https://-bad.example",
        "https://bad-.example", "https://" + "a" * 64 + ".example", "https://spot_example.com",
        "https://[::1]", "https://spot.example\n", "https://spot example", "https://spot.example,b", "",
    )

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_public_url_check_matches_create_problems(self):
        self.assertIn('PUBLIC_ORIGIN="$(public_origin_of "$PUBLIC_URL")" || fail', self.text)
        accepted = 0
        for url in self.PUBLIC_URLS:
            python_origin = cv.public_origin(url)
            result = self._run_function("public_origin_of", url)
            bash_origin = result.stdout if result.returncode == 0 else None
            self.assertEqual(python_origin, bash_origin, repr(url))
            problems = [p for p in config(public_url=url).create_problems() if "public URL" in p]
            self.assertEqual(not problems, python_origin is not None, repr(url))
            accepted += python_origin is not None
        self.assertEqual(accepted, 9)

    def test_each_public_url_check_refuses_on_its_own(self):
        # The parsed-URL check and the authority check are independent; each refuses these by itself.
        for url in ("https://u@spot.example", "https://u:p@spot.example", "https://spot.example?x",
                    "https://spot.example?", "https://spot.example#f", "https://spot.example/#", "https://spot.example/p",
                    "https://spot.example:65536", "https://spot.example:0", "https://spot.example:x", "http://spot.example",
                    "https://", "https:///p", None):
            self.assertFalse(cv._public_url_parts_ok(url), url)
        for url in ("https://u@spot.example", "https://spot.example?x", "https://spot.example#f",
                    "https://spot.example/p", "https://spot.example:65536", "https://spot.example:0",
                    "https://spot..example", "https://spot_example.com", "HTTPS://spot.example", None):
            self.assertIsNone(cv._public_authority(url), url)
        for url in ("https://spot.example", "https://spot.example:8443/"):
            self.assertTrue(cv._public_url_parts_ok(url), url)
            self.assertIsNotNone(cv._public_authority(url), url)

    def test_malformed_public_url_never_reaches_gcloud(self):
        for url in ("https://spot.example?bad", "https://spot.example/app", "https://u@spot.example"):
            with self.assertRaisesRegex(ValueError, "public URL", msg=url):
                cv.create_command(config(public_url=url), "a", offer(), "/s")

    def test_image_paths(self):
        self.assertIn("docker login ghcr.io", self.text)
        self.assertIn('docker build -q -f "$workdir/dashboard/Dockerfile" -t "$image"', self.text)
        self.assertIn('[[ "$head" == "$SOURCE_REF" ]]', self.text)
        self.assertIn('image_present "$image"', self.text)
        self.assertIn('image_present "$DASHBOARD_IMAGE"', self.text)
        self.assertIn("docker.io", self.text)


if __name__ == "__main__":
    unittest.main()


class PartialInventoryTests(unittest.TestCase):
    """Codex M4 round 4: partial gcloud inventory must never drive lifecycle actions."""

    def test_inventory_diagnostics_block_every_action(self):
        h = Harness()
        h.gcloud.add("a", status="TERMINATED")
        h.kv.set_origin("a")
        h.state[cv.STATE_KEY] = {"down_since": {"a": 1.0}}
        h.gcloud.list_stderr = "WARNING: Some requests did not succeed: zone us-east1-b unavailable"
        with self.assertRaisesRegex(RuntimeError, "completeness unknown"):
            h.tick()
        self.assertEqual(h.mutations(), [])
        self.assertNotIn("spot-control-b", h.gcloud.vms)

    FILTER_KEYS_WARNING = "WARNING: The following filter keys were not present in any resource : labels.spot-role\n"
    UPDATE_NOTICE = ("\n\nUpdates are available for some Google Cloud CLI components.  To install them,\n"
                     "please run:\n  $ gcloud components update\n\n")

    def test_empty_inventory_filter_keys_warning_still_bootstraps(self):
        """Live bootstrap: with zero control VMs gcloud exits 0 and warns that the label key matched nothing."""
        h = Harness()
        h.gcloud.list_stderr = self.FILTER_KEYS_WARNING
        obs = h.manager().observe(want_offers=False)
        self.assertEqual(obs.vms, [])
        plan = h.tick()
        self.assertEqual(plan.action, "bootstrap")
        self.assertEqual(h.events[0], ("create", "spot-control-a", "us-central1-a"))

    def test_update_notice_is_tolerated(self):
        for stderr in (self.UPDATE_NOTICE, self.FILTER_KEYS_WARNING + self.UPDATE_NOTICE):
            with self.subTest(stderr=stderr):
                h = Harness()
                h.gcloud.list_stderr = stderr
                self.assertEqual(h.tick().action, "bootstrap")

    def test_unknown_lines_mixed_with_benign_ones_still_block(self):
        for stderr in (self.FILTER_KEYS_WARNING + "WARNING: Some requests did not succeed:\n - zone unavailable\n",
                       "WARNING: Some requests did not succeed:\n" + self.UPDATE_NOTICE,
                       "please run:\n",  # a follow-up line without the update notice it belongs to
                       "WARNING: The following filter keys were not present in any resource : labels.other\n"):
            with self.subTest(stderr=stderr):
                h = Harness()
                h.gcloud.list_stderr = stderr
                with self.assertRaisesRegex(RuntimeError, "completeness unknown"):
                    h.tick()
                self.assertEqual(h.mutations(), [])
                self.assertEqual(h.gcloud.mutating(), [])

    def test_default_gcloud_disables_partial_compute_results(self):
        with mock.patch.object(cv.subprocess, "run") as run:
            cv.default_gcloud(["gcloud", "compute", "instances", "list"])
            self.assertEqual(run.call_args.kwargs["env"]["CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR"], "false")
            cv.default_gcloud(["gcloud", "secrets", "versions", "access", "latest"])
            self.assertIsNone(run.call_args.kwargs["env"])


class MalformedOriginTests(unittest.TestCase):
    """Codex M4 round 10: an invalid origin:active must hold lifecycle changes, not allow bootstrap."""

    def test_read_origin_distinguishes_missing_from_malformed(self):
        h = Harness()
        self.assertIsNone(edge_state.read_origin(h.kv))  # genuinely missing
        for raw in ("{not json", '{"slot": "z", "url": "https://x", "vm": "v", "updated_at": 1}', "[]"):
            with self.subTest(raw=raw):
                h.kv.store[edge_state.KV_ORIGIN_KEY] = raw
                with self.assertRaises(edge_state.KvError):
                    edge_state.read_origin(h.kv)

    def test_malformed_origin_blocks_bootstrap_with_both_slots_present(self):
        h = Harness()
        h.gcloud.add("a")
        h.gcloud.add("b", zone="us-east1-b")
        h.http.healthy("a")
        h.http.healthy("b")
        h.kv.store[edge_state.KV_ORIGIN_KEY] = "{corrupt"
        h.tick()
        self.assertEqual(h.kv.store[edge_state.KV_ORIGIN_KEY], "{corrupt")  # never rewritten
        self.assertEqual(h.mutations(), [])


class SavedOfferRevalidationTests(unittest.TestCase):
    """Codex M4 round 19: a resumed create never provisions above the cap on a stale saved quote."""

    def resume_create(self, offers):
        h = ResumeFromPersistedStepTests().setup_rotation("create")
        h.offers = offers
        h.tick()
        return [e for e in h.events if e[0] == "create"]

    def test_saved_offer_now_over_cap_is_not_used(self):
        # Saved plan: e2-small in us-east1-b at 0.10/day. Today that placement costs 0.80 (> cap).
        creates = self.resume_create([offer("e2-small", "us-east1-b", "0.80"),
                                      offer("e2-small", "us-central1-a", "0.20")])
        self.assertEqual(creates, [("create", "spot-control-b", "us-central1-a")])

    def test_saved_offer_still_affordable_is_preferred(self):
        creates = self.resume_create([offer("e2-small", "us-central1-a", "0.05"),
                                      offer("e2-small", "us-east1-b", "0.30")])
        self.assertEqual(creates, [("create", "spot-control-b", "us-east1-b")])

    def test_failed_price_scan_creates_nothing(self):
        self.assertEqual(self.resume_create([]), [])


# --------------------------------------------------------------------------------------------
# Free-tier standard (always-on e2-micro) provisioning mode
# --------------------------------------------------------------------------------------------

def micro(zone="us-central1-a"):
    return offer("e2-micro", zone, "0")


STANDARD_OFFERS = [micro("us-central1-a"), micro("us-central1-b"), micro("us-central1-c"), micro("us-central1-f")]


def std_vm(slot, status="RUNNING", zone="us-central1-a"):
    return cv.ControlVm(f"spot-control-{slot}", zone, status, slot, "e2-micro", f"id-{slot}", cv.MODEL_STANDARD)


def spot_vm(slot, status="RUNNING", zone="us-central1-a", machine="e2-small"):
    return cv.ControlVm(f"spot-control-{slot}", zone, status, slot, machine, f"id-{slot}", cv.MODEL_SPOT)


class ProvisioningConfigTests(unittest.TestCase):
    def test_default_is_spot_and_env_or_override_selects_standard(self):
        spot = cv.ControlConfig.from_env("proj", {}, dashboard_image=IMAGE)
        self.assertEqual((spot.provisioning, spot.regions, spot.standard), ("spot", cv.DEFAULT_REGIONS, False))
        std = cv.ControlConfig.from_env("proj", {"SPOT_CONTROL_PROVISIONING": " Standard "}, dashboard_image=IMAGE)
        self.assertEqual((std.provisioning, std.regions, std.standard), ("standard", ("us-central1",), True))
        listed = cv.ControlConfig.from_env("proj", {"SPOT_CONTROL_PROVISIONING": "standard",
                                                    "SPOT_CONTROL_REGIONS": "us-west1,us-east1"})
        self.assertEqual(listed.regions, ("us-west1", "us-east1"))
        self.assertEqual(listed.provisioning_problems(), [])
        override = cv.ControlConfig.from_env("proj", {"SPOT_CONTROL_PROVISIONING": "spot"}, provisioning="standard")
        self.assertEqual((override.provisioning, override.regions), ("standard", ("us-central1",)))
        cli = cv.ControlConfig.from_env("proj", {"SPOT_CONTROL_PROVISIONING": "standard"}, provisioning=None)
        self.assertEqual(cli.provisioning, "standard")  # an unset CLI flag keeps the environment's mode

    def test_cli_flag_reaches_the_overrides(self):
        args = cv.args_parser().parse_args(["plan", "--provisioning", "standard"])
        self.assertEqual(cv._overrides(args)["provisioning"], "standard")
        self.assertIsNone(cv._overrides(cv.args_parser().parse_args(["plan"]))["provisioning"])
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            cv.args_parser().parse_args(["plan", "--provisioning", "preemptible"])

    def test_standard_regions_must_be_free_tier(self):
        for regions in (("us-east4",), ("us-central1", "europe-west1"), ("us-west2",)):
            cfg = config(provisioning="standard", regions=regions)
            self.assertTrue(cfg.provisioning_problems(), regions)
            self.assertTrue(any("free-tier" in p for p in cfg.runtime_problems()), regions)
            self.assertTrue(cfg.create_problems(), regions)
            with self.assertRaises(ValueError, msg=regions):
                cv.create_command(cfg, "b", micro(f"{regions[0]}-a"), "/s")
        for regions in (("us-central1",), ("us-east1",), ("us-west1",), cv.FREE_TIER_REGIONS):
            self.assertEqual(config(provisioning="standard", regions=regions).runtime_problems(), [], regions)
        # Spot keeps its region freedom.
        self.assertEqual(config(regions=("us-east4",)).runtime_problems(), [])

    def test_misspelt_mode_fails_closed(self):
        cfg = cv.ControlConfig.from_env("proj", {"SPOT_CONTROL_PROVISIONING": "standrd"})
        self.assertEqual(cfg.provisioning, "standrd")
        self.assertTrue(any("must be spot or standard" in p for p in cfg.runtime_problems()))
        h = Harness(provisioning="standrd")
        h.gcloud.add("a", status="TERMINATED")
        h.kv.set_origin("a")
        self.assertEqual(h.tick().action, "wait")
        self.assertEqual(h.gcloud.mutating(), [])

    def test_free_tier_disk_size(self):
        self.assertLessEqual(cv.STANDARD_BOOT_DISK_GB * 2, cv.FREE_TIER_DISK_GB)
        self.assertGreaterEqual(cv.STANDARD_BOOT_DISK_GB, 10)  # the debian-12 image needs 10 GB
        self.assertEqual(config(provisioning="standard").boot_disk, (cv.STANDARD_BOOT_DISK_GB, "pd-standard"))
        self.assertEqual(config().boot_disk, (cv.BOOT_DISK_GB, "pd-balanced"))


class ProvisioningCreateCommandTests(unittest.TestCase):
    def test_standard_flags(self):
        command = cv.create_command(config(provisioning="standard"), "b", micro("us-central1-b"), "/tmp/s.sh")
        for flag in ("--zone=us-central1-b", "--machine-type=e2-micro", "--provisioning-model=STANDARD",
                     "--maintenance-policy=MIGRATE", "--no-restart-on-failure", "--boot-disk-type=pd-standard",
                     f"--boot-disk-size={cv.STANDARD_BOOT_DISK_GB}GB", "--boot-disk-auto-delete",
                     "--labels=spot-role=control,spot-control-slot=b", "--shielded-secure-boot"):
            self.assertIn(flag, command)
        joined = " ".join(command)
        for spot_only in ("SPOT", "--instance-termination-action", "pd-balanced", "--preemptible"):
            self.assertNotIn(spot_only, joined)
        self.assertNotIn("--no-address", command)  # GitHub has no IPv6, so the free-tier VM keeps its IPv4

    def test_spot_flags_unchanged(self):
        command = cv.create_command(config(), "b", offer("e2-small", "us-east1-b"), "/tmp/s.sh")
        for flag in ("--provisioning-model=SPOT", "--instance-termination-action=STOP", "--boot-disk-type=pd-balanced",
                     "--boot-disk-size=20GB"):
            self.assertIn(flag, command)
        for standard_only in ("STANDARD", "--maintenance-policy", "--no-restart-on-failure", "pd-standard"):
            self.assertNotIn(standard_only, " ".join(command))

    def test_shapes_and_zones_follow_the_mode(self):
        for shape in ("e2-small", "e2-medium", "t2d-standard-1", "e2-standard-2"):
            with self.assertRaises(ValueError, msg=shape):
                cv.create_command(config(provisioning="standard"), "a", offer(shape, "us-central1-a", "0"), "/s")
        with self.assertRaises(ValueError):
            cv.create_command(config(), "a", micro(), "/s")  # Spot never creates the free-tier shape
        with self.assertRaises(ValueError):  # a zone outside the configured free-tier region
            cv.create_command(config(provisioning="standard", regions=("us-central1",)), "a", micro("us-east1-b"), "/s")
        with self.assertRaises(ValueError):  # a zone outside every free-tier region
            cv.create_command(config(provisioning="standard"), "a", micro("us-east4-a"), "/s")


class StandardOfferTests(unittest.TestCase):
    def test_fixed_offers_in_region_order_without_prices(self):
        zones = {"e2-micro": ["us-west1-b", "us-central1-c", "us-central1-a", "us-east4-a", "us-east1-b",
                              "us-west1-a"],
                 "e2-small": ["us-central1-a"]}
        offers = cv.standard_control_offers(["us-central1", "us-west1", "us-east4"], zones)
        self.assertEqual([o.zone for o in offers], ["us-central1-a", "us-central1-c", "us-west1-a", "us-west1-b"])
        self.assertEqual({(o.machine_type, o.daily_usd) for o in offers}, {("e2-micro", Decimal("0"))})

    def test_offer_source_lists_e2_micro_zones_and_never_reads_prices(self):
        calls = []

        def gcloud(argv):
            calls.append(argv)
            rows = [{"name": "e2-micro", "zone": f"zones/{z}"} for z in ("us-central1-b", "us-central1-a", "us-east1-b")]
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        boom = mock.Mock(side_effect=AssertionError("no price lookup in standard mode"))
        with patch("lib.runner_manager.spot_rates", boom), patch("lib.storage_selection.storage_rates", boom):
            offers = cv.default_offer_source(config(provisioning="standard", regions=("us-central1",)), gcloud)
        self.assertEqual([o.zone for o in offers], ["us-central1-a", "us-central1-b"])
        self.assertIn("--filter=name=(e2-micro)", calls[0])
        boom.assert_not_called()
        self.assertIn("--filter=name=(e2-small e2-medium t2d-standard-1)", cv.zones_command("proj"))

    def test_inventory_reports_the_provisioning_model(self):
        base = {"name": "spot-control-a", "zone": "zones/us-central1-a", "status": "TERMINATED",
                "labels": {"spot-role": "control", "spot-control-slot": "a"}, "id": "1"}
        cases = [({"provisioningModel": "STANDARD", "preemptible": False}, cv.MODEL_STANDARD),
                 ({"preemptible": False}, cv.MODEL_STANDARD),
                 ({"provisioningModel": "SPOT"}, cv.MODEL_SPOT),
                 ({"provisioningModel": "STANDARD", "preemptible": True}, cv.MODEL_SPOT),
                 ({}, ""), (None, "")]
        for scheduling, model in cases:
            row = dict(base) if scheduling is None else dict(base, scheduling=scheduling)
            [parsed] = cv.parse_control_vms([row])
            self.assertEqual(parsed.provisioning_model, model, scheduling)
        self.assertIn("scheduling.provisioningModel", " ".join(cv.list_command("proj")))


class StandardPlannerTests(unittest.TestCase):
    def plan(self, vms, *, provisioning="standard", offers=STANDARD_OFFERS, cstate=None, now=100000.0, **kw):
        return cv.plan_next(vms=list(vms), origin=OriginRecord.for_slot("a", now=1), origin_known=True,
                            health=kw.pop("health", {"a": ok()}), offers=offers, control_state=cstate or {},
                            now=now, max_daily_usd=Decimal("0.50"), provisioning=provisioning, **kw)

    def test_no_price_rotation_away_from_a_standard_vm(self):
        cheaper = [offer("e2-small", "us-east1-b", "0.01")]
        for provisioning, offers in (("standard", STANDARD_OFFERS), ("spot", cheaper), ("spot", OFFERS)):
            plan = self.plan([std_vm("a")], provisioning=provisioning, offers=offers)
            self.assertEqual(plan.action, "noop", (provisioning, plan.reason))
            self.assertIn("never rotated for price", plan.reason)
        self.assertIn("run rotate", self.plan([std_vm("a")], provisioning="spot", offers=cheaper).reason)
        # An explicit operator rotation still works (for example to move back to Spot).
        self.assertEqual(self.plan([std_vm("a")], provisioning="spot", offers=cheaper, force_rotate=True).action,
                         "rotate")

    def test_stopped_standard_vm_is_restarted_not_replaced(self):
        long_down = {"down_since": {"a": 1.0}}
        plan = self.plan([std_vm("a", "TERMINATED")], cstate=long_down, health={})
        self.assertEqual((plan.action, plan.slot), ("start", "a"))
        self.assertIn("not preempted", plan.reason)
        plan = self.plan([std_vm("a", "TERMINATED")], cstate=dict(long_down, start_failed_at={"a": 50.0}), health={})
        self.assertEqual((plan.action, plan.target_slot), ("recreate_other_slot", "b"))
        self.assertEqual(plan.offer.zone, "us-central1-b")  # another free-tier zone of the same region
        recent = {"down_since": {"a": 100000.0 - 60}, "start_failed_at": {"a": 50.0}}
        self.assertEqual(self.plan([std_vm("a", "TERMINATED")], cstate=recent, health={}).action, "start")
        # A stopped Spot VM is still treated as preempted capacity and replaced after the grace period.
        self.assertEqual(self.plan([spot_vm("a", "TERMINATED")], cstate=long_down, health={}).action,
                         "recreate_other_slot")

    def test_switch_replaces_a_known_spot_vm_with_the_free_tier_vm(self):
        plan = self.plan([spot_vm("a")])
        self.assertEqual((plan.action, plan.slot, plan.target_slot), ("rotate", "a", "b"))
        self.assertEqual((plan.offer.machine_type, plan.offer.daily_usd), ("e2-micro", Decimal("0")))
        # Unknown provisioning model: never rotated for the mode, so a VM can't rotate in a loop.
        unknown = cv.ControlVm("spot-control-a", "us-central1-a", "RUNNING", "a", "e2-small", "id-a")
        self.assertEqual(self.plan([unknown]).action, "noop")
        # Cooldown after an earlier transition or abort spaces out retries.
        cooling = self.plan([spot_vm("a")], cstate={"last_abort_at": 100000.0 - 60})
        self.assertEqual(cooling.action, "noop")
        self.assertIn("cooldown", cooling.reason)
        # Spot mode never switches a Spot VM; no free-tier offer means nothing to switch to.
        self.assertEqual(self.plan([spot_vm("a")], provisioning="spot", offers=OFFERS[:1]).action, "noop")
        self.assertEqual(self.plan([spot_vm("a")], offers=[]).action, "noop")
        # A stray VM in the other slot still blocks every automatic rotation.
        self.assertEqual(self.plan([spot_vm("a"), std_vm("b")]).action, "noop")


class StandardExecutorTests(unittest.TestCase):
    def harness(self):
        return Harness(offers=STANDARD_OFFERS, provisioning="standard")

    def test_stopped_standard_vm_restart_then_replacement_only_after_failed_starts(self):
        h = self.harness()
        h.gcloud.add("a", status="TERMINATED", machine="e2-micro", provisioning="STANDARD")
        h.kv.set_origin("a")
        h.state[cv.STATE_KEY] = {"down_since": {"a": 1.0}}  # long past every grace period
        self.assertEqual(h.tick().action, "start")
        self.assertEqual(h.mutations(), [("start", "spot-control-a")])
        h.gcloud.status("a", "TERMINATED")  # stopped again, but the start itself succeeded
        h.clock[0] += cv.START_RETRY_SECONDS
        self.assertEqual(h.tick().action, "start")
        h.gcloud.status("a", "TERMINATED")
        h.gcloud.start_rc = 1
        h.clock[0] += cv.START_RETRY_SECONDS
        self.assertEqual(h.tick().action, "start")
        self.assertIn("a", h.state[cv.STATE_KEY]["start_failed_at"])
        h.clock[0] += cv.START_RETRY_SECONDS
        self.assertEqual(h.tick().action, "start")  # failing, but still inside the grace from the last start
        h.clock[0] += cv.START_GRACE_SECONDS
        self.assertEqual(h.tick().action, "recreate_other_slot")
        self.assertEqual(h.events[-1], ("create", "spot-control-b", "us-central1-b"))
        create = next(c for c in h.gcloud.calls if c[2:4] == ["instances", "create"])
        self.assertIn("--provisioning-model=STANDARD", create)

    def test_successful_start_clears_the_failure_and_restarts_the_grace(self):
        h = self.harness()
        h.gcloud.add("a", status="TERMINATED", machine="e2-micro", provisioning="STANDARD")
        h.kv.set_origin("a")
        h.state[cv.STATE_KEY] = {"down_since": {"a": h.clock[0] - 60}}
        h.gcloud.start_rc = 1
        h.tick()
        h.gcloud.start_rc = 0
        h.clock[0] += cv.START_RETRY_SECONDS
        self.assertEqual(h.tick().action, "start")
        self.assertNotIn("a", h.state[cv.STATE_KEY]["start_failed_at"])
        self.assertEqual(h.state[cv.STATE_KEY]["down_since"]["a"], h.clock[0])
        # Booting (and unhealthy) for longer than the original outage plus the start grace: not replaced.
        h.gcloud.status("a", "RUNNING")
        h.http.unhealthy("a")
        h.clock[0] += cv.START_GRACE_SECONDS
        self.assertEqual(h.tick().action, "wait")
        h.gcloud.status("a", "TERMINATED")
        h.clock[0] += cv.START_RETRY_SECONDS
        self.assertEqual(h.tick().action, "start")
        self.assertNotIn("spot-control-b", h.gcloud.vms)

    def test_spot_start_keeps_the_original_outage_time(self):
        h = Harness()
        h.gcloud.add("a", status="TERMINATED", provisioning="SPOT")
        h.kv.set_origin("a")
        h.state[cv.STATE_KEY] = {"down_since": {"a": h.clock[0] - 60}}
        self.assertEqual(h.tick().action, "start")
        self.assertEqual(h.state[cv.STATE_KEY]["down_since"]["a"], h.clock[0] - 60)

    def spot_active(self, h, version=7):
        h.gcloud.add("a", machine="e2-small", provisioning="SPOT")
        h.kv.set_origin("a")
        h.http.healthy("a", version=version)
        # A recent price check must not delay the switch.
        h.state[cv.STATE_KEY] = {"last_price_check_at": h.clock[0]}

    def test_spot_to_standard_cutover_end_to_end(self):
        h = self.harness()
        self.spot_active(h)
        plan = h.tick()
        self.assertEqual((plan.action, plan.slot, plan.target_slot), ("rotate", "a", "b"))
        self.assertEqual((h.transition["mode"], h.transition["provisioning"]), (cv.MODE_PRICE, "standard"))
        create = next(c for c in h.gcloud.calls if c[2:4] == ["instances", "create"])
        for flag in ("--machine-type=e2-micro", "--provisioning-model=STANDARD", "--maintenance-policy=MIGRATE",
                     "--boot-disk-type=pd-standard"):
            self.assertIn(flag, create)
        self.assertNotIn("--instance-termination-action=STOP", create)
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.tick()
        self.assertEqual(h.transition["step"], "propagate")
        self.assertTrue(h.transition["old_frozen"])
        self.assertEqual(h.transition["required_version"], 7)
        self.assertIn("spot-control-a", h.gcloud.vms)
        h.clock[0] += cv.PROPAGATION_SECONDS + 1
        h.tick()
        self.assertEqual(h.mutations(), [("create", "spot-control-b", "us-central1-a"), ("freeze", "a"),
                                         ("import", "b"), ("origin", "b"), ("delete", "spot-control-a")])
        self.assertIn("--delete-disks=all", delete_call(h, "spot-control-a"))  # confirmed freeze: no retained disk
        self.assertIsNone(h.transition)
        self.assertEqual(h.kv.origin_slot(), "b")
        self.assertEqual(h.http.versions["b"], 7)
        self.assertEqual(list(h.gcloud.vms), ["spot-control-b"])
        self.assertEqual(h.gcloud.vms["spot-control-b"]["scheduling"]["provisioningModel"], "STANDARD")
        # Settled on the free-tier VM: later checks never rotate it for price.
        before = len(h.mutations())
        for _ in range(3):
            h.clock[0] += cv.ROTATION_COOLDOWN_SECONDS + cv.PRICE_CHECK_INTERVAL_SECONDS
            self.assertEqual(h.tick().action, "noop")
        self.assertEqual(len(h.mutations()), before)

    def test_switch_never_skips_the_freeze(self):
        h = self.harness()
        self.spot_active(h)
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.http.freeze_status = 503
        for _ in range(cv.STEP_RETRY_LIMIT):
            h.tick()
            h.clock[0] += 60
        self.assertIsNone(h.transition)
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertNotIn(("origin", "b"), h.events)
        self.assertNotIn(("import", "b"), h.events)
        self.assertNotIn("spot-control-b", h.gcloud.vms)
        self.assertIn("spot-control-a", h.gcloud.vms)
        self.assertEqual(h.state[cv.STATE_KEY]["history"][-1]["result"], "aborted")

    def test_switch_keeps_the_version_floor_before_deleting_spot(self):
        h = self.harness()
        self.spot_active(h)
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.tick()
        self.assertEqual(h.transition["step"], "propagate")
        h.http.versions["b"] = 3  # the replacement restarted and recovered an older KV snapshot
        h.http.import_status = 500
        h.clock[0] += cv.PROPAGATION_SECONDS + 1
        h.tick()
        self.assertIn("spot-control-a", h.gcloud.vms)
        self.assertNotIn(("delete", "spot-control-a"), h.events)
        self.assertEqual(h.transition["step"], "delete_old")

    def test_switch_with_unreachable_old_slot_stops_and_keeps_its_disk(self):
        # The Spot VM was preempted right after the switch began: it finishes as a recovery, fenced by stop.
        h = self.harness()
        self.spot_active(h)
        h.tick()
        h.gcloud.status("b", "RUNNING")
        h.http.healthy("b")
        h.gcloud.status("a", "TERMINATED")
        h.tick()
        self.assertEqual(h.transition["mode"], cv.MODE_RECOVER)
        self.assertTrue(h.transition["import_skipped"])
        h.clock[0] += cv.PROPAGATION_SECONDS + 1
        h.tick()
        self.assertIn("--keep-disks=boot", delete_call(h, "spot-control-a"))

    def test_controller_in_the_other_mode_never_creates_the_planned_replacement(self):
        h = self.harness()
        self.spot_active(h)
        h.gcloud.fail_create_zones = {o.zone for o in STANDARD_OFFERS}
        h.tick()
        self.assertEqual((h.transition["step"], h.transition["provisioning"]), ("create", "standard"))
        attempts = len(h.gcloud.create_attempts)
        h.config = config()  # a second controller still configured for Spot resumes the shared transition
        h.offers = list(OFFERS)
        h.gcloud.fail_create_zones = set()
        for _ in range(cv.PRICE_CREATE_MAX_FAILURES):
            h.clock[0] += 60
            h.tick()
        self.assertEqual(len(h.gcloud.create_attempts), attempts)
        self.assertNotIn("spot-control-b", h.gcloud.vms)
        self.assertIsNone(h.transition)  # the switch aborts; the Spot VM keeps serving
        self.assertEqual(h.kv.origin_slot(), "a")
        self.assertIn("configured for SPOT_CONTROL_PROVISIONING=spot",
                      h.state[cv.STATE_KEY]["history"][-1]["reason"])

    def test_autoscaler_path_reads_the_mode_from_the_environment(self):
        seen = []

        def tick(self, apply):
            seen.append((self.config.provisioning, self.config.regions, apply))
            return cv.Plan("noop", "test")
        with patch.dict("os.environ", {"SPOT_CONTROL_PROVISIONING": "standard"}), \
                patch.dict("os.environ", {"SPOT_CONTROL_REGIONS": ""}), \
                patch.object(cv.ControlVmManager, "tick", tick):
            cv.autoscaler_tick(SimpleNamespace(dry_run=False, project="proj"), {}, lambda: None)
        self.assertEqual(seen, [("standard", ("us-central1",), True)])


class StartupScriptSwapTests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")
        self.main = re.search(r"^main\(\) \{\n.*?^\}\n", self.text, re.S | re.M).group(0)

    def _function(self, name):
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", self.text, re.S | re.M)
        self.assertIsNotNone(match, name)
        return match.group(0)

    def test_swap_only_on_e2_micro_and_before_packages_and_build(self):
        self.assertIn('"$METADATA/instance/machine-type"', self.main)
        self.assertIn('MACHINE_TYPE="${MACHINE_TYPE##*/}"', self.main)
        guard = self.main.index('if [[ "$MACHINE_TYPE" == "$MICRO_MACHINE_TYPE" ]]; then\n    '
                                '# Before apt, docker, and the image pull')
        swap = self.main.index('ensure_swap "$SWAP_SIZE_MIB"')
        self.assertLess(guard, swap)
        for later in ("install_packages\n", "build_from_source\n", "pull_published_image\n", "run_containers\n"):
            self.assertLess(swap, self.main.index(later), later)
        self.assertIn("readonly MICRO_MACHINE_TYPE=e2-micro", self.text)
        self.assertIn("readonly SWAP_SIZE_MIB=2048", self.text)
        zram = self.main.index("ensure_zram_swap\n")
        self.assertLess(guard, zram)
        self.assertLess(zram, self.main.index("install_packages\n"))
        priorities = dict(re.findall(r"^readonly (ZRAM_PRIORITY|SWAP_FILE_PRIORITY)=(\d+)$", self.text, re.M))
        self.assertGreater(int(priorities["ZRAM_PRIORITY"]), int(priorities["SWAP_FILE_PRIORITY"]))

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_swap_file_is_idempotent_and_private(self):
        stubs = r'''
log() { :; }
fail() { echo "FAIL: $*" >&2; exit 1; }
stat() { wc -c <"$3" | tr -d ' '; }
chown() { :; }
fallocate() { head -c "$2" /dev/zero >"$3"; echo fallocate >>"$CALLS"; }
mkswap() { echo mkswap >>"$CALLS"; }
swapoff() { echo swapoff >>"$CALLS"; }
swapon() {
  if [[ "$1" == --show=NAME ]]; then cat "$ACTIVE" 2>/dev/null || true; return 0; fi
  [[ "$1" == --priority ]] || { echo "swapon without --priority" >&2; return 1; }
  echo "$3" >>"$ACTIVE"; echo "swapon:$2:${3##*/}" >>"$CALLS"
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": "/usr/bin:/bin", "SWAP_FILE": f"{tmp}/swapfile", "CALLS": f"{tmp}/calls",
                   "ACTIVE": f"{tmp}/active", "SWAP_FILE_PRIORITY": "10"}
            program = stubs + self._function("ensure_swap") + 'umask 022\nensure_swap 1\n'

            def run():
                result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + program], env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = Path(env["CALLS"]).read_text().split() if Path(env["CALLS"]).exists() else []
                Path(env["CALLS"]).unlink(missing_ok=True)
                return calls

            self.assertEqual(run(), ["swapoff", "fallocate", "mkswap", "swapon:10:swapfile"])
            swapfile = Path(env["SWAP_FILE"])
            self.assertEqual(swapfile.stat().st_mode & 0o777, 0o600)
            self.assertEqual(swapfile.stat().st_size, 1024 * 1024)
            self.assertFalse(Path(f"{tmp}/swapfile.partial").exists())
            self.assertEqual(run(), [])  # active and complete: nothing to do
            Path(env["ACTIVE"]).unlink()  # reboot: the file stays, swap is off
            swapfile.chmod(0o644)
            self.assertEqual(run(), ["swapon:10:swapfile"])
            self.assertEqual(swapfile.stat().st_mode & 0o777, 0o600)
            Path(env["ACTIVE"]).unlink()
            swapfile.write_bytes(b"\0" * 10)  # wrong size (a different SWAP_SIZE_MIB): rebuilt
            self.assertEqual(run(), ["swapoff", "fallocate", "mkswap", "swapon:10:swapfile"])
            self.assertEqual(swapfile.stat().st_size, 1024 * 1024)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_container_memory_limits_only_on_e2_micro(self):
        log_args = re.search(r"^readonly LOG_ARGS=.*$", self.text, re.M).group(0)
        program = (log_args + "\nreadonly MICRO_MACHINE_TYPE=e2-micro\n" + self._function("set_container_limits")
                   + 'set_container_limits\necho "D ${DASHBOARD_RUN_ARGS[*]}"\necho "T ${TUNNEL_RUN_ARGS[*]}"\n')
        outputs = {}
        for shape in ("e2-micro", "e2-small"):
            result = subprocess.run(["bash", "-c", "set -euo pipefail\nMACHINE_TYPE=$1\n" + program, "x", shape],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs[shape] = dict(line.split(" ", 1) for line in result.stdout.splitlines())
        logs = "--log-driver json-file --log-opt max-size=10m --log-opt max-file=3"
        self.assertEqual(outputs["e2-micro"], {"D": logs + " --memory=320m --memory-swap=640m",
                                               "T": logs + " --memory=160m --memory-swap=320m"})
        self.assertEqual(outputs["e2-small"], {"D": logs, "T": logs})
        run = self._function("run_containers")
        self.assertIn('--network host \\\n    "${DASHBOARD_RUN_ARGS[@]}" \\\n    --user', run)
        self.assertIn('--network host \\\n    "${TUNNEL_RUN_ARGS[@]}" \\\n    --cap-drop', run)
        self.assertLess(self.main.index("set_container_limits"), self.main.index("run_containers"))


class StandardDashboardImageTests(unittest.TestCase):
    """The free-tier e2-micro never builds the dashboard image: it pulls a pinned, published one."""

    SHA = "c" * 40
    DIGEST = "ghcr.io/swack-tools/spot-dashboard@sha256:" + "d" * 64

    def std(self, **overrides):
        values = dict(provisioning="standard", dashboard_image="")
        values.update(overrides)
        return config(**values)

    def test_source_ref_resolves_to_the_published_commit_tag(self):
        cfg = self.std(dashboard_source_ref=self.SHA)
        self.assertEqual(cfg.effective_dashboard_image, f"ghcr.io/swack-tools/spot-dashboard:{self.SHA}")
        self.assertEqual(cfg.create_problems(), [])
        items = cv.metadata_items(cfg, "b")
        self.assertEqual(items["dashboard-image"], f"ghcr.io/swack-tools/spot-dashboard:{self.SHA}")
        self.assertNotIn("dashboard-source-ref", items)  # nothing on the box can build
        cv.create_command(cfg, "b", micro(), "/s")
        # An explicit digest wins over the derived tag.
        both = self.std(dashboard_image=self.DIGEST, dashboard_source_ref=self.SHA)
        self.assertEqual(cv.metadata_items(both, "b")["dashboard-image"], self.DIGEST)
        self.assertNotIn("dashboard-source-ref", cv.metadata_items(both, "b"))

    def test_missing_or_unpinned_image_refuses_creation(self):
        for bad in (dict(), dict(dashboard_source_ref="main"),
                    dict(dashboard_source_ref=self.SHA, dashboard_repo="someone/fork"),
                    dict(dashboard_image="ghcr.io/swack-tools/spot-dashboard:main"),
                    dict(dashboard_image="ghcr.io/swack-tools/spot-dashboard:latest"),
                    dict(dashboard_image="ghcr.io/swack-tools/spot-dashboard"),
                    dict(dashboard_image="ghcr.io/swack-tools/spot-dashboard:" + "c" * 39)):
            cfg = self.std(**bad)
            self.assertTrue(cfg.create_problems(), bad)
            with self.assertRaises(ValueError, msg=bad):
                cv.create_command(cfg, "b", micro(), "/s")
        for good in (dict(dashboard_image=self.DIGEST),
                     dict(dashboard_image="ghcr.io/swack-tools/spot-dashboard:v1@sha256:" + "d" * 64),
                     dict(dashboard_image=f"ghcr.io/swack-tools/spot-dashboard:{self.SHA}")):
            self.assertEqual(self.std(**good).create_problems(), [], good)

    def test_spot_mode_is_unchanged(self):
        source = config(dashboard_image="", dashboard_source_ref=self.SHA)
        self.assertEqual(source.effective_dashboard_image, "")
        items = cv.metadata_items(source, "a")
        self.assertEqual(items["dashboard-source-ref"], self.SHA)
        self.assertNotIn("dashboard-image", items)
        # Spot still accepts only a digest for SPOT_DASHBOARD_IMAGE.
        self.assertTrue(config(dashboard_image=f"ghcr.io/swack-tools/spot-dashboard:{self.SHA}").create_problems())

    def test_switch_without_an_image_never_begins(self):
        h = Harness(offers=STANDARD_OFFERS, provisioning="standard", dashboard_image="")
        h.gcloud.add("a", machine="e2-small", provisioning="SPOT")
        h.kv.set_origin("a")
        h.http.healthy("a", version=7)
        plan = h.tick()
        self.assertEqual(plan.action, "wait")
        self.assertIn("rotate refused", plan.reason)
        self.assertIn("never builds the dashboard image", plan.reason)
        self.assertIsNone(h.transition)
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(h.mutations(), [])

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_startup_image_check_matches_create_problems(self):
        text = SCRIPT.read_text(encoding="utf-8")
        function = re.search(r"^dashboard_image_ok\(\) \{\n.*?^\}\n", text, re.S | re.M).group(0)
        values = [self.DIGEST, f"ghcr.io/swack-tools/spot-dashboard:{self.SHA}",
                  "ghcr.io/swack-tools/spot-dashboard:v1@sha256:" + "d" * 64, "",
                  "ghcr.io/swack-tools/spot-dashboard:main", "ghcr.io/swack-tools/spot-dashboard",
                  "ghcr.io/swack-tools/spot-dashboard:" + "C" * 40, "Ghcr.io/x:" + self.SHA,
                  "ghcr.io/x:" + self.SHA + "0", "ghcr.io/x@sha256:" + "d" * 63]
        for value in values:
            result = subprocess.run(["bash", "-c", function + 'dashboard_image_ok "$1"', "check", value],
                                    capture_output=True, text=True, timeout=30)
            expected = not self.std(dashboard_image=value).create_problems() if value else False
            self.assertEqual(result.returncode == 0, expected, value)
        main = re.search(r"^main\(\) \{\n.*?^\}\n", text, re.S | re.M).group(0)
        micro_check = main.index('dashboard_image_ok "$DASHBOARD_IMAGE"')
        self.assertLess(main.index('if [[ "$MACHINE_TYPE" == "$MICRO_MACHINE_TYPE" ]]; then\n    # 1 GB'), micro_check)
        self.assertLess(micro_check, main.index("build_from_source\n"))
        # Other shapes keep the digest-only check and the source build.
        self.assertIn('[[ "$DASHBOARD_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || fail "dashboard-image must be pinned by digest"',
                      main)


class StartupScriptZramTests(unittest.TestCase):
    STUBS = r"""
log() { echo "LOG: $*" >&2; }
fail() { echo "FAIL: $*" >&2; exit 1; }
modprobe() { echo "modprobe:$*" >>"$CALLS"; return "${MODPROBE_RC:-0}"; }
mkswap() { echo "mkswap:${1##*/}" >>"$CALLS"; return "${MKSWAP_RC:-0}"; }
swapon() {
  if [[ "$1" == --show=NAME ]]; then cat "$ACTIVE" 2>/dev/null || true; return 0; fi
  [[ "$1" == --priority ]] || { echo "swapon without --priority" >&2; return 1; }
  echo "$3" >>"$ACTIVE"; echo "swapon:$2:${3##*/}" >>"$CALLS"
}
"""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.sys = self.tmp / "sys-zram0"
        self.sys.mkdir()
        (self.sys / "disksize").write_text("0\n")
        (self.tmp / "meminfo").write_text("MemTotal:        1004300 kB\nMemFree:          500000 kB\n")
        self.env = {"PATH": "/usr/bin:/bin", "CALLS": str(self.tmp / "calls"), "ACTIVE": str(self.tmp / "active"),
                    "ZRAM_DEV": str(self.tmp / "zram0"), "ZRAM_SYS": str(self.sys), "ZRAM_PRIORITY": "100",
                    "MEMINFO": str(self.tmp / "meminfo")}

    def function(self, name):
        return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", self.text, re.S | re.M).group(0)

    def run_zram(self, algorithms="lzo lzo-rle lz4 [zstd]", extra="", **env):
        (self.sys / "comp_algorithm").write_text(algorithms + "\n")
        program = "set -euo pipefail\n" + self.STUBS + self.function("ensure_zram_swap") + "ensure_zram_swap\n" + extra
        result = subprocess.run(["bash", "-c", program], env={**self.env, **env}, capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = Path(self.env["CALLS"]).read_text().split() if Path(self.env["CALLS"]).exists() else []
        Path(self.env["CALLS"]).unlink(missing_ok=True)
        return calls, result.stderr

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_zstd_half_of_ram_at_a_higher_priority_and_idempotent(self):
        calls, _ = self.run_zram()
        self.assertEqual(calls, ["modprobe:zram", "num_devices=1", "mkswap:zram0", "swapon:100:zram0"])
        self.assertEqual((self.sys / "comp_algorithm").read_text(), "zstd\n")
        self.assertEqual((self.sys / "disksize").read_text(), f"{1004300 * 1024 // 2}\n")
        self.assertEqual(self.run_zram()[0], [])  # already active: nothing changes

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_lz4_fallback_then_kernel_default(self):
        calls, _ = self.run_zram("lzo [lzo-rle] lz4 lz4hc")
        self.assertEqual((self.sys / "comp_algorithm").read_text(), "lz4\n")
        self.assertIn("swapon:100:zram0", calls)
        Path(self.env["ACTIVE"]).unlink()
        (self.sys / "disksize").write_text("0\n")
        calls, stderr = self.run_zram("[lzo-rle] lzo")
        self.assertEqual((self.sys / "comp_algorithm").read_text(), "[lzo-rle] lzo\n")  # left alone
        self.assertIn("neither zstd nor lz4", stderr)
        self.assertIn("swapon:100:zram0", calls)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_sized_device_from_an_interrupted_boot_is_reused(self):
        (self.sys / "disksize").write_text("4096\n")
        calls, _ = self.run_zram()
        self.assertEqual(calls[-2:], ["mkswap:zram0", "swapon:100:zram0"])
        self.assertEqual((self.sys / "disksize").read_text(), "4096\n")
        self.assertEqual((self.sys / "comp_algorithm").read_text(), "lzo lzo-rle lz4 [zstd]\n")

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_missing_zram_is_logged_and_the_swap_file_still_comes_up(self):
        swap_stubs = r"""
stat() { wc -c <"$3" | tr -d ' '; }
chown() { :; }
fallocate() { head -c "$2" /dev/zero >"$3"; echo fallocate >>"$CALLS"; }
swapoff() { :; }
"""
        extra = swap_stubs + self.function("ensure_swap") + f'SWAP_FILE={self.tmp}/swapfile\nSWAP_FILE_PRIORITY=10\n' \
            'ensure_swap 1\n'
        calls, stderr = self.run_zram(extra=extra, MODPROBE_RC="1")
        self.assertIn("zram is unavailable", stderr)
        self.assertNotIn("mkswap:zram0", calls)
        self.assertEqual(calls[-3:], ["fallocate", "mkswap:swapfile.partial", "swapon:10:swapfile"])
        # The module loads but exposes no device: also not fatal.
        calls, stderr = self.run_zram(ZRAM_SYS=str(self.tmp / "absent"))
        self.assertIn("zram is unavailable", stderr)
        self.assertEqual(calls, ["modprobe:zram", "num_devices=1"])

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_zram_errors_are_not_fatal(self):
        calls, stderr = self.run_zram(MKSWAP_RC="1")
        self.assertIn("could not enable zram swap", stderr)
        self.assertNotIn("swapon:100:zram0", calls)
        (self.tmp / "meminfo").write_text("garbage\n")
        (self.sys / "disksize").write_text("0\n")
        calls, stderr = self.run_zram()
        self.assertIn("could not read MemTotal", stderr)
        self.assertNotIn("mkswap:zram0", calls)
