"""Bounded metadata controls: no provider, guest, process or compilation calls."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock,patch

from lib import fleet_migration as migration
from lib import spot_runner_autoscaler as controller
from lib import tier_controller
from lib.runner_fleet import GceVm,GitHubRunner


class LowMigrationDeferralTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'state.json';self.tick=1000
        self.args=controller.args_parser().parse_args(['--scaling-mode','tiered','--remote-builder',
            '--tier-job-memory-gib','15','--job-repository','org/oxidex','--org','org'])
        self.vm=GceVm('oxidex-runners-original','z','RUNNING','n4-standard-16',True,16,64,instance_id='721',
            disks=({'source':'https://www.googleapis.com/compute/v1/projects/p/zones/z/disks/data',
                    'diskId':'22','deviceName':'runner-data-original','boot':False,'diskSizeGb':100,
                    'diskType':'hyperdisk-balanced','provisionedIops':3000,'provisionedThroughput':140},))
        self.vms=[self.vm]
        self.runners=[GitHubRunner(i,f'gha-{self.vm.name}-{i}','online',False,('X64','n4-standard-16')) for i in range(1,21)]
        self.state={'desired_topology':['large'],'host_deployments':{'z/721':dict(tier='large',expected_runner_count=20,runner_memory_gib=15)},
                    'tier_policy':dict(direction='down',streak=6,timestamp=1000,changed_at=0)}
        self.polls();self.discovery_complete=True;self.offers=[];self.scans=0;self.cpu=.02
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('time.time',side_effect=lambda:self.tick))
        self.stack.enter_context(patch.object(controller.subprocess,'run',side_effect=AssertionError('provider command forbidden')))
        self.stack.enter_context(patch.object(controller,'read_utilization',side_effect=lambda *_:[(self.cpu,.05)]))
        self.stack.enter_context(patch.object(tier_controller,'read_host_events',side_effect=lambda *_:self.health()))
        self.stack.enter_context(patch.object(controller,'check_running_prices',side_effect=self.pricing))
        self.stack.enter_context(patch.object(controller,'discover_tier_offers',side_effect=self.discover))
        self.launch=self.stack.enter_context(patch.object(controller,'scale_up',side_effect=AssertionError('launch forbidden')))
        self.drain=self.stack.enter_context(patch.object(controller,'begin_host_operation',side_effect=AssertionError('drain forbidden')))
        self.stack.enter_context(patch.object(controller,'builder_ready',side_effect=AssertionError('builder execution forbidden')))
        migration.begin_migration(self.vms,('expanded',),self.state,self.path,'low')
        self.entry=json.loads(json.dumps(self.state['migration']))

    def polls(self):
        observed=datetime.fromtimestamp(self.tick,timezone.utc).isoformat()
        self.state['job_tracking']={'last_poll_by_repository':{'org/oxidex':observed},'capacity_jobs':{},
            'active_coverage_by_repository':{'org/oxidex':dict(schema=1,observed_at=observed,complete=True,
                enumerated_statuses=['in_progress','queued','waiting','pending','requested'],unavailable_run_ids=[])}}

    def health(self):
        return dict(instance_id='721',boot_id='boot',complete=True,observed_at=self.tick,
                    boot_oom_complete=True,boot_host_oom_ids=[],events=[],io_complete=True,
                    io_pressure={scope+'_'+name:0 for scope in ('full','some') for name in ('avg10','avg60','avg300','total')})

    def pricing(self,*_,**kwargs):kwargs['totals'].update({vm.name:Decimal('2.8364') for vm in controller.spot_instances(self.vms)})

    def discover(self,*_,status=None):
        self.scans+=1
        if status is not None:status['complete']=self.discovery_complete
        return self.offers

    def check(self):tier_controller.check_tiers(self.vms,self.runners,self.args,self.state,self.path)

    def restart(self):self.state=json.loads(self.path.read_text())

    def test_composed_defer_json_restart_preserves_source_and_next_cpu_growth(self):
        self.check()
        self.assertNotIn('migration',self.state)
        self.assertEqual(self.state['desired_topology'],['large'])
        self.assertEqual(self.state['tier_policy'],dict(direction='down',streak=0,timestamp=1000,changed_at=0))
        self.assertEqual(json.loads(json.dumps(self.state['migration_history'][-1]['migration'])),self.entry)
        self.assertEqual(self.state['host_deployments']['z/721']['expected_runner_count'],20)
        histories=copy.deepcopy({k:self.state[k] for k in ('resource_history','io_history','host_health','seen_oom_ids')})
        self.restart()
        self.assertEqual({k:self.state[k] for k in histories},histories)
        self.cpu=.90
        for tick in (1300,1600):self.tick=tick;self.polls();self.check();self.restart()
        self.assertEqual(self.state['migration']['reason'],'cpu')
        self.assertEqual(self.state['migration']['target'],['bulky'])
        self.launch.assert_not_called();self.drain.assert_not_called()

    def test_six_real_down_windows_are_required_before_another_unavailable_plan(self):
        self.check();self.assertNotIn('migration',self.state);self.restart()
        for tick in (1300,1600,1900,2200,2500):
            self.tick=tick;self.polls();self.check();self.restart()
            self.assertEqual(self.scans,1);self.assertNotIn('migration',self.state)
        self.tick=2800;self.polls();self.check();self.restart()
        self.assertEqual(self.scans,2);self.assertNotIn('migration',self.state)
        self.assertEqual(len(self.state['migration_history']),2)
        self.assertEqual(self.state['tier_policy']['streak'],0)

    def test_known_host_oom_ledger_survives_deferral_then_normal_capacity_response(self):
        identity='a'*64
        from lib.oom_obligations import remember_oom
        remember_oom(self.state,'z/721','host','boot',[identity])
        before=copy.deepcopy(self.state['pending_oom_obligations'])
        self.check();self.assertNotIn('migration',self.state)
        self.assertEqual(self.state['pending_oom_obligations'],before)
        self.restart();self.tick=1300;self.polls();self.check()
        self.assertEqual(self.state['migration']['reason'],'oom')
        self.assertEqual(self.state['pending_oom_obligations'],before)

    def test_legacy_entry_without_marker_can_defer_only_complete_current_no_effect_plan(self):
        self.state['migration'].pop('launch_attempted',None)
        self.state['migration'].pop('initial_deployments',None)
        self.check();self.assertNotIn('migration',self.state)

    def test_partial_discovery_and_no_status_never_defer(self):
        for status in (False,None):
            with self.subTest(status=status):
                self.discovery_complete=status;self.check()
                self.assertIn('migration',self.state);self.assertNotIn('migration_history',self.state)

    def test_entry_awaiting_is_not_erased_into_deferral_permission(self):
        self.state['migration']['awaiting_launch']=dict(name='absent-attempt',zone='z')
        self.check();self.assertIn('migration',self.state)
        self.assertNotIn('migration_history',self.state)
        self.restart();self.check();self.restart()
        self.assertIn('migration',self.state)
        self.assertTrue(self.state['migration'].get('launch_attempted'))
        self.assertNotIn('migration_history',self.state)

    def test_low_downsize_with_equal_cpu_or_equal_ram_can_defer(self):
        for source,target in (('expanded','starting'),('starting','small')):
            with self.subTest(source=source,target=target):
                tier=migration.TIERS[source];self.vm=replace(self.vm,vcpu=tier.vcpu,ram_gib=tier.ram_gib,machine_type=f'n2-standard-{tier.vcpu}')
                self.vms=[self.vm];self.args.tier_job_memory_gib=2
                self.runners=[GitHubRunner(i,f'gha-{self.vm.name}-{i}','online',False,('X64',self.vm.machine_type)) for i in range(1,tier.initial_slots+1)]
                self.state.pop('migration',None);self.state.pop('migration_history',None)
                self.state['desired_topology']=[source]
                self.state['host_deployments']['z/721']=dict(tier=source,expected_runner_count=tier.initial_slots,runner_memory_gib=2)
                migration.begin_migration(self.vms,(target,),self.state,self.path,'low')
                self.check();self.restart();self.assertNotIn('migration',self.state)
                self.assertEqual(self.state['desired_topology'],[source])

    def test_invalid_current_evidence_or_recorded_effects_hold(self):
        baseline=copy.deepcopy(self.state)
        for defect in ('reason','marker','paused','partial_queue','changed_disk','missing_source','extra_vm','offline_runner',
                       'missing_runner','runner_name','partial_jobs','stale_jobs','extra_repository','health','boot','kernel',
                       'future_metric','stale_metric','negative_metric','invalid_io','unknown','deployment','desired','receipt',
                       'deletion_name','deletion_id','active_scaling','failed_candidate','completed_migration','later_history',
                       'retained_ids','duplicate_record','extra_nonspot','coverage','coverage_scope','coverage_unavailable'):
            with self.subTest(defect=defect):
                self.state=copy.deepcopy(baseline);self.vms=[self.vm];self.runners=[GitHubRunner(i,f'gha-{self.vm.name}-{i}','online',False,('X64','n4-standard-16')) for i in range(1,21)]
                self.args.job_repository=['org/oxidex'];extra=[]
                if defect=='reason':self.state['migration']['reason']='loss recovery'
                if defect=='marker':self.state['migration']['launch_attempted']=True
                if defect=='paused':self.state['migration']['paused_retirement']={'vm':{'instance_id':'721'}}
                if defect=='partial_queue':self.state['migration']['pending_slots']=[4]
                if defect=='changed_disk':self.vms=[replace(self.vm,disks=({'diskId':'foreign'},))]
                if defect=='missing_source':self.vms=[]
                if defect=='extra_vm':self.vms.append(replace(self.vm,name='oxidex-runners-extra',instance_id='foreign'))
                if defect=='extra_nonspot':self.vms.append(replace(self.vm,name='oxidex-runners-extra',instance_id='foreign',spot=False))
                if defect=='offline_runner':self.runners[0]=replace(self.runners[0],status='offline')
                if defect=='missing_runner':self.runners.pop()
                if defect=='runner_name':self.runners[0]=replace(self.runners[0],name='altered-'+self.vm.name)
                if defect=='partial_jobs':self.state['job_tracking']['last_poll_by_repository']={}
                if defect=='stale_jobs':self.state['job_tracking']['last_poll_by_repository']['org/oxidex']=datetime.fromtimestamp(0,timezone.utc).isoformat()
                if defect=='extra_repository':self.args.job_repository=['org/oxidex','org/second']
                if defect=='coverage':self.state['job_tracking'].pop('active_coverage_by_repository')
                if defect=='coverage_scope':self.state['job_tracking']['active_coverage_by_repository']['org/oxidex']['enumerated_statuses']=['in_progress','queued']
                if defect=='coverage_unavailable':self.state['job_tracking']['active_coverage_by_repository']['org/oxidex']['unavailable_run_ids']=[7]
                if defect=='deployment':self.state['host_deployments']['z/721']['runner_memory_gib']=8
                if defect=='desired':self.state['desired_topology']=['bulky']
                if defect=='receipt':self.state['migration']['completed_retirements']={'721':{'started_at':1000}}
                if defect=='deletion_name':self.state['intentional_deletions']=['z/'+self.vm.name]
                if defect=='deletion_id':self.state['intentional_deletion_ids']={'z/'+self.vm.name:'721'}
                if defect=='active_scaling':self.state['scale_up_active']=True
                if defect=='failed_candidate':self.state['failed_launch_candidates']={'candidate':1000}
                if defect=='completed_migration':self.state['migration_completed_at']=1000
                if defect=='later_history':self.state['migration_history']=[{'retargeted_at':1000,'migration':{'started_at':1000}}]
                if defect=='retained_ids':self.state['migration']['retained_ids']=['721']
                if defect=='duplicate_record':self.state['migration']['retained_records']=[copy.deepcopy(self.state['migration']['sources'][0])]
                if defect in ('health','boot','kernel','invalid_io'):
                    observed=self.health()
                    if defect=='health':observed['complete']=False
                    if defect=='boot':observed['boot_id']=''
                    if defect=='kernel':observed['boot_oom_complete']=False
                    if defect=='invalid_io':observed['io_pressure']['full_avg10']=True
                    extra.append(patch.object(tier_controller,'read_host_events',return_value=observed))
                if defect in ('future_metric','stale_metric','negative_metric'):
                    original=tier_controller.record_resources
                    def metrics(*args,**kwargs):
                        rows=original(*args,**kwargs)
                        if defect=='negative_metric':rows[0]['peak_ram_gib']=-1
                        else:rows[0]['observed_at']=self.tick+(1 if defect=='future_metric' else -451)
                        return rows
                    extra.append(patch.object(tier_controller,'record_resources',side_effect=metrics))
                if defect=='unknown':self.state['pending_oom']={'z/721':'unknown'}
                prior_history=copy.deepcopy(self.state.get('migration_history'))
                with ExitStack() as stack:
                    for context in extra:stack.enter_context(context)
                    self.check()
                self.assertIn('migration',self.state);self.assertEqual(self.state.get('migration_history'),prior_history)
        self.launch.assert_not_called();self.drain.assert_not_called()

    def test_old_receipts_and_unmanaged_benchmark_do_not_block_current_capacity_retention(self):
        self.state['migration'].pop('launch_attempted');self.state['migration'].pop('initial_deployments')
        self.state.update(failed_launch_candidates={'old':999},migration_completed_at=999,
                          migration_history=[{'retargeted_at':999,'migration':{'started_at':900}}],
                          intentional_deletions=['elsewhere/retired'],intentional_deletion_ids={'elsewhere/retired':'old'})
        self.state['host_deployments']['elsewhere/old']={'tier':'bulky','expected_runner_count':20,'runner_memory_gib':15}
        self.vms.append(replace(self.vm,name='benchmark-other',instance_id='outside',vcpu=32,ram_gib=128))
        self.check();self.assertNotIn('migration',self.state)

    def test_exact_retained_and_replaced_sources_survive_json_restart_deferral(self):
        bulky=replace(self.vm,name='oxidex-runners-bulky',instance_id='722',machine_type='n4-standard-32',vcpu=32,ram_gib=128,
                      disks=(dict(self.vm.disks[0],diskId='23',deviceName='runner-data-bulky',source=self.vm.disks[0]['source']+'-bulky'),))
        self.vms.append(bulky)
        self.runners.extend(GitHubRunner(i+20,f'gha-{bulky.name}-{i}','online',False,('X64','n4-standard-32')) for i in range(1,21))
        self.state.pop('migration');self.state['desired_topology']=['bulky','large']
        self.state['host_deployments']['z/722']=dict(tier='bulky',expected_runner_count=20,runner_memory_gib=15)
        migration.begin_migration(self.vms,('large','expanded'),self.state,self.path,'low')
        self.restart();entry=copy.deepcopy(self.state['migration'])
        with patch.object(tier_controller,'read_host_events',side_effect=lambda _project,vm,_cursor:dict(self.health(),instance_id=vm.instance_id)),\
             patch.object(controller,'read_utilization',side_effect=lambda _project,fleet,_timestamp:[(.02,.05) for _ in fleet]):
            self.check();self.restart()
        self.assertNotIn('migration',self.state)
        self.assertEqual(self.state['desired_topology'],['bulky','large'])
        self.assertEqual(self.state['migration_history'][-1]['migration'],entry)
        self.assertEqual(set(self.state['host_deployments']),{'z/721','z/722'})

    def test_archive_is_bounded_without_claiming_migration_completion(self):
        previous=[{'deferred_at':tick,'migration':{'started_at':tick-100}} for tick in (700,800,900)]
        self.state.update(migration_history=copy.deepcopy(previous),migration_completed_at=900)
        self.state.pop('desired_topology')
        self.check();self.restart()
        self.assertNotIn('migration',self.state)
        self.assertEqual(len(self.state['migration_history']),3)
        self.assertEqual(self.state['migration_history'][:2],previous[1:])
        self.assertEqual(self.state['migration_completed_at'],900)
        self.assertEqual(self.state['desired_topology'],['large'])

    def test_deferral_write_failure_keeps_original_migration_and_policy(self):
        from lib.fleet_evidence import record_resources,record_io_pressure
        self.state['latest_resources']=record_resources(self.state,self.vms,[(.02,.05)],self.tick)
        self.state['host_health']={'z/721':self.health()}
        record_io_pressure(self.state,self.state['latest_resources'],self.state['host_health'],self.tick)
        self.state['latest_workload']={'complete':True}
        before=copy.deepcopy(self.state)
        with patch.object(controller,'write_scale_state',side_effect=OSError('atomic write failed')):
            with self.assertRaises(OSError):migration.defer_unused_low_plan(copy.deepcopy(self.state['migration']),self.vms,self.runners,self.args,self.state,self.path,{'complete':True})
        self.assertEqual(self.state,before)

    def test_launch_attempt_is_durable_before_dispatch_and_cannot_later_defer(self):
        self.offers=[type('Offer',(),{'total_daily_usd':Decimal('1.20')})()]
        def dispatch(*_args,**_kwargs):
            self.assertTrue(json.loads(self.path.read_text())['migration'].get('launch_attempted'))
        self.launch.side_effect=dispatch
        self.check();self.assertTrue(self.state['migration'].get('launch_attempted'));self.restart()
        self.offers=[];self.check();self.assertIn('migration',self.state)


class DiscoveryCompletenessTests(unittest.TestCase):
    def run_discovery(self, errors=(), inventory_error=False):
        status={};offer=type('Offer',(),dict(vcpu=8,ram_gib=32,region='r',machine_type='n2-standard-8'))()
        def find(*_args,**kwargs):
            kwargs['status'].update(inventory_complete=True,missing_compute_scopes=[])
            return 'metadata-scan'
        with ExitStack() as stack:
            for name,value in (('clear_catalog_cache',None),('get_auth',None),('parse_component_rates',None),
                    ('catalog_skus',[]),('find','metadata-scan'),('storage_rates',{kind+':capacity':Decimal('.1') for kind in ('pd-standard','pd-balanced','pd-ssd')}),('eligible_tier_offers',[]),
                    ('launchable_candidates',([],[]))):
                stack.enter_context(patch.object(controller,name,new=Mock(return_value=value)))
            stack.enter_context(patch.object(controller.asyncio,'run',return_value=([offer],None,None,errors)))
            stack.enter_context(patch.object(controller,'find',new=Mock(side_effect=find)))
            stack.enter_context(patch.object(controller,'load_launch_inventory',side_effect=RuntimeError('partial inventory') if inventory_error else None,return_value=None))
            stack.enter_context(patch.object(controller.subprocess,'run',side_effect=AssertionError('provider operation forbidden')))
            try:
                result=controller.discover_tier_offers('p',migration.TIERS['expanded'],100,15,status=status)
            except TypeError:
                self.fail('discovery does not support explicit per-call completeness status')
            except RuntimeError:
                self.assertTrue(inventory_error);result=None
        return result,status

    def test_complete_empty_discovery_reports_true(self):
        result,status=self.run_discovery();self.assertEqual(result,[]);self.assertIs(status.get('complete'),True)

    def test_partial_price_discovery_reports_false(self):
        result,status=self.run_discovery(errors=['partial-price']);self.assertEqual(result,[]);self.assertIs(status.get('complete'),False)

    def test_inventory_exception_leaves_incomplete_status(self):
        result,status=self.run_discovery(inventory_error=True);self.assertIsNone(result);self.assertIs(status.get('complete'),False)


if __name__=='__main__':unittest.main()
