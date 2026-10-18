"""Unavailable lifecycle charges remain explicit across refreshes and selected cost windows."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import (DashboardStore, _live_instance_record, _mark_instance_deleted,
                                  _unpriced_history_in_window, instance_usage, reconcile_instance_records)


class LifecycleHistoryCompletenessTests(unittest.TestCase):
    now = 1800000000.0

    def setUp(self):
        self.pricing = patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={})
        self.pricing.start()
        self.addCleanup(self.pricing.stop)

    def observation(self, **changes):
        obs = {"instance_id": "history", "name": "oxidex-runners-history", "role": "cicd_runner",
               "status": "RUNNING", "zone": "us-south1-b", "region": "us-south1",
               "machine_type": "n4-highcpu-16", "vcpu": 16, "ram_gib": 32.0, "spot": True,
               "specs_known": True, "boot_disk_type": "pd-balanced", "boot_disk_gb": 40,
               "data_disk_type": "pd-balanced", "data_disk_gb": 0, "runners_count": 4,
               "runners_count_known": True, "created_epoch": self.now - 10 * 3600,
               "last_start_epoch": self.now - 10 * 3600, "last_stop_epoch": None,
               "last_suspended_epoch": None}
        return dict(obs, **changes)

    def test_suspended_history_survives_resume_and_expires_only_from_window(self):
        suspended = self.observation(status="SUSPENDED", last_suspended_epoch=self.now - 3 * 3600)
        record = _live_instance_record(suspended, project="p", now=self.now - 2 * 3600)
        self.assertFalse(record["lifetime_cost_complete"])
        resumed = self.observation(last_start_epoch=self.now - 3600, last_suspended_epoch=self.now - 3 * 3600)
        record = _live_instance_record(resumed, project="p", now=self.now,
                                       previous=json.loads(json.dumps(record)))
        self.assertTrue(record["pricing_complete"])  # Current compute/disk rates are known.
        self.assertFalse(record["lifetime_cost_complete"])
        self.assertEqual(record["unpriced_history_intervals"]["suspended_memory"],
                         [[self.now - 3 * 3600, self.now - 3600]])
        self.assertTrue(_unpriced_history_in_window(record, self.now - 86400, self.now))
        self.assertFalse(_unpriced_history_in_window(record, self.now - 1800, self.now))
        self.assertFalse(_unpriced_history_in_window(record, self.now - 86400, self.now, include_storage=False))
        with tempfile.TemporaryDirectory() as temp:
            store = DashboardStore(state_path=Path(temp) / "state.json")
            store.start()
            self.addCleanup(store.close)
            store.state["instances"] = [record]
            with patch("lib.dashboard_server.time.time", return_value=self.now):
                self.assertIn(record["name"], store._incomplete_cost_hosts("cicd_runner"))
                self.assertFalse(store.query_instances(window_hours=24)["window_cost_complete"])
                self.assertTrue(store.query_instances(window_hours=0.5)["window_cost_complete"])
                self.assertTrue(store.query_instances(window_hours=24, include_disk_and_tax=False)["window_cost_complete"])
            future = self.now + 32 * 86400
            rows, _ = reconcile_instance_records([record], [resumed], project="p", now=future)
            store.state["instances"] = rows
            with patch("lib.dashboard_server.time.time", return_value=future):
                self.assertEqual(store._incomplete_cost_hosts("cicd_runner"), [])
                self.assertTrue(store.query_instances(window_hours=744)["window_cost_complete"])
            self.assertFalse(rows[0]["lifetime_cost_complete"])

    def test_missed_suspend_cycle_is_also_uncertain(self):
        running = _live_instance_record(self.observation(), project="p", now=self.now - 5 * 3600)
        resumed = self.observation(last_start_epoch=self.now - 3600, last_suspended_epoch=self.now - 3 * 3600)
        record = _live_instance_record(resumed, project="p", now=self.now, previous=running)
        self.assertFalse(record["lifetime_cost_complete"])
        self.assertEqual(record["unpriced_history_intervals"]["suspended_memory"],
                         [[self.now - 3 * 3600, self.now - 3600]])

    def test_legacy_suspended_record_preserves_memory_uncertainty_on_resume(self):
        legacy = _live_instance_record(self.observation(status="SUSPENDED", last_suspended_epoch=self.now - 3 * 3600),
                                      project="p", now=self.now - 2 * 3600)
        legacy.pop("unpriced_history_intervals")
        legacy["unpriced_lifetime_history"] = False
        record = _live_instance_record(self.observation(last_start_epoch=self.now - 3600), project="p",
                                       now=self.now, previous=legacy)
        self.assertFalse(record["lifetime_cost_complete"])
        self.assertEqual(record["unpriced_history_intervals"]["suspended_memory"],
                         [[self.now - 3 * 3600, self.now - 3600]])

    def test_recent_deleted_uncertainty_survives_count_compaction(self):
        record = _live_instance_record(self.observation(status="SUSPENDED", last_suspended_epoch=self.now - 3600),
                                       project="p", now=self.now)
        deleted = _mark_instance_deleted(record, self.now + 60, project="p")
        with patch("lib.dashboard_server.MAX_DETAILED_INSTANCE_RECORDS", 0):
            rows, _ = reconcile_instance_records([deleted], [], project="p", now=self.now + 120, compacted={})
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["lifetime_cost_complete"])
        self.assertTrue(_unpriced_history_in_window(rows[0], self.now - 86400, self.now + 120))

    def test_restarted_initial_baseline_does_not_claim_complete_compute_history(self):
        obs = self.observation(last_start_epoch=self.now - 3600, last_stop_epoch=self.now - 2 * 3600)
        record = _live_instance_record(obs, project="p", now=self.now)
        self.assertFalse(record["lifetime_cost_complete"])
        self.assertEqual(record["unpriced_history_intervals"]["prior_compute"],
                         [[self.now - 10 * 3600, self.now - 2 * 3600]])
        self.assertAlmostEqual(instance_usage(record, self.now)["compute_hours"], 1)
        again = _live_instance_record(obs, project="p", now=self.now,
                                      previous=json.loads(json.dumps(record)))
        self.assertEqual(again["unpriced_history_intervals"], record["unpriced_history_intervals"])
        self.assertFalse(again["lifetime_cost_complete"])
        self.assertFalse(_unpriced_history_in_window(again, self.now - 1800, self.now))
        self.assertTrue(_live_instance_record(self.observation(), project="p", now=self.now)["lifetime_cost_complete"])

    def test_deleted_suspension_closes_uncertainty_without_mutating_previous_record(self):
        suspended = _live_instance_record(self.observation(status="SUSPENDED", last_suspended_epoch=self.now - 3600),
                                         project="p", now=self.now)
        deleted = _mark_instance_deleted(suspended, self.now + 3600, project="p")
        self.assertIsNone(suspended["unpriced_history_intervals"]["suspended_memory"][0][1])
        self.assertEqual(deleted["unpriced_history_intervals"]["suspended_memory"][0][1], self.now + 3600)
        self.assertFalse(deleted["lifetime_cost_complete"])
