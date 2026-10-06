import copy
import json
import tempfile
import unittest
from dataclasses import asdict,replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_storage_retarget as fixtures
from lib import fleet_migration as migration,spot_runner_autoscaler as controller
from lib.runner_fleet import GceVm,GitHubRunner


class StagedStorageMigrationTests(unittest.TestCase):
    def setUp(self):
        verified_disks=patch('lib.vm_recovery.cleanup_lost_vm',return_value=True)
        verified_disks.start();self.addCleanup(verified_disks.stop)
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'state.json'
        self.hosts,self.state,self.args=fixtures.StorageRetargetTests().fixture(self.path)
        self.hosts=[replace(vm,disks=tuple({**disk,'source':'https://www.googleapis.com/compute/v1/projects/p/zones/z/disks/disk-'+vm.instance_id} for disk in vm.disks)) for vm in self.hosts]
        for row in self.state['migration']['sources']:
            row['disks']=asdict(next(vm for vm in self.hosts if vm.instance_id==row['instance_id']))['disks']
        self.args.__dict__.update(project='p',org='o',disk_size_gb=100,remote_builder=True,scaling_mode='tiered')
        self.state['latest_resources'][0]['peak_cpu_cores']=11.7
        self.state['latest_resources'][1]['peak_cpu_cores']=.98
        self.runners=[]

    def retarget(self):
        with patch('lib.fleet_migration.time.time',return_value=2500):
            self.assertTrue(migration.retarget_unhealthy_storage_canary(self.hosts,self.args,self.state,self.path))

    def add_destination(self,identity):
        vm=GceVm('oxidex-runners-new'+identity,'z','RUNNING','n2-standard-16',True,16,64,
                 instance_id=identity,disks=({'source':'https://www.googleapis.com/compute/v1/projects/p/zones/z/disks/disk-'+identity,'diskId':identity+'0','deviceName':'runner-data-new'+identity,
                     'boot':False,'diskType':'pd-balanced','diskSizeGb':100},))
        self.hosts.append(vm)
        self.state['migration']['replacement_ids'].append(identity)
        self.state['migration']['pending_tiers'].pop(0);self.state['migration']['pending_slots'].pop(0)
        self.state['host_deployments']['z/'+identity]=dict(tier='large',expected_runner_count=20,runner_memory_gib=15)
        row=copy.deepcopy(self.state['latest_resources'][0]);row.update(instance_id=identity,peak_cpu_cores=.1,peak_ram_gib=1,
            io_pressure_peaks=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0))
        self.state['latest_resources'].append(row)
        self.state['host_health']['z/'+identity]=dict(instance_id=identity,boot_id='boot-'+identity,complete=True,
            boot_oom_complete=True,boot_host_oom_ids=[],observed_at=2500,events=[])
        self.runners.extend(GitHubRunner(int(identity)*100+i,f'gha-{vm.name}-{i}','online',False,()) for i in range(1,21))
        return vm

    def refresh(self,tick):
        for row in self.state['latest_resources']:row['observed_at']=tick
        for row in self.state['host_health'].values():row['observed_at']=tick

    def reconcile(self,tick,builder=True):
        self.refresh(tick)
        with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=builder):
            migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)

    def staged(self):
        self.retarget();self.add_destination('3');self.reconcile(2800);self.reconcile(3100)
        self.assertEqual(self.state['host_operation']['vm']['instance_id'],'2')

    def test_demand_that_fails_single_large_creates_bounded_two_large_plan(self):
        self.retarget()
        self.assertEqual(self.state['migration']['target'],['large','large'])
        self.assertEqual(self.state['migration']['storage_staged_bad_id'],'2')
        self.assertEqual(self.state['migration']['spend_ceiling'],'15')
        self.assertNotIn('host_operation',self.state)

    def test_first_canary_requires_two_windows_builder_and_drains_only_bad_exact_receipt(self):
        self.retarget();self.add_destination('3')
        self.reconcile(2800);self.assertNotIn('host_operation',self.state)
        self.reconcile(3100,builder=False);self.assertNotIn('host_operation',self.state)
        self.reconcile(3400);self.assertNotIn('host_operation',self.state)
        self.reconcile(3700)
        operation=self.state['host_operation']
        self.assertEqual(operation['vm']['instance_id'],'2')
        self.assertEqual(operation['vm']['disks'],self.state['migration']['sources'][1]['disks'])
        self.assertEqual(self.state['migration']['pending_tiers'],['large'])
        self.assertEqual(self.state['migration']['early_retirement_ids'],['2'])
        restarted=json.loads(self.path.read_text())
        self.assertEqual(restarted['host_operation']['staged_retirement']['stage'],'early')

    def test_global_unknown_missing_io_remaining_pressure_or_changed_disk_holds_early_drain(self):
        for defect in ('host','unknown','io_missing','good_io','disk'):
            with self.subTest(defect=defect):
                self.setUp();self.retarget();self.add_destination('3');self.reconcile(2800)
                if defect in ('host','unknown'):self.state['pending_oom']={'z/2':defect}
                if defect=='io_missing':self.state['latest_resources'][1]['io_complete']=False
                if defect=='good_io':self.state['latest_resources'][0]['io_pressure_peaks']['full_avg10']=90
                if defect=='disk':self.hosts[1]=replace(self.hosts[1],disks=({'source':'new','diskId':'x'},))
                self.reconcile(3100);self.assertNotIn('host_operation',self.state)

    def test_predelete_every_retry_revalidates_peak_fit_and_does_not_delete_good_host(self):
        self.staged();operation=self.state['host_operation'];operation.update(kind='delete',drained=True)
        self.state['latest_resources'][0]['peak_cpu_cores']=40
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=[]),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,3100)),\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=3100):
            controller.reconcile_host_operation(self.hosts,[],self.args,self.state,self.path)
            delete.assert_not_called()
        self.assertEqual(self.state['host_operation']['vm']['instance_id'],'2')
        self.state['latest_resources'][0]['peak_cpu_cores']=11.7
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=[]),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,3100)),\
             patch.object(controller,'builder_ready',return_value=True),\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=3100):
            controller.reconcile_host_operation(self.hosts,[],self.args,self.state,self.path)
            # No registered destination runners means readiness proof must hold.
            delete.assert_not_called()

    def test_survivor_loss_pauses_actual_receipt_then_restarts_without_duplicate_operation(self):
        self.staged();saved=copy.deepcopy(self.state['host_operation']);self.hosts=self.hosts[1:]
        self.state['latest_resources']=self.state['latest_resources'][1:]
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,3100)),\
             patch.object(controller,'list_github_runners',return_value=self.runners),\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=3100):
            controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
            delete.assert_not_called()
        self.assertNotIn('host_operation',self.state)
        self.assertEqual(self.state['migration']['paused_retirement'],saved)
        restarted=json.loads(self.path.read_text())
        self.assertEqual(restarted['migration']['paused_retirement']['vm']['instance_id'],'2')
        self.assertNotIn('host_operation',restarted)

    def test_confirmed_early_absence_launches_second_within_64_cpu_then_waits_for_both(self):
        self.staged();self.state['host_operation'].update(kind='delete',drained=True)
        self.hosts=[vm for vm in self.hosts if vm.instance_id!='2']
        controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
        self.assertIn('2',self.state['migration']['completed_retirements'])
        self.state['latest_resources']=[row for row in self.state['latest_resources'] if row['instance_id']!='2']
        offer=SimpleNamespace(total_daily_usd=Decimal('2.9'))
        def prices(vms,project,cap,totals):totals.update({vm.name:Decimal('5') if vm.vcpu==32 else Decimal('2.9') for vm in vms})
        with patch.object(controller,'check_running_prices',side_effect=prices),\
             patch.object(controller,'discover_tier_offers',return_value=[offer]),\
             patch.object(controller,'scale_up') as launch,patch('lib.fleet_migration.time.time',return_value=3400):
            self.refresh(3400);migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
            launch.assert_called_once();self.assertEqual(self.args.launch_tier,'large')
        self.add_destination('4');self.reconcile(3700)
        self.assertNotIn('host_operation',self.state)
        self.reconcile(4000)
        self.assertEqual(self.state['host_operation']['vm']['instance_id'],'1')
        self.assertEqual(sum(vm.vcpu for vm in self.hosts),64)

    def test_idle_incompatible_canary_retargets_without_pressure_history_and_never_retires_good(self):
        self.hosts[1]=replace(self.hosts[1],disks=tuple({**disk,'diskType':'pd-standard'} for disk in self.hosts[1].disks))
        self.state['io_history']={}
        self.state['latest_resources'][1]['io_pressure_peaks'].update(full_avg10=0,full_avg60=0)
        with patch('lib.fleet_migration.time.time',return_value=2500),patch.object(controller,'builder_ready',return_value=True):
            runners=[GitHubRunner(i,f'gha-{self.hosts[1].name}-{i}','online',False,()) for i in range(1,17)]
            self.assertFalse(migration.replacements_healthy(self.hosts,runners,self.args,self.state,self.path))
        self.retarget()
        self.assertEqual(self.state['migration']['target'],['large','large'])
        self.assertNotIn('host_operation',self.state)

    def test_boot_change_and_future_clock_cannot_reuse_first_canary_health_count(self):
        self.retarget();self.add_destination('3');self.reconcile(2800)
        self.state['host_health']['z/3']['boot_id']='different-boot'
        self.reconcile(3100);self.assertNotIn('host_operation',self.state)
        self.assertEqual(self.state['migration']['healthy_checks']['z/3']['count'],1)
        self.refresh(3400);self.state['host_health']['z/3']['observed_at']=4000
        with patch('lib.fleet_migration.time.time',return_value=3400),patch.object(controller,'builder_ready',return_value=True):
            migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
        self.assertNotIn('host_operation',self.state)

    def test_valid_deletion_retry_uses_original_disk_receipt_and_refreshes_evidence(self):
        self.staged();operation=self.state['host_operation'];operation.update(kind='delete',drained=True)
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts) as inventory,\
             patch.object(controller,'list_github_runners',return_value=self.runners),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,3100)) as evidence,\
             patch.object(controller,'builder_ready',return_value=True),\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=3100):
            controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
            inventory.assert_called_once();evidence.assert_called_once()
            delete.assert_called_once()
            self.assertEqual(asdict(delete.call_args.args[0]),operation['vm'])

    def test_actual_busy_source_drain_never_reaches_builder_quiesce_or_delete(self):
        self.staged()
        busy=GitHubRunner(990,f'gha-{self.hosts[1].name}-1','online',True,())
        runners=[*self.runners,busy]
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=runners),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,3100)),\
             patch.object(controller,'builder_ready',return_value=True),\
             patch('lib.runner_drain.drain_runners',return_value=False) as drain,\
             patch.object(controller,'quiesce_remote_builder') as quiesce,\
             patch.object(controller,'delete_drained_vm') as delete,\
             patch('lib.fleet_migration.time.time',return_value=3100):
            controller.reconcile_host_operation(self.hosts,runners,self.args,self.state,self.path)
            drain.assert_called_once();self.assertTrue(drain.call_args.args[-1][0].busy)
            quiesce.assert_not_called();delete.assert_not_called()
        self.assertEqual(self.state['host_operation']['kind'],'drain')

    def test_pause_write_failures_restart_from_actual_active_or_saved_receipt(self):
        for fail_at in (1,2):
            with self.subTest(fail_at=fail_at):
                self.setUp();self.staged();saved=copy.deepcopy(self.state['host_operation'])
                self.hosts=self.hosts[1:];self.state['latest_resources']=self.state['latest_resources'][1:]
                actual_write=controller.write_scale_state;writes=[]
                def write(path,state):
                    writes.append(copy.deepcopy(state))
                    if len(writes)==fail_at:raise OSError('simulated process failure')
                    actual_write(path,state)
                with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
                     patch.object(controller,'list_github_runners',return_value=self.runners),\
                     patch('lib.tier_controller.evidence_for',return_value=(None,None,3100)),\
                     patch.object(controller,'write_scale_state',side_effect=write),\
                     patch('lib.fleet_migration.time.time',return_value=3100):
                    with self.assertRaises(OSError):controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
                restarted=json.loads(self.path.read_text())
                self.assertEqual(restarted['host_operation']['vm']['instance_id'],saved['vm']['instance_id'])
                if fail_at==2:self.assertEqual(restarted['migration']['paused_retirement'],restarted['host_operation'])
                self.state=restarted
                with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
                     patch.object(controller,'list_github_runners',return_value=self.runners),\
                     patch('lib.tier_controller.evidence_for',return_value=(None,None,3100)),\
                     patch('lib.fleet_migration.time.time',return_value=3100):
                    controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
                self.assertNotIn('host_operation',self.state)
                self.assertEqual(self.state['migration']['paused_retirement']['vm']['instance_id'],'2')

    def test_lost_survivor_recovery_can_launch_second_then_resume_original_isolation_receipt(self):
        self.staged();operation=self.state['host_operation']
        operation.update(runners_isolated=True,drain_group_id=77,runner_services_stopped=True)
        saved=copy.deepcopy(operation)
        self.hosts=self.hosts[1:];self.state['latest_resources']=self.state['latest_resources'][1:]
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=self.runners),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,3100)),\
             patch('lib.fleet_migration.time.time',return_value=3100):
            controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
        # A real cleanup receipt remains a barrier until every recorded disk is absent.
        self.state['pending_vm_recovery']={'old':dict(self.state['migration']['sources'][0],_cleanup_complete=False)}
        with patch.object(controller,'scale_up') as launch:
            self.reconcile(3400);launch.assert_not_called()
        self.state['pending_vm_recovery']['old'].update(_cleanup_complete=True,_disks_absent=True)
        offer=SimpleNamespace(total_daily_usd=Decimal('2.9'))
        def prices(vms,project,cap,totals):totals.update({vm.name:Decimal('4') for vm in vms})
        with patch.object(controller,'check_running_prices',side_effect=prices),\
             patch.object(controller,'discover_tier_offers',return_value=[offer]),\
             patch.object(controller,'scale_up') as launch:
            self.reconcile(3400);launch.assert_called_once()
        self.add_destination('4');self.reconcile(3700);self.assertNotIn('host_operation',self.state)
        self.reconcile(4000)
        self.assertEqual(self.state['host_operation'],saved)
        self.assertNotIn('paused_retirement',self.state['migration'])
        self.assertEqual({vm.instance_id for vm in self.hosts},{'2','3','4'})

    def test_staged_prices_never_expand_fifteen_dollar_overlap_ceiling(self):
        self.retarget()
        def prices(vms,project,cap,totals):totals.update({vm.name:Decimal('8') for vm in vms})
        with patch.object(controller,'check_running_prices',side_effect=prices),\
             patch.object(controller,'discover_tier_offers',return_value=[SimpleNamespace(total_daily_usd=Decimal('2'))]),\
             patch.object(controller,'scale_up') as launch:
            self.reconcile(2800);launch.assert_not_called()
        self.assertEqual(self.state['migration']['spend_ceiling'],'15')
        self.assertNotIn('overlap_allowance_daily_usd',self.state['migration'])

    def test_json_roundtrip_keeps_scope_disks_and_missing_scope_holds(self):
        self.staged();self.state=json.loads(self.path.read_text())
        with patch.object(controller,'list_gcloud_vms',return_value=self.hosts),\
             patch.object(controller,'list_github_runners',return_value=self.runners),\
             patch('lib.tier_controller.evidence_for',return_value=(None,None,3100)),\
             patch.object(controller,'builder_ready',return_value=True),\
             patch('lib.fleet_migration.time.time',return_value=3100):
            self.assertTrue(migration.guard_staged_retirement(self.hosts,self.runners,self.args,self.state,self.path))
        self.state['host_operation'].pop('staged_retirement')
        with patch.object(controller,'delete_drained_vm') as delete:
            controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
            delete.assert_not_called()

    def test_final_incomplete_duplicate_or_missing_replacement_receipt_holds(self):
        for defect in ('missing','duplicate','bad_receipt'):
            with self.subTest(defect=defect):
                self.setUp();self.staged();self.state.pop('host_operation')
                self.hosts=[vm for vm in self.hosts if vm.instance_id!='2']
                self.state['latest_resources']=[row for row in self.state['latest_resources'] if row['instance_id']!='2']
                self.add_destination('4')
                if defect=='missing':self.state['migration']['replacement_ids'].remove('4')
                if defect=='duplicate':self.state['migration']['replacement_ids']=['3','3']
                if defect=='bad_receipt':self.state['host_deployments']['z/4']['runner_memory_gib']=2
                self.reconcile(3400);self.reconcile(3700)
                self.assertNotIn('host_operation',self.state)
                self.assertIn('migration',self.state)

    def test_first_canary_preemption_restores_missing_allowed_tier_after_loss_cleanup(self):
        self.staged();self.state.pop('host_operation')
        self.hosts=[vm for vm in self.hosts if vm.instance_id!='3']
        self.state['latest_resources']=[row for row in self.state['latest_resources'] if row['instance_id']!='3']
        self.state['pending_vm_recovery']={'first':dict(instance_id='3',_cleanup_complete=False)}
        with patch.object(controller,'scale_up') as launch:
            self.reconcile(3400);launch.assert_not_called()
        self.state['pending_vm_recovery']['first']['_cleanup_complete']=True
        def prices(vms,project,cap,totals):totals.update({vm.name:Decimal('4') for vm in vms})
        with patch.object(controller,'check_running_prices',side_effect=prices),\
             patch.object(controller,'discover_tier_offers',return_value=[SimpleNamespace(total_daily_usd=Decimal('2.9'))]),\
             patch.object(controller,'scale_up') as launch:
            self.reconcile(3700);launch.assert_called_once()
        self.assertEqual(self.state['migration']['pending_tiers'],['large','large'])
        self.assertEqual(self.state['migration']['replacement_ids'],[])
        self.assertNotIn('host_operation',self.state)

    def test_active_launch_never_duplicates_a_paused_retirement(self):
        self.staged();self.state['migration']['paused_retirement']=copy.deepcopy(self.state['host_operation'])
        self.state['host_operation']={'kind':'launch','vm':asdict(self.hosts[-1])}
        before=copy.deepcopy(self.state)
        with patch.object(controller,'begin_host_operation') as begin,patch.object(controller,'scale_up') as launch:
            self.reconcile(3400);begin.assert_not_called();launch.assert_not_called()
        self.assertEqual(self.state['migration'],before['migration'])
        self.assertEqual(self.state['host_operation'],before['host_operation'])

    def test_unknown_actual_data_specifications_hold_retarget_even_with_pressure(self):
        self.hosts[1]=replace(self.hosts[1],disks=tuple({key:value for key,value in disk.items() if key!='diskType'}
                                                    for disk in self.hosts[1].disks))
        with patch('lib.fleet_migration.time.time',return_value=2500):
            self.assertFalse(migration.retarget_unhealthy_storage_canary(self.hosts,self.args,self.state,self.path))
        self.assertEqual(self.state['migration']['target'],['bulky'])

    def test_good_source_loss_before_early_intent_retires_bad_after_two_recovered_canaries(self):
        self.retarget()
        self.hosts=self.hosts[1:]
        self.state['latest_resources']=self.state['latest_resources'][1:]
        self.state['pending_vm_recovery']={'old':dict(self.state['migration']['sources'][0],_cleanup_complete=True,_disks_absent=True)}
        self.add_destination('3');self.add_destination('4')
        self.assertNotIn('early_retirement_ids',self.state['migration'])
        self.reconcile(2800);self.assertNotIn('host_operation',self.state)
        self.state=json.loads(self.path.read_text())
        self.reconcile(3100)
        self.assertIn('host_operation',self.state,'recovered two-large capacity must permit retiring the bad source')
        operation=self.state['host_operation']
        self.assertEqual(operation['vm']['instance_id'],'2')
        self.assertEqual(operation['staged_retirement']['stage'],'early')
        self.assertEqual(self.state['migration']['replacement_ids'],['3','4'])

    def test_staged_final_completion_after_json_requires_exact_receipts_and_fresh_destination_readiness(self):
        for defect in (None,'receipt','jobs','builder','boot','intent_only'):
            with self.subTest(defect=defect):
                self.setUp();self.staged();self.state['host_operation'].update(kind='delete',drained=True)
                self.hosts=[vm for vm in self.hosts if vm.instance_id!='2']
                self.state['latest_resources']=[row for row in self.state['latest_resources'] if row['instance_id']!='2']
                controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
                self.add_destination('4');self.reconcile(3400);self.reconcile(3700)
                self.assertEqual(self.state['host_operation']['vm']['instance_id'],'1')
                self.state['host_operation'].update(kind='delete',drained=True)
                self.hosts=[vm for vm in self.hosts if vm.instance_id!='1']
                self.state['latest_resources']=[row for row in self.state['latest_resources'] if row['instance_id']!='1']
                controller.reconcile_host_operation(self.hosts,self.runners,self.args,self.state,self.path)
                self.state=json.loads(self.path.read_text())
                self.assertEqual(set(self.state['migration']['completed_retirements']),{'1','2'})
                self.refresh(4000)
                if defect=='receipt':self.state['host_deployments']['z/4']['runner_memory_gib']=2
                if defect=='jobs':self.state['latest_workload']['complete']=False
                if defect=='boot':self.state['host_health']['z/4']['boot_id']='new-boot'
                if defect=='intent_only':self.state['migration'].pop('completed_retirements');self.state['intentional_deletions']=['z/oxidex-runners-old','z/oxidex-runners-bad']
                with patch('lib.fleet_migration.time.time',return_value=4000),patch.object(controller,'builder_ready',return_value=defect!='builder'):
                    migration.reconcile_migration(self.hosts,self.runners,self.args,self.state,self.path)
                self.assertEqual('migration' in self.state,defect is not None)
                if defect is None:self.assertEqual(self.state['desired_topology'],['large','large'])

if __name__=='__main__':unittest.main()
