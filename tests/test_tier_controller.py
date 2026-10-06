import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lib import spot_runner_autoscaler as controller
from job_coverage_fixtures import complete_job_tracking


class TierControllerTests(unittest.TestCase):
    def test_already_seen_unknown_id_is_promoted_but_budget_obligation_survives_partial_repair(self):
        import json
        from lib import tier_controller
        from lib.runner_fleet import GceVm
        identity='a'*64
        host=GceVm('oxidex-runners-full','z','RUNNING','n2-standard-32',True,32,128,instance_id='1')
        args=controller.args_parser().parse_args(['--scaling-mode','tiered'])
        state=dict(seen_oom_ids=[identity],host_health={'z/1':dict(boot_id='boot')},
            pending_oom={'z/1':'unknown'},pending_oom_origins={'z/1':dict(kind='unknown',boot_id='boot',
            event_ids=[identity],quarantined=False)},
            job_tracking=complete_job_tracking(args.org+'/oxidex','1970-01-01T00:16:40Z'))
        observed=dict(instance_id='1',boot_id='boot',observed_at=1000,complete=True,events=[],
            io_complete=True,io_pressure=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0),
            reclassification_complete=True,reclassified_events=[dict(id=identity,kind='container',workload='runner')])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json'
            for tick,outcome in ((1000,False),(1300,True)):
                observed['observed_at']=tick
                with patch('lib.tier_controller.time.time',return_value=tick),\
                     patch.object(controller,'read_utilization',return_value=[(.1,.06)]),\
                     patch('lib.tier_controller.read_host_events',return_value=observed) as sample,\
                     patch.object(controller,'adjust_admission',return_value=outcome) as adjust:
                    tier_controller.check_tiers([host],[],args,state,path)
                self.assertEqual(sample.call_args.args[2],[identity] if tick==1000 else [])
                adjust.assert_called_once_with(host,args,'runner')
                saved=json.loads(path.read_text())
                self.assertEqual(saved['seen_oom_ids'],[identity])
                self.assertEqual(saved['pending_oom'],{'z/1':'runner'} if tick==1000 else {})
                if tick==1000:
                    self.assertEqual(saved['pending_oom_obligations']['z/1']['runner']['event_ids'],[identity])
                    self.assertNotIn('unknown',saved['pending_oom_obligations']['z/1'])
                state=saved

    def test_runner_and_builder_ooms_survive_independently_through_composed_repair(self):
        import json
        from lib import tier_controller
        from lib.runner_fleet import GceVm
        host=GceVm('oxidex-runners-full','z','RUNNING','n2-standard-32',True,32,128,instance_id='1')
        args=controller.args_parser().parse_args(['--scaling-mode','tiered'])
        state={'job_tracking':complete_job_tracking(args.org+'/oxidex','1970-01-01T00:16:40+00:00')}
        observed=dict(instance_id='1',boot_id='boot',observed_at=1000,complete=True,
            events=[dict(id=kind+'-oom',kind='container',workload=kind,at=999) for kind in ('runner','builder')])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json'
            for tick,outcomes in ((1000,[False,True]),(1300,[True])):
                observed['observed_at']=tick
                if tick==1300:observed['events']=[]
                with patch('lib.tier_controller.time.time',return_value=tick),\
                     patch.object(controller,'read_utilization',return_value=[(.1,.06)]),\
                     patch('lib.tier_controller.read_host_events',return_value=observed),\
                     patch.object(controller,'adjust_admission',side_effect=outcomes) as adjust:
                    tier_controller.check_tiers([host],[],args,state,path)
                self.assertEqual([call.args[2] for call in adjust.call_args_list],['runner','builder'] if tick==1000 else ['runner'])
                saved=json.loads(path.read_text())
                self.assertEqual(saved['pending_oom'],{'z/1':'runner'} if tick==1000 else {})
                self.assertEqual(set(saved['seen_oom_ids']),{'runner-oom','builder-oom'})
                if tick==1000:
                    self.assertEqual(saved['pending_oom_obligations']['z/1']['runner']['event_ids'],['runner-oom'])

    def test_aggregate_loss_recovery_boundary_retains_receipts_without_exception(self):
        import copy,json
        from lib import tier_controller
        args=controller.args_parser().parse_args(['--scaling-mode','tiered','--tier-job-memory-gib','60'])
        receipts={str(i):dict(instance_id=str(i),zone='z',vcpu=16,ram_gib=64,_cleanup_complete=True) for i in range(3)}
        state=dict(pending_vm_recovery=copy.deepcopy(receipts),desired_topology=['large']*3,price_replacement_target=3)
        with tempfile.TemporaryDirectory() as folder,\
             patch.object(tier_controller,'evidence_for',return_value=([],dict(minimum_job_memory_gib=60),1000)),\
             patch.object(tier_controller,'reconcile_migration') as reconcile:
            path=Path(folder)/'state.json'
            tier_controller.check_tiers([],[],args,state,path)
            saved=json.loads(path.read_text())
        self.assertEqual(saved['pending_vm_recovery'],receipts)
        self.assertEqual(saved['price_replacement_target'],3)
        self.assertNotIn('migration',saved)
        reconcile.assert_not_called()

    def test_budget_capacity_recovery_persists_only_reservation_compatible_target(self):
        import json
        from lib import tier_controller
        from lib.runner_fleet import GceVm
        host=GceVm('oxidex-runners-full','z','RUNNING','n2-standard-32',True,32,128,instance_id='1')
        sample=dict(instance_id='1',zone='z',windows=6,cpu_cores=1,peak_cpu_cores=1,
                    used_ram_gib=10,peak_ram_gib=10,cpu_fraction=.03,memory_fraction=.08)
        for reservation,target in ((15,['bulky','expanded']),(2,['bulky','small']),(1000,None)):
            with self.subTest(reservation=reservation),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'state.json'
                args=controller.args_parser().parse_args(['--scaling-mode','tiered',
                    '--tier-job-memory-gib',str(reservation)])
                state={'pending_oom':{'z/1':'runner'}}
                signals=dict(complete=True,container_oom=True,minimum_job_memory_gib=reservation)
                with patch.object(tier_controller,'evidence_for',return_value=([sample],signals,1000)),\
                     patch.object(controller,'adjust_admission',return_value='capacity') as adjust,\
                     patch.object(tier_controller,'reconcile_migration') as reconcile,\
                     patch.object(controller,'check_running_prices',side_effect=AssertionError('unexpected price path')):
                    tier_controller.check_tiers([host],[],args,state,path)
                adjust.assert_called_once_with(host,args,'runner')
                saved=json.loads(path.read_text())
                self.assertEqual(saved.get('migration',{}).get('target'),target)
                self.assertEqual(saved['pending_oom'],{'z/1':'runner'})
                if target:
                    self.assertIn('1',saved['migration']['retained_ids'])
                    reconcile.assert_called_once()
                else:
                    reconcile.assert_not_called()

    def test_dry_run_cannot_mutate_live_state_or_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';path.write_text('{"idle_loops":0}')
            args=controller.args_parser().parse_args(['--scaling-mode','tiered','--dry-run','--once',
                '--state-file',str(path),'--migrate-oversized'])
            with patch.object(controller,'list_gcloud_vms',return_value=[]),patch.object(controller,'list_github_runners',return_value=[]),\
                 patch('lib.job_recovery.JobRecovery._scan',return_value=({},{})),\
                 patch.object(controller,'discover_tier_offers',return_value=[]),\
                 patch.object(controller,'write_scale_state',side_effect=AssertionError('state mutation')),\
                 patch.object(controller,'launch_offer',side_effect=AssertionError('provider mutation')):
                self.assertEqual(controller._run_controller(args),0)
            self.assertEqual(path.read_text(),'{"idle_loops":0}')

    def test_tier_launch_rejects_64_core_recovery_before_any_provider_call(self):
        from lib.machine_profiles import CpuOffer
        args=controller.args_parser().parse_args(['--scaling-mode','tiered'])
        args.launch_tier='large'
        offer=CpuOffer('us-west4','n2-standard-64',64,256,.05,'Intel Ice Lake',7)
        with patch.object(controller.subprocess,'run',side_effect=AssertionError('external side effect')):
            with self.assertRaisesRegex(RuntimeError,'forbidden'):
                controller.launch_offer(offer,args,{},Path('/tmp/never-used-tier-state'))

    def test_host_oom_topology_replaces_affected_retained_tier(self):
        from lib import tier_controller
        from lib.runner_fleet import GceVm
        hosts=[GceVm('oxidex-runners-large','z','RUNNING','n4-standard-16',True,16,64,instance_id='1'),
               GceVm('oxidex-runners-tail','z','RUNNING','n4-standard-8',True,8,32,instance_id='2')]
        args=controller.args_parser().parse_args(['--scaling-mode','tiered'])
        state={'pending_oom':{'z/1':'host'}}
        with tempfile.TemporaryDirectory() as folder, \
             patch('lib.tier_controller.evidence_for',return_value=({}, {},1000)), \
             patch('lib.tier_controller.plan_capacity',return_value=dict(action='topology',target=('large','large'),reason='oom')), \
             patch('lib.tier_controller.begin_migration') as begin, \
             patch('lib.tier_controller.reconcile_migration'):
            tier_controller.check_tiers(hosts,[],args,state,Path(folder)/'state.json')
        self.assertEqual(begin.call_args.kwargs.get('force_ids'),['1'])
