"""Physical disk costs survive initial inventory outages and legacy state reloads."""

import json
import unittest
from unittest.mock import patch

from lib.dashboard_server import _live_instance_record, _single_disk_all_in, instance_usage, reconcile_instance_records


class InitialDiskHistoryTests(unittest.TestCase):
    now = 1800000000.0

    def setUp(self):
        self.pricing = patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={})
        self.pricing.start()
        self.addCleanup(self.pricing.stop)
        self.disk = {"source": "z/builder-initial-cache", "id": "123", "type": "pd-ssd", "gb": 100,
                     "created_epoch": self.now - 10 * 3600, "auto_delete": False, "boot": False,
                     "billing_owner": True}
        self.unknown = dict(self.disk, id="", type="pd-balanced", created_epoch=None)
        self.boot = dict(self.disk, source="z/builder-initial-boot", id="456", type="pd-balanced", gb=40,
                         boot=True, created_epoch=self.now - 12 * 3600)

    def observation(self, disks, *, known=True, detached=False):
        data = {}
        for d in disks:
            if not d["boot"]:
                data[d["type"]] = data.get(d["type"], 0) + d["gb"]
        return {"instance_id": "disk-123" if detached else "a", "name": "builder-initial",
                "role": "rust_builder", "status": "DETACHED" if detached else "RUNNING",
                "kind": "disk" if detached else "vm", "zone": "us-south1-b", "region": "us-south1",
                "machine_type": "" if detached else "n4-highcpu-16", "vcpu": 0, "ram_gib": 0,
                "spot": True, "created_epoch": self.now - 12 * 3600, "last_start_epoch": self.now - 12 * 3600,
                "boot_disk_type": "pd-balanced", "boot_disk_gb": sum(d["gb"] for d in disks if d["boot"]),
                "data_disk_type": next(iter(data), "pd-balanced"), "data_disk_gb": sum(data.values()),
                "data_disks_attached_gb": sum(data.values()), "data_disks": data,
                "disks_detail": disks, "disk_types_known": known}

    def poll(self, rows, observations, hours, known):
        # Reload exactly as persisted JSON between observations; no shared Python objects carry the debt.
        return reconcile_instance_records(json.loads(json.dumps(rows)), observations, project="p",
                                          now=self.now - hours * 3600, disks_listed=known,
                                          tax_overrides={"rust_builder": 0})[0]

    def cost(self, rows, window=None):
        return sum(instance_usage(r, self.now, window)["window_all_in"] for r in rows)

    def rate(self, disk):
        return _single_disk_all_in(disk, family="n4", region="us-south1", project="p", tax_rate_percent=0)

    def assert_settled(self, rows, observations, expected):
        self.assertAlmostEqual(self.cost(rows), expected, delta=0.0001)
        self.assertFalse(any(r.get("disk_history_deferred_at") or r.get("unpaid_disk_details")
                             or r.get("deferred_disk_sources") for r in rows))
        repeated = self.poll(rows, observations, 0, True)
        self.assertAlmostEqual(self.cost(repeated), expected, delta=0.0001)
        later = reconcile_instance_records(repeated, observations, project="p", now=self.now + 3600,
                                             tax_overrides={"rust_builder": 0})[0]
        self.assertAlmostEqual(sum(instance_usage(r, self.now + 3600)["cost_all_in"] for r in later),
                               expected + sum(r["pricing"]["disk_hourly_usd"] for r in repeated
                                              if r["status"] != "DELETED"), delta=0.0001)

    def test_initial_denial_then_detachment_recovers_full_physical_history(self):
        for mixed in (False, True):
            for deleted in (False, True):
                with self.subTest(mixed=mixed, deleted=deleted):
                    boot = [self.boot] if mixed else []
                    rows = self.poll([], [self.observation(boot + [self.unknown], known=False)], 8, False)
                    self.assertFalse(rows[0]["lifetime_cost_complete"])
                    remaining = [] if deleted else [self.observation(boot, known=False)]
                    rows = self.poll(rows, remaining, 5, False)
                    rows = self.poll(rows, remaining, 2, False)
                    recovered = ([] if deleted else [self.observation(boot)]) + [self.observation([self.disk], detached=True)]
                    if deleted and mixed:
                        recovered.append(dict(self.observation([self.boot], detached=True), instance_id="disk-456"))
                    rows = self.poll(rows, recovered, 0, True)
                    self.assert_settled(rows, recovered, 10 * self.rate(self.disk) + (12 * self.rate(self.boot) if mixed else 0))

    def test_legacy_global_deferred_fallback_is_repaired_after_detach(self):
        for mixed in (False, True):
            for deleted in (False, True):
                for disk_type in ("pd-ssd", "pd-standard"):
                    with self.subTest(mixed=mixed, deleted=deleted, disk_type=disk_type):
                        boot = [self.boot] if mixed else []
                        # Legacy initial records billed a fallback rate from first poll and stored all
                        # missing creation history on the VM. The type was never authoritative.
                        legacy = _live_instance_record(self.observation(boot + [self.unknown]),
                                                       project="p", now=self.now - 8 * 3600,
                                                       tax_overrides={"rust_builder": 0})
                        legacy["disk_types_known"] = False
                        self.assertIsNotNone(legacy["disk_history_deferred_at"])
                        remaining = [] if deleted else [self.observation(boot, known=False)]
                        rows = self.poll([legacy], remaining, 5, False)
                        rows = self.poll(rows, remaining, 2, False)
                        real = dict(self.disk, type=disk_type)
                        recovered = ([] if deleted else [self.observation(boot)]) + [self.observation([real], detached=True)]
                        if deleted and mixed:
                            recovered.append(dict(self.observation([self.boot], detached=True), instance_id="disk-456"))
                        rows = self.poll(rows, recovered, 0, True)
                        self.assert_settled(rows, recovered, 10 * self.rate(real) + (12 * self.rate(self.boot) if mixed else 0))
                        self.assertAlmostEqual(self.cost(rows, self.now - 7 * 3600),
                                               7 * (self.rate(real) + (self.rate(self.boot) if mixed else 0)), delta=0.0001)

    def test_persisted_deleted_legacy_carrier_recovers_without_source_list(self):
        from lib.dashboard_server import _mark_instance_deleted

        legacy = _live_instance_record(self.observation([self.unknown]), project="p", now=self.now - 8 * 3600,
                                       tax_overrides={"rust_builder": 0})
        legacy["disk_types_known"] = False
        legacy.pop("disk_history_deferred_sources")
        legacy = _mark_instance_deleted(legacy, self.now - 5 * 3600, disks_pending=True, project="p")
        recovered = [self.observation([self.disk], detached=True)]
        rows = self.poll([legacy], recovered, 0, True)
        self.assert_settled(rows, recovered, 10 * self.rate(self.disk))
        self.assertAlmostEqual(self.cost(rows, self.now - 7 * 3600), 7 * self.rate(self.disk), delta=0.0001)

    def test_legacy_same_carrier_repairs_fallback_without_double_billing(self):
        legacy = _live_instance_record(self.observation([self.unknown]), project="p", now=self.now - 8 * 3600,
                                       tax_overrides={"rust_builder": 0})
        legacy["disk_types_known"] = False
        recovered = [self.observation([self.disk])]
        rows = self.poll([legacy], recovered, 0, True)
        self.assert_settled(rows, recovered, 10 * self.rate(self.disk))

    def test_known_shared_disk_keeps_its_type_when_new_owner_is_denied(self):
        original = self.observation([self.disk])
        rows = self.poll([], [original], 8, True)
        old = dict(original, disks_detail=[dict(self.unknown, billing_owner=False)], data_disks={}, disk_types_known=False)
        new = dict(self.observation([self.unknown], known=False), instance_id="0")
        rows = self.poll(rows, [old, new], 4, False)
        owner = next(r for r in rows if r["instance_id"] == "0")
        self.assertAlmostEqual(owner["pricing"]["disk_hourly_usd"], self.rate(self.disk), delta=0.0001)
        self.assertEqual(owner["disks_detail"][0]["type"], "pd-ssd")
        self.assertAlmostEqual(self.cost(rows), 10 * self.rate(self.disk), delta=0.0001)
