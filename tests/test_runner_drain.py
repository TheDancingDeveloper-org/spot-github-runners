"""Offline checks that voluntary scale-down never interrupts an assigned job."""
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib import spot_runner_autoscaler as controller
from lib.runner_fleet import GceVm, GitHubRunner


class RunnerDrainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.json'
        self.args = controller.args_parser().parse_args(['--state-file', str(self.path),
                                                        '--scaling-mode', 'legacy', '--no-remote-builder'])
        self.vm = GceVm('oxidex-runners-test', 'asia-south2-a', 'RUNNING', 'n2-standard-32', True, instance_id='123')
        self.other = replace(self.vm, name='oxidex-runners-other', instance_id='456')
        self.runners = [GitHubRunner(i, f'gha-{self.vm.name}-{i}', 'online', False,
                                     ('self-hosted', 'Linux', 'X64', 'spot', 'n2-standard-32')) for i in range(1, 9)]
        self.live = list(self.runners)
        self.members = set()
        self.group = None
        self.repo_access = []
        self.commands = []
        self.fail_label = False
        self.fail_deregister = False
        self.fail_delete = False
        self.provider_id = '123'
        self.timeout_deregister = False
        self.fail_describe = False
        self.job_status = 'in_progress'
        self.state = {}
        self.external = patch('subprocess.run', side_effect=self.command)
        self.external.start()
        self.addCleanup(self.external.stop)
        self.inventory = patch('lib.spot_runner_autoscaler.list_github_runners', side_effect=lambda *a, **k: tuple(self.live))
        self.builder_guard=patch('lib.spot_runner_autoscaler.quiesce_remote_builder',return_value=True)
        self.builder_guard.start()
        self.addCleanup(self.builder_guard.stop)
        self.inventory.start()
        self.addCleanup(self.inventory.stop)

    def row(self, runner):
        return {'id':runner.runner_id, 'name':runner.name, 'status':runner.status, 'busy':runner.busy,
                'labels':[{'name':name,'type':'read-only' if name in ('self-hosted','Linux','X64') else 'custom'} for name in runner.labels]}

    def command(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == 'gcloud':
            if command[3] == 'describe':
                if self.fail_describe:
                    raise subprocess.CalledProcessError(1,command,stderr='PERMISSION_DENIED: required compute.instances.get permission')
                return SimpleNamespace(returncode=0, stdout=self.provider_id)
            self.assertFalse(any(r.busy for r in self.live), 'Deleted a busy host')
            self.assertEqual(self.live, [], 'Deleted before runner deregistration')
            if self.fail_delete:
                raise subprocess.CalledProcessError(1,command,stderr='PERMISSION_DENIED: required compute.instances.delete permission')
            return SimpleNamespace(returncode=0, stdout='')
        self.assertEqual(command[:2], ['gh', 'api'])
        path = next(c for c in command if c.startswith(('orgs/', 'repos/')))
        method = command[command.index('-X')+1] if '-X' in command else 'GET'
        data = {}
        if path.endswith('/runner-groups'):
            if method == 'POST':
                body=json.loads(kwargs['input'])
                self.assertEqual(body['visibility'], 'selected')
                self.assertEqual(body['selected_repository_ids'], [])
                self.group={'id':99,'name':body['name'],'visibility':'selected','default':False,'inherited':False,
                            'allows_public_repositories':False}
                data=self.group
            else:
                data=[{'runner_groups':[self.group] if self.group else []}]
        elif '/runner-groups/' in path:
            if path.endswith('/repositories'):
                data=[{'repositories':self.repo_access}]
            elif '/runners/' in path:
                rid=int(path.rsplit('/',1)[-1]);self.members.add(rid)
            elif path.endswith('/runners'):
                data=[{'runners':[self.row(r) for r in self.live if r.runner_id in self.members]}]
            elif method == 'DELETE':
                self.assertEqual(self.members,set());self.group=None
            else:
                data=self.group
        elif '/actions/jobs/' in path:
            data={'status':self.job_status}
        elif path.endswith('/labels'):
            rid=int(path.split('/')[-2])
            if self.fail_label:
                raise subprocess.CalledProcessError(1,command,stderr='labels unavailable')
            self.live=[replace(r,labels=('self-hosted','Linux','X64')) if r.runner_id==rid else r for r in self.live]
            data={'labels':self.row(next(r for r in self.live if r.runner_id==rid))['labels']}
        elif '/actions/runners/' in path:
            rid=int(path.rsplit('/',1)[-1]);runner=next(r for r in self.live if r.runner_id==rid)
            if method=='DELETE':
                self.assertFalse(runner.busy)
                if self.fail_deregister and rid==2:
                    raise subprocess.CalledProcessError(1,command,stderr='removal unavailable')
                self.live=[r for r in self.live if r.runner_id!=rid];self.members.discard(rid)
                if self.timeout_deregister:
                    self.timeout_deregister=False
                    raise subprocess.TimeoutExpired(command,60)
            else:data=self.row(runner)
        else:self.fail('Unexpected API: '+path)
        return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr='')

    def begin(self, legacy=False):
        with patch('lib.spot_runner_autoscaler.removable_spot_vms',return_value=[self.vm]):
            controller.scale_down([self.vm,self.other],self.runners,self.args,self.state,self.path)
        if legacy:
            self.state['host_operation'].pop('drain_mode',None)
            self.state['host_operation']['drain_group_name']='spot-drain-legacy'
            self.state['host_operation']['runners_isolated']=False
            controller.write_scale_state(self.path,self.state)
            self.reconcile()

    def reconcile(self, vms=None):
        controller.reconcile_host_operation(vms if vms is not None else [self.vm,self.other],
                                            list(self.live),self.args,self.state,self.path)

    def test_resource_mode_quarantines_builtin_labels_and_checks_remote_builder(self):
        self.args.scaling_mode='resource'
        self.args.remote_builder=True
        with patch('lib.spot_runner_autoscaler.quiesce_remote_builder', return_value=True) as guard:
            self.begin()
        guard.assert_called_once()
        self.assertEqual(self.state['host_operation']['drain_mode'], 'isolated_group')
        self.assertEqual(self.members, {r.runner_id for r in self.runners})
        self.assertEqual(self.repo_access, [])

    def test_resource_drain_rechecks_builder_before_provider_delete(self):
        self.args.scaling_mode='resource';self.args.remote_builder=True
        with patch('lib.spot_runner_autoscaler.quiesce_remote_builder', return_value=True):
            self.begin()
        self.reconcile()
        with patch('lib.spot_runner_autoscaler.quiesce_remote_builder', return_value=False) as guard:
            self.reconcile()
        guard.assert_called_once()
        self.assertEqual(self.state['host_operation']['kind'], 'drain')
        self.assertFalse(any(c[:4]==['gcloud','compute','instances','delete'] for c in self.commands))

    def test_disabled_future_builder_install_still_blocks_active_source_drain(self):
        self.args.remote_builder=False
        with patch('lib.spot_runner_autoscaler.quiesce_remote_builder',return_value=False) as guard:
            self.begin()
            self.assertNotIn('host_operation',self.state)
        guard.assert_called_once()
        self.begin();self.reconcile()
        with patch('lib.spot_runner_autoscaler.quiesce_remote_builder',return_value=False) as guard:
            self.reconcile()
        guard.assert_called_once()
        self.assertEqual(self.state['host_operation']['kind'],'drain')
        self.assertFalse(any(c[:4]==['gcloud','compute','instances','delete'] for c in self.commands))

    def test_labels_are_removed_before_waiting_and_deletion_requires_fresh_absence(self):
        self.begin()
        self.assertIn('host_operation',self.state)
        self.assertEqual(self.state['host_operation']['kind'],'drain')
        self.assertEqual(self.state['host_operation']['drain_mode'],'label_only')
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))
        self.assertTrue(all(r.labels==('self-hosted','Linux','X64') for r in self.live))
        self.assertEqual(self.members,set())
        self.assertFalse(any('runner-groups' in str(c) for c in self.commands))
        self.reconcile()
        self.assertEqual(self.live,[])
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))
        self.reconcile()
        self.assertTrue(any(c[:4]==['gcloud','compute','instances','delete'] for c in self.commands))
        self.assertEqual(self.state['host_operation']['kind'],'delete')
        self.reconcile([self.other])
        self.assertNotIn('host_operation',self.state)
        self.assertIsNone(self.group)

    def test_label_only_drain_keeps_runner_group_and_waits_for_active_jobs(self):
        self.live[0]=replace(self.live[0],busy=True)
        self.begin()
        self.assertEqual(len(self.live),8)
        self.assertFalse(any('runner-groups' in str(c) for c in self.commands))
        self.live[0]=replace(self.live[0],busy=False)
        self.reconcile();self.reconcile()
        self.assertTrue(any(c[:4]==['gcloud','compute','instances','delete'] for c in self.commands))

    def test_legacy_group_drain_remains_supported(self):
        self.state['host_operation']={'kind':'drain','vm':controller.asdict(self.vm),
            'runner_ids':[r.runner_id for r in self.runners],
            'runner_names':{str(r.runner_id):r.name for r in self.runners},
            'drain_group_name':'spot-drain-legacy'}
        self.reconcile()
        self.assertEqual(self.members,set(range(1,9)))

    def test_job_assigned_after_selection_finishes_before_any_deregistration(self):
        self.live[0]=replace(self.live[0],busy=True)
        self.begin()
        self.reconcile()
        self.assertEqual(len(self.live),8)
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))
        self.live[0]=replace(self.live[0],busy=False)
        self.reconcile();self.reconcile()
        self.assertTrue(any(c[0]=='gcloud' for c in self.commands))

    def test_ci_job_must_be_terminal_even_if_busy_flag_is_false(self):
        self.state['job_tracking']={'jobs':{'job':{'repository':'swack-tools/oxidex',
            'vm':{'instance_id':'123'},'vm_key':'asia-south2-a/oxidex-runners-test',
            'job':{'id':22,'status':'in_progress'}}}}
        self.begin();self.reconcile()
        self.assertEqual(len(self.live),8)
        self.job_status='completed'
        self.reconcile();self.reconcile()
        self.assertTrue(any(c[0]=='gcloud' for c in self.commands))

    def test_failed_label_removal_preserves_drain_across_restart(self):
        self.fail_label=True;self.begin(legacy=True)
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))
        self.state=controller.read_scale_state(self.path)
        self.fail_label=False;self.reconcile()
        self.assertEqual(self.members,set(range(1,9)))
        self.assertTrue(all('spot' not in r.labels for r in self.live))

    def test_group_with_repository_access_blocks_deregistration(self):
        self.begin(legacy=True);self.repo_access=[{'id':999}]
        self.reconcile()
        self.assertEqual(len(self.live),8)
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))

    def test_group_with_public_repository_access_blocks_deregistration(self):
        self.begin(legacy=True);self.group['allows_public_repositories']=True
        self.reconcile()
        self.assertEqual(len(self.live),8)
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))

    def test_group_with_unknown_public_repository_access_blocks_deregistration(self):
        self.begin(legacy=True);self.group.pop('allows_public_repositories')
        self.reconcile()
        self.assertEqual(len(self.live),8)
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))

    def test_partial_deregistration_resumes_without_deleting_vm_early(self):
        self.begin();self.fail_deregister=True;self.reconcile()
        self.assertEqual(len(self.live),7)
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))
        self.state=controller.read_scale_state(self.path)
        self.fail_deregister=False;self.reconcile();self.reconcile()
        self.assertTrue(any(c[0]=='gcloud' for c in self.commands))

    def test_missing_runner_slot_never_selects_a_host_for_deletion(self):
        docker=GitHubRunner(88,'unrelated-docker-1','online',False,('docker',))
        self.assertEqual(controller.removable_spot_vms([self.vm,self.other], self.runners[:7]+[docker]),[])

    def test_old_docker_labeled_slots_do_not_block_draining_the_spot_vm(self):
        legacy = [replace(r, labels=('docker', 'n2-standard-32', 'X64')) if r.runner_id <= 2 else r
                  for r in self.runners]
        self.assertEqual(controller.removable_spot_vms([self.vm, self.other], legacy), [self.vm])

    def test_custom_runner_prefix_with_all_eight_slots_can_be_drained(self):
        assigned=[replace(r,name=r.name.replace('gha-','oxidex-example-',1)) for r in self.runners]
        self.assertEqual(controller.removable_spot_vms([self.vm,self.other],assigned),[self.vm])

    def test_two_incomplete_prefixes_do_not_prove_all_slots_registered(self):
        assigned=[replace(r,name=r.name.replace('gha-','first-' if r.runner_id<=4 else 'second-',1)) for r in self.runners]
        self.assertEqual(controller.removable_spot_vms([self.vm,self.other],assigned),[])

    def test_changed_provider_identity_blocks_deletion_without_intent(self):
        self.begin();self.reconcile();self.provider_id='999';self.reconcile()
        self.assertFalse(any(c[:4]==['gcloud','compute','instances','delete'] for c in self.commands))
        self.assertFalse(self.state.get('intentional_deletions'))

    def test_provider_read_rejection_preserves_drain_and_group_identity(self):
        self.begin(legacy=True);self.reconcile();self.fail_describe=True;self.reconcile()
        self.state=controller.read_scale_state(self.path)
        self.assertEqual(self.state['host_operation']['kind'],'drain')
        self.assertEqual(self.state['host_operation']['drain_group_id'],99)
        self.assertEqual(len(self.state['host_operation']['deregistered_runner_ids']),8)
        self.assertFalse(self.state.get('intentional_deletions'))
        self.fail_describe=False;self.reconcile()
        self.assertEqual(self.state['host_operation']['kind'],'delete')

    def test_rejected_delete_keeps_drain_state_and_can_retry(self):
        self.begin(legacy=True);self.reconcile();self.fail_delete=True;self.reconcile()
        self.assertEqual(self.state['host_operation']['kind'],'drain')
        self.assertFalse(self.state['intentional_deletions'])
        self.fail_delete=False;self.reconcile()
        self.assertEqual(self.state['host_operation']['kind'],'delete')

    def test_timeout_after_successful_runner_removal_recovers_from_fresh_absence(self):
        self.begin();self.timeout_deregister=True;self.reconcile()
        self.assertEqual(len(self.live),7)
        self.state=controller.read_scale_state(self.path)
        self.reconcile();self.reconcile()
        self.assertTrue(any(c[:4]==['gcloud','compute','instances','delete'] for c in self.commands))

    def test_legacy_pending_scale_down_also_drains_before_retrying_delete(self):
        controller.begin_host_operation('delete',self.vm,self.runners,[self.vm,self.other],self.state,self.path)
        self.live[0]=replace(self.live[0],busy=True)
        self.reconcile()
        self.assertEqual(self.state['host_operation']['kind'],'drain')
        self.assertFalse(any(c[:4]==['gcloud','compute','instances','delete'] for c in self.commands))

    def test_preemption_during_drain_remains_an_unexpected_loss(self):
        from lib.fleet_logging import observe_vms
        observe_vms([self.vm,self.other],self.state)
        self.begin()
        observe_vms([self.other],self.state)
        with patch('lib.vm_recovery.cleanup_lost_vm',return_value=False):
            self.reconcile([self.other])
        self.assertFalse(self.state.get('intentional_deletions'))
        self.assertEqual(self.state['host_operation']['kind'],'recovery')
        self.assertEqual(self.state['price_replacement_target'],2)


