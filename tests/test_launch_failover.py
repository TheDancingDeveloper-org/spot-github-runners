"""Failed or stalled launches are removed once, then the next candidate is tried."""
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib import spot_runner_autoscaler as controller
from lib.launch_candidates import candidate_key
from lib.machine_profiles import CpuOffer
from lib.runner_fleet import GceVm, GitHubRunner


def candidate(zone, machine='c4-standard-32', platform='Intel Emerald Rapids', rank=3):
    return CpuOffer(zone.rsplit('-', 1)[0], machine, 32, 120, .2, platform, rank,
                    'hyperdisk-balanced', 'hyperdisk-balanced', Decimal('10'), zone=zone, zones=(zone,))


class FakeCloud:
    """Stand in for gcloud, gh, and the deployment manager."""

    def __init__(self, test):
        self.test = test
        self.vms: list[GceVm] = []
        self.disks: dict[str, list[str]] = {}
        self.runners: list[GitHubRunner] = []
        self.outcomes: dict[str, str] = {}
        self.launched: list[str] = []
        self.commands: list[list[str]] = []
        self.clock = [0.0]
        self.launch_seconds = 0

    def vm_list(self, *args, **kwargs):
        return list(self.vms)

    def runner_list(self, *args, **kwargs):
        return list(self.runners)

    def run(self, command, **kwargs):
        self.commands.append(command)
        if command[:4] == ['gcloud', 'compute', 'instances', 'delete']:
            name = command[4]
            self.vms = [vm for vm in self.vms if vm.name != name]
            self.disks.pop(name, None)
            self.disks.pop(name.replace('oxidex-runners-', 'runner-data-'), None)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if command[:4] == ['gcloud', 'compute', 'disks', 'list']:
            rows = [{'name': name, 'users': users} for name, users in self.disks.items()]
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr='')
        if command[:4] == ['gcloud', 'compute', 'disks', 'delete']:
            self.disks.pop(command[4])
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if command[:3] == ['gh', 'api', '-X']:
            runner_id = int(command[4].rsplit('/', 1)[-1])
            self.runners = [runner for runner in self.runners if runner.runner_id != runner_id]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if command[0].endswith('python3') or 'spot_runner_manager.py' in ' '.join(command):
            return self.manager(command, **kwargs)
        raise AssertionError(f'Unexpected command {command}')

    def manager(self, command, **kwargs):
        self.test.assertIn('--registration-timeout-seconds', command)
        self.test.assertIsNotNone(kwargs.get('timeout'))
        zone = command[command.index('--zone') + 1]
        name = command[command.index('--instance-name') + 1]
        self.launched.append(zone)
        self.clock[0] += self.launch_seconds
        outcome = self.outcomes.get(zone, 'ok')
        vm = GceVm(name, zone, 'RUNNING', command[command.index('--instance-type') + 1], True, instance_id='77',
                   cpu_platform=command[command.index('--min-cpu-platform') + 1])
        if outcome == 'ok':
            self.vms.append(vm)
            self.runners += [GitHubRunner(100 + i, f'gha-{name}-{i}', 'online', False, ('spot', 'X64')) for i in range(1, 9)]
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if outcome == 'vm-left-behind':
            self.vms.append(vm)
            self.disks[name.replace('oxidex-runners-', 'runner-data-')] = [name]
            self.runners += [GitHubRunner(200 + i, f'gha-{name}-{i}', 'offline', False, ('spot', 'X64')) for i in range(1, 4)]
            return SimpleNamespace(returncode=1, stdout='', stderr='Only 3/8 expected runners are online')
        if outcome == 'uncertain':
            return SimpleNamespace(returncode=1, stdout='', stderr='URLError: Operation timed out')
        return SimpleNamespace(returncode=1, stdout='', stderr='ZONE_RESOURCE_POOL_EXHAUSTED')


