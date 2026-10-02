"""Offline proofs of the persisted, one-host fleet operation barrier."""
import subprocess
import json
from dataclasses import asdict
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib.runner_fleet import GceVm, GitHubRunner
from lib import spot_runner_autoscaler as controller


class SequentialScalingTests(unittest.TestCase):
    def setUp(self):
        external = patch('subprocess.run', side_effect=AssertionError('Unexpected external command in offline test'))
        external.start()
        self.addCleanup(external.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.json'
        self.args = controller.args_parser().parse_args(['--state-file', str(self.path)])
        self.vm = GceVm('oxidex-runners-test', 'asia-south2-a', 'RUNNING', 'n2-standard-32', True, instance_id='123')
        self.other = replace(self.vm, name='oxidex-runners-other', instance_id='456')
        self.runners = [GitHubRunner(i, f'gha-{self.vm.name}-{i}', 'online', False, ('spot', 'X64', 'n2-standard-32')) for i in range(1, 9)]

    def test_manager_receipt_preserves_success_and_failure_launch_identity(self):
        offer = SimpleNamespace(region='asia-south2', machine_type='n2-standard-32', vcpu=32, ram_gib=128, hourly_usd=.1)
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                state = {}
                def command(args, **kwargs):
                    if args[:3] == ['gcloud', 'compute', 'zones']:
                        return SimpleNamespace(returncode=0, stdout='asia-south2-a\n')
                    path = Path(args[args.index('--deployment-receipt') + 1])
                    actual = replace(self.vm, name=state['host_operation']['vm']['name'],
                                     disks=({'source': 'disks/root', 'autoDelete': True},))
                    path.write_text(json.dumps(asdict(actual)))
                    return SimpleNamespace(returncode=exit_code, stdout='', stderr='')
                with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=command), \
                     patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[]):
                    controller.scale_up(1, [offer], self.args, state, self.path)
                restarted = controller.read_scale_state(self.path)
                controller.reconcile_host_operation([], [], self.args, restarted, self.path)
                self.assertEqual(restarted['host_operation']['kind'], 'recovery')
                self.assertEqual(restarted['host_operation']['vm']['instance_id'], '123')

    def test_restart_loads_receipt_before_absent_launch_reconciliation(self):
        state = {}
        receipt = self.path.parent / 'receipt.json'
        controller.begin_host_operation('launch', replace(self.vm, instance_id=''), [], [], state, self.path,
                                        deployment_receipt=str(receipt))
        receipt.write_text(json.dumps(asdict(self.vm)))
        restarted = controller.read_scale_state(self.path)
        controller.reconcile_host_operation([], [], self.args, restarted, self.path)
        self.assertEqual(restarted['host_operation']['kind'], 'recovery')
        self.assertEqual(restarted['host_operation']['vm']['instance_id'], '123')

    def test_receipt_for_another_host_never_authorizes_recovery(self):
        state = {}
        receipt = self.path.parent / 'wrong-receipt.json'
        controller.begin_host_operation('launch', replace(self.vm, instance_id=''), [], [], state, self.path,
                                        deployment_receipt=str(receipt))
        receipt.write_text(json.dumps(asdict(self.other)))
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            controller.reconcile_host_operation([], [], self.args, state, self.path)
        self.assertEqual(state['host_operation']['kind'], 'launch')
        self.assertEqual(state['host_operation']['vm']['instance_id'], '')
        self.assertFalse(state.get('pending_vm_recovery'))

    def test_high_demand_adds_one_and_sticky_old_state_does_not_override_idle_load(self):
        self.assertEqual(controller.scaling_decision(24, 14, 3, 0, False, 25), ('up', 1, 0, False))
        self.assertEqual(controller.scaling_decision(24, 2, 3, 0, True, 25), ('wait', 0, 1, False))
        self.assertEqual(controller.scaling_decision(48, 10, 6, 1, True, 25), ('down', 1, 0, False))

    def test_partial_registration_and_restart_block_additional_host_actions(self):
        state = {}
        controller.begin_host_operation('launch', self.vm, self.runners, [self.vm], state, self.path)
        restarted = controller.read_scale_state(self.path)
        with patch('lib.spot_runner_autoscaler.subprocess.run') as command:
            self.assertTrue(controller.reconcile_host_operation([self.vm], self.runners[:7], self.args, restarted, self.path))
            self.assertEqual(controller.scale_up(5, ['offer'], self.args, restarted, self.path), 0)
            self.assertEqual(controller.scale_down([self.vm, self.other], self.runners, self.args, restarted, self.path), 0)
            command.assert_not_called()
        self.assertIn('host_operation', restarted)
        self.assertTrue(controller.reconcile_host_operation([self.vm], self.runners, self.args, restarted, self.path))
        self.assertNotIn('host_operation', restarted)

    def test_failed_deregistration_blocks_second_delete_and_retries_only_stale_idle_ids(self):
        state = {}
        controller.begin_host_operation('delete', self.vm, self.runners, [self.vm], state, self.path)
        restarted = controller.read_scale_state(self.path)
        offline = [replace(runner, status='offline') for runner in self.runners]
        with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['gh'])):
            self.assertTrue(controller.reconcile_host_operation([self.other], offline, self.args, restarted, self.path))
        with patch('lib.spot_runner_autoscaler.terminate_spot_vm') as delete:
            self.assertEqual(controller.scale_down([self.other], [], self.args, restarted, self.path), 0)
            delete.assert_not_called()
        with patch('lib.spot_runner_autoscaler.subprocess.run') as command:
            self.assertTrue(controller.reconcile_host_operation([self.other], [replace(offline[0], busy=True)], self.args, restarted, self.path))
            command.assert_not_called()
        with patch('lib.spot_runner_autoscaler.subprocess.run') as command:
            controller.reconcile_host_operation([self.other], offline, self.args, restarted, self.path)
            self.assertEqual(command.call_count, 8)
        self.assertIn('host_operation', restarted)  # DELETE receipts still need an absent-record observation.
        controller.reconcile_host_operation([self.other], [], self.args, restarted, self.path)
        self.assertNotIn('host_operation', restarted)

    def test_failed_launch_with_surviving_vm_is_deleted_before_another_zone(self):
        offer = SimpleNamespace(region='asia-south2', machine_type='n2-standard-32', vcpu=32, ram_gib=128, hourly_usd=.1)
        state = {}
        commands = []
        def command(args, **kwargs):
            commands.append(args[:4])
            if args[:3] == ['gcloud', 'compute', 'zones']:
                return SimpleNamespace(returncode=0, stdout='asia-south2-a\nasia-south2-b\n')
            if args[:4] == ['gcloud', 'compute', 'instances', 'delete']:
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            persisted = controller.read_scale_state(self.path)
            self.assertEqual(persisted['host_operation']['kind'], 'launch')
            return SimpleNamespace(returncode=1, stdout='', stderr='runner registration failed')
        def inventory(*args, **kwargs):
            operation = state['host_operation']
            return [replace(self.vm, name=operation['vm']['name'])]  # Deletion is not yet confirmed.
        with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=command), \
                patch('lib.spot_runner_autoscaler.list_gcloud_vms', side_effect=inventory):
            self.assertEqual(controller.scale_up(3, [offer, offer], self.args, state, self.path), 0)
        self.assertEqual(commands[-1], ['gcloud', 'compute', 'instances', 'delete'])
        self.assertEqual(len(commands), 3)  # Zones, one manager run, then deletion; no second zone yet.
        self.assertIn('host_operation', state)
        self.assertIn(f"asia-south2-a/{state['host_operation']['vm']['name']}", state['intentional_deletions'])

    def test_price_replacement_deficit_restores_only_one_and_keeps_target(self):
        state = {'price_replacement_target': 3}
        with patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[]), \
                patch('lib.spot_runner_autoscaler.discover_offers', return_value=['offer']), \
                patch('lib.spot_runner_autoscaler.scale_up', return_value=1) as launch:
            self.assertTrue(controller.reconcile_prices([self.vm], [], self.args, state, self.path))
        self.assertEqual(launch.call_args.args[0], 1)
        self.assertEqual(state['price_replacement_target'], 3)

    def test_multiple_overpriced_hosts_delete_only_one_before_any_launch(self):
        state = {}
        with patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[self.vm, self.other]), \
                patch('lib.spot_runner_autoscaler.delete_overpriced_vm') as delete, \
                patch('lib.spot_runner_autoscaler.scale_up') as launch:
            controller.reconcile_prices([self.vm, self.other], self.runners, self.args, state, self.path)
        delete.assert_called_once_with(self.vm, self.args.project)
        launch.assert_not_called()
        self.assertEqual(state['price_replacement_target'], 2)

    def test_registration_captures_instance_identity_and_requires_exact_names(self):
        state = {}
        controller.begin_host_operation('launch', replace(self.vm, instance_id=''), [], [], state, self.path)
        bogus = [replace(runner, name=runner.name.replace('gha-', 'unrelated-')) for runner in self.runners]
        controller.reconcile_host_operation([self.vm], bogus, self.args, state, self.path)
        self.assertEqual(controller.read_scale_state(self.path)['host_operation']['vm']['instance_id'], '123')
        controller.reconcile_host_operation([replace(self.vm, instance_id='999')], self.runners, self.args, state, self.path)
        self.assertIn('host_operation', state)

    def test_uncertain_launch_absence_never_permits_further_launch_after_restart(self):
        state = {}
        controller.begin_host_operation('launch', replace(self.vm, instance_id=''), [], [], state, self.path)
        restarted = controller.read_scale_state(self.path)
        controller.reconcile_host_operation([], [], self.args, restarted, self.path)
        with patch('lib.spot_runner_autoscaler.launch_offer') as launch:
            self.assertEqual(controller.scale_up(1, ['offer'], self.args, restarted, self.path), 0)
            launch.assert_not_called()

    def test_failed_launch_with_attached_leftover_disk_blocks_failover(self):
        offer = SimpleNamespace(region='asia-south2', machine_type='n2-standard-32', vcpu=32, ram_gib=128, hourly_usd=.1)
        state = {}
        def command(args, **kwargs):
            if args[:3] == ['gcloud', 'compute', 'zones']:
                return SimpleNamespace(returncode=0, stdout='asia-south2-a\nasia-south2-b\n')
            if args[:3] == ['gcloud', 'compute', 'disks']:
                import json
                name = state['host_operation']['vm']['name'].removeprefix('oxidex-runners-')
                return SimpleNamespace(returncode=0, stdout=json.dumps([{'name': f'runner-data-{name}', 'users': ['vm']}]))
            return SimpleNamespace(returncode=1, stdout='', stderr='capacity unavailable')
        with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=command) as external, \
                patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[]):
            self.assertEqual(controller.scale_up(3, [offer, offer], self.args, state, self.path), 0)
            self.assertEqual(external.call_count, 3)
            self.assertIn('host_operation', state)

    def test_recovery_operation_metadata_is_persisted_before_cleanup(self):
        from dataclasses import asdict
        from lib.vm_recovery import reconcile_lost_vms
        state = {'pending_vm_recovery': {'lost': asdict(self.vm)}}
        def cleanup(*args):
            saved = controller.read_scale_state(self.path)
            self.assertEqual(saved['host_operation']['recovery_key'], 'lost')
            raise subprocess.TimeoutExpired('gcloud', 180)
        with patch('lib.vm_recovery.cleanup_lost_vm', side_effect=cleanup):
            reconcile_lost_vms([], [], self.args, state, self.path)
        self.assertIn('host_operation', controller.read_scale_state(self.path))

    def test_pending_price_eviction_pauses_retry_without_releasing_uncertain_barrier(self):
        key = f'{self.vm.zone}/{self.vm.name}'
        state = {'price_replacement_target': 1}
        controller.begin_host_operation('delete', self.vm, self.runners, [self.vm], state, self.path, price_eviction=True)
        with patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[]) as prices, \
                patch('lib.spot_runner_autoscaler.delete_overpriced_vm') as delete:
            controller.reconcile_host_operation([self.vm], self.runners, self.args, state, self.path)
            prices.assert_called_once_with([self.vm], self.args.project, self.args.max_daily_usd, raise_on_error=True)
            delete.assert_not_called()
        self.assertIn('host_operation', state)
        self.assertIn(key, state['intentional_deletions'])
        self.assertIn(key, state['intentional_deletion_ids'])
        self.assertEqual(state['price_replacement_target'], 1)
        self.assertIn('host_operation', controller.read_scale_state(self.path))
        with patch('lib.spot_runner_autoscaler.launch_offer') as launch:
            self.assertEqual(controller.scale_up(1, ['offer'], self.args, state, self.path), 0)
            launch.assert_not_called()

    def test_pending_price_eviction_waits_when_fresh_price_is_unavailable(self):
        state = {}
        controller.begin_host_operation('delete', self.vm, self.runners, [self.vm], state, self.path, price_eviction=True)
        with patch('lib.spot_runner_autoscaler.check_running_prices', side_effect=RuntimeError('pricing unavailable')), \
                patch('lib.spot_runner_autoscaler.delete_overpriced_vm') as delete:
            controller.reconcile_host_operation([self.vm], self.runners, self.args, state, self.path)
            delete.assert_not_called()
        self.assertIn('host_operation', state)
        self.assertIn(f'{self.vm.zone}/{self.vm.name}', state['intentional_deletions'])

    def test_pending_price_eviction_retries_only_after_fresh_overpriced_check(self):
        state = {}
        controller.begin_host_operation('delete', self.vm, self.runners, [self.vm], state, self.path, price_eviction=True)
        with patch('lib.spot_runner_autoscaler.check_running_prices', return_value=[self.vm]) as prices, \
                patch('lib.spot_runner_autoscaler.delete_overpriced_vm') as delete:
            controller.reconcile_host_operation([self.vm], self.runners, self.args, state, self.path)
            prices.assert_called_once_with([self.vm], self.args.project, self.args.max_daily_usd, raise_on_error=True)
            delete.assert_called_once_with(self.vm, self.args.project)
        self.assertIn('host_operation', state)

    def test_inflight_launch_and_concurrent_old_host_loss_restore_four_hosts(self):
        from dataclasses import asdict
        from lib.vm_recovery import reconcile_lost_vms
        lost_launch = replace(self.vm, name='oxidex-runners-new', status='TERMINATED', instance_id='999')
        lost_old = replace(self.vm, status='TERMINATED')
        survivor = replace(self.vm, name='oxidex-runners-third', instance_id='333')
        live = [self.other, survivor]
        state = {'pending_vm_recovery': {'new': asdict(lost_launch), 'old': asdict(lost_old)}}
        controller.begin_host_operation('launch', lost_launch, [], [], state, self.path)
        controller.reconcile_host_operation([lost_launch, lost_old, *live], [], self.args, state, self.path)
        self.assertEqual(state['price_replacement_target'], 4)
        self.assertTrue(all(record['_target_recorded'] for record in state['pending_vm_recovery'].values()))
        with patch('lib.vm_recovery.cleanup_lost_vm', return_value=True):
            controller.reconcile_host_operation([lost_launch, lost_old, *live], [], self.args, state, self.path)
            controller.reconcile_host_operation(live, [], self.args, state, self.path)
            reconcile_lost_vms(live, [], self.args, state, self.path)
        self.assertEqual(state['price_replacement_target'], 4)
        self.assertEqual(controller.read_scale_state(self.path)['price_replacement_target'], 4)

    def test_failed_launch_vm_deleted_by_cleanup_is_not_a_spot_loss(self):
        from lib.fleet_logging import observe_vms
        offer = SimpleNamespace(region='asia-south2', machine_type='n2-standard-32', vcpu=32, ram_gib=128, hourly_usd=.1)
        state = {}
        present = []
        def command(args, **kwargs):
            if args[:3] == ['gcloud', 'compute', 'zones']:
                return SimpleNamespace(returncode=0, stdout='asia-south2-a\n')
            if args[:4] == ['gcloud', 'compute', 'instances', 'delete']:
                return SimpleNamespace(returncode=0, stdout='', stderr='')  # Confirmed only by a later inventory.
            if args[:4] == ['gcloud', 'compute', 'disks', 'list']:
                return SimpleNamespace(returncode=0, stdout='[]', stderr='')
            return SimpleNamespace(returncode=1, stdout='', stderr='registration failed')
        def inventory(*args, **kwargs):
            if not present:
                present.append(replace(self.vm, name=state['host_operation']['vm']['name']))
            return list(present)
        with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=command), \
                patch('lib.spot_runner_autoscaler.list_gcloud_vms', side_effect=inventory):
            controller.scale_up(1, [offer], self.args, state, self.path)
        restarted = controller.read_scale_state(self.path)
        observe_vms([present[0]], restarted)
        observe_vms([], restarted)  # Cleanup deleted the VM.
        with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=command), \
                patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[]), \
                patch('lib.spot_runner_autoscaler.list_github_runners', return_value=[]):
            controller.reconcile_host_operation([], [], self.args, restarted, self.path)
        self.assertNotIn('host_operation', restarted)
        self.assertEqual(restarted['pending_vm_recovery'], {})
        self.assertEqual(restarted.get('price_replacement_target', 0), 0)

    def test_known_launch_identity_without_old_observation_still_detects_loss(self):
        from lib.fleet_logging import observe_vms
        state = {'observed_vms': {}}
        controller.begin_host_operation('launch', self.vm, [], [], state, self.path)
        observe_vms([], state)
        controller.reconcile_host_operation([], [], self.args, state, self.path)
        self.assertEqual(state['host_operation']['kind'], 'recovery')
        self.assertEqual(state['price_replacement_target'], 1)

    def test_pending_price_and_scale_down_delete_cleanup_terminated_same_host(self):
        for price_eviction in (False, True):
            with self.subTest(price_eviction=price_eviction):
                state = {'price_replacement_target': 2 if price_eviction else 0}
                baseline = replace(self.vm, disks=({'source': 'saved-source', 'diskId': '123', 'autoDelete': True},))
                controller.begin_host_operation('delete', baseline, self.runners, [baseline], state, self.path,
                                                price_eviction=price_eviction)
                stopped = replace(baseline, status='TERMINATED')
                with patch('lib.vm_recovery.cleanup_lost_vm', return_value=True) as cleanup, \
                        patch('lib.spot_runner_autoscaler.check_running_prices') as prices, \
                        patch('lib.spot_runner_autoscaler.delete_overpriced_vm') as delete, \
                        patch('lib.spot_runner_autoscaler.terminate_spot_vm') as terminate:
                    controller.reconcile_host_operation([stopped, self.other], [], self.args, state, self.path)
                    cleanup.assert_called_once_with(stopped, self.args.project)
                    prices.assert_not_called()
                    delete.assert_not_called()
                    terminate.assert_not_called()
                self.assertEqual(state['host_operation']['kind'], 'delete_cleanup')
                self.assertTrue(state['host_operation']['drives_deleted'])
                controller.reconcile_host_operation([self.other], [], self.args, state, self.path)
                self.assertNotIn('host_operation', state)
                self.assertEqual(state['price_replacement_target'], 2 if price_eviction else 0)
                self.assertFalse(state.get('pending_vm_recovery'))

    def test_terminated_delete_keeps_saved_disk_identity_during_partial_inventory(self):
        baseline = replace(self.vm, disks=({'source': 'saved-source', 'diskId': '123', 'autoDelete': True},))
        stopped = replace(baseline, status='TERMINATED')
        partial = replace(stopped, disks=({'source': 'saved-source', 'autoDelete': True},))
        state = {}
        controller.begin_host_operation('delete', baseline, self.runners, [baseline], state, self.path)
        with patch('lib.vm_recovery.cleanup_lost_vm', side_effect=[False, True]) as cleanup:
            controller.reconcile_host_operation([partial], [], self.args, state, self.path)
            cleanup.assert_called_once_with(stopped, self.args.project)
            saved = controller.read_scale_state(self.path)
            self.assertEqual(saved['host_operation']['vm']['disks'][0]['diskId'], '123')
            controller.reconcile_host_operation([stopped], [], self.args, saved, self.path)
            self.assertEqual(cleanup.call_count, 2)
            self.assertEqual(cleanup.call_args.args[0].disks[0]['diskId'], '123')
        controller.reconcile_host_operation([], [], self.args, saved, self.path)
        self.assertNotIn('host_operation', saved)


if __name__ == '__main__':
    unittest.main()
