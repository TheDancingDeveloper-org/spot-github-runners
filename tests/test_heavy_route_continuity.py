import json
from dataclasses import asdict
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from lib import runner_fleet, spot_runner_autoscaler as controller, tier_controller, fleet_migration

class HeavyRouteContinuityTests(unittest.TestCase):
    def setUp(self):
        self.vm=runner_fleet.GceVm('oxidex-runners-host','z','RUNNING','n4-standard-16',True,16,64,instance_id='721')
        self.runner=runner_fleet.GitHubRunner(1,'gha-oxidex-runners-host-1','online',False,('spot-heavy32',))
    def test_live_route_is_remembered_when_label_disappears(self):
        state={}
        self.assertEqual(controller.protected_heavy_routes([self.vm],[self.runner],state),[self.vm])
        self.assertEqual(controller.protected_heavy_routes([self.vm],[],state),[self.vm])
        lost=runner_fleet.GceVm(self.vm.name,'z','TERMINATED','n4-standard-16',True,16,64,instance_id='721')
        self.assertEqual(controller.protected_heavy_routes([lost],[],state),[])
    def test_root_planned_protection_holds_before_first_label_observation(self):
        state={'protected_heavy_routes':{'721':{'name':self.vm.name,'zone':'z'}}}
        self.assertEqual(controller.protected_heavy_routes([self.vm],[],state),[self.vm])
    def test_unrelated_runner_does_not_protect_host(self):
        other=runner_fleet.GitHubRunner(2,'unrelated','online',False,('spot-heavy32',))
        state={}
        self.assertEqual(controller.protected_heavy_routes([self.vm],[other],state),[])
        self.assertNotIn('protected_heavy_routes',state)
    def test_delete_is_held_before_any_deployment_or_drain_effect(self):
        state={'host_operation':{'kind':'delete','vm':asdict(self.vm)}}
        with patch.object(controller,'import_deployment_receipt') as effect, patch.object(controller,'write_scale_state'):
            self.assertTrue(controller.reconcile_host_operation([self.vm],[self.runner],None,state,None))
            effect.assert_not_called()
    def test_migration_retirement_retries_are_protected(self):
        operation={'vm':{'instance_id':'721'}}
        for guard in (fleet_migration.retirement_safe,fleet_migration.staged_retirement_safe):
            self.assertFalse(guard([self.vm],[self.runner],None,{},None,operation))
    def test_tier_hold_preserves_pending_migration_and_collects_metrics(self):
        state={'migration':{'started_at':123,'target':['expanded']}}
        with tempfile.TemporaryDirectory() as root, patch.object(tier_controller,'evidence_for',return_value=({}, {}, time.time())) as observed, patch.object(tier_controller,'reconcile_migration') as mutate:
            tier_controller.check_tiers([self.vm],[self.runner],None,state,Path(root)/'state.json')
            observed.assert_called_once()
            mutate.assert_not_called()
            self.assertEqual(state['migration'],{'started_at':123,'target':['expanded']})
    def test_stale_and_future_inventory_are_not_absence_permission(self):
        for value in (time.time()-121,time.time()+10,'invalid'):
            state={'runner_inventory_observed_at':value,'host_operation':{'kind':'delete','vm':{'instance_id':'721'}}}
            with patch.object(controller,'import_deployment_receipt') as effect:
                self.assertTrue(controller.reconcile_host_operation([self.vm],[],None,state,None))
                effect.assert_not_called()
    def test_resource_and_legacy_modes_do_not_bypass_route_hold(self):
        from contextlib import ExitStack
        for mode in ('resource','legacy'):
            args=controller.args_parser().parse_args(['--once','--scaling-mode',mode])
            state={}
            with ExitStack() as stack:
                for name,value in (('read_scale_state',state),('load_cpu_platform_map',{}),
                                   ('list_github_runners',[self.runner]),('list_gcloud_vms',[self.vm]),
                                   ('reconcile_host_operation',False),('reconcile_lost_vms',False)):
                    stack.enter_context(patch.object(controller,name,return_value=value))
                for name in ('write_scale_state','observe_vms','reconcile_deletion_identities','log_hourly_spend'):
                    stack.enter_context(patch.object(controller,name))
                stack.enter_context(patch.object(controller,'JobRecovery'))
                price=stack.enter_context(patch.object(controller,'reconcile_prices'))
                observed=stack.enter_context(patch.object(tier_controller,'evidence_for',return_value=({}, {}, time.time())))
                self.assertEqual(controller._run_controller(args),0)
                price.assert_not_called();observed.assert_called_once()
    def test_lost_cleanup_can_finish_without_unqualified_replacement_launch(self):
        from lib import vm_recovery
        state={'pending_vm_recovery':{'lost':{'_cleanup_complete':True}}}
        with patch.object(vm_recovery,'record_loss_restoration_target'), patch.object(controller,'reconcile_prices') as launch:
            self.assertFalse(vm_recovery.reconcile_lost_vms([self.vm],[self.runner],SimpleNamespace(scaling_mode='resource'),state,None))
            launch.assert_not_called()
    def test_known_heavy_host_loss_does_not_launch_an_unrouted_replacement(self):
        from lib import vm_recovery
        state={'pending_vm_recovery':{'lost':{'_cleanup_complete':True}},
               'protected_heavy_routes':{'721':{'name':self.vm.name,'zone':'z'}}}
        with patch.object(vm_recovery,'record_loss_restoration_target'), patch.object(controller,'reconcile_prices') as launch:
            self.assertFalse(vm_recovery.reconcile_lost_vms([],[],SimpleNamespace(scaling_mode='legacy'),state,None))
            launch.assert_not_called()
        with tempfile.TemporaryDirectory() as root, patch.object(tier_controller,'evidence_for',return_value=({}, {}, time.time())) as observed, patch.object(tier_controller,'begin_migration') as launch:
            tier_controller.check_tiers([],[],SimpleNamespace(),state,Path(root)/'state.json')
            observed.assert_called_once();launch.assert_not_called()
    def test_protected_pending_launch_holds_abandonment_but_allows_registration(self):
        args=SimpleNamespace(org='swack-tools',scaling_mode='legacy')
        for count,complete in ((2,False),(1,True)):
            state={'host_operation':{'kind':'launch','vm':asdict(self.vm),'started_at':0,
                                    'expected_runner_count':count,'abandon':'deadline'}}
            with patch.object(controller,'import_deployment_receipt'), patch.object(controller,'write_scale_state'), patch.object(controller,'abandon_launch') as destroy:
                self.assertTrue(controller.reconcile_host_operation([self.vm],[self.runner],args,state,None))
                destroy.assert_not_called()
                self.assertEqual('host_operation' not in state,complete)
    def test_missing_operation_identity_cannot_bypass_protection(self):
        record=asdict(self.vm);record['instance_id']=None
        state={'host_operation':{'kind':'delete','vm':record}}
        with patch.object(controller,'import_deployment_receipt') as mutate,patch.object(controller,'write_scale_state'):
            self.assertTrue(controller.reconcile_host_operation([self.vm],[self.runner],None,state,None))
            mutate.assert_not_called()
            self.assertIsNone(state['host_operation']['vm']['instance_id'])
    def test_fresh_drain_label_is_protected_before_labels_are_removed(self):
        ordinary=runner_fleet.GitHubRunner(1,self.runner.name,'online',False,('spot',))
        state={'host_operation':{'kind':'drain','vm':asdict(self.vm),'drain_mode':'label_only'}}
        from lib import runner_drain
        with patch.object(controller,'import_deployment_receipt'),patch.object(controller,'write_scale_state'),patch.object(controller,'list_github_runners',return_value=[self.runner]),patch.object(fleet_migration,'guard_staged_retirement',return_value=True),patch.object(runner_drain,'drain_runners') as mutate:
            self.assertTrue(controller.reconcile_host_operation([self.vm],[ordinary],SimpleNamespace(org='swack-tools',scaling_mode='legacy'),state,None))
            mutate.assert_not_called()
            self.assertIn('721',state['protected_heavy_routes'])
    def test_prices_that_expire_inventory_cannot_delete_or_begin_operation(self):
        state={'runner_inventory_observed_at':time.time()-119}
        def prices(*args,**kwargs):
            state['runner_inventory_observed_at']=time.time()-121
            return [self.vm]
        with patch.object(controller,'check_running_prices',side_effect=prices),patch.object(controller,'delete_overpriced_vm') as destroy,patch.object(controller,'begin_host_operation') as begin:
            with self.assertRaises(RuntimeError):controller.reconcile_prices([self.vm],[],SimpleNamespace(project='p',max_daily_usd=3),state,None)
            destroy.assert_not_called();begin.assert_not_called()
        with patch.object(controller,'platform_preferences') as discovery:
            with self.assertRaises(RuntimeError):controller.launch_offer(None,SimpleNamespace(state_file=Path('/unused')),state,None)
            discovery.assert_not_called()
    def test_stale_main_loop_still_observes_metrics_without_mutating(self):
        from contextlib import ExitStack
        args=controller.args_parser().parse_args(['--once','--scaling-mode','tiered']);state={}
        with ExitStack() as stack:
            for name,value in (('read_scale_state',state),('load_cpu_platform_map',{}),('list_github_runners',[]),('list_gcloud_vms',[self.vm])):
                stack.enter_context(patch.object(controller,name,return_value=value))
            for name in ('write_scale_state','observe_vms','reconcile_deletion_identities'):
                stack.enter_context(patch.object(controller,name))
            stack.enter_context(patch.object(controller,'JobRecovery'))
            stack.enter_context(patch.object(controller,'log_hourly_spend',side_effect=lambda *a:state.update(runner_inventory_observed_at=time.time()-121)))
            observed=stack.enter_context(patch.object(tier_controller,'evidence_for',return_value=({}, {}, time.time())))
            mutation=stack.enter_context(patch.object(controller,'reconcile_host_operation'))
            self.assertEqual(controller._run_controller(args),0)
            observed.assert_called_once();mutation.assert_not_called()
    def test_drain_api_checks_ttl_at_each_write(self):
        from lib import runner_drain
        with patch.object(runner_drain,'api') as write:
            with self.assertRaises(RuntimeError):runner_drain.guarded_api(lambda:controller.guard_fleet_mutation({'runner_inventory_observed_at':time.time()-121},self.vm),'org','DELETE','/labels')
            write.assert_not_called()
    def test_confirmed_loss_cleanup_is_not_blocked_as_a_launch(self):
        state={'protected_heavy_routes':{'721':{'name':self.vm.name,'zone':'z'}}}
        with patch.object(controller,'write_scale_state'):
            controller.begin_host_operation('recovery',self.vm,[],[],state,None)
        self.assertEqual(state['host_operation']['kind'],'recovery')
        self.assertTrue(controller.heavy_route_capacity_hold(state))
    def test_receipt_cannot_adopt_reused_provider_identity(self):
        record=asdict(self.vm);record['instance_id']=None
        current=runner_fleet.GceVm(self.vm.name,'z','RUNNING','n4-standard-16',True,16,64,instance_id='new')
        old=asdict(self.vm)
        with tempfile.TemporaryDirectory() as root:
            receipt=Path(root)/'receipt.json';receipt.write_text(json.dumps(old))
            state={'host_operation':{'kind':'launch','vm':record,'deployment_receipt':str(receipt)}}
            with patch.object(controller,'abandon_launch') as destroy,patch.object(controller,'write_scale_state') as write:
                self.assertTrue(controller.reconcile_host_operation([current],[],SimpleNamespace(),state,None))
                self.assertIsNone(state['host_operation']['vm']['instance_id'])
                destroy.assert_not_called();write.assert_not_called()
    def test_retirement_revalidation_uses_the_new_snapshot_timestamp(self):
        record=asdict(self.vm)
        state={'runner_inventory_observed_at':0,'host_operation':{'vm':record,'migration_retirement':{'started_at':1}},
               'migration':{'sources':[record],'started_at':1}}
        clock=[115]
        def observe(*args):clock[0]=125
        def safe(*args):return not controller.runner_inventory_stale(state)
        with patch.object(controller,'list_gcloud_vms',return_value=[self.vm]),patch.object(controller,'observe_vms'),patch.object(controller,'list_github_runners',return_value=[]),patch.object(fleet_migration,'record_missing_sources'),patch.object(fleet_migration,'retirement_safe',side_effect=safe),patch.object(tier_controller,'evidence_for',side_effect=observe),patch.object(fleet_migration.time,'time',side_effect=lambda:clock[0]):
            self.assertTrue(fleet_migration.guard_staged_retirement([self.vm],[],SimpleNamespace(project='p',org='org'),state,None))
        self.assertEqual(state['runner_inventory_observed_at'],115)
    def test_inventory_requires_total_pages_labels_and_no_diagnostics(self):
        item={'id':1,'name':'r','status':'online','busy':False,'labels':[{'name':'spot-heavy32'}]}
        valid=[{'total_count':1,'runners':[item]}]
        cases=[([1],''),([{'total_count':2,'runners':[item]}],''),([{'total_count':1,'runners':[dict(item,labels=None)]}],''),(valid,'partial warning')]
        for pages,stderr in cases:
            with patch.object(runner_fleet.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(pages),stderr=stderr)):
                with self.assertRaises(RuntimeError):runner_fleet.list_github_runners()
        with patch.object(runner_fleet.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(valid),stderr='')):
            self.assertEqual(runner_fleet.list_github_runners()[0].labels,('spot-heavy32',))
    def test_provider_partial_diagnostic_holds_and_strict_flag_is_set(self):
        with patch.object(runner_fleet.subprocess,'run',return_value=SimpleNamespace(stdout='[]',stderr='partial zones')) as cli:
            with self.assertRaises(RuntimeError):runner_fleet._command_json(['gcloud','compute','instances','list'])
            self.assertEqual(cli.call_args.kwargs['env']['CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR'],'false')
    def test_malformed_protection_and_unknown_or_reused_host_cannot_retire(self):
        self.assertEqual(controller.protected_heavy_routes([self.vm],[],{'protected_heavy_routes':None}),[self.vm])
        other=runner_fleet.GceVm(self.vm.name,'z','UNKNOWN','n4-standard-16',True,instance_id=None)
        state={'protected_heavy_routes':{'721':{'name':self.vm.name,'zone':'z'}}}
        self.assertEqual(controller.protected_heavy_routes([other],[],state),[other])
    def test_duplicate_and_changed_total_pages_hold(self):
        item={'id':1,'name':'r','status':'online','busy':False,'labels':[]}
        pages=[{'total_count':2,'runners':[item,item]}]
        with patch.object(runner_fleet.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(pages),stderr='')):
            with self.assertRaises(RuntimeError):runner_fleet.list_github_runners()

if __name__=='__main__':unittest.main()