class RepositoryHostCordonDrainTests(unittest.TestCase):
    """Repository runners keep built-in labels: drain only behind a verified host job-start cordon."""

    row = RunnerDrainTests.row
    command = RunnerDrainTests.command
    begin = RunnerDrainTests.begin
    reconcile = RunnerDrainTests.reconcile

    def setUp(self):
        RunnerDrainTests.setUp(self)
        self.args.target = 'octo/hello-world'
        self.host = {'marker': False, 'workers': 0, 'supported': True, 'reachable': True}
        self.cordon_results = []
        cordon = patch('lib.runner_drain.cordon_runner_host', side_effect=self.cordon)
        self.cordon_mock = cordon.start()
        self.addCleanup(cordon.stop)

    def cordon(self, vm, args, token, names, mutation_guard=None):
        """Model of the host program: unsupported, else place the marker once, else report Workers."""
        self.assertEqual(vm.instance_id, '123')
        if mutation_guard is not None:
            mutation_guard()
        if not self.host['reachable']:
            result = 'unreachable'
        elif not self.host['supported']:
            result = 'unsupported'
        elif not self.host['marker']:
            self.host['marker'] = token
            result = 'placed'
        else:
            result = 'busy' if self.host['workers'] else 'idle'
        self.commands.append(['host-cordon', result, token, tuple(names)])
        self.cordon_results.append(result)
        return result, {'reason': 'model'}

    def mutations(self):
        return [c for c in self.commands if c[0] == 'gh' and '-X' in c and c[c.index('-X') + 1] != 'GET']

    def runner_deletes(self):
        return [c for c in self.mutations() if c[c.index('-X') + 1] == 'DELETE' and not c[-1].endswith('/labels')
                and '/actions/runners/' in c[-1]]

    def test_repository_target_is_detected(self):
        self.assertTrue(controller.is_repo_target(self.args.target))

    def test_host_is_cordoned_before_any_github_change_and_first_observation_never_deregisters(self):
        self.begin()
        first_cordon = next(i for i, c in enumerate(self.commands) if c[0] == 'host-cordon')
        first_mutation = next(i for i, c in enumerate(self.commands) if c in self.mutations())
        self.assertLess(first_cordon, first_mutation)
        self.assertEqual(self.cordon_results, ['placed'])
        self.assertEqual(self.state['host_operation']['host_cordon_token'], 'drain-123')
        self.assertEqual(self.state['host_operation']['cordon_mode'], 'host')
        self.assertEqual(self.runner_deletes(), [])
        self.assertEqual(len(self.live), 8)
        self.assertEqual(self.cordon_mock.call_args.args[3], [r.name for r in self.runners])

    def test_deregistration_only_after_a_later_idle_observation_under_the_cordon(self):
        self.begin()
        # A job assigned before the cordon still runs: GitHub may not show it busy yet, the host does.
        self.host['workers'] = 1
        self.reconcile()
        self.assertEqual(self.cordon_results[-1], 'busy')
        self.assertEqual(self.runner_deletes(), [])
        self.assertFalse(any(c[:4] == ['gcloud', 'compute', 'instances', 'delete'] for c in self.commands))
        self.host['workers'] = 0
        self.reconcile()
        self.assertEqual(self.live, [])
        # Every deregistration in that observation came after an idle cordon verification.
        last_idle = max(i for i, c in enumerate(self.commands) if c[:2] == ['host-cordon', 'idle'])
        deletes = [i for i, c in enumerate(self.commands) if c in self.runner_deletes()]
        self.assertEqual(len(deletes), 8)
        self.assertTrue(all(i > last_idle for i in deletes))
        self.reconcile()
        self.assertTrue(any(c[:4] == ['gcloud', 'compute', 'instances', 'delete'] for c in self.commands))

    def test_job_assigned_under_the_cordon_is_waited_out_not_deleted(self):
        self.begin()
        # An idle listener still accepts an assignment; its hook refuses the job, which fails fast.
        self.live[3] = replace(self.live[3], busy=True)
        self.host['workers'] = 1
        self.reconcile()
        self.assertEqual(self.runner_deletes(), [])
        self.live[3] = replace(self.live[3], busy=False)
        self.host['workers'] = 0
        self.reconcile()
        self.assertEqual(self.live, [])

    def test_unsupported_host_holds_retirement_without_any_github_or_provider_change(self):
        self.host['supported'] = False
        with self.assertLogs('spot_runner_autoscaler', level='ERROR') as logs:
            self.begin()
            for _ in range(3):
                self.reconcile()
        self.assertTrue(any('Retirement held' in line and 'cannot refuse new jobs' in line for line in logs.output))
        self.assertEqual(self.mutations(), [])
        self.assertEqual(len(self.live), 8)
        self.assertTrue(all('spot' in r.labels for r in self.live))
        self.assertFalse(any(c[0] == 'gcloud' for c in self.commands))
        self.assertEqual(self.state['host_operation']['kind'], 'drain')

    def test_unreachable_or_unverified_host_holds_retirement(self):
        self.begin()
        self.host['reachable'] = False
        for _ in range(2):
            self.reconcile()
        self.assertEqual(self.runner_deletes(), [])
        self.assertFalse(any(c[0] == 'gcloud' for c in self.commands))

    def test_lost_marker_is_placed_again_and_needs_another_idle_observation(self):
        self.begin()
        self.host['marker'] = False  # for example, a reboot cleared /run
        self.reconcile()
        self.assertEqual(self.cordon_results[-1], 'placed')
        self.assertEqual(self.runner_deletes(), [])
        self.reconcile()
        self.assertEqual(self.live, [])

    def test_mutation_guard_refusal_prevents_the_cordon(self):
        self.host['reachable'] = False
        self.begin()
        self.host['reachable'] = True
        with patch.object(controller, 'guard_fleet_mutation', side_effect=RuntimeError('held')):
            self.reconcile()
        self.assertFalse(self.host['marker'])
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.state['host_operation']['kind'], 'drain')