class LaunchFailoverTests(unittest.TestCase):
    def setUp(self):
        blocker = patch('subprocess.run', side_effect=AssertionError('Unexpected external command'))
        blocker.start()
        self.addCleanup(blocker.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state.json'
        self.args = controller.args_parser().parse_args(['--state-file', str(self.path)])
        self.cloud = FakeCloud(self)
        self.clock = [1_000_000.0]
        self.cloud.clock = self.clock
        for target, value in (('subprocess.run', self.cloud.run), ('list_gcloud_vms', self.cloud.vm_list),
                              ('list_github_runners', self.cloud.runner_list), ('now', lambda: self.clock[0])):
            patcher = patch(f'lib.spot_runner_autoscaler.{target}', side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_each_failure_moves_to_the_next_candidate_from_a_fresh_price_scan(self):
        first, second, third = candidate('us-west4-a', 'c4d-standard-32', 'AMD Turin', 0), candidate('us-west4-b'), candidate('asia-east2-a', 'n4-standard-32', rank=4)
        self.cloud.outcomes = {'us-west4-a': 'fail', 'us-west4-b': 'fail'}
        scans = []
        def refresh():
            scans.append(True)
            return [first, second, third]
        state = {}
        self.assertEqual(controller.scale_up(1, [first, second, third], self.args, state, self.path, refresh=refresh), 1)
        self.assertEqual(self.cloud.launched, ['us-west4-a', 'us-west4-b', 'asia-east2-a'])
        self.assertEqual(len(scans), 2)
        self.assertEqual(state['failed_launch_candidates'], {})  # A success starts the next search from the top.
        self.assertEqual(state['host_operation']['candidate_key'], candidate_key(third))

    def test_exhausted_list_is_retried_from_the_top_on_the_next_check(self):
        first, second = candidate('us-west4-a'), candidate('us-west4-b')
        self.cloud.outcomes = {'us-west4-a': 'fail', 'us-west4-b': 'fail'}
        state = {}
        with self.assertLogs('spot_runner_autoscaler', 'WARNING') as logs:
            self.assertEqual(controller.scale_up(1, [first, second], self.args, state, self.path,
                                                 refresh=lambda: [first, second]), 0)
        self.assertIn('exhausted', '\n'.join(logs.output))
        self.assertEqual(self.cloud.launched, ['us-west4-a', 'us-west4-b'])
        self.assertEqual(controller.read_scale_state(self.path)['failed_launch_candidates'], {})
        self.cloud.outcomes = {}
        self.assertEqual(controller.scale_up(1, [first, second], self.args, state, self.path), 1)
        self.assertEqual(self.cloud.launched, ['us-west4-a', 'us-west4-b', 'us-west4-a'])

    def test_failure_memory_survives_a_controller_restart(self):
        first, second = candidate('us-west4-a'), candidate('us-west4-b')
        state = {'failed_launch_candidates': {candidate_key(first): self.clock[0]}}
        controller.write_scale_state(self.path, state)
        restarted = controller.read_scale_state(self.path)
        self.assertEqual(controller.scale_up(1, [first, second], self.args, restarted, self.path), 1)
        self.assertEqual(self.cloud.launched, ['us-west4-b'])

    def test_failed_launch_that_left_a_vm_deletes_it_and_moves_on(self):
        first, second = candidate('us-west4-a'), candidate('us-west4-b')
        self.cloud.outcomes = {'us-west4-a': 'vm-left-behind'}
        state = {}
        self.assertEqual(controller.scale_up(1, [first, second], self.args, state, self.path, refresh=lambda: [first, second]), 1)
        self.assertEqual(self.cloud.launched, ['us-west4-a', 'us-west4-b'])
        self.assertEqual([vm.zone for vm in self.cloud.vms], ['us-west4-b'])
        self.assertFalse([name for name in self.cloud.disks if 'uswest4-a' in name])
        self.assertFalse([runner for runner in self.cloud.runners if 'uswest4-a' in runner.name])
        deleted = [command[4] for command in self.cloud.commands if command[:4] == ['gcloud', 'compute', 'instances', 'delete']]
        self.assertEqual(len(deleted), 1)
        self.assertIn(f'us-west4-a/{deleted[0]}', state['intentional_deletions'])

    def test_uncertain_launch_holds_until_the_deadline_then_cleans_up(self):
        first, second = candidate('us-west4-a'), candidate('us-west4-b')
        self.cloud.outcomes = {'us-west4-a': 'uncertain'}
        state = {}
        self.assertEqual(controller.scale_up(1, [first, second], self.args, state, self.path, refresh=lambda: [first, second]), 0)
        self.assertEqual(state['host_operation']['kind'], 'launch')
        self.assertEqual(self.cloud.launched, ['us-west4-a'])
        self.clock[0] += controller.LAUNCH_DEADLINE_SECONDS - 60
        self.assertTrue(controller.reconcile_host_operation([], [], self.args, state, self.path))
        self.assertIn('host_operation', state)
        self.clock[0] += 120
        self.assertTrue(controller.reconcile_host_operation([], [], self.args, state, self.path))
        self.assertNotIn('host_operation', state)
        self.assertIn(candidate_key(first), state['failed_launch_candidates'])
        self.assertEqual(controller.scale_up(1, [first, second], self.args, state, self.path), 1)
        self.assertEqual(self.cloud.launched, ['us-west4-a', 'us-west4-b'])

    def test_host_without_eight_runners_after_ten_minutes_is_deleted(self):
        # The controller restarted mid-launch, so no manager confirmation was recorded.
        first = candidate('us-west4-a')
        vm = GceVm('oxidex-runners-uswest4-a-5', 'us-west4-a', 'RUNNING', 'c4-standard-32', True,
                   instance_id='5', cpu_platform='Intel Emerald Rapids')
        self.cloud.vms = [vm]
        self.cloud.runners = [GitHubRunner(i, f'gha-{vm.name}-{i}', 'offline', False, ('spot', 'X64')) for i in range(1, 6)]
        state = {}
        controller.begin_host_operation('launch', vm, [], [], state, self.path, started_at=self.clock[0],
                                        candidate_key=candidate_key(first),
                                        requested_cpu_platform='Intel Emerald Rapids')
        self.clock[0] += controller.LAUNCH_DEADLINE_SECONDS - 1
        self.assertTrue(controller.reconcile_host_operation([vm], self.cloud.runners, self.args, state, self.path))
        self.assertEqual(self.cloud.vms, [vm])
        self.clock[0] += 2
        with self.assertLogs('spot_runner_autoscaler', 'WARNING') as logs:
            self.assertTrue(controller.reconcile_host_operation([vm], self.cloud.runners, self.args, state, self.path))
        self.assertIn('10 minutes', '\n'.join(logs.output))
        self.assertEqual(self.cloud.vms, [])
        self.assertEqual(self.cloud.runners, [])
        self.assertNotIn('host_operation', state)
        self.assertIn(f'{vm.zone}/{vm.name}', state['intentional_deletions'])
        self.assertIn(candidate_key(first), state['failed_launch_candidates'])

    def test_registered_host_completes_even_when_checked_after_the_deadline(self):
        state = {}
        self.assertEqual(controller.scale_up(1, [candidate('us-west4-a')], self.args, state, self.path), 1)
        self.clock[0] += controller.LAUNCH_DEADLINE_SECONDS * 2
        self.assertTrue(controller.reconcile_host_operation(self.cloud.vms, self.cloud.runners, self.args, state, self.path))
        self.assertNotIn('host_operation', state)
        self.assertEqual(len(self.cloud.vms), 1)

    def test_legacy_pending_launch_without_a_start_time_expires_ten_minutes_after_restart(self):
        state = {}
        vm = GceVm('oxidex-runners-northamericanortheast1-b-1790895655', 'northamerica-northeast1-b',
                   'PROVISIONING', 'c4-standard-32', True)
        controller.begin_host_operation('launch', vm, [], [], state, self.path)
        restarted = controller.read_scale_state(self.path)
        self.assertTrue(controller.reconcile_host_operation([], [], self.args, restarted, self.path))
        self.assertIn('host_operation', restarted)
        self.clock[0] += controller.LAUNCH_DEADLINE_SECONDS + 1
        self.assertTrue(controller.reconcile_host_operation([], [], self.args, restarted, self.path))
        self.assertNotIn('host_operation', restarted)

    def test_one_check_stops_searching_after_its_time_budget_and_resumes_next_check(self):
        offers = [candidate(f'us-west4-{letter}') for letter in 'abc']
        self.cloud.outcomes = {offer.zone: 'fail' for offer in offers}
        self.cloud.launch_seconds = controller.LAUNCH_SEARCH_BUDGET_SECONDS
        scans = []
        def slow_refresh():
            scans.append(True)
            return offers
        state = {}
        with self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
            self.assertEqual(controller.scale_up(1, offers, self.args, state, self.path, refresh=slow_refresh), 0)
        self.assertEqual(self.cloud.launched, ['us-west4-a'])
        self.assertEqual(scans, [])  # The next check scans fresh prices anyway.
        self.assertIn('next check', '\n'.join(logs.output))
        self.cloud.outcomes = {}
        self.assertEqual(controller.scale_up(1, offers, self.args, state, self.path, refresh=slow_refresh), 1)
        self.assertEqual(self.cloud.launched, ['us-west4-a', 'us-west4-b'])

    def test_cleanup_error_during_reconcile_is_logged_and_retried(self):
        state = {}
        vm = GceVm('oxidex-runners-uswest4-a-1', 'us-west4-a', 'RUNNING', 'c4-standard-32', True, instance_id='9')
        self.cloud.vms = [vm]
        controller.begin_host_operation('launch', vm, [], [], state, self.path, started_at=self.clock[0])
        self.clock[0] += controller.LAUNCH_DEADLINE_SECONDS + 1
        with patch('lib.spot_runner_autoscaler.subprocess.run',
                   side_effect=subprocess.CalledProcessError(1, ['gcloud'], stderr='network unreachable')), \
             self.assertLogs('spot_runner_autoscaler', 'WARNING') as logs:
            self.assertTrue(controller.reconcile_host_operation([vm], [], self.args, state, self.path))
        self.assertIn('Launch cleanup pending', '\n'.join(logs.output))
        self.assertIn('host_operation', state)
        self.assertTrue(controller.reconcile_host_operation([vm], [], self.args, state, self.path))
        self.assertNotIn('host_operation', state)

    def test_manager_confirmed_host_is_never_abandoned_by_the_deadline(self):
        state = {}
        self.assertEqual(controller.scale_up(1, [candidate('us-west4-a')], self.args, state, self.path), 1)
        self.assertTrue(state['host_operation']['registered'])
        vm = self.cloud.vms[0]
        flapping = [replace(runner, status='offline') if runner.runner_id == 101 else runner for runner in self.cloud.runners]
        self.clock[0] += controller.LAUNCH_DEADLINE_SECONDS + 60
        self.assertTrue(controller.reconcile_host_operation([vm], flapping, self.args, state, self.path))
        self.assertEqual(self.cloud.vms, [vm])
        self.assertIn('host_operation', state)
        self.assertTrue(controller.reconcile_host_operation([vm], self.cloud.runners, self.args, state, self.path))
        self.assertNotIn('host_operation', state)

    def test_hung_manager_with_all_runners_online_counts_as_success(self):
        def hung(command, **kwargs):
            self.cloud.manager(command, **kwargs)  # The host comes up, then the manager hangs.
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        original = self.cloud.run
        def run(command, **kwargs):
            if 'spot_runner_manager.py' in ' '.join(command):
                return hung(command, **kwargs)
            return original(command, **kwargs)
        state = {}
        with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=run):
            self.assertEqual(controller.scale_up(1, [candidate('us-west4-a')], self.args, state, self.path), 1)
        self.assertEqual(len(self.cloud.vms), 1)
        self.assertTrue(state['host_operation']['registered'])

    def test_discovery_error_during_refresh_stops_the_search_without_crashing(self):
        first, second = candidate('us-west4-a'), candidate('us-west4-b')
        self.cloud.outcomes = {'us-west4-a': 'fail'}
        with patch('lib.spot_runner_autoscaler.discover_offers',
                   side_effect=RuntimeError('CPU platform zone inventory is incomplete')), \
             self.assertLogs('spot_runner_autoscaler', 'ERROR') as logs:
            refresh = controller.offer_refresher(self.args)
            self.assertEqual(refresh(), [])
            self.assertEqual(controller.scale_up(1, [first, second], self.args, {}, self.path, refresh=refresh), 0)
        self.assertIn('Spot scan failed', '\n'.join(logs.output))
        self.assertEqual(self.cloud.launched, ['us-west4-a'])

    def test_disk_still_attached_keeps_cleanup_pending(self):
        state = {}
        vm = GceVm('oxidex-runners-uswest4-a-1', 'us-west4-a', 'PROVISIONING', 'c4-standard-32', True)
        controller.begin_host_operation('launch', vm, [], [], state, self.path, started_at=self.clock[0])
        self.cloud.disks['runner-data-uswest4-a-1'] = ['some-other-vm']
        self.clock[0] += controller.LAUNCH_DEADLINE_SECONDS + 1
        self.assertTrue(controller.reconcile_host_operation([], [], self.args, state, self.path))
        self.assertIn('host_operation', state)
        self.assertIn('runner-data-uswest4-a-1', self.cloud.disks)



class FleetSummaryTests(unittest.TestCase):
    def test_summary_counts_runners_and_describes_each_spot_vm_group(self):
        vms = [GceVm(f'oxidex-runners-asiaeast2-a-{i}', 'asia-east2-a', 'RUNNING', 'n4-standard-32', True,
                     cpu_platform='Intel Emerald Rapids') for i in (1, 2)]
        vms.append(GceVm('oxidex-runners-uswest4-a-3', 'us-west4-a', 'PROVISIONING', 'c4d-standard-32', True))
        runners = [GitHubRunner(i, f'gha-{vms[i % 2].name}-{i}', 'online', i < 3, ('spot', 'X64', 'n4-standard-32'))
                   for i in range(16)]
        runners += [GitHubRunner(90, 'oxidex-m4air-1', 'online', False, ('ARM64',)),
                    GitHubRunner(91, 'gha-old-1', 'offline', False, ('spot', 'X64', 'n2-standard-32'))]
        state = {'host_operation': {'kind': 'launch', 'vm': {'name': vms[2].name}, 'started_at': 1000.0}}
        with patch('lib.spot_runner_autoscaler.now', return_value=1000.0 + 252):
            summary = controller.fleet_summary(vms, runners, state)
        self.assertEqual(summary,
                         'Fleet: GitHub runners 17 online (3 busy), 16 on Spot VMs, 1 offline; '
                         'Spot VMs 3: 2 RUNNING n4-standard-32 Intel Emerald Rapids in asia-east2-a, '
                         '1 PROVISIONING c4d-standard-32 CPU platform unknown in us-west4-a; '
                         'pending launch of oxidex-runners-uswest4-a-3 for 4m 12s of the 10m limit')

    def test_summary_without_vms_or_pending_work(self):
        self.assertEqual(controller.fleet_summary([], [], {}),
                         'Fleet: GitHub runners 0 online (0 busy), 0 on Spot VMs, 0 offline; Spot VMs 0; no pending host operation')


class ManagerDeadlineTests(unittest.TestCase):
    def test_bootstrap_wait_stops_at_the_registration_deadline(self):
        from lib import runner_manager as manager
        args = manager.args_parser().parse_args(['--max-cost', '.2', '--registration-timeout-seconds', '600'])
        args.deployment_started_at = 1000.0
        clock = [1000.0]
        def ssh(command, **kwargs):
            clock[0] += 120
            return SimpleNamespace(returncode=255, stdout='', stderr='')
        with patch.object(manager.subprocess, 'run', side_effect=ssh) as run, \
             patch.object(manager.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(manager.time, 'sleep'):
            with self.assertRaises(TimeoutError):
                manager.wait_for_bootstrap(args, 'us-west4-a', 'deployment')
        self.assertLessEqual(clock[0], 1000.0 + 600 + 120)
        self.assertEqual(run.call_count, 5)

    def test_manual_runs_keep_the_twenty_minute_bootstrap_wait(self):
        from lib import runner_manager as manager
        args = manager.args_parser().parse_args(['--max-cost', '.2'])
        self.assertEqual(args.registration_timeout_seconds, 1200)


if __name__ == '__main__':
    unittest.main()
