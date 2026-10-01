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
        self.args = controller.args_parser().parse_args(['--state-file', str(self.path)])
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

    def begin(self):
        with patch('lib.spot_runner_autoscaler.removable_spot_vms',return_value=[self.vm]):
            controller.scale_down([self.vm,self.other],self.runners,self.args,self.state,self.path)

    def reconcile(self, vms=None):
        controller.reconcile_host_operation(vms if vms is not None else [self.vm,self.other],
                                            list(self.live),self.args,self.state,self.path)

    def test_labels_are_removed_before_waiting_and_deletion_requires_fresh_absence(self):
        self.begin()
        self.assertIn('host_operation',self.state)
        self.assertEqual(self.state['host_operation']['kind'],'drain')
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))
        self.assertTrue(all(r.labels==('self-hosted','Linux','X64') for r in self.live))
        self.assertEqual(self.members,set(range(1,9)))
        self.reconcile()
        self.assertEqual(self.live,[])
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))
        self.reconcile()
        self.assertTrue(any(c[:4]==['gcloud','compute','instances','delete'] for c in self.commands))
        self.assertEqual(self.state['host_operation']['kind'],'delete')
        self.reconcile([self.other])
        self.assertNotIn('host_operation',self.state)
        self.assertIsNone(self.group)

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
        self.fail_label=True;self.begin()
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))
        self.state=controller.read_scale_state(self.path)
        self.fail_label=False;self.reconcile()
        self.assertEqual(self.members,set(range(1,9)))
        self.assertTrue(all('spot' not in r.labels for r in self.live))

    def test_group_with_repository_access_blocks_deregistration(self):
        self.begin();self.repo_access=[{'id':999}]
        self.reconcile()
        self.assertEqual(len(self.live),8)
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))

    def test_group_with_public_repository_access_blocks_deregistration(self):
        self.begin();self.group['allows_public_repositories']=True
        self.reconcile()
        self.assertEqual(len(self.live),8)
        self.assertFalse(any(c[0]=='gcloud' for c in self.commands))

    def test_group_with_unknown_public_repository_access_blocks_deregistration(self):
        self.begin();self.group.pop('allows_public_repositories')
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
        self.assertEqual(controller.removable_spot_vms([self.vm,self.other], self.runners[:7]+[docker],0,0),[])

    def test_custom_runner_prefix_with_all_eight_slots_can_be_drained(self):
        assigned=[replace(r,name=r.name.replace('gha-','oxidex-example-',1)) for r in self.runners]
        self.assertEqual(controller.removable_spot_vms([self.vm,self.other],assigned,0,0),[self.vm])

    def test_two_incomplete_prefixes_do_not_prove_all_slots_registered(self):
        assigned=[replace(r,name=r.name.replace('gha-','first-' if r.runner_id<=4 else 'second-',1)) for r in self.runners]
        self.assertEqual(controller.removable_spot_vms([self.vm,self.other],assigned,0,0),[])

    def test_changed_provider_identity_blocks_deletion_without_intent(self):
        self.begin();self.reconcile();self.provider_id='999';self.reconcile()
        self.assertFalse(any(c[:4]==['gcloud','compute','instances','delete'] for c in self.commands))
        self.assertFalse(self.state.get('intentional_deletions'))

    def test_provider_read_rejection_preserves_drain_and_group_identity(self):
        self.begin();self.reconcile();self.fail_describe=True;self.reconcile()
        self.state=controller.read_scale_state(self.path)
        self.assertEqual(self.state['host_operation']['kind'],'drain')
        self.assertEqual(self.state['host_operation']['drain_group_id'],99)
        self.assertEqual(len(self.state['host_operation']['deregistered_runner_ids']),8)
        self.assertFalse(self.state.get('intentional_deletions'))
        self.fail_describe=False;self.reconcile()
        self.assertEqual(self.state['host_operation']['kind'],'delete')

    def test_rejected_delete_keeps_drain_state_and_can_retry(self):
        self.begin();self.reconcile();self.fail_delete=True;self.reconcile()
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