class HostCordonProgramTests(unittest.TestCase):
    """Run the real host-side cordon program against a sandboxed Docker, /proc, and cordon directory."""

    def setUp(self):
        import os
        import sys
        from lib import runner_drain
        self.runner_drain = runner_drain
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cordon_dir = root / 'cordon'
        self.cordon_dir.mkdir(mode=0o755)
        self.cordon_dir.chmod(0o755)
        self.proc = root / 'proc'
        self.proc.mkdir()
        self.fixtures = root / 'fixtures'
        self.fixtures.mkdir()
        self.state_dir = root / 'state'
        self.state_dir.mkdir()
        self.hook = (Path(__file__).resolve().parents[1] / 'runner' / 'job-started.sh').read_text()
        (self.fixtures / 'hook.sh').write_text(self.hook)
        docker = root / 'docker'
        docker.write_text(f'#!{sys.executable}\n' + '''import io, os, sys, tarfile
root = os.environ['FAKE_DOCKER_ROOT']
args = sys.argv[1:]
if args[0] == 'inspect':
    path = os.path.join(root, args[-1] + '.json')
    if not os.path.exists(path):
        sys.exit(1)
    sys.stdout.write(open(path).read())
elif args[0] == 'cp':
    data = open(os.path.join(root, 'hook.sh'), 'rb').read()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        info = tarfile.TarInfo('gha-runner-job-started.sh'); info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    sys.stdout.buffer.write(buffer.getvalue())
else:
    sys.exit(2)
''')
        docker.chmod(0o755)
        self.program = runner_drain.host_cordon_program(directory=str(self.cordon_dir), docker=str(docker),
                                                        proc=str(self.proc), owner_uid=os.getuid())
        self.env = {**os.environ, 'FAKE_DOCKER_ROOT': str(self.fixtures)}
        self.names = ['gha-host-1', 'gha-host-2']
        for name in self.names:
            self.container(name)

    def container(self, name, **changes):
        row = {'Name': '/' + name,
               'Mounts': [{'Type': 'bind', 'Source': str(self.cordon_dir), 'Destination': str(self.cordon_dir),
                           'RW': False},
                          {'Type': 'bind', 'Source': str(self.state_dir), 'Destination': '/runner-state', 'RW': True}],
               'Config': {'Env': ['PATH=/usr/bin',
                                  'ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/gha-runner-job-started.sh']}}
        row.update(changes)
        (self.fixtures / (name + '.json')).write_text(json.dumps([row]))
        return row

    def run_program(self, action='cordon', token='drain-123', names=None):
        import sys
        names = self.names if names is None else names
        result = subprocess.run([sys.executable, '-I', '-c', self.program, action, token, *names],
                                env=self.env, capture_output=True, text=True, timeout=60)
        detail = json.loads(result.stdout.splitlines()[-1]) if result.stdout.strip() else {}
        return result.returncode, detail

    @property
    def marker(self):
        return self.cordon_dir / 'draining'

    def worker(self, pid, comm='Runner.Worker'):
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / 'comm').write_text(comm + '\n')

    def test_places_marker_then_reports_idle_and_busy(self):
        self.assertEqual(self.run_program(), (10, {'state': 'placed'}))
        self.assertEqual(self.marker.read_text(), 'drain-123')
        self.worker(77, comm='Runner.Listener')
        self.assertEqual(self.run_program(), (0, {'state': 'idle'}))
        self.worker(78)
        self.assertEqual(self.run_program(), (75, {'state': 'busy', 'workers': 1}))
        # Any owner's marker refuses jobs; it is never replaced while present.
        self.assertEqual(self.run_program(token='rotation-cordon-123')[0], 75)
        self.assertEqual(self.marker.read_text(), 'drain-123')

    def test_unsupported_hosts_are_refused_without_placing_a_marker(self):
        env_hook = ['ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/gha-runner-job-started.sh']
        cordon_mount = {'Type': 'bind', 'Source': str(self.cordon_dir), 'Destination': str(self.cordon_dir)}
        state_mount = {'Type': 'bind', 'Source': str(self.state_dir), 'Destination': '/runner-state', 'RW': True}
        cases = {
            'writable mount': {'Mounts': [{**cordon_mount, 'RW': True}, state_mount]},
            'no mount': {'Mounts': [state_mount]},
            'other source': {'Mounts': [{**cordon_mount, 'Source': '/tmp', 'RW': False}, state_mount]},
            'volume mount': {'Mounts': [{**cordon_mount, 'Type': 'volume', 'RW': False}, state_mount]},
            'no hook env': {'Config': {'Env': ['PATH=/usr/bin']}},
            'other hook': {'Config': {'Env': ['ACTIONS_RUNNER_HOOK_JOB_STARTED=/tmp/hook.sh']}},
            'two hooks': {'Config': {'Env': env_hook + ['ACTIONS_RUNNER_HOOK_JOB_STARTED=/tmp/hook.sh']}},
            'no state mount': {'Mounts': [{**cordon_mount, 'RW': False}]},
            'renamed': {'Name': '/someone-else'},
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.container('gha-host-2', **change)
                code, detail = self.run_program()
                self.assertEqual(code, 76, detail)
                self.assertFalse(self.marker.exists())
        self.container('gha-host-2')
        (self.fixtures / 'gha-host-2.json').unlink()
        self.assertEqual(self.run_program()[0], 76)

    def test_old_hook_or_env_override_or_unsafe_directory_is_unsupported(self):
        (self.fixtures / 'hook.sh').write_text(self.hook.replace(self.runner_drain.HOST_CORDON_CAPABILITY, 'OLD'))
        self.assertEqual(self.run_program()[0], 76)
        (self.fixtures / 'hook.sh').write_text(self.hook)
        (self.state_dir / '.env').write_text('ACTIONS_RUNNER_HOOK_JOB_STARTED=/work/evil.sh\n')
        self.assertEqual(self.run_program()[0], 76)
        (self.state_dir / '.env').unlink()
        self.cordon_dir.chmod(0o777)
        self.assertEqual(self.run_program()[0], 76)
        self.cordon_dir.chmod(0o755)
        self.assertEqual(self.run_program()[0], 10)
        self.assertEqual(self.run_program(names=[])[0], 76)
        self.assertEqual(self.run_program(token='')[0], 76)

    def test_release_removes_only_the_owners_marker(self):
        self.assertEqual(self.run_program(token='rotation-cordon-123')[0], 10)
        self.assertEqual(self.run_program('release', 'drain-123', []), (0, {'state': 'released'}))
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.run_program('release', 'rotation-cordon-123', []), (0, {'state': 'released'}))
        self.assertFalse(self.marker.exists())

    def test_remote_command_checks_identity_and_holds_the_lock(self):
        vm = GceVm('oxidex-runners-test', 'asia-south2-a', 'RUNNING', 'n2-standard-32', True, instance_id='123')
        command = self.runner_drain.host_cordon_command(vm, 'cordon', 'drain-123', ['gha-a-1'])
        self.assertTrue(command.startswith('sudo flock -w 30 -E 75 /run/gha-runner-cordon.lock sh -c '))
        self.assertIn('computeMetadata/v1/instance/id', command)
        self.assertLess(command.index('123'), command.index('python3 -I -c'))
        with self.assertRaises(ValueError):
            self.runner_drain.host_cordon_command(replace(vm, instance_id=''), 'cordon', 'drain-123', ['gha-a-1'])

    def test_ssh_exit_codes_map_to_cordon_states(self):
        vm = GceVm('oxidex-runners-test', 'asia-south2-a', 'RUNNING', 'n2-standard-32', True, instance_id='123')
        args = SimpleNamespace(project='homelab-424523')
        for code, expected in ((0, 'idle'), (10, 'placed'), (75, 'busy'), (76, 'unsupported'), (77, 'identity'),
                               (255, 'unreachable'), (1, 'unreachable')):
            with patch('lib.runner_drain.subprocess.run',
                       return_value=SimpleNamespace(returncode=code, stdout='noise\n{"reason": "x"}\n')) as run:
                state, detail = self.runner_drain.cordon_runner_host(vm, args, 'drain-123', ['gha-a-1'])
            self.assertEqual((state, detail), (expected, {'reason': 'x'}))
            self.assertEqual(run.call_args.args[0][:3], ['gcloud', 'compute', 'ssh'])
        with patch('lib.runner_drain.subprocess.run', side_effect=subprocess.TimeoutExpired('gcloud', 120)):
            self.assertEqual(self.runner_drain.cordon_runner_host(vm, args, 't', ['n'])[0], 'unreachable')
        with patch('lib.runner_drain.subprocess.run', side_effect=AssertionError('no SSH without names')):
            self.assertEqual(self.runner_drain.cordon_runner_host(vm, args, 't', [])[0], 'unsupported')
            with self.assertRaisesRegex(RuntimeError, 'held'):
                self.runner_drain.cordon_runner_host(vm, args, 't', ['n'],
                                                     mutation_guard=lambda: (_ for _ in ()).throw(RuntimeError('held')))
        with patch('lib.runner_drain.subprocess.run',
                   return_value=SimpleNamespace(returncode=0, stdout='{"state": "released"}')):
            self.assertTrue(self.runner_drain.release_runner_host_cordon(vm, args, 't'))
        with patch('lib.runner_drain.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='')):
            self.assertFalse(self.runner_drain.release_runner_host_cordon(vm, args, 't'))


class JobStartedHookCordonTests(unittest.TestCase):
    """The runner image's job-started hook refuses a job while the host cordon marker exists."""

    def setUp(self):
        from lib import runner_drain
        self.runner_drain = runner_drain
        root = Path(__file__).resolve().parents[1]
        self.hook = (root / 'runner' / 'job-started.sh').read_text()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / 'draining'
        self.script = Path(self.tmp.name) / 'job-started.sh'
        self.assertEqual(self.hook.count(runner_drain.HOST_CORDON_MARKER), 1)
        self.script.write_text(self.hook.replace(runner_drain.HOST_CORDON_MARKER, str(self.marker)))

    def run_hook(self, admission='0'):
        import os
        return subprocess.run(['bash', str(self.script)], capture_output=True, text=True, timeout=30,
                              env={**os.environ, 'GHA_ADMISSION_REQUIRED': admission})

    def test_marker_refuses_the_job_before_admission(self):
        self.marker.write_text('drain-123')
        result = self.run_hook(admission='1')  # admission would otherwise exec the (absent) admission client
        self.assertEqual(result.returncode, 1)
        self.assertIn('::error title=Runner host is being retired::', result.stderr)
        self.assertIn('Re-run the job', result.stderr)

    def test_without_marker_the_hook_proceeds(self):
        self.assertEqual(self.run_hook().returncode, 0)

    def test_hook_carries_the_capability_and_checks_before_admission(self):
        self.assertIn(self.runner_drain.HOST_CORDON_CAPABILITY, self.hook)
        self.assertLess(self.hook.index(self.runner_drain.HOST_CORDON_MARKER), self.hook.index('gha-host-admission.py'))

    def test_runner_containers_mount_the_cordon_directory_read_only(self):
        from lib.runner_assets import RUNNER_DOCKERFILE, STARTUP_SCRIPT
        directory = self.runner_drain.HOST_CORDON_DIR
        mount = f'-v {directory}:{directory}:ro'
        self.assertEqual(STARTUP_SCRIPT.count(mount), 1)
        loop = STARTUP_SCRIPT.index('for i in $(seq 1 "$RUNNER_COUNT"); do')
        self.assertLess(STARTUP_SCRIPT.index(f'install -d -o root -g root -m 0755 {directory}'), loop)
        # Only the long-running listener container gets it, not the one-shot configure container.
        args_start = STARTUP_SCRIPT.index('docker_args=(-d --restart unless-stopped')
        self.assertLess(args_start, STARTUP_SCRIPT.index(mount))
        self.assertLess(STARTUP_SCRIPT.index(mount), STARTUP_SCRIPT.index('docker run "${docker_args[@]}"'))
        self.assertIn(f'ENV ACTIONS_RUNNER_HOOK_JOB_STARTED={self.runner_drain.JOB_STARTED_HOOK}', RUNNER_DOCKERFILE)
