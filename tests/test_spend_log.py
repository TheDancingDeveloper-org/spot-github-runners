"""Log the fleet's spend rate and reported project spend once an hour."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from lib import spot_runner_autoscaler as controller
from lib.runner_fleet import GceVm


class SpendLogTests(unittest.TestCase):
    def setUp(self):
        blocker = patch('subprocess.run', side_effect=AssertionError('Unexpected external command'))
        blocker.start()
        self.addCleanup(blocker.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state.json'
        self.args = controller.args_parser().parse_args(['--state-file', str(self.path)])
        self.args.billing_export_table = 'homelab-424523.billing_export.gcp_billing_export_v1_X'
        self.vms = [GceVm(f'oxidex-runners-asiaeast2-a-{i}', 'asia-east2-a', 'RUNNING', 'n4-standard-32', True)
                    for i in (1, 2)]
        self.clock = [100_000.0]
        clock = patch('lib.spot_runner_autoscaler.now', side_effect=lambda: self.clock[0])
        clock.start()
        self.addCleanup(clock.stop)

    def prices(self, vms, project, cap, *, totals=None, **kwargs):
        for vm in vms:
            totals[vm.name] = Decimal('5.157299')
        return []

    def test_hourly_line_reports_fleet_rate_and_project_spend(self):
        state = {}
        with patch('lib.spot_runner_autoscaler.check_running_prices', side_effect=self.prices), \
             patch('lib.spot_runner_autoscaler.log_project_costs') as project_costs, \
             self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
            controller.log_hourly_spend(self.vms, self.args, state, self.path)
        self.assertIn('Spend rate: 2 Spot VMs = $10.31/day ($0.43/hour) for compute and storage at current prices',
                      '\n'.join(logs.output))
        project_costs.assert_called_once_with('homelab-424523', self.args.billing_export_table)
        self.assertEqual(controller.read_scale_state(self.path)['spend_logged_at'], self.clock[0])

    def test_spend_is_logged_at_most_once_an_hour(self):
        state = {}
        with patch('lib.spot_runner_autoscaler.check_running_prices', side_effect=self.prices) as prices, \
             patch('lib.spot_runner_autoscaler.log_project_costs') as project_costs:
            controller.log_hourly_spend(self.vms, self.args, state, self.path)
            self.clock[0] += 3599
            controller.log_hourly_spend(self.vms, self.args, state, self.path)
            self.clock[0] += 1
            controller.log_hourly_spend(self.vms, self.args, state, self.path)
        self.assertEqual(prices.call_count, 2)
        self.assertEqual(project_costs.call_count, 2)

    def test_unpriced_vms_are_counted_instead_of_hidden(self):
        def partial(vms, project, cap, *, totals=None, **kwargs):
            totals[vms[0].name] = Decimal('5')
            return []
        with patch('lib.spot_runner_autoscaler.check_running_prices', side_effect=partial), \
             patch('lib.spot_runner_autoscaler.log_project_costs'), \
             self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
            controller.log_hourly_spend(self.vms, self.args, {}, self.path)
        self.assertIn('2 Spot VMs = $5.00/day ($0.21/hour) for compute and storage at current prices; '
                      '1 VM price unavailable', '\n'.join(logs.output))

    def test_price_guard_reports_each_priced_vm_total(self):
        from lib import price_guard
        vm = GceVm('oxidex-runners-asiaeast2-a-1', 'asia-east2-a', 'RUNNING', 'n4-standard-32', True,
                   disks=({'diskType': 'hyperdisk-balanced', 'diskSizeGb': 40, 'provisionedIops': 3000,
                           'provisionedThroughput': 140},))
        totals = {}
        with patch.object(price_guard, 'spot_rates', return_value=(Decimal('.004'), Decimal('.0005'))), \
             patch.object(price_guard, 'storage_rates', return_value={'hyperdisk-balanced:capacity': Decimal('.073')}), \
             patch.object(price_guard, 'clear_catalog_cache'), \
             patch.object(price_guard.subprocess, 'run', return_value=type('R', (), {'stdout': '{"guestCpus": 32, "memoryMb": 131072}'})()):
            price_guard.check_running_prices([vm], 'project', Decimal('10'), totals=totals)
        expected = (Decimal('.004') * 32 + Decimal('.0005') * 128) * 24 + Decimal('.073') * 40 * 24 / 730
        self.assertEqual(totals, {vm.name: expected})


if __name__ == '__main__':
    unittest.main()
