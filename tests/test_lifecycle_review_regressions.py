import tempfile
import unittest
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib import spot_runner_autoscaler as controller
from lib.fleet_migration import begin_migration, reconcile_migration
from lib.runner_fleet import GceVm
from lib.tier_controller import check_tiers


class LifecycleReviewRegressions(unittest.TestCase):
    def host(self, identity='1'):
        return GceVm('oxidex-runners-old', 'z', 'RUNNING', 'n2-standard-32',
                     True, 32, 128, instance_id=identity,
                     disks=({'source': 'disk-old', 'diskId': '10'},))

    def test_loss_recovery_restores_twenty_slot_bulky_receipt(self):
        lost = self.host()
        state = {'desired_topology': ['bulky'],
                 'pending_vm_recovery': {'lost': {**asdict(lost), '_cleanup_complete': True}},
                 'host_deployments': {'z/1': {'tier': 'bulky', 'expected_runner_count': 20}}}
        args = controller.args_parser().parse_args(['--scaling-mode', 'tiered'])
        with tempfile.TemporaryDirectory() as folder, \
             patch('lib.tier_controller.evidence_for', return_value=(None, {'complete': True}, 1000)), \
             patch.object(controller, 'check_running_prices'), \
             patch.object(controller, 'discover_tier_offers', return_value=[SimpleNamespace(total_daily_usd=Decimal('4'))]), \
             patch.object(controller, 'scale_up'):
            check_tiers([], [], args, state, Path(folder)/'state.json')
        self.assertEqual(args.launch_slots, 20)

    def test_overpriced_starting_fleet_has_bounded_cheaper_overlap(self):
        old = self.host()
        args = controller.args_parser().parse_args(['--scaling-mode', 'tiered'])
        state = {}
        def prices(vms, project, cap, totals):
            totals[old.name] = Decimal('16')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            begin_migration([old], ('starting',), state, path, 'oversized migration')
            with patch.object(controller, 'check_running_prices', side_effect=prices), \
                 patch.object(controller, 'discover_tier_offers', return_value=[SimpleNamespace(total_daily_usd=Decimal('1.10'))]), \
                 patch.object(controller, 'scale_up') as launch:
                reconcile_migration([old], [], args, state, path)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(Decimal(state['migration']['spend_ceiling']), Decimal('17.25'))

    def test_existing_unsatisfiable_overlap_ceiling_can_resume(self):
        old = self.host()
        args = controller.args_parser().parse_args(['--scaling-mode', 'tiered'])
        state = {}
        def prices(vms, project, cap, totals):
            totals[old.name] = Decimal('16')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            begin_migration([old], ('starting',), state, path, 'oversized migration')
            state['migration']['spend_ceiling'] = '16'
            with patch.object(controller, 'check_running_prices', side_effect=prices), \
                 patch.object(controller, 'discover_tier_offers', return_value=[SimpleNamespace(total_daily_usd=Decimal('1.10'))]), \
                 patch.object(controller, 'scale_up') as launch:
                reconcile_migration([old], [], args, state, path)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(Decimal(state['migration']['spend_ceiling']), Decimal('17.25'))

    def test_recovery_keeps_distinct_densities_for_same_tier(self):
        state = {}
        with tempfile.TemporaryDirectory() as folder:
            begin_migration([], ('bulky','bulky'), state, Path(folder)/'state.json',
                            'loss recovery', slot_counts=[('bulky',20),('bulky',16)])
        self.assertEqual(state['migration']['pending_slots'], [20,16])

    def test_overpriced_overlap_rejects_a_more_expensive_replacement(self):
        first = self.host('1')
        second = replace(first, name='oxidex-runners-second', instance_id='2')
        old = replace(first, name='oxidex-runners-small', instance_id='3', vcpu=4, ram_gib=16)
        args = controller.args_parser().parse_args(['--scaling-mode','tiered'])
        state = {}
        def prices(vms, project, cap, totals):
            totals.update({first.name:Decimal('7.5'), second.name:Decimal('7.5'), old.name:Decimal('1')})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            begin_migration([first,second,old], ('bulky','bulky','expanded'), state, path, 'replacement')
            with patch.object(controller, 'check_running_prices', side_effect=prices), \
                 patch.object(controller, 'discover_tier_offers', return_value=[SimpleNamespace(total_daily_usd=Decimal('1.10'))]), \
                 patch.object(controller, 'scale_up') as launch:
                reconcile_migration([first,second,old], [], args, state, path)
            launch.assert_not_called()
            self.assertEqual(Decimal(state['migration']['overlap_allowance_daily_usd']), Decimal('1'))

    def test_drained_delete_retry_uses_recorded_disk_authorization(self):
        recorded = self.host()
        current = replace(recorded, disks=({'source': 'disk-new', 'diskId': '99'},))
        state = {'host_operation': {'kind': 'delete', 'vm': asdict(recorded),
                                   'drained': True, 'runner_ids': []}}
        args = controller.args_parser().parse_args(['--scaling-mode', 'tiered'])
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(controller, 'list_github_runners', return_value=[]), \
             patch.object(controller, 'delete_drained_vm') as delete:
            controller.reconcile_host_operation([current], [], args, state, Path(folder)/'state.json')
        self.assertEqual(delete.call_args.args[0].disks, recorded.disks)
