"""Query and scale-up completeness follow the cost components selected by the user."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import DashboardStore, _pricing_component_reasons, _selected_pricing_complete


class DashboardComponentPricingTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        pricing = patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={})
        pricing.start()
        self.addCleanup(pricing.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DashboardStore(state_path=Path(self.temp.name) / "state.json")
        self.store.start()
        self.addCleanup(self.store.close)

    def observation(self, role="rust_builder", **changes):
        name = "builder-components" if role == "rust_builder" else "oxidex-runners-components"
        obs = {"instance_id": role, "name": name, "role": role,
               "status": "RUNNING", "zone": "us-south1-b", "region": "us-south1",
               "machine_type": "n4-highcpu-16", "vcpu": 16, "ram_gib": 32.0, "spot": True,
               "specs_known": True, "runner_limits_known": True, "runners_count_known": True,
               "runners_count": 1, "runner_vcpu": 16, "runner_ram_gib": 28.0,
               "boot_disk_type": "pd-balanced", "boot_disk_gb": 0, "data_disk_type": "pd-balanced",
               "data_disk_gb": 0, "data_disks": {}, "disks_detail": [], "disk_types_known": True,
               "created_epoch": self.now - 36000, "last_start_epoch": self.now - 36000}
        return dict(obs, **changes)

    def install(self, observations):
        # Each subcase starts with fresh evidence, avoiding debt from a previous scenario.
        self.store.state["instances"] = []
        self.store.fleet_source = lambda: observations
        self.store.refresh_fleet_inventory()
        self.assertEqual(len(self.store.state["instances"]), len(observations))
        self.assertEqual(self.store._inventory_stale_reason(), "")
        return self.store.state["instances"]

    def assert_budget_mode(self, role, *, storage, complete, expected_hosts=None):
        self.store.state["profiles"][role]["budget"].update(include_disk_costs=storage, include_taxes=False)
        self.store.state["scaling"][role]["include_disk_in_scale_budget"] = storage
        incomplete = self.store._incomplete_cost_hosts(role)
        self.assertEqual(incomplete, [] if complete else
                         (expected_hosts if expected_hosts is not None else [self.observation(role)["name"]]))
        evaluation = self.store._fail_closed_evaluation(
            {"direction": "scale_up", "current_runners": 1, "desired_runners": 2,
             "current_instances": 1, "desired_instances": 2}, role)
        self.assertEqual(evaluation["direction"], "scale_up" if complete else "hold")
        if not complete:
            self.assertIn("not fully priced", evaluation["hold_reason"])

    def assert_storage_only(self, changes, *, history=False):
        for role in ("rust_builder", "cicd_runner"):
            with self.subTest(role=role, changes=changes):
                if history:
                    # The earlier run was observed and priced before the suspension.
                    self.install([self.observation(role)])
                    self.store.fleet_source = lambda: [self.observation(role, **changes)]
                    self.store.refresh_fleet_inventory()
                    record = self.store.state["instances"][0]
                else:
                    record = self.install([self.observation(role, **changes)])[0]
                compute_reasons, storage_reasons = _pricing_component_reasons(record)
                self.assertEqual(compute_reasons, [])
                if not history:
                    self.assertTrue(storage_reasons)
                    self.assertFalse(_selected_pricing_complete(record, include_storage=True))
                self.assertTrue(_selected_pricing_complete(record, include_storage=False))
                for storage in (False, True):
                    view = self.store.query_instances(window_hours=24, include_disk_and_tax=storage)
                    self.assertEqual(len(view["instances"]), 1)
                    row = view["instances"][0]
                    self.assertEqual(view["window_cost_complete"], not storage)
                    self.assertEqual(row["window_cost_complete"], not storage)
                    self.assertEqual(row["lifetime_selected_cost_complete"], not storage)
                    self.assertTrue(row["compute_pricing_complete"])
                    self.assert_budget_mode(role, storage=storage, complete=not storage)

    def test_regional_storage_does_not_poison_compute_query_or_budget(self):
        self.assert_storage_only({"regional_disks": True})

    def test_unpriced_disk_performance_does_not_poison_compute_query_or_budget(self):
        self.assert_storage_only({"unpriced_disk_performance": True})

    def test_unknown_disk_catalog_does_not_poison_compute_query_or_budget(self):
        # The inventory itself succeeded; an individual attachment's type is unresolved.
        self.assert_storage_only({"disk_types_known": False})

    def test_unknown_disk_rate_does_not_poison_compute_query_or_budget(self):
        self.assert_storage_only({"data_disk_type": "future-disk-type", "data_disk_gb": 100,
                                  "data_disks": {"future-disk-type": 100},
                                  "disks_detail": [{"type": "future-disk-type", "gb": 100,
                                      "source": "z/unknown-rate", "id": "disk-1", "boot": False,
                                      "billing_owner": True, "auto_delete": False,
                                      "created_epoch": self.now - 36000}]})

    def test_current_suspension_is_storage_only(self):
        self.assert_storage_only({"status": "SUSPENDED", "last_stop_epoch": self.now - 3600,
                                  "last_suspended_epoch": self.now - 3600})

    def test_resumed_suspension_history_is_storage_only(self):
        self.assert_storage_only({"last_suspended_epoch": self.now - 7200,
                                  "last_start_epoch": self.now - 3600}, history=True)

    def test_shared_role_ambiguity_is_consumed_as_storage_only_for_both_roles(self):
        observations = [self.observation(role, storage_billing_ambiguous=True,
            data_disk_gb=100, data_disk_type="pd-balanced", data_disks_attached_gb=100,
            data_disks={"pd-balanced": 100} if role == "rust_builder" else {},
            disks_detail=[{"id": "shared", "source": "z/custom-shared", "type": "pd-balanced", "gb": 100,
                "created_epoch": self.now - 36000, "billing_owner": role == "rust_builder", "boot": False,
                "auto_delete": False, "storage_billing_ambiguous_roles": ["cicd_runner", "rust_builder"]}])
            for role in ("rust_builder", "cicd_runner")]
        records = self.install(observations)
        for record in records:
            self.assertTrue(record["storage_billing_ambiguous"])
            self.assertEqual(_pricing_component_reasons(record)[0], [])
            self.assertTrue(_pricing_component_reasons(record)[1])
            for storage in (False, True):
                self.assert_budget_mode(record["role"], storage=storage, complete=not storage,
                                        expected_hosts=[r["name"] for r in records])
        for storage in (False, True):
            view = self.store.query_instances(include_disk_and_tax=storage)
            self.assertEqual(len(view["instances"]), 2)
            self.assertEqual(view["window_cost_complete"], not storage)
            self.assertTrue(all(row["window_cost_complete"] is (not storage) for row in view["instances"]))

    def test_unknown_compute_stays_held_in_both_modes(self):
        for role in ("rust_builder", "cicd_runner"):
            for changes in ({"spot": False}, {"machine_type": "unknownfamily-standard-16"},
                            {"spot": False, "regional_disks": True}):
                with self.subTest(role=role, changes=changes):
                    record = self.install([self.observation(role, **changes)])[0]
                    self.assertTrue(_pricing_component_reasons(record)[0])
                    for storage in (False, True):
                        self.assertFalse(_selected_pricing_complete(record, include_storage=storage))
                        view = self.store.query_instances(include_disk_and_tax=storage)
                        self.assertFalse(view["window_cost_complete"])
                        self.assertFalse(view["instances"][0]["compute_pricing_complete"])
                        self.assertFalse(view["instances"][0]["lifetime_selected_cost_complete"])
                        self.assert_budget_mode(role, storage=storage, complete=False)

    def test_prior_compute_history_stays_held_in_compute_only_mode(self):
        for role in ("rust_builder", "cicd_runner"):
            record = self.install([self.observation(role, last_stop_epoch=self.now - 7200,
                                                    last_start_epoch=self.now - 3600)])[0]
            self.assertIn("prior_compute", record["unpriced_history_intervals"])
            for storage in (False, True):
                view = self.store.query_instances(window_hours=24, include_disk_and_tax=storage)
                self.assertFalse(view["window_cost_complete"])
                self.assertFalse(view["instances"][0]["lifetime_selected_cost_complete"])
                self.assert_budget_mode(role, storage=storage, complete=False)

    def test_known_gap_is_complete_while_storage_lifetime_remains_incomplete(self):
        record = self.install([self.observation()])[0]
        # Exercise the persisted nested format at the query/budget consumer boundary.
        record["unpriced_history_intervals"] = {"suspended_memory": [
            [self.now - 14400, self.now - 10800], [self.now - 7200, self.now - 5400]]}
        record["unpriced_storage_lifetime_history"] = True
        record["unpriced_lifetime_history"] = True
        with patch("lib.dashboard_server.time.time", return_value=self.now - 8100):
            gap = self.store.query_instances(window_hours=0.5, include_disk_and_tax=True)
        self.assertTrue(gap["window_cost_complete"])
        self.assertFalse(gap["instances"][0]["lifetime_selected_cost_complete"])
        recent = self.store.query_instances(window_hours=1, include_disk_and_tax=True)
        self.assertTrue(recent["window_cost_complete"])
        self.assertFalse(recent["instances"][0]["lifetime_selected_cost_complete"])
        covering = self.store.query_instances(window_hours=5, include_disk_and_tax=True)
        self.assertFalse(covering["window_cost_complete"])
        compute = self.store.query_instances(window_hours=5, include_disk_and_tax=False)
        self.assertTrue(compute["window_cost_complete"])
        self.assertTrue(compute["instances"][0]["lifetime_selected_cost_complete"])
        self.assert_budget_mode("rust_builder", storage=False, complete=True)
        self.assert_budget_mode("rust_builder", storage=True, complete=False)


if __name__ == "__main__":
    unittest.main()
