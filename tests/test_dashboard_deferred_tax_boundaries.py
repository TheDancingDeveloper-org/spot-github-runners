"""Deferred storage must retain rates through budget writes, handoffs, and JSON reloads.

Finding4308169843: these are integration regressions, intentionally not expectedFailure.
The fixture supplies a fixed catalog (USD 7.30/GiB/month = USD .01/GiB/hour),
so the cost oracle never calls the implementation's pricing/history helpers.
Only provider pricing and inventory inputs are substituted; store mutations,
reconciliation, persistence, hydration, and usage calculations are real.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import DashboardStore, instance_usage
from lib.fleet_inventory import FleetListing


class DeferredTaxBoundaryTests(unittest.TestCase):
    epoch = 1800000000.0

    def setUp(self):
        self.now = self.epoch
        self.catalog = {"disk_monthly_gib": {"pd-ssd": 7.30, "pd-balanced": 3.65}}
        for name in ("lib.dashboard_models.fetch_live_spot_pricing",
                     "lib.dashboard_server.fetch_live_spot_pricing"):
            pricing = patch(name, side_effect=lambda **_kwargs: copy.deepcopy(self.catalog))
            pricing.start()
            self.addCleanup(pricing.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = self.new_store()
        self.tax(0, 0)

    def new_store(self):
        # Hydrate without starting threads. A source is required to retain inventory
        # records on reload; the fixture only invokes its explicit manual polls.
        store = DashboardStore(state_path=self.path, project="test-project", clock=lambda: self.now,
                               fleet_source=lambda: FleetListing([]))
        store.hydrate()
        self.addCleanup(store.close)
        return store

    def at(self, hour):
        self.now = self.epoch + hour * 3600

    def reload(self):
        persisted = json.loads(self.path.read_text())
        self.assertEqual(persisted["instances"], json.loads(json.dumps(self.store.state["instances"])))
        self.store.close()
        self.store = self.new_store()
        self.assertEqual(self.store.state["instances"], persisted["instances"])

    def tax(self, hour, percent):
        self.at(hour)
        result = self.store.update_budget({"target": "rust_builder", "override_regional_tax": True,
                                          "tax_rate_percent": percent})
        self.assertEqual(result["status"], "ok")
        self.reload()

    def observation(self, *, known, size=100, owner="a", detached=False, mixed_boot=False):
        disk = {"source": "z/builder-cache", "id": "physical-disk-1" if known else "",
                "created_epoch": self.epoch if known else None,
                "type": "pd-ssd" if known else "pd-balanced", "gb": size,
                "boot": False, "auto_delete": False, "billing_owner": True}
        boot = dict(disk, source="z/boot", id="boot-1", created_epoch=self.epoch,
                    type="pd-balanced", gb=40, boot=True, auto_delete=True)
        return {"instance_id": owner, "name": "builder-" + owner, "role": "rust_builder",
                "kind": "disk" if detached else "vm", "status": "DETACHED" if detached else "RUNNING",
                "zone": "us-south1-b", "region": "us-south1", "spot": True,
                "machine_type": "" if detached else "n4-highcpu-16", "vcpu": 0, "ram_gib": 0,
                "created_epoch": self.epoch, "last_start_epoch": self.epoch,
                "boot_disk_type": "pd-balanced", "boot_disk_gb": 40 if mixed_boot else 0,
                "data_disk_type": disk["type"], "data_disk_gb": size,
                "data_disks_attached_gb": size, "data_disks": {disk["type"]: size},
                "disks_detail": ([boot] if mixed_boot else []) + [disk], "disk_types_known": known}

    def poll(self, hour, *, known, size=100, owner="a", detached=False, mixed_boot=False):
        self.at(hour)
        listing = FleetListing([self.observation(known=known, size=size, owner=owner,
                                              detached=detached, mixed_boot=mixed_boot)])
        listing.disks_listed = known
        self.store.fleet_source = lambda: copy.deepcopy(listing)
        self.store.refresh_fleet_inventory()
        status = self.store.fleet_inventory_status()
        self.assertEqual(status["last_error"], "")
        self.assertEqual(status["disks_listed"], known)
        self.reload()

    def cost(self, start_hour=None):
        start = None if start_hour is None else self.epoch + start_hour * 3600
        return sum(instance_usage(row, self.now, start)["window_all_in"]
                   for row in self.store.state["instances"])

    def assert_history(self, intervals):
        # Half-open intervals: (start hour, end hour, size GiB, tax %, pretax USD/GiB/hour).
        # Windows exercise the boundary itself and intervals on both sides of it.
        for start in (0, 2, 4, 5, 6, 7):
            expected = sum(max(0, end - max(begin, start)) * size * rate * (1 + tax / 100)
                           for begin, end, size, tax, rate in intervals)
            with self.subTest(window_start_hour=start):
                self.assertAlmostEqual(self.cost(start), expected, places=6)
        expected = sum((end - begin) * size * rate * (1 + tax / 100)
                       for begin, end, size, tax, rate in intervals)
        self.assertAlmostEqual(self.cost(), expected, places=6)

    def assert_recovery_stable(self, *, size=100, owner="a", detached=False, hourly=1.2):
        before = self.cost()
        self.poll(8, known=True, size=size, owner=owner, detached=detached)
        self.assertAlmostEqual(self.cost(), before, places=6, msg="Repeated recovery double billed history")
        self.poll(9, known=True, size=size, owner=owner, detached=detached)
        self.assertAlmostEqual(self.cost(), before + hourly, places=6)

    def test_authoritative_disk_tax_change_control(self):
        self.poll(1, known=True)
        self.tax(4, 20)
        self.poll(8, known=True)
        self.assert_history([(0, 4, 100, 0, .01), (4, 8, 100, 20, .01)])
        self.assert_recovery_stable()

    def test_deferred_disk_recovers_tax_boundary_after_budget_write_and_reload(self):
        self.poll(1, known=False)
        self.tax(4, 20)  # No inventory poll at the budget-change timestamp.
        self.poll(6, known=False)
        self.poll(8, known=True)
        self.assert_history([(0, 4, 100, 0, .01), (4, 8, 100, 20, .01)])
        self.assert_recovery_stable()

    def test_deferred_resize_without_tax_change_control(self):
        self.poll(1, known=False)
        self.poll(6, known=False, size=200)
        self.poll(8, known=True, size=200)
        self.assert_history([(0, 6, 100, 0, .01), (6, 8, 200, 0, .01)])
        self.assert_recovery_stable(size=200, hourly=2)

    def test_deferred_resize_intersects_size_and_tax_boundaries(self):
        self.poll(1, known=False)
        self.tax(4, 20)
        self.poll(6, known=False, size=200)
        self.poll(8, known=True, size=200)
        self.assert_history([(0, 4, 100, 0, .01), (4, 6, 100, 20, .01), (6, 8, 200, 20, .01)])
        self.assert_recovery_stable(size=200, hourly=2.4)

    def test_deferred_history_follows_new_owner_with_original_tax_boundary(self):
        self.poll(1, known=False)
        self.tax(4, 20)
        self.poll(5, known=False, owner="b")
        self.poll(8, known=True, owner="b")
        self.assert_history([(0, 4, 100, 0, .01), (4, 8, 100, 20, .01)])
        self.assert_recovery_stable(owner="b")

    def test_deferred_history_recovers_on_detached_owner(self):
        self.poll(1, known=False)
        self.tax(4, 20)
        self.poll(8, known=True, owner="disk-1", detached=True)
        self.assert_history([(0, 4, 100, 0, .01), (4, 8, 100, 20, .01)])
        self.assert_recovery_stable(owner="disk-1", detached=True)

    def test_deferred_price_and_tax_changes_use_matching_historical_catalog(self):
        self.poll(1, known=False)
        self.catalog["disk_monthly_gib"]["pd-ssd"] = 14.60
        self.tax(4, 20)
        self.poll(6, known=False)
        self.poll(8, known=True)
        self.assert_history([(0, 4, 100, 0, .01), (4, 8, 100, 20, .02)])
        self.assert_recovery_stable(hourly=2.4)

    def test_deferred_price_change_with_unchanged_zero_aggregate_is_persisted(self):
        self.poll(1, known=False)
        self.catalog["disk_monthly_gib"]["pd-ssd"] = 14.60
        self.poll(4, known=False)  # Aggregate billed disk and compute rates remain zero.
        self.poll(8, known=True)
        self.assert_history([(0, 4, 100, 0, .01), (4, 8, 100, 0, .02)])
        self.assert_recovery_stable(hourly=2)

    def test_initial_fallback_credits_preserve_tax_boundary(self):
        from lib.dashboard_server import _live_instance_record

        # A known boot disk alongside an unknown data disk exercises the legacy
        # record-level disk_history_deferred_at -> portable per-disk conversion.
        self.poll(1, known=False, mixed_boot=True)
        # Current refreshes immediately port initial history. Persist and hydrate
        # a legacy record so the budget mutation must perform the upgrade itself.
        legacy = _live_instance_record(self.observation(known=False, mixed_boot=True),
                                       project=self.store.project, now=self.now,
                                       tax_overrides={"rust_builder": 0})
        self.store.state["instances"] = [legacy]
        self.store._persist()
        self.reload()
        self.assertEqual(self.store.state["instances"][0]["disk_history_deferred_at"], self.now)
        self.tax(4, 20)
        self.poll(6, known=False, mixed_boot=True)
        self.poll(8, known=True, mixed_boot=True)
        self.assert_history([(0, 4, 100, 0, .01), (4, 8, 100, 20, .01),
                             (0, 4, 40, 0, .005), (4, 8, 40, 20, .005)])

    def test_deferred_tax_returning_to_zero_preserves_both_boundaries(self):
        self.poll(1, known=False)
        self.tax(4, 20)
        self.tax(6, 0)
        self.poll(8, known=True)
        self.assert_history([(0, 4, 100, 0, .01), (4, 6, 100, 20, .01), (6, 8, 100, 0, .01)])
        self.assert_recovery_stable(hourly=1)


if __name__ == "__main__":
    unittest.main()
