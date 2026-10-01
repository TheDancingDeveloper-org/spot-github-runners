"""Test provider inventory and the controller's recovery integration."""
import json
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib.runner_fleet import GceVm, GitHubRunner, list_gcloud_vms
from lib.spot_runner_autoscaler import args_parser, read_scale_state, run_controller, scale_down, write_scale_state


class FleetInventoryTests(unittest.TestCase):
    def setUp(self):
        external = patch('subprocess.run', side_effect=AssertionError('Unexpected external command in offline test'))
        external.start()
        self.addCleanup(external.stop)

    def test_inventory_retains_actual_shapes_disk_types_ids_and_gpu(self):
        source = 'https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/disks/root'
        vm = {'name': 'oxidex-runners-test', 'zone': 'zones/us-central1-a', 'status': 'RUNNING',
              'id': '42', 'machineType': 'machineTypes/custom-8-16384',
              'scheduling': {'provisioningModel': 'SPOT'},
              'disks': [{'source': source, 'diskSizeGb': '20', 'boot': True, 'autoDelete': True}],
              'guestAccelerators': [{'acceleratorType': 'nvidia-tesla-t4', 'acceleratorCount': 1}]}
        payloads = [SimpleNamespace(stdout=json.dumps([vm, {**vm, 'name': 'oxidex-runners-test2'}])),
                    SimpleNamespace(stdout=json.dumps([{'selfLink': source, 'id': '7', 'type': 'diskTypes/pd-ssd'}])),
                    SimpleNamespace(stdout=json.dumps({'guestCpus': 8, 'memoryMb': 16384}))]
        with patch('lib.runner_fleet.subprocess.run', side_effect=payloads) as command:
            vms = list_gcloud_vms('p', name_prefix='oxidex-runners-')
        self.assertEqual(command.call_count, 3)  # Shared shape queried once per snapshot.
        self.assertEqual((vms[0].vcpu, vms[0].ram_gib), (8, 16))
        self.assertEqual(vms[0].disks[0]['diskType'], 'pd-ssd')
        self.assertEqual(vms[0].disks[0]['diskId'], '7')
        self.assertEqual(vms[0].instance_id, '42')
        self.assertEqual(vms[0].accelerators[0]['acceleratorCount'], 1)

    def test_missing_metadata_keeps_instance_visible_with_unknown_specs(self):
        vm = {'name': 'oxidex-runners-test', 'zone': 'zones/us-central1-a', 'status': 'RUNNING',
              'machineType': 'machineTypes/n2-standard-32', 'scheduling': {'provisioningModel': 'SPOT'}}
        error = subprocess.CalledProcessError(1, ['gcloud'], stderr='permission denied\nretry later')
        with patch('lib.runner_fleet.subprocess.run', side_effect=[SimpleNamespace(stdout=json.dumps([vm])), error, error]), \
                self.assertLogs('spot_runner_autoscaler', 'WARNING'):
            vms = list_gcloud_vms('p')
        self.assertEqual(len(vms), 1)
        self.assertIsNone(vms[0].vcpu)
        self.assertIsNone(vms[0].ram_gib)

    def test_controller_cleans_one_missing_vm_before_replacement(self):
        vm = GceVm('oxidex-runners-test', 'us-central1-a', 'RUNNING', 'n2-standard-32', True)
        state = {'observed_vms': {f'{vm.zone}/{vm.name}': asdict(vm)}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            write_scale_state(path, state)
            args = args_parser().parse_args(['--once', '--state-file', str(path)])
            with patch('lib.spot_runner_autoscaler.JobRecovery.poll'), \
                    patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[]), \
                    patch('lib.spot_runner_autoscaler.list_github_runners', return_value=[]), \
                    patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[]) as prices, \
                    patch('lib.vm_recovery.cleanup_lost_vm', return_value=True), \
                    patch('lib.spot_runner_autoscaler.discover_offers', return_value=['offer']), \
                    patch('lib.spot_runner_autoscaler.scale_up', return_value=1) as launch:
                self.assertEqual(run_controller(args), 0)
            self.assertIn(f'{vm.zone}/{vm.name}', read_scale_state(path)['pending_vm_recovery'])
            self.assertEqual(read_scale_state(path)['host_operation']['kind'], 'recovery')
            prices.assert_not_called()
            launch.assert_not_called()

    def test_job_and_fleet_observers_share_intent_until_disappearance(self):
        from lib.job_recovery import JobRecovery
        from lib.fleet_logging import observe_vms
        vm = GceVm('oxidex-runners-test', 'us-central1-a', 'TERMINATED', 'n2-standard-32', True, instance_id='123')
        key = f'{vm.zone}/{vm.name}'
        state = {'intentional_deletions': [key], 'intentional_deletion_ids': {key: '123'},
                 'observed_vms': {key: asdict(vm)}}
        jobs = JobRecovery(state, lambda: None, [])
        jobs.poll([vm])
        observe_vms([vm], state)
        self.assertEqual(state['pending_vm_recovery'], {})
        self.assertIn(key, state['intentional_deletions'])
        jobs.poll([])
        observe_vms([], state)
        self.assertEqual(state['pending_vm_recovery'], {})
        self.assertEqual(state['intentional_deletions'], [])
        self.assertEqual(state['job_tracking']['losses'], {})

    def test_deletion_intent_is_persisted_before_provider_request(self):
        from lib.spot_runner_autoscaler import reconcile_prices
        vm = GceVm('oxidex-runners-test', 'us-central1-a', 'RUNNING', 'n2-standard-32', True, instance_id='123')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            args = args_parser().parse_args(['--state-file', str(path)])
            state = {'idle_loops': 0, 'scale_up_active': False}
            def uncertain(vm, project):
                recovered = read_scale_state(path)
                self.assertIn(f'{vm.zone}/{vm.name}', recovered['intentional_deletions'])
                self.assertEqual(recovered['intentional_deletion_ids'][f'{vm.zone}/{vm.name}'], '123')
                raise subprocess.TimeoutExpired('gcloud', 180)
            with patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[vm]), \
                    patch('lib.spot_runner_autoscaler.delete_overpriced_vm', side_effect=uncertain), \
                    patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[vm]):
                reconcile_prices([vm], [], args, state, path)

    def test_rejected_deletion_does_not_hide_later_spot_loss(self):
        from dataclasses import replace
        from lib.fleet_logging import observe_vms
        from lib.job_recovery import JobRecovery
        from lib.spot_runner_autoscaler import reconcile_prices
        vm = GceVm('oxidex-runners-test', 'us-central1-a', 'RUNNING', 'n2-standard-32', True, instance_id='123')
        other = replace(vm, name='oxidex-runners-other', instance_id='456')
        runner = GitHubRunner(1, f'gha-{vm.name}-1', 'online', False, ('spot',))
        key = f'{vm.zone}/{vm.name}'
        for operation in ('scale_down', 'price_eviction'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'state.json'
                args = args_parser().parse_args(['--state-file', str(path)])
                state = {}
                jobs = JobRecovery(state, lambda: write_scale_state(path, state), [])
                jobs.poll([vm, other])
                observe_vms([vm, other], state)
                rejected = subprocess.CalledProcessError(1, ['gcloud', 'compute', 'instances', 'delete'], stderr='PERMISSION_DENIED: required compute.instances.delete permission')
                with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=rejected), \
                        patch('lib.spot_runner_autoscaler.removable_spot_vms', return_value=[vm]), \
                        patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[vm]), \
                        patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[vm, other]):
                    if operation == 'scale_down':
                        self.assertEqual(scale_down([vm, other], [runner], args, state, path), 0)
                    else:
                        reconcile_prices([vm, other], [], args, state, path)
                state = read_scale_state(path)
                self.assertNotIn(key, state['intentional_deletions'])
                jobs = JobRecovery(state, lambda: None, [])
                jobs.poll([vm, other])
                observe_vms([vm, other], state)
                lost = replace(vm, status='TERMINATED')
                jobs.poll([lost, other])
                observe_vms([lost, other], state)
                self.assertIn(key, state['job_tracking']['losses'])
                self.assertIn(key, state['pending_vm_recovery'])

    def test_scale_down_persists_drain_without_deletion_intent_if_inventory_fails(self):
        vm = GceVm('oxidex-runners-test', 'us-central1-a', 'RUNNING', 'n2-standard-32', True)
        other = GceVm('oxidex-runners-other', vm.zone, 'RUNNING', vm.machine_type, True)
        runner = GitHubRunner(1, f'gha-{vm.name}-1', 'online', False, ('spot',))
        with tempfile.TemporaryDirectory() as directory:
            args = args_parser().parse_args(['--state-file', str(Path(directory) / 'state.json')])
            state = {'idle_loops': 0, 'scale_up_active': False}
            with patch('lib.spot_runner_autoscaler.removable_spot_vms', return_value=[vm]), \
                    patch('lib.spot_runner_autoscaler.list_github_runners', side_effect=RuntimeError('GitHub unavailable')):
                scale_down([vm, other], [runner], args, state)
            persisted = read_scale_state(args.state_file)
            self.assertEqual(persisted['host_operation']['kind'], 'drain')
            self.assertEqual(persisted['host_operation']['runner_ids'], [1])
            self.assertFalse(persisted['intentional_deletions'])


if __name__ == '__main__':
    unittest.main()
