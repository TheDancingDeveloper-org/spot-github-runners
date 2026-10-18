"""Stopped gaps, restart staging, and unseen runs keep separate billing and planning boundaries."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import (DashboardStore, _live_instance_record, _nonrunning_hourly, _pricing_rates,
                                  _unpriced_history_in_window, instance_usage)


class LifecycleIntervalTests(unittest.TestCase):
    now = 1800000000.0

    def setUp(self):
        self.pricing = patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={})
        self.pricing.start()
        self.addCleanup(self.pricing.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DashboardStore(state_path=Path(self.temp.name) / "state.json", clock=lambda: self.now)
        self.store.start()
        self.addCleanup(self.store.close)

    def observation(self, **changes):
        obs = {"instance_id": "a", "name": "oxidex-runners-lifecycle-a", "role": "cicd_runner",
               "status": "TERMINATED", "zone": "us-south1-b", "region": "us-south1",
               "machine_type": "n4-highcpu-16", "vcpu": 16, "ram_gib": 32.0, "spot": True,
               "specs_known": True, "boot_disk_type": "pd-balanced", "boot_disk_gb": 0,
               "data_disk_type": "pd-balanced", "data_disk_gb": 0, "runners_count": 4,
               "runners_count_known": True, "created_epoch": self.now - 8 * 3600,
               "last_start_epoch": self.now - 8 * 3600, "last_stop_epoch": self.now - 5 * 3600}
        return dict(obs, **changes)

    def record(self, obs, at, previous=None):
        return _live_instance_record(obs, project="p", now=at, previous=previous, tax_overrides={"cicd_runner": 0})

    def test_restart_staging_does_not_reopen_the_known_stopped_gap(self):
        stopped = self.record(self.observation(), self.now - 4 * 3600)
        original_rate = _pricing_rates(stopped["pricing"])[0]
        for status in ("PROVISIONING", "STAGING"):
            for report_stop in (False, True):
                with self.subTest(status=status, report_stop=report_stop):
                    obs = self.observation(status=status, last_stop_epoch=stopped["stopped_epoch"] if report_stop else None)
                    staging = self.record(obs, self.now - 2 * 3600, json.loads(json.dumps(stopped)))
                    self.assertEqual(staging["stopped_epoch"], stopped["stopped_epoch"])
                    self.assertEqual(staging["transitional_since_epoch"], self.now - 2 * 3600)
                    self.assertAlmostEqual(instance_usage(staging, self.now - 2 * 3600)["compute_hours"], 3)
                    self.assertAlmostEqual(instance_usage(staging, self.now - 2 * 3600)["cost_all_in"], 3 * original_rate)
                    self.assertGreater(_nonrunning_hourly(staging, include_disk=True, include_tax=True), 0)
                    with patch("lib.dashboard_server.fetch_live_spot_pricing", return_value={
                            "machine_hourly": {"us-south1/n4-highcpu-16": 0.5}}), \
                         patch.object(self.store, "_clock", return_value=self.now - 90 * 60):
                        self.store._refresh_instances_pricing([staging], live=True)
                    staging = self.record(obs, self.now - 75 * 60, json.loads(json.dumps(staging)))
                    self.assertAlmostEqual(instance_usage(staging, self.now - 75 * 60)["compute_hours"], 3)
                    running = self.record(self.observation(status="RUNNING", last_start_epoch=self.now - 3600),
                                          self.now, json.loads(json.dumps(staging)))
                    usage = instance_usage(running, self.now)
                    self.assertAlmostEqual(usage["compute_hours"], 4)
                    self.assertAlmostEqual(usage["cost_all_in"], 3 * original_rate + _pricing_rates(running["pricing"])[0])
                    self.assertIsNone(running.get("transitional_since_epoch"))
                    self.assertTrue(running["lifetime_cost_complete"])

    def test_unseen_intervening_run_records_bounded_uncertainty_without_billing_the_gap(self):
        stopped = self.record(self.observation(), self.now - 4 * 3600)
        for end_field in ("last_stop_epoch", "last_suspended_epoch"):
            with self.subTest(end_field=end_field):
                obs = self.observation(status="RUNNING", last_start_epoch=self.now - 3600)
                obs[end_field] = self.now - 2 * 3600
                running = self.record(obs, self.now, json.loads(json.dumps(stopped)))
                self.assertEqual(running["prior_runs"][0][:2], [self.now - 8 * 3600, self.now - 5 * 3600])
                self.assertAlmostEqual(instance_usage(running, self.now)["compute_hours"], 4)
                self.assertFalse(running["lifetime_cost_complete"])
                self.assertIn("prior_compute", running["unpriced_history_intervals"])
                self.assertEqual(running["unpriced_history_intervals"]["prior_compute"],
                                 [[self.now - 5 * 3600, self.now - 2 * 3600]])
                self.assertFalse(running["lifetime_cost_complete"])
                self.assertTrue(_unpriced_history_in_window(running, self.now - 24 * 3600, self.now))
                self.assertFalse(_unpriced_history_in_window(running, self.now - 1800, self.now))
                self.store.state["instances"] = [running]
                with patch("lib.dashboard_server.time.time", return_value=self.now):
                    self.assertIn(running["name"], self.store._incomplete_cost_hosts("cicd_runner"))
                again = self.record(obs, self.now + 33 * 86400, json.loads(json.dumps(running)))
                self.store.state["instances"] = [again]
                with patch("lib.dashboard_server.time.time", return_value=self.now + 33 * 86400):
                    self.assertEqual(self.store._incomplete_cost_hosts("cicd_runner"), [])
                self.assertFalse(again["lifetime_cost_complete"])

    def test_provable_single_intervening_run_is_retained_without_false_uncertainty(self):
        stopped = self.record(self.observation(), self.now - 4 * 3600)
        for status in ("TERMINATED", "STAGING"):
            with self.subTest(status=status):
                later = self.record(self.observation(status=status, last_start_epoch=self.now - 4 * 3600,
                                                     last_stop_epoch=self.now - 2 * 3600), self.now, stopped)
                self.assertAlmostEqual(instance_usage(later, self.now)["compute_hours"], 5)
                self.assertTrue(later["lifetime_cost_complete"])
                self.assertFalse(later.get("unpriced_history_intervals"))
                latest = self.record(self.observation(status="RUNNING", last_start_epoch=self.now - 3600,
                                                      last_stop_epoch=self.now - 2 * 3600), self.now, later)
                self.assertAlmostEqual(instance_usage(latest, self.now)["compute_hours"], 6)
                self.assertTrue(latest["lifetime_cost_complete"])

    def test_size_pressure_keeps_nonempty_billing_identity_and_cost_semantics(self):
        from lib.dashboard_server import _billed_record_disk_keys, _coarsen_record_buckets

        record = self.record(self.observation(), self.now)
        record.update(billed_disk_sources=["id:kept"], billed_disk_source_bounds={}, billed_disk_identified_sources=[])
        original = copy.deepcopy(record)
        compact = json.loads(json.dumps(_coarsen_record_buckets(record)))
        self.assertEqual(_billed_record_disk_keys(compact), _billed_record_disk_keys(original))
        self.assertEqual(instance_usage(compact, self.now), instance_usage(original, self.now))
        self.assertEqual(record, original)
        self.assertIn("id:kept", compact["billed_disk_sources"])
        self.assertNotIn("billed_disk_source_bounds", compact)
        self.assertNotIn("billed_disk_identified_sources", compact)

    def test_regional_plans_require_settled_role_capacity_for_all_targets(self):
        active = [self.observation(instance_id=str(i), name=f"oxidex-runners-live-{i}", status="RUNNING", last_stop_epoch=None)
                  for i in (1, 2)]
        self.fleet = []
        self.store.fleet_source = lambda: self.fleet
        for status in ("PROVISIONING", "STAGING", "STOPPING", "REPAIRING"):
            self.fleet = active + [self.observation(instance_id="pending", name="oxidex-runners-pending", status=status,
                                                   created_epoch=self.now - 60, last_start_epoch=None, last_stop_epoch=None)]
            self.store.refresh_fleet_inventory()
            before = copy.deepcopy(self.store.state)
            for target in (0, 1, 2, 3):
                with self.subTest(status=status, target=target):
                    with self.assertRaisesRegex(ValueError, "capacity has settled"):
                        self.store.scale_region_instances({"region": "us-south1", "role": "cicd_runner",
                                                           "target_instances": target})
            held = self.store._fail_closed_evaluation({"direction": "scale_down", "current_instances": 2,
                "desired_instances": 1, "current_runners": 8, "desired_runners": 4}, "cicd_runner")
            self.assertEqual(held["direction"], "hold")
            self.assertEqual(held["desired_instances"], 2)
            self.assertEqual(self.store.state, before)
        # A different role's pending VM does not change this role's eventual VM count.
        self.fleet = active + [self.observation(instance_id="builder", name="builder-pending", role="rust_builder",
                                               status="STAGING", last_start_epoch=None, last_stop_epoch=None)]
        self.store.refresh_fleet_inventory()
        plan = self.store.scale_region_instances({"region": "us-south1", "role": "cicd_runner", "target_instances": 1})
        self.assertEqual(len(plan["would_retire"]), 1)
