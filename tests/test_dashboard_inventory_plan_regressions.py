"""Inventory plans require trustworthy snapshots and exact identities; resized debt keeps size boundaries."""

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import (DashboardStore, _live_instance_record, _single_disk_all_in, instance_usage,
                                  reconcile_instance_records)


class InventoryPlanRegressions(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.pricing = patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={})
        self.pricing.start()
        self.addCleanup(self.pricing.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DashboardStore(state_path=Path(self.temp.name) / "state.json")
        self.store.start()
        self.addCleanup(self.store.close)

    def observation(self, instance_id="a", **changes):
        obs = {"instance_id": instance_id, "name": "builder-shared", "role": "rust_builder",
               "status": "RUNNING", "zone": "us-south1-b", "region": "us-south1",
               "machine_type": "n4-highcpu-16", "vcpu": 16, "ram_gib": 32.0, "spot": True,
               "specs_known": True, "runner_limits_known": True, "runners_count_known": True,
               "runners_count": 1, "runner_vcpu": 16, "runner_ram_gib": 28.0,
               "boot_disk_type": "pd-balanced", "boot_disk_gb": 0, "data_disk_type": "pd-balanced",
               "data_disk_gb": 0, "data_disks": {}, "disks_detail": [],
               "created_epoch": self.now - 36000, "last_start_epoch": self.now - 36000}
        return dict(obs, **changes)

    def test_scale_down_and_noop_require_fresh_complete_inventory(self):
        self.store.fleet_source = lambda: [self.observation()]
        self.store.refresh_fleet_inventory()
        payload = {"role": "rust_builder", "region": "us-south1", "target_instances": 0}
        original_status = copy.deepcopy(self.store._fleet_status)
        for status in (dict(original_status, fresh=False, last_error="API failed"),
                       dict(original_status, fresh=True, last_success_epoch=self.now - 4 * self.store.fleet_inventory_interval),
                       dict(original_status, fresh=True, disks_listed=False)):
            for target in (0, 1):
                with self.subTest(status=status, target=target):
                    self.store._fleet_status = status
                    with self.assertRaisesRegex(ValueError, "complete listing"):
                        self.store.scale_region_instances(dict(payload, target_instances=target))
        self.store._fleet_status = original_status
        before = copy.deepcopy(self.store.state)
        plan = self.store.scale_region_instances(payload)
        self.assertEqual(plan["would_retire"], ["builder-shared"])
        self.assertEqual(self.store.state, before)

    def test_name_only_resolution_rejects_zonal_and_historical_ambiguity(self):
        first = _live_instance_record(self.observation("a"), project="p", now=self.now)
        second = _live_instance_record(self.observation("b", zone="us-south1-c"), project="p", now=self.now)
        for historical in (False, True):
            with self.subTest(historical=historical):
                self.store.state["instances"] = [first, dict(second, status="DELETED" if historical else "RUNNING")]
                before = copy.deepcopy(self.store.state)
                for method in (self.store.create_vm_template_from_instance, self.store.delete_instance):
                    with self.assertRaisesRegex(ValueError, "ambiguous.*instance_id"):
                        method({"instance_name": "builder-shared", "assign_to_profile": False})
                self.assertEqual(self.store.state, before)

    def test_explicit_id_wins_and_unknown_id_never_falls_back_to_name(self):
        first = _live_instance_record(self.observation("a", name="builder-other", vcpu=8), project="p", now=self.now)
        second = _live_instance_record(self.observation("b"), project="p", now=self.now)
        self.store.state["instances"] = [first, second]
        payload = {"instance_id": "b", "instance_name": "builder-other", "assign_to_profile": False}
        template = self.store.create_vm_template_from_instance(payload)["vm_template"]
        self.assertEqual(template["vcpu"], 16)
        self.assertEqual(template["source_instance"], "builder-shared")
        self.assertEqual(self.store.delete_instance(payload)["instance"]["instance_id"], "b")
        for method in (self.store.create_vm_template_from_instance, self.store.delete_instance):
            with self.assertRaisesRegex(ValueError, "not found"):
                method(dict(payload, instance_id="missing"))
        # Unambiguous legacy callers remain supported.
        self.assertEqual(self.store.delete_instance({"instance_name": "builder-shared"})["instance"]["instance_id"], "b")

    def test_resized_data_disk_recovers_completeness_with_trusted_unchanged_boot(self):
        created = self.now - 36000
        boot = {"id": "boot-immutable", "source": "z/boot", "type": "pd-balanced", "gb": 40,
                "created_epoch": created, "auto_delete": True, "boot": True, "billing_owner": True}
        data = dict(boot, id="cache-immutable", source="z/cache", type="pd-ssd", gb=100,
                    auto_delete=False, boot=False)
        base = self.observation(vcpu=0, ram_gib=0, boot_disk_gb=40, boot_disk_type="pd-balanced",
                                data_disk_type="pd-ssd", data_disk_gb=100, data_disks={"pd-ssd": 100},
                                data_disks_attached_gb=100, disks_detail=[boot, data])
        rows, _ = reconcile_instance_records([], [base], project="p", now=self.now - 28800,
                                             tax_overrides={"rust_builder": 0})
        initial_rate = rows[0]["pricing"]["disk_hourly_usd"]
        for hours, size in ((5, 200), (4, 200), (2, 300)):
            unknown = [dict(boot, id="", created_epoch=None),
                       dict(data, id="", created_epoch=None, type="pd-balanced", gb=size)]
            obs = dict(base, data_disk_type="pd-balanced", data_disk_gb=size,
                       data_disks={"pd-balanced": size}, data_disks_attached_gb=size,
                       disks_detail=unknown, disk_types_known=False)
            rows, _ = reconcile_instance_records(json.loads(json.dumps(rows)), [obs], project="p",
                now=self.now - hours * 3600, disks_listed=False, tax_overrides={"rust_builder": 0})
            boot_rate = rows[0]["pricing"]["disk_hourly_usd"]
        recovered = dict(base, data_disk_gb=300, data_disks={"pd-ssd": 300}, data_disks_attached_gb=300,
                         disks_detail=[boot, dict(data, gb=300)])
        rows, _ = reconcile_instance_records(rows, [recovered], project="p", now=self.now,
                                             tax_overrides={"rust_builder": 0})
        expected = 5 * initial_rate + 5 * boot_rate
        expected += sum(hours * _single_disk_all_in(dict(data, gb=size), family="n4", region="us-south1",
            project="p", tax_rate_percent=0) for hours, size in ((3, 200), (2, 300)))
        self.assertAlmostEqual(instance_usage(rows[0], self.now)["cost_all_in"], expected, delta=0.0002)
        self.assertTrue(rows[0]["pricing_complete"])
        self.assertTrue(rows[0]["lifetime_cost_complete"])
        self.store.state["instances"] = rows
        self.assertEqual(self.store._incomplete_cost_hosts("rust_builder"), [])
        self.assertTrue(self.store.query_instances(window_hours=24)["window_cost_complete"])

    def test_resized_disk_during_denied_listing_recovers_each_observed_size_once(self):
        disk = {"id": "disk-123", "source": "z/cache", "type": "pd-ssd", "gb": 100,
                "created_epoch": self.now - 36000, "auto_delete": False, "boot": False, "billing_owner": True}
        base = self.observation(vcpu=0, ram_gib=0, data_disk_type="pd-ssd", data_disk_gb=100,
                                data_disks={"pd-ssd": 100}, data_disks_attached_gb=100, disks_detail=[disk])
        for keep_creation in (False, True):
            with self.subTest(keep_creation=keep_creation):
                rows, _ = reconcile_instance_records([], [base], project="p", now=self.now - 28800,
                                                       tax_overrides={"rust_builder": 0})
                for hours, size in ((5, 200), (4, 200), (2, 300)):
                    unknown = dict(disk, id="", type="pd-balanced", gb=size,
                                   created_epoch=disk["created_epoch"] if keep_creation else None)
                    obs = dict(base, data_disk_type="pd-balanced", data_disk_gb=size,
                               data_disks={"pd-balanced": size}, data_disks_attached_gb=size,
                               disks_detail=[unknown], disk_types_known=False)
                    rows, _ = reconcile_instance_records(json.loads(json.dumps(rows)), [obs], project="p",
                        now=self.now - hours * 3600, disks_listed=False, tax_overrides={"rust_builder": 0})
                    self.assertEqual(rows[0]["deferred_disk_sources"], [disk["source"]])
                    self.assertEqual(rows[0]["pricing"]["disk_hourly_usd"], 0)
                real = dict(disk, gb=300)
                obs = dict(base, data_disk_gb=300, data_disks={"pd-ssd": 300}, data_disks_attached_gb=300,
                           disks_detail=[real])
                rows, _ = reconcile_instance_records(json.loads(json.dumps(rows)), [obs], project="p", now=self.now,
                                                      tax_overrides={"rust_builder": 0})
                cost = instance_usage(rows[0], self.now)["cost_all_in"]
                expected = sum(hours * _single_disk_all_in(dict(disk, gb=size), family="n4", region="us-south1",
                    project="p", tax_rate_percent=0) for hours, size in ((5, 100), (3, 200), (2, 300)))
                self.assertAlmostEqual(cost, expected, delta=0.0002)
                self.assertFalse(rows[0]["deferred_disk_sources"])
                repeated, _ = reconcile_instance_records(rows, [obs], project="p", now=self.now,
                                                         tax_overrides={"rust_builder": 0})
                self.assertAlmostEqual(instance_usage(repeated[0], self.now)["cost_all_in"], cost, places=6)
