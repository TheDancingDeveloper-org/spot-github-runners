"""Prove disk ownership, cleanup retries and replacement persistence offline."""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib.runner_fleet import GceVm
from lib.spot_runner_autoscaler import args_parser, read_scale_state, write_scale_state
from lib import vm_recovery

SOURCE = 'https://www.googleapis.com/compute/v1/projects/project/zones/asia-south2-a/disks/root'


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        external = patch('subprocess.run', side_effect=AssertionError('Unexpected external command in offline test'))
        external.start()
        self.addCleanup(external.stop)
        self.vm = GceVm('oxidex-runners-test', 'asia-south2-a', 'TERMINATED', 'n2-standard-32', True,
                        vcpu=32, ram_gib=128, instance_id='123',
                        disks=({'source': SOURCE, 'boot': True, 'autoDelete': True, 'diskSizeGb': '40', 'diskId': '456'},))
        self.args = args_parser().parse_args(['--project', 'project', '--once'])

    def run_cleanup(self, instances, disks):
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if 'instances' in args and 'list' in args:
                return SimpleNamespace(stdout=json.dumps([] if any('delete' in call and 'instances' in call for call in calls) else instances), returncode=0)
            if 'disks' in args and 'list' in args:
                return SimpleNamespace(stdout=json.dumps(disks.pop(0)), returncode=0)
            return SimpleNamespace(stdout='', returncode=0)
        with patch('lib.vm_recovery.subprocess.run', side_effect=command):
            result = vm_recovery.cleanup_lost_vm(self.vm, 'project')
        return result, calls

    def test_terminated_vm_and_surviving_detached_drive_are_deleted_and_verified(self):
        result, calls = self.run_cleanup(
            [{'name': self.vm.name, 'id': '123', 'status': 'TERMINATED', 'disks': list(self.vm.disks)}],
            [[{'name': 'root', 'selfLink': SOURCE, 'id': '456', 'status': 'READY', 'users': []}], [{'name': 'root', 'selfLink': SOURCE, 'id': '456', 'status': 'READY', 'users': []}], []])
        self.assertTrue(result)
        self.assertIn('--delete-disks=all', next(call for call in calls if 'instances' in call and 'delete' in call))
        self.assertIn('root', next(call for call in calls if 'disks' in call and 'delete' in call))
        self.assertEqual(sum('disks' in call and 'list' in call for call in calls), 3)

    def test_missing_vm_attached_disk_is_retained_and_cleanup_remains_pending(self):
        result, calls = self.run_cleanup([], [[{'name': 'root', 'selfLink': SOURCE, 'id': '456', 'status': 'READY', 'users': ['another-vm']}],
                                              [{'name': 'root', 'selfLink': SOURCE, 'id': '456', 'status': 'READY', 'users': ['another-vm']}]])
        self.assertFalse(result)
        self.assertFalse(any('delete' in call for call in calls))

    def test_recreated_disk_or_running_vm_is_never_deleted(self):
        result, calls = self.run_cleanup([{'name': self.vm.name, 'id': '999', 'status': 'RUNNING'}], [])
        self.assertFalse(result)
        self.assertFalse(any('delete' in call for call in calls))
        result, calls = self.run_cleanup([], [[{'name': 'root', 'selfLink': SOURCE, 'id': '999', 'status': 'READY', 'users': []}],
                                              [{'name': 'root', 'selfLink': SOURCE, 'id': '999', 'status': 'READY', 'users': []}]])
        self.assertFalse(result)
        self.assertFalse(any('delete' in call for call in calls))

    def test_foreign_project_or_retained_disk_never_deleted(self):
        for disk in ({'source': SOURCE.replace('/project/', '/other/'), 'autoDelete': True},
                     {'source': SOURCE, 'autoDelete': False}):
            vm = GceVm(self.vm.name, self.vm.zone, 'TERMINATED', self.vm.machine_type, True, disks=(disk,))
            with patch('lib.vm_recovery.subprocess.run') as command:
                self.assertFalse(vm_recovery.cleanup_lost_vm(vm, 'project'))
                command.assert_not_called()

    def test_changed_attachments_and_unrecorded_disk_identity_are_retained(self):
        current = {'name': self.vm.name, 'id': '123', 'status': 'TERMINATED',
                   'disks': [{'source': SOURCE + '-other', 'autoDelete': False}]}
        result, calls = self.run_cleanup([current], [])
        self.assertFalse(result)
        self.assertFalse(any('delete' in call for call in calls))
        vm = GceVm(self.vm.name, self.vm.zone, 'TERMINATED', self.vm.machine_type, True,
                   disks=({'source': SOURCE, 'autoDelete': True},))
        with patch('lib.vm_recovery.subprocess.run', side_effect=[
                SimpleNamespace(stdout='[]'),
                SimpleNamespace(stdout=json.dumps([{'name': 'root', 'selfLink': SOURCE, 'id': '456', 'users': [], 'status': 'READY'}])),
                SimpleNamespace(stdout=json.dumps([{'name': 'root', 'selfLink': SOURCE}]))]) as command:
            self.assertFalse(vm_recovery.cleanup_lost_vm(vm, 'project'))
        self.assertFalse(any('delete' in call.args[0] for call in command.call_args_list))

    def test_recreated_attached_disk_is_rejected_before_vm_deletion(self):
        current = {'name': self.vm.name, 'id': '123', 'status': 'TERMINATED', 'disks': list(self.vm.disks)}
        result, calls = self.run_cleanup([current], [[{'name': 'root', 'selfLink': SOURCE, 'id': '999', 'status': 'READY', 'users': []}]])
        self.assertFalse(result)
        self.assertFalse(any('delete' in call for call in calls))

    def test_regional_disk_cleanup_uses_region_and_verifies_absence(self):
        source = SOURCE.replace('/zones/asia-south2-a/', '/regions/asia-south2/')
        self.vm = GceVm(self.vm.name, self.vm.zone, 'TERMINATED', self.vm.machine_type, True,
                        disks=({'source': source, 'autoDelete': True, 'diskId': '456'},))
        result, calls = self.run_cleanup([], [[{'name': 'root', 'selfLink': source, 'id': '456', 'users': [], 'status': 'READY'}], []])
        self.assertTrue(result)
        deletion = next(call for call in calls if 'delete' in call)
        self.assertIn('--region=asia-south2', deletion)
        self.assertNotIn('--zone=asia-south2-a', deletion)

    def test_existing_price_target_and_loss_share_one_capacity_deficit(self):
        from lib.spot_runner_autoscaler import reconcile_host_operation
        vm = GceVm('oxidex-runners-live', self.vm.zone, 'RUNNING', self.vm.machine_type, True)
        state = {'price_replacement_target': 3,
                 'pending_vm_recovery': {f'{self.vm.zone}/{self.vm.name}': asdict(self.vm)}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch('lib.vm_recovery.cleanup_lost_vm', return_value=True), \
                    patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[]), \
                    patch('lib.spot_runner_autoscaler.discover_offers', return_value=['offer']), \
                    patch('lib.spot_runner_autoscaler.scale_up', return_value=1) as launch:
                vm_recovery.reconcile_lost_vms([vm], [], self.args, state, path)
                launch.assert_not_called()
                reconcile_host_operation([vm], [], self.args, state, path)
                vm_recovery.reconcile_lost_vms([vm], [], self.args, state, path)
                self.assertEqual([call.args[0] for call in launch.call_args_list], [1])
                self.assertEqual(read_scale_state(path)['price_replacement_target'], 3)
                self.assertTrue(state['pending_vm_recovery'][f'{self.vm.zone}/{self.vm.name}']['_cleanup_complete'])

    def test_new_loss_does_not_recount_loss_already_covered_by_live_replacement(self):
        from lib.spot_runner_autoscaler import reconcile_host_operation
        old = {**asdict(self.vm), '_target_recorded': True, '_cleanup_complete': True}
        newly_lost = {**asdict(self.vm), 'name': 'oxidex-runners-second-loss'}
        live = [GceVm(name, self.vm.zone, 'RUNNING', self.vm.machine_type, True)
                for name in ('oxidex-runners-live', 'oxidex-runners-replacement')]
        state = {'price_replacement_target': 3, 'pending_vm_recovery': {'old': old, 'new': newly_lost}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch('lib.vm_recovery.cleanup_lost_vm', return_value=True), \
                    patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[]), \
                    patch('lib.spot_runner_autoscaler.discover_offers', return_value=['offer']), \
                    patch('lib.spot_runner_autoscaler.scale_up', return_value=1) as launch:
                vm_recovery.reconcile_lost_vms(live, [], self.args, state, path)
                reconcile_host_operation(live, [], self.args, state, path)
                vm_recovery.reconcile_lost_vms(live, [], self.args, state, path)
                self.assertEqual([call.args[0] for call in launch.call_args_list], [1])
                self.assertEqual(state['price_replacement_target'], 3)

    def test_new_instance_identity_never_reuses_previous_cleanup_authorization(self):
        from dataclasses import replace
        from lib.fleet_logging import observe_vms
        key = f'{self.vm.zone}/{self.vm.name}'
        old = {**asdict(self.vm), '_target_recorded': True, '_cleanup_complete': True}
        state = {'idle_loops': 0, 'scale_up_active': False, 'price_replacement_target': 1,
                 'pending_vm_recovery': {key: old}, 'observed_vms': {key: asdict(self.vm)}}
        recreated = replace(self.vm, instance_id='999', disks=({'source': SOURCE, 'diskId': '999', 'autoDelete': True},))
        observe_vms([recreated], state)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch('lib.vm_recovery.cleanup_lost_vm', return_value=False) as cleanup, \
                    patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[]), \
                    patch('lib.spot_runner_autoscaler.scale_up') as launch:
                vm_recovery.reconcile_lost_vms([recreated], [], self.args, state, path)
            cleanup.assert_called_once()
            self.assertEqual(cleanup.call_args.args[0].instance_id, '999')
            launch.assert_not_called()

    def test_restart_with_partial_replacement_retains_barrier_and_restoration_target(self):
        from lib.spot_runner_autoscaler import begin_host_operation, reconcile_host_operation
        key = f'{self.vm.zone}/{self.vm.name}'
        state = {'price_replacement_target': 1, 'pending_vm_recovery':
                 {key: {**asdict(self.vm), '_target_recorded': True, '_cleanup_complete': True}}}
        replacement = GceVm('oxidex-runners-new', self.vm.zone, 'RUNNING', self.vm.machine_type, True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            begin_host_operation('launch', replacement, [], [], state, path)
            state = read_scale_state(path)
            reconcile_host_operation([replacement], [], self.args, state, path)
            with patch('lib.spot_runner_autoscaler.scale_up') as launch:
                vm_recovery.reconcile_lost_vms([replacement], [], self.args, state, path)
                launch.assert_not_called()
            self.assertIn(key, state['pending_vm_recovery'])
            self.assertEqual(state['price_replacement_target'], 1)

    def test_pending_replacement_survives_restart_until_fresh_capacity_restored(self):
        from lib.spot_runner_autoscaler import reconcile_host_operation
        key = f'{self.vm.zone}/{self.vm.name}'
        state = {'pending_vm_recovery': {key: asdict(self.vm)}}
        replacement = GceVm('oxidex-runners-new', self.vm.zone, 'RUNNING', self.vm.machine_type, True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch('lib.vm_recovery.cleanup_lost_vm', return_value=True):
                vm_recovery.reconcile_lost_vms([], [], self.args, state, path)
            state = read_scale_state(path)
            reconcile_host_operation([], [], self.args, state, path)
            with patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[]), \
                    patch('lib.spot_runner_autoscaler.discover_offers', return_value=['offer']), \
                    patch('lib.spot_runner_autoscaler.scale_up', return_value=0):
                vm_recovery.reconcile_lost_vms([], [], self.args, state, path)
            state = read_scale_state(path)
            self.assertIn(key, state['pending_vm_recovery'])
            with patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[]), \
                    patch('lib.spot_runner_autoscaler.discover_offers', return_value=['offer']), \
                    patch('lib.spot_runner_autoscaler.scale_up', return_value=1) as launch:
                vm_recovery.reconcile_lost_vms([], [], self.args, state, path)
                self.assertEqual(launch.call_args.args[0], 1)
                self.assertIn(key, state['pending_vm_recovery'])
                vm_recovery.reconcile_lost_vms([replacement], [], self.args, state, path)
            self.assertEqual(read_scale_state(path)['pending_vm_recovery'], {})
            self.assertFalse(vm_recovery.reconcile_lost_vms([replacement], [], self.args, state, path))

    def test_two_lost_hosts_cleanup_only_one_and_block_second_until_deregistered(self):
        from lib.spot_runner_autoscaler import reconcile_host_operation
        second = {**asdict(self.vm), 'name': 'oxidex-runners-second', 'instance_id': '999'}
        state = {'pending_vm_recovery': {'first': asdict(self.vm), 'second': second}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with patch('lib.vm_recovery.cleanup_lost_vm', return_value=True) as cleanup:
                vm_recovery.reconcile_lost_vms([], [], self.args, state, path)
                vm_recovery.reconcile_lost_vms([], [], self.args, state, path)
                cleanup.assert_called_once()
                reconcile_host_operation([], [], self.args, state, path)
                vm_recovery.reconcile_lost_vms([], [], self.args, state, path)
                self.assertEqual(cleanup.call_count, 2)
            self.assertEqual(state['price_replacement_target'], 2)


if __name__ == '__main__':
    unittest.main()
