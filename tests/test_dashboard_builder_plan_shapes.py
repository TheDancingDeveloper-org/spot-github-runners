"""Round45: proposed builders have one fixed shape and unambiguous storage sources."""

import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import DashboardStore


class BuilderPlanShapeTests(unittest.TestCase):
    invalid_shapes = ({"machine_type": "c4d-highcpu-16"}, {"vcpu": 32}, {"ram_gib": 64})
    shape_error = r"n4-highcpu-16 / 16 vCPU / 32 GiB"

    def setUp(self):
        self.now = time.time()
        prices = {"machine_hourly": {"us-south1/n4-highcpu-16": .1,
                                     "us-south1/c4d-highcpu-16": .1},
                  "disk_monthly_gib": {"pd-balanced": .1, "hyperdisk-balanced": .1}}
        for name in ("lib.dashboard_models.fetch_live_spot_pricing",
                     "lib.dashboard_server.fetch_live_spot_pricing"):
            stub = patch(name, return_value=prices)
            stub.start()
            self.addCleanup(stub.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = DashboardStore(state_path=self.path, project="test-project", clock=lambda: self.now,
                                    fleet_source=lambda: [])
        self.store.hydrate()  # Manual inventory refreshes only; no background workers.
        self.addCleanup(self.store.close)
        self.profile = self.store.state["profiles"]["rust_builder"]
        self.profile.update(min_benchmark_score=0, disk_category="any", data_disk_gb=0,
                            min_instance_vcpu=16, min_instance_ram_gib=32, max_instances=8,
                            runner_vcpu=16, runner_ram_gib=28, host_overhead_gib=4)
        self.profile["budget"].update(max_hourly_usd=100, max_daily_usd=2400, max_monthly_usd=73000,
                                       include_disk_costs=False, include_taxes=False,
                                       override_regional_tax=True, tax_rate_percent=0)
        self.store.state["scaling"]["rust_builder"].update(
            max_instances=8, max_scale_up_hourly_usd=100, max_scale_up_daily_usd=2400,
            max_scale_up_monthly_usd=73000, include_disk_in_scale_budget=False, shared_global_budget=False)
        self.store.state["scaling"]["cicd_runner"]["shared_global_budget"] = False
        self.store.state["global_budget_enforced"] = False
        self.install([])

    def observation(self, **changes):
        disk = {"source": "z/builder-cache", "id": "disk-1", "created_epoch": self.now - 3600,
                "type": "pd-balanced", "gb": 100, "boot": False, "auto_delete": False,
                "billing_owner": True}
        return dict({"instance_id": "builder-1", "name": "builder-source", "role": "rust_builder",
                     "status": "RUNNING", "kind": "vm", "region": "us-south1", "zone": "us-south1-b",
                     "machine_type": "n4-highcpu-16", "vcpu": 16, "ram_gib": 32,
                     "cpu_platform": "Intel Emerald Rapids", "spot": True, "specs_known": True,
                     "runner_limits_known": True, "runners_count_known": True, "runners_count": 1,
                     "runner_vcpu": 16, "runner_ram_gib": 28, "disk_types_known": True,
                     "boot_disk_type": "pd-balanced", "boot_disk_gb": 0,
                     "data_disk_type": "pd-balanced", "data_disk_gb": 100,
                     "data_disks_attached_gb": 100, "data_disks": {"pd-balanced": 100},
                     "disks_detail": [disk], "shared_attachments": [],
                     "created_epoch": self.now - 3600, "last_start_epoch": self.now - 3600}, **changes)

    def install(self, observations):
        self.store.fleet_source = lambda: copy.deepcopy(observations)
        self.store.refresh_fleet_inventory()
        status = self.store.fleet_inventory_status()
        self.assertEqual(status["last_error"], "")
        self.assertTrue(status["fresh"], status)
        self.assertEqual(len(self.store.state["instances"]), len(observations))

    def candidate(self, **changes):
        return dict({"id": "test-builder", "name": "Test builder", "role": "rust_builder",
                     "machine_type": "n4-highcpu-16", "vcpu": 16, "ram_gib": 32,
                     "cpu_platform": "Intel Emerald Rapids", "benchmark_score": 2000,
                     "disk_category": "normal", "boot_disk_type": "pd-balanced", "boot_disk_gb": 40,
                     "data_disk_type": "pd-balanced", "data_disk_gb": 111,
                     "runners_count": 1, "runner_vcpu": 16, "runner_ram_gib": 28,
                     "unit_vcpu": 16, "unit_ram_gib": 28, "units_per_instance": 1,
                     "cloud_init_template_id": "ci-rust-builder-199"}, **changes)

    def assign_template(self, **changes):
        self.store.state["vm_templates"] = [self.candidate(**changes)]
        self.profile.update(use_vm_template=True, vm_template_id="test-builder")

    def plan(self, target=3):
        before = copy.deepcopy(self.store.state)
        persisted = self.path.read_bytes()
        try:
            return self.store.scale_region_instances({"role": "rust_builder", "region": "us-south1",
                                                       "target_instances": target})
        finally:
            self.assertEqual(self.store.state, before, "Plan changed controller state")
            self.assertEqual(self.path.read_bytes(), persisted, "Plan persisted changes")

    def assert_fixed_plan(self, plan, *, target, previous):
        self.assertEqual(plan["status"], "plan_only")
        self.assertFalse(plan["applied"])
        self.assertEqual(plan["previous_count"], previous)
        self.assertEqual(plan["planned_count"], target)
        self.assertEqual(len(plan["would_launch"]), target - previous)
        self.assertEqual(len(plan["planned_instances"]), target)
        for record in plan["would_launch"] + plan["planned_instances"]:
            self.assertEqual((record["machine_type"], record["vcpu"], record["ram_gib"]),
                             ("n4-highcpu-16", 16, 32))
            self.assertEqual((record["runner_vcpu"], record["runner_ram_gib"]), (16, 28))

    def test_default_c4d_template_growth_rejected_without_existing_builders(self):
        self.assertEqual(self.store.state["instances"], [])
        self.assertTrue(self.profile["use_vm_template"])
        template = next(t for t in self.store.state["vm_templates"]
                        if t["id"] == self.profile["vm_template_id"])
        self.assertEqual(template["id"], "tpl-builder-c4d-16")
        self.assertEqual(template["machine_type"], "c4d-highcpu-16")
        with self.assertRaisesRegex(ValueError, self.shape_error):
            self.plan()

    def test_supplied_templates_reject_wrong_machine_cpu_or_ram(self):
        for shape in self.invalid_shapes:
            with self.subTest(shape=shape):
                self.assign_template(**shape)
                with self.assertRaisesRegex(ValueError, self.shape_error):
                    self.plan()

    def test_qualifier_cannot_bypass_fixed_shape_for_either_offer_list(self):
        self.profile["use_vm_template"] = False
        for key in ("spot_price_qualifying", "hardware_qualifying"):
            for shape in self.invalid_shapes:
                offers = {"spot_price_qualifying": [], "hardware_qualifying": []}
                offers[key] = [self.candidate(**shape)]
                with self.subTest(key=key, shape=shape), \
                     patch("lib.dashboard_server.qualify_instances", return_value=offers) as qualify:
                    with self.assertRaisesRegex(ValueError, self.shape_error):
                        self.plan()
                    qualify.assert_called_once()

    def test_valid_n4_template_plan_contains_only_fixed_builders(self):
        self.assign_template()
        self.assert_fixed_plan(self.plan(), target=3, previous=0)

    def test_valid_n4_qualified_plan_contains_only_fixed_builders(self):
        self.profile["use_vm_template"] = False
        for key in ("spot_price_qualifying", "hardware_qualifying"):
            offers = {"spot_price_qualifying": [], "hardware_qualifying": []}
            offers[key] = [self.candidate()]
            with self.subTest(key=key), \
                 patch("lib.dashboard_server.qualify_instances", return_value=offers) as qualify:
                self.assert_fixed_plan(self.plan(), target=3, previous=0)
                qualify.assert_called_once()

    def test_valid_existing_source_and_every_planned_builder_have_fixed_shape(self):
        self.profile["use_vm_template"] = False
        self.install([self.observation()])
        with patch("lib.dashboard_server.qualify_instances", side_effect=AssertionError("Unexpected fallback")):
            self.assert_fixed_plan(self.plan(), target=3, previous=1)

    def test_wrong_shape_source_cannot_create_template_even_without_assignment(self):
        for shape in self.invalid_shapes:
            self.install([self.observation(**shape)])
            for assign in (False, True):
                with self.subTest(shape=shape, assign=assign):
                    before = copy.deepcopy(self.store.state)
                    persisted = self.path.read_bytes()
                    with self.assertRaisesRegex(ValueError, self.shape_error):
                        self.store.create_vm_template_from_instance({"instance_id": "builder-1",
                                                                    "assign_to_profile": assign})
                    self.assertEqual(self.store.state, before)
                    self.assertEqual(self.path.read_bytes(), persisted)

    def test_ambiguous_storage_cannot_create_template_with_empty_shared_attachments(self):
        self.install([self.observation(storage_billing_ambiguous=True)])
        source = self.store.state["instances"][0]
        self.assertTrue(source["storage_billing_ambiguous"])
        self.assertEqual(source["shared_attachments"], [])
        for assign in (False, True):
            with self.subTest(assign=assign):
                before = copy.deepcopy(self.store.state)
                persisted = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "(?i)ambiguous|shared"):
                    self.store.create_vm_template_from_instance({"instance_id": "builder-1",
                                                                "assign_to_profile": assign})
                self.assertEqual(self.store.state, before)
                self.assertEqual(self.path.read_bytes(), persisted)

    def test_growth_cannot_copy_ambiguous_storage_but_may_use_safe_offer(self):
        self.profile["use_vm_template"] = False
        self.install([self.observation(storage_billing_ambiguous=True)])
        # Compute-only budget deliberately avoids an unrelated missing-storage-cost hold.
        self.assertEqual(self.store._incomplete_cost_hosts("rust_builder"), [])
        offers = {"spot_price_qualifying": [self.candidate(data_disk_gb=111)], "hardware_qualifying": []}
        with patch("lib.dashboard_server.qualify_instances", return_value=offers) as qualify:
            try:
                plan = self.plan()
            except ValueError as exc:
                self.assertRegex(str(exc), "(?i)ambiguous|shared")
            else:
                qualify.assert_called_once()
                self.assert_fixed_plan(plan, target=3, previous=1)
                self.assertTrue(all(row["data_disk_gb"] == 111 for row in plan["would_launch"]))
                self.assertTrue(all(not row.get("storage_billing_ambiguous")
                                    for row in plan["planned_instances"] if row["name"] != "builder-source"))


if __name__ == "__main__":
    unittest.main()
