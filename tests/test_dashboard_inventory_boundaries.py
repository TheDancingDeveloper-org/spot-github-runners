"""Disk naming, first-start proof, and OOM eligibility share the inventory's actual boundaries."""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import DashboardStore, _live_instance_record, _pricing_rates, instance_usage
from lib.dashboard_scaling import default_scaling_config, evaluate_scaling_decision
from lib.fleet_inventory import observe_detached_disks


class InventoryBoundaryTests(unittest.TestCase):
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
        obs = {"instance_id": "first-start", "name": "oxidex-runners-first-start", "role": "cicd_runner",
               "status": "PROVISIONING", "zone": "us-south1-b", "region": "us-south1", "machine_type": "n4-highcpu-16",
               "vcpu": 16, "ram_gib": 32.0, "spot": True, "specs_known": True, "boot_disk_type": "pd-balanced",
               "boot_disk_gb": 0, "data_disk_type": "pd-balanced", "data_disk_gb": 0,
               "created_epoch": self.now - 3600, "last_start_epoch": None, "runners_count": 4,
               "runners_count_known": True}
        return dict(obs, **changes)

    def test_runner_data_namespace_and_fleet_user_roles_are_observed(self):
        for name, users, role in (("runner-data", [], "cicd_runner"),
                ("runner-data-pool-a", [], "cicd_runner"),
                ("custom-cache", ["oxidex-runners-host"], "cicd_runner"),
                ("runner-data-shared", ["builder-host"], "rust_builder")):
            with self.subTest(name=name, users=users):
                link = "projects/p/zones/us-south1-b/disks/" + name
                detail = {link: {"name": name, "id": "123", "type": "pd-balanced", "sizeGb": "100",
                    "zone": "zones/us-south1-b", "users": ["projects/p/zones/us-south1-b/instances/" + u for u in users]}}
                observations = observe_detached_disks(detail)
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]["role"], role)
                self.assertEqual(observations[0]["status"], "DETACHED")
                self.assertEqual(observe_detached_disks(detail, frozenset({link})), [])
        self.assertEqual(observe_detached_disks({"x": {"name": "runner-database", "id": "outside"}}), [])

    def test_first_start_survives_prestart_repricing_and_reload_without_compute_history(self):
        for initial_status in ("PROVISIONING", "STAGING"):
            with self.subTest(initial_status=initial_status):
                obs = self.observation(status=initial_status)
                record = _live_instance_record(obs, project="p", now=self.now - 1800,
                                               tax_overrides={"cicd_runner": 0})
                self.assertEqual(instance_usage(record, self.now - 1800)["compute_hours"], 0)
                self.assertFalse(record.get("compute_start_known", True))
                with patch("lib.dashboard_server.fetch_live_spot_pricing", return_value={
                        "machine_hourly": {"us-south1/n4-highcpu-16": 0.5}}), \
                     patch.object(self.store, "_clock", return_value=self.now - 900):
                    self.assertTrue(self.store._refresh_instances_pricing([record], live=True))
                self.assertEqual(record["prior_runs"], [])
                self.assertEqual(instance_usage(record, self.now - 900)["cost_all_in"], 0)
                record = _live_instance_record(dict(obs, status="STAGING"), project="p", now=self.now - 600,
                                               previous=json.loads(json.dumps(record)))
                started = self.now - 300
                record = _live_instance_record(dict(obs, status="RUNNING", last_start_epoch=started), project="p",
                                               now=self.now, previous=json.loads(json.dumps(record)))
                usage = instance_usage(record, self.now, self.now - 86400)
                self.assertTrue(record.get("compute_start_known", True))
                self.assertEqual(record["prior_runs"], [])
                self.assertAlmostEqual(usage["compute_hours"], 300 / 3600)
                self.assertAlmostEqual(usage["compute_hours_in_window"], 300 / 3600)
                self.assertAlmostEqual(usage["cost_all_in"], 300 / 3600 * _pricing_rates(record["pricing"])[0])

    def test_existing_started_vm_keeps_real_history_through_staging_restart(self):
        first_start = self.now - 7200
        stop = self.now - 1800
        obs = self.observation(status="RUNNING", created_epoch=first_start, last_start_epoch=first_start)
        record = _live_instance_record(obs, project="p", now=self.now - 3600)
        record = _live_instance_record(dict(obs, status="STAGING", last_start_epoch=None, last_stop_epoch=stop),
                                       project="p", now=self.now - 900, previous=json.loads(json.dumps(record)))
        self.assertTrue(record.get("compute_start_known", True))
        self.assertEqual(record["started_epoch"], first_start)
        record = _live_instance_record(dict(obs, last_start_epoch=self.now - 300, last_stop_epoch=stop), project="p",
                                       now=self.now, previous=json.loads(json.dumps(record)))
        self.assertTrue(record["prior_runs"])
        self.assertEqual(record["prior_runs"][0][:2], [first_start, stop])
        self.assertAlmostEqual(instance_usage(record, self.now)["compute_hours"], (5400 + 300) / 3600)

    def ignored_runs(self):
        stamp = datetime.fromtimestamp(self.now - 120, timezone.utc).isoformat()
        base = {"failed_at": stamp, "runner_name": "gha-oxidex-runners-owned", "handled": False,
                "baseline_runners": 0}
        return [dict(base, id="timeout", error_excerpt="Timeout reached, aborting!", failure_classification="TIMEOUT"),
                dict(base, id="external", runner_name="GitHub Actions 1", error_excerpt="out of memory"),
                dict(base, id="not-oom", error_excerpt="compilation failed", is_oom=False),
                dict(base, id="not-oom-no-excerpt", is_oom=False),
                dict(base, id="not-ours", runner_name="GitHub Actions 2", error_excerpt="out of memory", is_self_hosted_runner=False),
                dict(base, id="other", error_excerpt="compilation failed", failure_classification="OTHER_FAILURE")]

    def test_unrelated_ci_history_is_unchanged_at_every_oom_touchpoint(self):
        runs = self.ignored_runs()
        original = copy.deepcopy(runs)
        telem = self.store.state["scaling_telemetry"]
        telem["github_oom_runs"], telem["journal_oom_events"] = runs, []
        self.assertEqual(self.store._runner_oom_events(), [])
        self.assertFalse(self.store._active_oom_events("cicd_runner"))
        current = [{"instance_id": "new", "role": "cicd_runner", "status": "RUNNING", "runners_count": 8,
                    "runners_count_known": True, "capacity_pending": True, "started_epoch": self.now - 7200,
                    "capacity_first_ready_lower_bound": self.now - 7200}]
        self.store._consume_ooms_for_fleet_growth([], current)
        self.assertFalse(current[0]["capacity_pending"])
        self.store._consume_active_oom_events(role="cicd_runner", previous_runners=0, current_runners=8,
                                               recent_within=3600, new_capacity_epoch=self.now - 60)
        evaluate_scaling_decision(default_scaling_config()["cicd_runner"],
            {"current_runners": 8, "current_instances": 2, "github_oom_runs": runs}, now_epoch=self.now)
        self.assertEqual(runs, original)

    def test_eligible_fleet_oom_still_gets_baseline_and_acknowledgement(self):
        run = {"id": "real", "runner_name": "gha-oxidex-runners-owned", "error_excerpt": "out of memory",
               # Stale cached flags must not override actual OOM evidence on our fleet runner.
               "is_oom": False, "failure_classification": "TIMEOUT", "is_self_hosted_runner": False,
               "failed_at": datetime.fromtimestamp(self.now - 120, timezone.utc).isoformat()}
        telem = self.store.state["scaling_telemetry"]
        telem["github_oom_runs"], telem["journal_oom_events"] = [run], []
        old = {"instance_id": "old", "role": "cicd_runner", "status": "RUNNING", "runners_count": 4,
               "runners_count_known": True, "capacity_pending": False, "started_epoch": self.now - 7200,
               "capacity_first_ready_lower_bound": self.now - 7200}
        new = dict(old, instance_id="new", capacity_pending=True, started_epoch=self.now - 60,
                   capacity_first_ready_lower_bound=self.now - 60)
        self.assertTrue(self.store._active_oom_events("cicd_runner"))
        self.store._consume_ooms_for_fleet_growth([old], [old, new])
        self.assertEqual(run["baseline_runners"], 4)
        self.assertTrue(run["handled"])
        self.assertTrue(run["acknowledged"])
