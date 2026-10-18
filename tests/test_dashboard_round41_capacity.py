"""Inventory capacity plans and storage liabilities follow observed resource contracts."""

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import (DashboardStore, _live_instance_record, instance_usage,
                                  reconcile_instance_records, _unpriced_history_in_window)


class CapacityContracts(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.pricing = patch('lib.dashboard_models.fetch_live_spot_pricing', return_value={})
        self.pricing.start()
        self.addCleanup(self.pricing.stop)

    def observation(self, **changes):
        return dict({"instance_id": "vm", "name": "builder-real", "role": "rust_builder", "status": "RUNNING",
                     "zone": "us-south1-b", "region": "us-south1", "machine_type": "n4-highcpu-16",
                     "vcpu": 16, "ram_gib": 32., "spot": True, "runner_limits_known": True,
                     "runners_count_known": True, "runners_count": 4, "boot_disk_type": "pd-balanced",
                     "boot_disk_gb": 0, "data_disk_type": "pd-balanced", "data_disk_gb": 100,
                     "data_disks": {"pd-balanced": 100}, "created_epoch": self.now - 36000,
                     "last_start_epoch": self.now - 36000, "disks_detail": [
                         {"id": "disk", "source": "zones/us-south1-b/disks/cache", "type": "pd-balanced",
                          "gb": 100, "created_epoch": self.now - 36000, "auto_delete": False}]}, **changes)

    def test_runner_retained_attachment_keeps_billing_when_list_denied(self):
        for retained in (True, False):
            with self.subTest(retained=retained):
                obs = self.observation(role="cicd_runner", name="oxidex-runners-real")
                obs['disks_detail'][0]['auto_delete'] = not retained
                before = _live_instance_record(obs, project='p', now=self.now)
                original = copy.deepcopy(before)
                records, changed = reconcile_instance_records([before], [], project='p', now=self.now + 3600,
                                                               disks_listed=False)
                self.assertTrue(changed)
                self.assertEqual(before, original)
                deleted = records[0]
                self.assertEqual(bool(deleted.get('disk_liability_pending')), retained)
                cost = instance_usage(deleted, self.now + 7200)['cost_all_in']
                previous_cost = instance_usage(deleted, self.now + 3600)['cost_all_in']
                if retained:
                    self.assertGreater(cost, previous_cost)
                    self.assertEqual(deleted['pricing']['compute_hourly_usd'], 0)
                else:
                    self.assertAlmostEqual(cost, previous_cost)

    def test_incompatible_running_builder_cannot_satisfy_noop_target(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DashboardStore(state_path=Path(temp) / 'state.json')
            store.start()
            self.addCleanup(store.close)
            store.fleet_source = lambda: [self.observation(runner_limits_known=False)]
            store.refresh_fleet_inventory()
            original = copy.deepcopy(store.state)
            for target in (0, 1, 2):
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'incompatible'):
                    store.scale_region_instances({'role': 'rust_builder', 'region': 'us-south1',
                                                  'target_instances': target})
            self.assertEqual(store.state, original)
            store.fleet_source = lambda: [self.observation()]
            store.refresh_fleet_inventory()
            self.assertEqual(store.scale_region_instances({'role': 'rust_builder', 'region': 'us-south1',
                                                           'target_instances': 1})['would_retire'], [])

    def test_two_suspensions_keep_clean_running_gap_after_reload(self):
        first_stop, first_start = self.now - 8*3600, self.now - 7*3600
        second_stop, second_start = self.now - 3*3600, self.now - 2*3600
        first = _live_instance_record(self.observation(status='SUSPENDED', last_suspended_epoch=first_stop),
                                      project='p', now=first_stop+60)
        resumed = _live_instance_record(self.observation(last_start_epoch=first_start,
                    last_suspended_epoch=first_stop), project='p', now=first_start+60, previous=first)
        second = _live_instance_record(self.observation(status='SUSPENDED', last_start_epoch=first_start,
                    last_suspended_epoch=second_stop), project='p', now=second_stop+60, previous=resumed)
        final = _live_instance_record(self.observation(last_start_epoch=second_start,
                    last_suspended_epoch=second_stop), project='p', now=self.now, previous=second)
        final = json.loads(json.dumps(final))
        self.assertEqual(final['unpriced_history_intervals']['suspended_memory'],
                         [[first_stop, first_start], [second_stop, second_start]])
        self.assertFalse(_unpriced_history_in_window(final, first_start+60, second_stop-60))
        self.assertTrue(_unpriced_history_in_window(final, second_stop, second_start))
        self.assertFalse(_unpriced_history_in_window(final, first_stop, self.now, include_storage=False))
