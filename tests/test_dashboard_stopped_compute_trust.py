"""Current storage trust stays separate from historical fallback compute."""
import tempfile
import unittest
from pathlib import Path

from lib.dashboard_server import (DashboardStore, _live_instance_record, _pricing_component_reasons,
                                  _selected_pricing_complete, _unpriced_history_in_window)
from unittest.mock import patch


class StoppedComputeTrustTests(unittest.TestCase):
    def test_stopped_rates_do_not_hold_but_historical_compute_remains_incomplete(self):
        now = 1800000000.
        for status in ('TERMINATED', 'STOPPED', 'SUSPENDED'):
            for pricing in ({'complete': False, 'pricing_source': 'spot_baseline_for_on_demand_vm'},
                            {'complete': False, 'family_rate_unknown': True},
                            {'complete': False, 'regional_compute_rate_unknown': True}):
                with self.subTest(status=status, pricing=pricing):
                    record = {'status': status, 'machine_type': 'n4-highcpu-16', 'pricing': pricing}
                    self.assertEqual(_pricing_component_reasons(record)[0], [])
                    self.assertTrue(_pricing_component_reasons(record, historical=True)[0])
                    self.assertTrue(_selected_pricing_complete(record, include_storage=False))
        obs = {'instance_id': '1', 'name': 'builder-old', 'role': 'rust_builder',
               'status': 'TERMINATED', 'machine_type': 'n4-highcpu-16', 'vcpu': 16, 'ram_gib': 32.,
               'spot': False, 'region': 'us-south1', 'zone': 'us-south1-a',
               'created_epoch': now - 40*86400, 'last_start_epoch': now - 40*86400,
               'last_stop_epoch': now - 35*86400, 'boot_disk_gb': 0, 'data_disk_gb': 100,
               'data_disk_type': 'pd-balanced', 'data_disks': {'pd-balanced': 100},
               'disk_types_known': True}
        with patch('lib.dashboard_models.fetch_live_spot_pricing', return_value={}):
            record = _live_instance_record(obs, project='p', now=now)
        self.assertEqual(_pricing_component_reasons(record), ([], []))
        self.assertTrue(_selected_pricing_complete(record, include_storage=True))
        self.assertTrue(record['unpriced_compute_lifetime_history'])
        self.assertFalse(_unpriced_history_in_window(record, now-31*86400, now, include_storage=True))
        with patch('lib.dashboard_models.fetch_live_spot_pricing', return_value={}):
            recent = _live_instance_record(obs, project='p', now=now-34*86400)
        self.assertTrue(_unpriced_history_in_window(recent, now-41*86400, now-34*86400, include_storage=True))
        with tempfile.TemporaryDirectory() as tmp, patch('lib.dashboard_models.fetch_live_spot_pricing', return_value={}):
            store = DashboardStore(state_path=Path(tmp)/'state.json', clock=lambda:now, fleet_source=lambda:[])
            self.addCleanup(store.close)
            store.hydrate()
            store.state['instances'] = [record]
            store.state['profiles']['rust_builder']['budget'].update(include_disk_costs=True, include_taxes=False)
            store.state['scaling']['rust_builder']['include_disk_in_scale_budget'] = True
            with patch('lib.dashboard_server.time.time', return_value=now):
                self.assertEqual(store._incomplete_cost_hosts('rust_builder'), [])
                view = store.query_instances(window_hours=744, include_disk_and_tax=False)
            self.assertTrue(view['window_cost_complete'])
            self.assertFalse(view['instances'][0]['lifetime_selected_cost_complete'])

    def test_transitional_states_keep_current_compute_hold(self):
        for status in ('RUNNING', 'PROVISIONING', 'STAGING', 'STOPPING', 'REPAIRING'):
            with self.subTest(status=status):
                record = {'status': status, 'pricing': {'complete': False, 'family_rate_unknown': True}}
                self.assertFalse(_selected_pricing_complete(record, include_storage=False))
