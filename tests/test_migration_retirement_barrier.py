import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from lib import fleet_migration as migration,spot_runner_autoscaler as controller
from lib.runner_fleet import GceVm,GitHubRunner


def host(name,cpu,ram,identity):
    return GceVm('oxidex-runners-'+name,'z','RUNNING','n2-standard-'+str(cpu),True,cpu,ram,instance_id=identity,
        disks=({'source':'https://www.googleapis.com/compute/v1/projects/homelab-424523/zones/z/disks/disk-'+identity,'diskId':identity+'0','boot':False,'deviceName':'runner-data-'+name,
                'diskType':'pd-balanced','diskSizeGb':100},))


class MigrationRetirementBarrierTests(unittest.TestCase):
    def setUp(self):
        verified_disks=patch('lib.vm_recovery.cleanup_lost_vm',return_value=True)
        verified_disks.start();self.addCleanup(verified_disks.stop)
        from lib.tier_controller import evidence_for
        self.real_evidence=evidence_for
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'state.json'
        self.args=controller.args_parser().parse_args(['--scaling-mode','tiered','--state-file',str(self.path)])
        self.large=host('retained',16,64,'1');self.small=host('source',4,16,'2')
        self.hosts=[self.large,self.small];self.state={}
        self.runners=[GitHubRunner(i,f'gha-{self.large.name}-{i}','online',False,()) for i in range(1,9)]

    def evidence(self,tick):
        self.state['latest_workload']={'complete':True}
        self.state['latest_resources']=[dict(instance_id=vm.instance_id,zone=vm.zone,observed_at=tick,windows=6,
            peak_cpu_cores=1,peak_ram_gib=2,cpu_fraction=.1,memory_fraction=.1,io_complete=True,
            io_pressure_peaks=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0)) for vm in self.hosts]
        self.state['host_health']={f'z/{vm.instance_id}':dict(instance_id=vm.instance_id,boot_id='boot-'+vm.instance_id,
            complete=True,boot_oom_complete=True,boot_host_oom_ids=[],observed_at=tick,events=[]) for vm in self.hosts}

    def removal(self):
        migration.begin_migration(self.hosts,('large',),self.state,self.path,'consolidation')
        self.state['host_deployments']={'z/1':dict(tier='large',expected_runner_count=8,runner_memory_gib=15)}
        for tick in (1000,1300):
            self.evidence(tick)
            with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=True):
                migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
        self.assertEqual(self.state['host_operation']['vm']['instance_id'],'2')
        self.state=json.loads(self.path.read_text())

    def test_removal_only_lost_retained_host_after_json_restart_does_not_delete_last_source(self):
        self.removal();self.hosts=[self.small];self.evidence(1600)
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=[]),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,1600)),\
             patch('lib.runner_drain.drain_runners',return_value=True),\
             patch.object(controller,'quiesce_remote_builder',return_value=True),\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=1600):
            controller.reconcile_host_operation(self.hosts,[],self.args,self.state,self.path)
        delete.assert_not_called()
        self.assertNotIn('host_operation',self.state)
        self.assertEqual(self.state['migration']['paused_retirement']['vm']['instance_id'],'2')

    def test_additive_host_oom_growth_completes_without_clearing_retained_oom(self):
        old=host('oom',32,128,'1');new=host('added',8,32,'2');self.hosts=[old,new]
        self.runners=[GitHubRunner(i,f'gha-{vm.name}-{i}','online',False,()) for vm in self.hosts for i in range(1,9)]
        migration.begin_migration([old],('bulky','expanded'),self.state,self.path,'global OOM relief')
        self.state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['2'],phase='healthy')
        self.state['host_deployments']={'z/1':dict(tier='bulky',expected_runner_count=8,runner_memory_gib=15),
                                       'z/2':dict(tier='expanded',expected_runner_count=8,runner_memory_gib=15)}
        identity='a'*64
        for tick in (1000,1300):
            self.evidence(tick)
            self.state['host_health']['z/1']['boot_host_oom_ids']=[identity]
            self.state['pending_oom']={'z/1':'host'}
            self.state['pending_oom_origins']={'z/1':dict(kind='host',boot_id='boot-1',event_ids=[identity],quarantined=False)}
            with patch('lib.fleet_migration.time.time',return_value=tick),\
                 patch.object(controller,'builder_ready',return_value=True),\
                 patch.object(controller,'begin_host_operation') as drain:
                migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
            drain.assert_not_called()
            if tick==1000:
                self.assertIn('migration',self.state);self.state=json.loads(self.path.read_text())
        self.assertNotIn('migration',self.state)
        self.assertEqual(self.state['desired_topology'],['bulky','expanded'])
        self.assertEqual(self.state['pending_oom'],{'z/1':'host'})
        self.assertEqual(self.state['pending_oom_origins']['z/1']['event_ids'],[identity])

    def test_every_drained_deletion_retry_holds_changed_source_and_unproven_destination_evidence(self):
        from dataclasses import replace
        for defect in ('source_disk','source_boot','source_kernel','source_unknown','source_unknown_event','source_global',
                       'destination_boot','destination_global','destination_io','jobs','peak','identity'):
            with self.subTest(defect=defect):
                self.setUp();self.removal();self.state['host_operation'].update(kind='delete',drained=True)
                controller.write_scale_state(self.path,self.state);self.state=json.loads(self.path.read_text())
                self.evidence(1600)
                if defect=='source_disk':self.hosts[1]=replace(self.small,disks=({'source':'changed','diskId':'x'},))
                if defect=='source_boot':self.state['host_health']['z/2']['boot_id']=None
                if defect=='source_kernel':self.state['host_health']['z/2']['boot_oom_complete']=False
                if defect=='source_unknown':self.state['pending_oom']={'z/2':'unknown'}
                if defect=='source_unknown_event':self.state['host_health']['z/2']['events']=[dict(id='unmapped',kind='container',workload='unknown')]
                if defect=='source_global':self.state['host_health']['z/2']['boot_host_oom_ids']=['new-global']
                if defect=='destination_boot':self.state['host_health']['z/1']['boot_id']='new-boot'
                if defect=='destination_global':self.state['host_health']['z/1']['boot_host_oom_ids']=['global']
                if defect=='destination_io':self.state['latest_resources'][0]['io_complete']=False
                if defect=='jobs':self.state['latest_workload']['complete']=False
                if defect=='peak':self.state['latest_resources'][0]['peak_cpu_cores']=20
                if defect=='identity':self.hosts[0]=replace(self.large,instance_id='different')
                with patch.object(controller,'list_gcloud_vms',return_value=self.hosts) as provider,\
                     patch.object(controller,'list_github_runners',return_value=self.runners),\
                     patch('lib.tier_controller.evidence_for',return_value=(None,None,1600)) as telemetry,\
                     patch.object(controller,'builder_ready',return_value=True),\
                     patch.object(controller,'delete_drained_vm') as delete,\
                     patch('lib.fleet_migration.time.time',return_value=1600):
                    for _ in range(2):controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
                delete.assert_not_called()
                self.assertGreaterEqual(provider.call_count,1);self.assertEqual(provider.call_count,telemetry.call_count)

    def test_delete_phase_survivor_loss_pauses_then_restores_original_receipt_after_normal_recovery(self):
        self.removal();self.state['host_operation'].update(kind='delete',drained=True,runners_isolated=True,drain_group_id=77)
        saved=copy.deepcopy(self.state['host_operation']);controller.write_scale_state(self.path,self.state)
        self.state=json.loads(self.path.read_text());self.hosts=[self.small];self.evidence(1600)
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=[]),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,1600)),\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=1600):
            controller.reconcile_host_operation(self.hosts,[],self.args,self.state,self.path)
        delete.assert_not_called();self.assertNotIn('host_operation',self.state)
        self.state=json.loads(self.path.read_text())
        self.state['pending_vm_recovery']={'lost':dict(instance_id='1',_cleanup_complete=False)}
        with patch.object(controller,'scale_up') as launch:
            migration.reconcile_migration(self.hosts,[],self.args,self.state,self.path);launch.assert_not_called()
        self.state['pending_vm_recovery']['lost']['_cleanup_complete']=True
        def prices(vms,project,cap,totals):
            from decimal import Decimal
            totals.update({vm.name:Decimal('.4') for vm in vms})
        from decimal import Decimal
        with patch.object(controller,'check_running_prices',side_effect=prices),\
             patch.object(controller,'discover_tier_offers',return_value=[type('Offer',(),{'total_daily_usd':Decimal('2.9')})()]),\
             patch.object(controller,'scale_up') as launch,patch('lib.fleet_migration.time.time',return_value=1600):
            migration.reconcile_migration(self.hosts,[],self.args,self.state,self.path);launch.assert_called_once()
        new=host('restored',16,64,'3');self.hosts.append(new)
        self.state['migration']['pending_tiers']=[];self.state['migration']['pending_slots']=[]
        self.state['migration']['replacement_ids']=['3']
        self.state['host_deployments']['z/3']=dict(tier='large',expected_runner_count=8,runner_memory_gib=15)
        self.runners=[GitHubRunner(i,f'gha-{new.name}-{i}','online',False,()) for i in range(1,9)]
        for tick in (1900,2200):
            self.evidence(tick)
            with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=True):
                migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
            if tick==1900:self.assertNotIn('host_operation',self.state)
        self.assertEqual(self.state['host_operation'],saved)
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=self.runners),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,2200)),\
             patch.object(controller,'builder_ready',return_value=True),\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=2200):
            controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
        delete.assert_called_once();self.assertEqual(delete.call_args.args[0].instance_id,'2')

    def test_recorded_same_boot_global_source_can_retire_only_as_larger_ram_oom_response(self):
        for defect in (None,'new_global','quarantine','reason','same_ram','boot'):
            with self.subTest(defect=defect):
                self.setUp();source=host('oom-source',32 if defect=='same_ram' else 16,128 if defect=='same_ram' else 64,'1');new=host('oom-destination',32,128,'2')
                self.hosts=[source,new];self.evidence(1000);identity='a'*64
                self.state['host_health']['z/1']['boot_host_oom_ids']=[identity]
                self.state['pending_oom']={'z/1':'host'}
                self.state['pending_oom_origins']={'z/1':dict(kind='host',boot_id='boot-1',event_ids=[identity],quarantined=defect=='quarantine')}
                migration.begin_migration([source],('bulky',),self.state,self.path,'oom' if defect!='reason' else 'price replacement',force_ids=['1'])
                self.state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['2'])
                self.state['host_deployments']={'z/2':dict(tier='bulky',expected_runner_count=8,runner_memory_gib=15)}
                self.state=json.loads(self.path.read_text());self.state.update(host_deployments={'z/2':dict(tier='bulky',expected_runner_count=8,runner_memory_gib=15)})
                self.state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['2'])
                self.runners=[GitHubRunner(i,f'gha-{new.name}-{i}','online',False,()) for i in range(1,9)]
                for tick in (1300,1600):
                    self.evidence(tick);self.state['host_health']['z/1']['boot_host_oom_ids']=[identity]
                    if defect=='new_global':self.state['host_health']['z/1']['boot_host_oom_ids'].append('b'*64)
                    if defect=='boot':self.state['host_health']['z/1']['boot_id']='other-boot'
                    with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=True):
                        migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
                self.assertEqual('host_operation' in self.state,defect is None)
                self.assertEqual(self.state['pending_oom'],{'z/1':'host'})

    def test_high_demand_additive_growth_settles_so_policy_can_continue_scaling(self):
        old=host('busy',32,128,'1');new=host('added',8,32,'2');self.hosts=[old,new]
        migration.begin_migration([old],('bulky','expanded'),self.state,self.path,'oom')
        self.state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['2'])
        self.state['host_deployments']={f'z/{vm.instance_id}':dict(expected_runner_count=8,runner_memory_gib=15) for vm in self.hosts}
        self.runners=[GitHubRunner(i,f'gha-{new.name}-{i}','online',False,()) for i in range(1,9)]
        for tick in (1000,1300):
            self.evidence(tick);self.state['latest_resources'][0].update(peak_cpu_cores=30,peak_ram_gib=127)
            self.state['pending_oom']={'z/1':'host'}
            self.state['host_health']['z/1']['boot_host_oom_ids']=['genuine-global']
            with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=True):
                migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
        self.assertNotIn('migration',self.state)
        self.assertEqual(self.state['pending_oom'],{'z/1':'host'})
        self.assertEqual(self.state['desired_topology'],['bulky','expanded'])

    def test_pure_growth_completion_still_holds_incomplete_roster_metrics_reservation_or_hard_limits(self):
        for defect in ('missing_metric','stale','negative','reservation','cpu_ceiling','slots','jobs'):
            with self.subTest(defect=defect):
                self.setUp();old=host('busy',32,128,'1');new=host('added',8,32,'2');self.hosts=[old,new]
                migration.begin_migration([old],('bulky','expanded'),self.state,self.path,'oom')
                self.state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['2'])
                self.state['host_deployments']={f'z/{vm.instance_id}':dict(expected_runner_count=8,runner_memory_gib=15) for vm in self.hosts}
                self.runners=[GitHubRunner(i,f'gha-{new.name}-{i}','online',False,()) for i in range(1,9)]
                for tick in (1000,1300):
                    self.evidence(tick)
                    if defect=='missing_metric':self.state['latest_resources'].pop(0)
                    if defect=='stale':self.state['latest_resources'][0]['observed_at']=tick-500
                    if defect=='negative':self.state['latest_resources'][0]['peak_ram_gib']=-1
                    if defect=='reservation':self.args.tier_job_memory_gib=60
                    if defect=='cpu_ceiling':self.hosts.append(host('unplanned',64,256,'3'))
                    if defect=='slots':self.state['host_deployments']['z/1']['expected_runner_count']=95
                    if defect=='jobs':self.state['latest_workload']['complete']=False
                    with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=True):
                        migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
                self.assertIn('migration',self.state)
                self.assertNotIn('host_operation',self.state)

    def test_sibling_source_preemption_releases_barrier_for_exact_cleanup_without_restoring_source(self):
        sibling=host('sibling',4,16,'3');self.hosts=[self.small,sibling,self.large]
        migration.begin_migration([self.small,sibling],('large',),self.state,self.path,'consolidation')
        self.state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['1'])
        self.state['host_deployments']={'z/1':dict(tier='large',expected_runner_count=8,runner_memory_gib=15)}
        controller.observe_vms(self.hosts,self.state)
        for tick in (1000,1300):
            self.evidence(tick)
            with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=True):
                migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
        self.state['host_operation'].update(kind='delete',drained=True,runners_isolated=True,drain_group_id=77)
        saved=json.loads(json.dumps(self.state['host_operation']));controller.write_scale_state(self.path,self.state)
        self.state=json.loads(self.path.read_text());self.hosts=[self.small,self.large]
        controller.observe_vms(self.hosts,self.state);self.evidence(1600)
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=self.runners),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,1600)),\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=1600):
            controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
        delete.assert_not_called();self.assertNotIn('host_operation',self.state)
        self.state=json.loads(self.path.read_text())
        with patch('lib.vm_recovery.cleanup_lost_vm',return_value=True) as cleanup,\
             patch.object(controller,'scale_up') as launch:
            controller.reconcile_lost_vms(self.hosts,self.runners,self.args,self.state,self.path)
            self.assertEqual(self.state['host_operation']['kind'],'recovery')
            self.assertEqual(cleanup.call_args.args[0].instance_id,'3')
            controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
            self.assertNotIn('host_operation',self.state)
            self.assertTrue(all(record.get('_cleanup_complete') for record in self.state['pending_vm_recovery'].values()))
            with patch('lib.fleet_migration.time.time',return_value=1600),patch.object(controller,'builder_ready',return_value=True):
                migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
            launch.assert_not_called()
        self.assertEqual(self.state['host_operation'],saved)
        self.assertEqual(self.state['migration']['pending_tiers'],[])
        self.assertEqual(self.state['host_operation']['vm']['instance_id'],'2')

    def fresh_evidence(self,tick):
        from datetime import datetime,timezone
        from job_coverage_fixtures import complete_job_tracking
        self.state['job_tracking']=complete_job_tracking(self.args.org+'/oxidex',datetime.fromtimestamp(tick,timezone.utc).isoformat())
        def health(project,vm,ids):
            return dict(instance_id=vm.instance_id,boot_id='boot-'+vm.instance_id,complete=True,
                boot_oom_complete=True,boot_host_oom_ids=[],observed_at=tick,events=[],io_complete=True,
                io_pressure={scope+'_'+name:0 for scope in ('full','some') for name in ('avg10','avg60','avg300','total')})
        with patch.object(controller,'read_utilization',return_value=[(.01,.01)]*len(self.hosts)),\
             patch('lib.tier_controller.read_host_events',side_effect=health),\
             patch('lib.tier_controller.time.time',return_value=tick):
            return self.real_evidence(self.hosts,self.runners,self.args,self.state)

    def test_fresh_barrier_sibling_loss_before_observe_pauses_with_real_evidence(self):
        sibling=host('sibling',4,16,'3');self.hosts=[self.small,sibling,self.large]
        migration.begin_migration([self.small,sibling],('large',),self.state,self.path,'consolidation')
        self.state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['1'])
        self.state['host_deployments']={'z/1':dict(tier='large',expected_runner_count=8,runner_memory_gib=15)}
        controller.observe_vms(self.hosts,self.state)
        for tick in (1000,1300):
            self.fresh_evidence(tick)
            with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=True):
                migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
        self.state['host_operation'].update(kind='delete',drained=True)
        self.state=json.loads(json.dumps(self.state));stale=list(self.hosts);self.hosts=[self.small,self.large]
        self.assertFalse(self.state.get('pending_vm_recovery'))
        def refreshed(vms,runners,args,state):return self.fresh_evidence(1600)
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=self.runners),\
             patch('lib.tier_controller.evidence_for',side_effect=refreshed),\
             patch.object(controller,'builder_ready',return_value=True),\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=1600):
            # refreshed() calls the real evidence function, only provider/SSH inputs are mocked.
            controller.reconcile_host_operation(stale,self.runners,self.args,self.state,self.path)
        delete.assert_not_called();self.assertNotIn('host_operation',self.state)
        self.assertEqual(next(iter(self.state['pending_vm_recovery'].values()))['instance_id'],'3')
        self.assertEqual(self.state['migration']['paused_retirement']['vm']['instance_id'],'2')

    def test_restored_destination_in_drain_phase_requires_fresh_two_windows_and_builder(self):
        self.removal();source=copy.deepcopy(self.state['host_operation']['vm']);self.state.pop('host_operation')
        self.state['pending_vm_recovery']={'source':dict(source,_cleanup_complete=True,_disks_absent=True)}
        self.hosts=[];self.runners=[]
        from decimal import Decimal
        def prices(vms,project,cap,totals):totals.update({vm.name:Decimal('.4') for vm in vms})
        new=host('restored',16,64,'3')
        def launched(*args,**kwargs):
            self.state['host_operation']=dict(kind='launch',vm=asdict(new),expected_runner_count=8)
        with patch.object(controller,'check_running_prices',side_effect=prices),\
             patch.object(controller,'discover_tier_offers',return_value=[type('Offer',(),{'total_daily_usd':Decimal('2.9')})()]),\
             patch.object(controller,'scale_up',side_effect=launched),patch('lib.fleet_migration.time.time',return_value=1600):
            migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
        self.state=json.loads(json.dumps(self.state));self.hosts=[new]
        self.runners=[GitHubRunner(i,f'gha-{new.name}-{i}','online',False,()) for i in range(1,9)]
        self.state['host_deployments']['z/3']=dict(tier='large',expected_runner_count=8,runner_memory_gib=15)
        controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
        self.state=json.loads(self.path.read_text())
        for tick,builder in ((1900,False),(2200,True),(2500,True)):
            self.fresh_evidence(tick)
            with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=builder) as readiness:
                migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
            if tick<2500:self.assertIn('migration',self.state,'restored destination must complete readiness before topology commit')
            self.assertGreater(readiness.call_count,0)
        self.assertNotIn('migration',self.state);self.assertEqual(self.state['desired_topology'],['large'])

    def test_confirmed_retirement_receipt_survives_json_and_rejects_wrong_transaction_or_inventory(self):
        sibling=host('sibling',4,16,'3');self.hosts=[self.small,sibling,self.large]
        migration.begin_migration([self.small,sibling],('large',),self.state,self.path,'consolidation')
        self.state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['1'])
        self.state['host_deployments']={'z/1':dict(tier='large',expected_runner_count=8,runner_memory_gib=15)}
        for tick in (1000,1300):
            self.fresh_evidence(tick)
            with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=True):
                migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
        operation=self.state['host_operation'];operation.update(kind='delete',drained=True)
        self.hosts=[sibling,self.large]
        controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
        self.state=json.loads(self.path.read_text());source=self.state['migration']['sources'][0]
        saved=copy.deepcopy(self.state)
        self.assertTrue(migration.source_absence_proven(source,self.state))
        for defect in ('intent_only','transaction','disk','identity','unconfirmed'):
            with self.subTest(defect=defect):
                self.state=copy.deepcopy(saved);receipt=self.state['migration']['completed_retirements']['2']
                if defect=='intent_only':self.state['migration'].pop('completed_retirements');self.state['intentional_deletion_ids']={'z/'+self.small.name:'2'}
                if defect=='transaction':receipt['started_at']-=1
                if defect=='disk':receipt['vm']['disks'][0]['diskId']='wrong'
                if defect=='identity':receipt['vm']['instance_id']='different'
                if defect=='unconfirmed':receipt['absence_confirmed']=False
                self.fresh_evidence(1600)
                with patch('lib.fleet_migration.time.time',return_value=1600),patch.object(controller,'builder_ready',return_value=True):
                    migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
                self.assertNotIn('host_operation',self.state)
                self.assertFalse(migration.source_absence_proven(source,self.state))
                self.assertTrue(any(row['instance_id']=='2' and not row.get('_cleanup_complete')
                                    for row in self.state['pending_vm_recovery'].values()))
        self.state=saved;self.fresh_evidence(1600)
        with patch('lib.fleet_migration.time.time',return_value=1600),patch.object(controller,'builder_ready',return_value=True):
            migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
        self.assertEqual(self.state['host_operation']['vm']['instance_id'],'3')

    def test_persisted_drain_with_all_sources_absent_still_checks_final_readiness_and_workload(self):
        for defect in ('builder','jobs','io','boot','peak'):
            with self.subTest(defect=defect):
                self.setUp();self.removal()
                self.state['host_operation'].update(kind='delete',drained=True);self.hosts=[self.large]
                controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
                self.state=json.loads(self.path.read_text());self.fresh_evidence(1600)
                if defect=='jobs':self.state['latest_workload']['complete']=False
                if defect=='io':self.state['latest_resources'][0]['io_complete']=False
                if defect=='boot':self.state['host_health']['z/1']['boot_id']='changed'
                if defect=='peak':self.state['latest_resources'][0]['peak_cpu_cores']=20
                with patch('lib.fleet_migration.time.time',return_value=1600),patch.object(controller,'builder_ready',return_value=defect!='builder'):
                    migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
                self.assertIn('migration',self.state)
                self.assertNotIn('desired_topology',self.state)

if __name__=='__main__':unittest.main()
