"""Exercise platform selection and confirmed-absence fallback offline."""
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pricing_fixtures import storage_prices_for
from lib.compute_api import Offer
from lib import spot_runner_autoscaler as controller
from lib import runner_manager as manager
from lib.runner_fleet import GceVm
from lib.fleet_logging import observe_vms, vm_details, vm_from_record


class CpuPlatformTests(unittest.TestCase):
    def setUp(self):
        blocker = patch('subprocess.run', side_effect=AssertionError('Unexpected external command'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_selection_expands_platforms_and_prefers_family_before_price(self):
        offers = [Offer('region', family + '-standard-32', 32, 128, price)
                  for family, price in [('e2', .01), ('n2', .02), ('n2d', .03),
                                        ('c2d', .04), ('n4', .05), ('c4', .06),
                                        ('n4d', .07), ('c4d', .08)]]
        selected = controller.eligible_offers(offers, controller.Decimal('5'), storage_rates=storage_prices_for(offers))
        self.assertEqual([(o.machine_type, getattr(o, 'cpu_platform', None)) for o in selected], [
            ('c4d-standard-32', 'AMD Turin'), ('n4d-standard-32', 'AMD Turin'),
            ('c4-standard-32', 'Intel Granite Rapids'), ('c4-standard-32', 'Intel Emerald Rapids'),
            ('n4-standard-32', 'Intel Emerald Rapids'), ('c2d-standard-32', 'AMD Milan'),
            ('n2d-standard-32', 'AMD Milan'), ('n2-standard-32', 'Intel Ice Lake'),
            ('n2-standard-32', 'Intel Cascade Lake')])

    def test_failed_platform_falls_back_only_after_confirmed_absence(self):
        offers = controller.eligible_offers([Offer('region', 'c4-standard-32', 32, 120, .1),
                                             Offer('region', 'n4-standard-32', 32, 128, .09)],
                                            controller.Decimal('5'), storage_rates=storage_prices_for([Offer('region', 'n4-standard-32', 32, 128, .1)]))
        platforms = []
        def external(command, **kwargs):
            if command[:3] == ['gcloud', 'compute', 'zones']:
                return SimpleNamespace(returncode=0, stdout='northamerica-northeast1-a\n')
            if command[:3] == ['gcloud', 'compute', 'disks']:
                return SimpleNamespace(returncode=0, stdout='[]')
            platform = command[command.index('--min-cpu-platform') + 1]
            platforms.append(platform)
            return SimpleNamespace(returncode=0 if len(platforms) == 3 else 1,
                                   stdout='', stderr='requested CPU platform unavailable')
        with tempfile.TemporaryDirectory() as directory:
            args = controller.args_parser().parse_args(['--state-file', str(Path(directory) / 'state.json')])
            state = {}
            with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=external), \
                 patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[]), \
                 patch('lib.spot_runner_autoscaler.list_github_runners', return_value=[]):
                self.assertEqual(controller.scale_up(1, offers, args, state), 1)
            self.assertEqual(platforms, ['Intel Granite Rapids', 'Intel Emerald Rapids', 'Intel Emerald Rapids'])
            self.assertEqual(state['host_operation']['requested_cpu_platform'], 'Intel Emerald Rapids')
            self.assertEqual(state['host_operation']['vm']['machine_type'], 'n4-standard-32')

    def test_manager_emits_requested_platform_and_rejects_incompatible_family(self):
        args = manager.args_parser().parse_args(['--max-cost', '.2', '--instance-type', 'n2-standard-32',
                                                '--min-cpu-platform', 'Intel Cascade Lake'])
        manager.select_sizing(args)
        calls = []
        def cloud(command, **kwargs):
            calls.append(command)
            return '[]' if command[:3] == ['compute', 'disks', 'list'] else ''
        with patch('lib.runner_manager.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='')), \
             patch('lib.runner_manager.gcloud', side_effect=cloud):
            manager.apply_startup(args, 'region-a', 'offline startup script')
        create = next(c for c in calls if c[:3] == ['compute', 'instances', 'create'])
        self.assertIn('--min-cpu-platform=Intel Cascade Lake', create)
        args.min_cpu_platform = 'AMD Milan'
        with self.assertRaisesRegex(RuntimeError, 'platform'):
            manager.select_sizing(args)

    def test_wrong_observed_platform_cannot_confirm_deployment(self):
        args = manager.args_parser().parse_args(['--max-cost', '.2', '--min-cpu-platform', 'Intel Ice Lake'])
        manager.select_sizing(args)
        vm = GceVm(args.instance_name, 'region-a', 'RUNNING', 'n2-standard-32', True,
                   cpu_platform='Intel Cascade Lake')
        with patch('lib.runner_manager.list_gcloud_vms', return_value=[vm]):
            with self.assertRaisesRegex(RuntimeError, 'platform'):
                manager.observed_deployment_vm(args, 'region-a')

    def test_extended_custom_c4_parses_cpu_count_before_memory_and_ext(self):
        from lib.machine_profiles import platform_preferences
        args = manager.args_parser().parse_args(['--max-cost', '.4', '--instance-type',
                                                'c4-custom-32-122880-ext', '--ram', '120'])
        manager.select_sizing(args)
        self.assertEqual(args.min_cpu_platform, 'Intel Granite Rapids')
        self.assertEqual([platform for _, platform in platform_preferences('c4-custom-144-552960-ext')],
                         ['Intel Granite Rapids'])

    def test_newer_supported_platform_satisfies_minimum_after_restart(self):
        from lib.runner_fleet import GitHubRunner
        args = manager.args_parser().parse_args(['--max-cost', '.2', '--min-cpu-platform', 'Intel Cascade Lake'])
        manager.select_sizing(args)
        vm = GceVm(args.instance_name, 'region-a', 'RUNNING', 'n2-standard-32', True,
                   instance_id='123', cpu_platform='Intel Ice Lake')
        with patch('lib.runner_manager.list_gcloud_vms', return_value=[vm]):
            self.assertEqual(manager.observed_deployment_vm(args, 'region-a'), vm)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            state = {}
            controller.begin_host_operation('launch', vm, [], [], state, path,
                                            requested_cpu_platform='Intel Cascade Lake')
            runners = [GitHubRunner(i, f'gha-{vm.name}-{i}', 'online', False, ('spot',)) for i in range(1, 9)]
            controller.reconcile_host_operation([vm], runners, args, state, path)
            self.assertNotIn('host_operation', state)

    def test_cpu_platform_survives_loss_and_is_not_reused_for_new_identity(self):
        vm = GceVm('oxidex-runners-test', 'region-a', 'RUNNING', 'n2-standard-32', True,
                   instance_id='123', cpu_platform='Intel Ice Lake')
        state = {}
        observe_vms([vm], state)
        observe_vms([replace(vm, cpu_platform='')], state)
        self.assertIn('Intel Ice Lake', vm_details(vm_from_record(state['observed_vms']['region-a/oxidex-runners-test'])))
        observe_vms([], state)
        self.assertEqual(next(iter(state['pending_vm_recovery'].values()))['cpu_platform'], 'Intel Ice Lake')
        observe_vms([replace(vm, instance_id='456', cpu_platform='')], state)
        self.assertEqual(state['observed_vms']['region-a/oxidex-runners-test']['cpu_platform'], '')

    def test_launch_on_wrong_platform_is_deleted_instead_of_completed(self):
        from lib.runner_fleet import GitHubRunner
        vm = GceVm('oxidex-runners-test', 'region-a', 'RUNNING', 'n2-standard-32', True,
                   instance_id='123', cpu_platform='Intel Cascade Lake')
        runners = [GitHubRunner(i, f'gha-{vm.name}-{i}', 'online', False, ('spot',)) for i in range(1, 9)]
        inventory = [vm]
        commands = []
        def external(command, **kwargs):
            commands.append(command[:4])
            if command[:4] == ['gcloud', 'compute', 'instances', 'delete']:
                inventory.clear()
            return SimpleNamespace(returncode=0, stdout='[]', stderr='')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            args = controller.args_parser().parse_args(['--state-file', str(path)])
            state = {}
            controller.begin_host_operation('launch', vm, [], [], state, path,
                                            requested_cpu_platform='Intel Ice Lake')
            with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=external), \
                 patch('lib.spot_runner_autoscaler.list_gcloud_vms', side_effect=lambda *a, **k: list(inventory)), \
                 patch('lib.spot_runner_autoscaler.list_github_runners', return_value=[]):
                controller.reconcile_host_operation([vm], runners, args, state, path)
            self.assertIn(['gcloud', 'compute', 'instances', 'delete'], commands)
            self.assertNotIn('host_operation', state)

    def test_launch_with_unreported_platform_waits_instead_of_completing(self):
        from lib.runner_fleet import GitHubRunner
        vm = GceVm('oxidex-runners-test', 'region-a', 'RUNNING', 'n2-standard-32', True, instance_id='123')
        runners = [GitHubRunner(i, f'gha-{vm.name}-{i}', 'online', False, ('spot',)) for i in range(1, 9)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            args = controller.args_parser().parse_args(['--state-file', str(path)])
            state = {}
            controller.begin_host_operation('launch', vm, [], [], state, path,
                                            requested_cpu_platform='Intel Ice Lake', started_at=controller.now())
            controller.reconcile_host_operation([vm], runners, args, state, path)
            self.assertIn('host_operation', state)
            controller.reconcile_host_operation([replace(vm, cpu_platform='Intel Ice Lake')],
                                                 runners, args, state, path)
            self.assertNotIn('host_operation', state)

    def test_timeout_does_not_try_lower_platform(self):
        offers = controller.eligible_offers([Offer('region', 'n2-standard-32', 32, 128, .1)],
                                            controller.Decimal('5'), storage_rates=storage_prices_for([Offer('region', 'n2-standard-32', 32, 128, .1)]))
        requested = []
        def external(command, **kwargs):
            if command[:3] == ['gcloud', 'compute', 'zones']:
                return SimpleNamespace(returncode=0, stdout='region-a\n')
            requested.append(command[command.index('--min-cpu-platform') + 1])
            raise subprocess.TimeoutExpired(command, 60)
        with tempfile.TemporaryDirectory() as directory:
            args = controller.args_parser().parse_args(['--state-file', str(Path(directory) / 'state.json')])
            state = {}
            with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=external), \
                 patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[]):
                self.assertEqual(controller.scale_up(1, offers, args, state), 0)
            self.assertEqual(requested, ['Intel Ice Lake'])
            self.assertTrue(state['host_operation']['outcome_uncertain'])
            self.assertIn('host_operation', state)

    def test_price_breaks_ties_only_for_identical_family_and_platform(self):
        offers = [Offer('expensive', 'n4-standard-32', 32, 128, .19),
                  Offer('cheap', 'n4-standard-32', 32, 128, .10),
                  Offer('cap', 'c4d-standard-32', 32, 124, 5 / 24),
                  Offer('small', 'c4d-highcpu-32', 32, 60, .01)]
        selected = controller.eligible_offers(offers, controller.Decimal('5'), storage_rates=storage_prices_for(offers))
        self.assertEqual([o.region for o in selected], ['cheap', 'expensive'])

    def test_stopped_adoption_sets_platform_with_supported_update_command(self):
        args = manager.args_parser().parse_args(['--max-cost', '.2', '--existing-instance',
                                                '--min-cpu-platform', 'Intel Ice Lake'])
        manager.select_sizing(args)
        vm = {'machineType': 'machineTypes/n2-standard-32', 'status': 'TERMINATED',
              'scheduling': {'provisioningModel': 'SPOT'}, 'disks': []}
        replies = [SimpleNamespace(returncode=0, stdout=json.dumps(vm)),
                   SimpleNamespace(returncode=1, stdout='')]
        calls = []
        def cloud(command, **kwargs):
            calls.append(command)
            return '[]' if command[:3] == ['compute', 'disks', 'list'] else ''
        with patch('lib.runner_manager.subprocess.run', side_effect=replies), \
             patch('lib.runner_manager.gcloud', side_effect=cloud):
            manager.apply_startup(args, 'region-a', 'offline startup script')
        self.assertIn(['compute', 'instances', 'update', args.instance_name, '--zone=region-a',
                       '--min-cpu-platform=Intel Ice Lake', '--quiet'], calls)

    def test_inventory_reads_observed_and_requested_cpu_platforms(self):
        from lib.runner_fleet import list_gcloud_vms
        responses = [[{'name': 'oxidex-runners-test', 'zone': 'zones/region-a',
                       'machineType': 'machineTypes/n2-standard-32', 'status': 'RUNNING',
                       'cpuPlatform': 'Intel Ice Lake', 'minCpuPlatform': 'Intel Ice Lake',
                       'id': '123', 'disks': [], 'scheduling': {'provisioningModel': 'SPOT'}}],
                     [], {'guestCpus': 32, 'memoryMb': 131072}]
        with patch('lib.runner_fleet._command_json', side_effect=responses):
            vm, = list_gcloud_vms('project')
        self.assertIn('CPU platform: Intel Ice Lake', vm_details(vm))
        self.assertIn('requested CPU platform: Intel Ice Lake', vm_details(vm))

    def test_c4_platform_default_respects_shape_and_zone_constraints(self):
        args = manager.args_parser().parse_args(['--max-cost', '.2', '--instance-type', 'c4-standard-32',
                                                '--ram', '120', '--zone', 'northamerica-northeast1-b'])
        manager.select_sizing(args)
        self.assertEqual(args.min_cpu_platform, 'Intel Emerald Rapids')
        args.min_cpu_platform = 'Intel Granite Rapids'
        with self.assertRaisesRegex(RuntimeError, 'platform'):
            manager.select_sizing(args)
        args.zone = 'northamerica-northeast1-a'
        manager.select_sizing(args)
        self.assertEqual(args.min_cpu_platform, 'Intel Granite Rapids')
        args = manager.args_parser().parse_args(['--max-cost', '2', '--instance-type', 'c4-standard-144',
                                                '--cpu', '144', '--ram', '540', '--zone', 'us-central1-a',
                                                '--min-cpu-platform', 'Intel Emerald Rapids'])
        with self.assertRaisesRegex(RuntimeError, 'platform'):
            manager.select_sizing(args)


if __name__ == '__main__':
    unittest.main()
