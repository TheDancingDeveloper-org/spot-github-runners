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
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib import control_vm as cv
from lib.edge_state import (
    KV_ORIGIN_KEY,
    KV_STATE_KEY,
    OriginRecord,
    build_snapshot,
    verify_control_token,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cloud-init" / "control-vm-startup.sh"
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
        self.start_rc = 0
        self.delete_rc = 0
        self.scripts: list[str] = []

    def add(self, slot, status="RUNNING", zone="us-central1-a", machine="e2-small"):
        name = f"spot-control-{slot}"
        self.vms[name] = {"name": name, "zone": f"projects/proj/zones/{zone}", "status": status,
                          "labels": {"spot-role": "control", "spot-control-slot": slot},
                          "id": f"id-{slot}", "machineType": f"zones/{zone}/machineTypes/{machine}"}

    def status(self, slot, status):
        self.vms[f"spot-control-{slot}"]["status"] = status

    def __call__(self, argv):
        self.calls.append(list(argv))
        verb = argv[2:4]
        if verb == ["instances", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(list(self.vms.values())), stderr="")
        if verb == ["machine-types", "list"]:
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        name = argv[4]
        zone = next(a.split("=", 1)[1] for a in argv if a.startswith("--zone="))
        if verb == ["instances", "create"]:
            script = next(a.split("=", 2)[2] for a in argv if a.startswith("--metadata-from-file="))
            self.scripts.append(Path(script).read_text(encoding="utf-8"))
            if zone in self.fail_create_zones:
                self.events.append(("create_failed", name, zone))
                return SimpleNamespace(returncode=1, stdout="", stderr="ZONE_RESOURCE_POOL_EXHAUSTED")
            slot = name.rsplit("-", 1)[1]
            machine = next(a.split("=", 1)[1] for a in argv if a.startswith("--machine-type="))
            self.add(slot, status="PROVISIONING", zone=zone, machine=machine)
            self.events.append(("create", name, zone))
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        if verb == ["instances", "start"]:
            self.events.append(("start", name))
            if self.start_rc == 0:
                self.vms[name]["status"] = "STAGING"
            return SimpleNamespace(returncode=self.start_rc, stdout="", stderr="stockout" if self.start_rc else "")
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
        return [e for e in self.events if e[0] in ("create", "start", "delete", "origin", "freeze", "import",
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
        self.assertEqual(h.mutations()[-2:], [("import", "b"), ("origin", "b")])
        self.assertEqual(h.http.versions["b"], 7)

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
        state = {"control_vm": {"transition": None}}
        with patch("lib.control_vm.autoscaler_tick") as tick:
            autoscaler.manage_control_vm(SimpleNamespace(dry_run=True), state, Path("/nonexistent/state.json"))
        passed_state, save = tick.call_args[0][1], tick.call_args[0][2]
        self.assertIsNot(passed_state, state)
        save()  # No-op: must not try to write the nonexistent path.


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
        with redirect_stdout(out), patch("lib.environment.load_env"):
            code = cv.main(argv, manager_factory=factory)
        return code, out.getvalue()

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
            saved = json.loads(state_file.read_text())
            self.assertEqual(saved["control_vm"]["transition"]["step"], "wait_health")


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
        self.assertIn('--host 127.0.0.1 --port 8787 --state-file /state/state.json --control-slot "$SLOT"',
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
                            ("https://spot.swacktech.com:8443/app", "https://spot.swacktech.com:8443")):
            result = self._run_function("public_origin_of", url)
            self.assertEqual((result.returncode, result.stdout), (0, origin), url)
        for url in ("https://bad host", "https://user@spot.swacktech.com", "https://"):
            self.assertNotEqual(self._run_function("public_origin_of", url).returncode, 0, url)

    def test_image_paths(self):
        self.assertIn("docker login ghcr.io", self.text)
        self.assertIn('docker build -q -f "$workdir/dashboard/Dockerfile" -t "$image"', self.text)
        self.assertIn('[[ "$head" == "$SOURCE_REF" ]]', self.text)
        self.assertIn('image_present "$image"', self.text)
        self.assertIn('image_present "$DASHBOARD_IMAGE"', self.text)
        self.assertIn("docker.io", self.text)


if __name__ == "__main__":
    unittest.main()
