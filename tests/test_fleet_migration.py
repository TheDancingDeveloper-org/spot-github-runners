import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import asdict
from lib.runner_fleet import GceVm,GitHubRunner
from lib import spot_runner_autoscaler as controller
from lib.fleet_migration import begin_migration, reconcile_migration, expected_slots


def vm(name,cpu,ram,id):
    return GceVm('oxidex-runners-'+name,'z','RUNNING',f'n2-standard-{cpu}',True,cpu,ram,instance_id=id,
        disks=({'source':'disk-'+id,'diskId':id+'0','boot':False,'deviceName':'runner-data-'+name,
                'diskType':'pd-balanced','diskSizeGb':100},))


class MigrationTests(unittest.TestCase):
    def test_removal_only_migration_validates_retained_destination_then_drains_only_small(self):
        import json
        from lib import spot_runner_autoscaler as controller
        large,small=vm('retained',16,64,'1'),vm('small',4,16,'2')
        args=SimpleNamespace(org='org',remote_builder=True,maximum_hosts=5,scaling_mode='tiered',tier_job_memory_gib=15)
        runners=[GitHubRunner(i,f'gha-{large.name}-{i}','online',False,()) for i in range(1,21)]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';state={}
            begin_migration([large,small],('large',),state,path,'consolidation')
            self.assertEqual(state['migration']['replacement_ids'],[])
            self.assertEqual(state['migration']['retained_ids'],['1'])
            state['host_deployments']={'z/1':dict(tier='large',expected_runner_count=20,runner_memory_gib=15)}
            for tick in (1000,1300):
                state['latest_workload']={'complete':True}
                state['latest_resources']=[dict(instance_id=identity,zone='z',observed_at=tick,cpu_fraction=.1,
                    memory_fraction=.1,peak_cpu_cores=1,peak_ram_gib=2,io_complete=True,
                    io_pressure_peaks=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0)) for identity in ('1','2')]
                state['host_health']={f'z/{identity}':dict(instance_id=identity,boot_id='boot-'+identity,
                    complete=True,boot_oom_complete=True,boot_host_oom_ids=[],observed_at=tick,events=[]) for identity in ('1','2')}
                with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=True) as canary:
                    reconcile_migration([large,small],runners,args,state,path)
                self.assertEqual(canary.call_args.args,(large,args))
                self.assertEqual(canary.call_count,1 if tick==1000 else 2)
                if tick==1000:
                    self.assertNotIn('host_operation',state)
                    state=json.loads(path.read_text())
                else:
                    self.assertEqual(state['host_operation']['vm']['instance_id'],'2')
                    self.assertEqual(state['migration']['healthy_checks']['z/1']['count'],2)

    def test_removal_only_retained_destination_holds_incomplete_or_unsafe_evidence(self):
        from lib import spot_runner_autoscaler as controller
        for defect in ('missing_boot','host_oom','unknown_oom','incomplete_io','incomplete_jobs','peak_demand','builder'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as folder:
                large,small=vm('retained',16,64,'1'),vm('small',4,16,'2')
                args=SimpleNamespace(org='org',remote_builder=True,maximum_hosts=5,scaling_mode='tiered',tier_job_memory_gib=15)
                runners=[GitHubRunner(i,f'gha-{large.name}-{i}','online',False,()) for i in range(1,21)]
                path=Path(folder)/'state.json';state={}
                begin_migration([large,small],('large',),state,path,'consolidation')
                state['host_deployments']={'z/1':dict(tier='large',expected_runner_count=20,runner_memory_gib=15)}
                state['latest_workload']={'complete':defect!='incomplete_jobs'}
                for tick in (1000,1300):
                    state['latest_resources']=[dict(instance_id=identity,zone='z',observed_at=tick,cpu_fraction=.1,
                        memory_fraction=.1,peak_cpu_cores=20 if defect=='peak_demand' else 1,peak_ram_gib=2,
                        io_complete=defect!='incomplete_io',io_pressure_peaks=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0))
                        for identity in ('1','2')]
                    state['host_health']={'z/1':dict(instance_id='1',boot_id=None if defect=='missing_boot' else 'boot',
                        complete=True,boot_oom_complete=True,boot_host_oom_ids=['oom'] if defect=='host_oom' else [],observed_at=tick,events=[])}
                    if defect=='unknown_oom':state['pending_oom']={'z/1':'unknown'}
                    with patch('lib.fleet_migration.time.time',return_value=tick),patch.object(controller,'builder_ready',return_value=defect!='builder'):
                        reconcile_migration([large,small],runners,args,state,path)
                    self.assertNotIn('host_operation',state)
                    self.assertIn('migration',state)

    def test_lost_retained_destination_restores_receipted_tier_before_source_retirement(self):
        from lib import spot_runner_autoscaler as controller
        large,small=vm('retained',16,64,'1'),vm('small',4,16,'2')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';state={}
            begin_migration([large,small],('large',),state,path,'consolidation')
            state['host_deployments']={'z/1':dict(tier='large',expected_runner_count=20,runner_memory_gib=15)}
            state['pending_vm_recovery']={'large':dict(instance_id='1',_cleanup_complete=True)}
            args=SimpleNamespace(remote_builder=True,maximum_hosts=5,scaling_mode='tiered',tier_job_memory_gib=15,project='p',disk_size_gb=100)
            with patch.object(controller,'begin_host_operation') as drain,\
                 patch.object(controller,'check_running_prices') as pricing,\
                 patch('lib.fleet_migration.time.time',return_value=1000):
                reconcile_migration([small],[],args,state,path)
            drain.assert_not_called()
            self.assertEqual(state['migration']['pending_tiers'],['large'])
            self.assertEqual(state['migration']['pending_slots'],[20])
            self.assertEqual(state['migration']['retained_ids'],[])
            self.assertEqual(state['migration']['replacement_ids'],[])

    def test_completion_holds_unresolved_ooms_on_retained_or_unrelated_hosts(self):
        import json
        old,new=vm('old',32,128,'1'),vm('retained',16,64,'2')
        for kind in ('runner','builder','host'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'state.json';state={}
                begin_migration([old,new],('large',),state,path,'shrink')
                state['migration'].update(pending_tiers=[],phase='drain')
                state['pending_oom']={'z/1':'runner','z/2':kind,'other/99':'builder'}
                state['seen_oom_ids']=['unresolved-on-retained']
                controller.write_scale_state(path,state)
                reconcile_migration([new],[],SimpleNamespace(),state,path)
                saved=json.loads(path.read_text())
                self.assertEqual(saved.get('pending_oom'),{'z/1':'runner','z/2':kind,'other/99':'builder'})
                self.assertIn('migration',saved)
                self.assertEqual(saved['seen_oom_ids'],['unresolved-on-retained'])

    def test_invalid_tier_receipt_cannot_be_marked_imported(self):
        import json
        from lib.spot_runner_autoscaler import import_deployment_receipt
        host=vm('a',16,64,'1')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';receipt=Path(folder)/'receipt.json'
            receipt.write_text(json.dumps({**asdict(host),'tier':'large','expected_runner_count':100}))
            state={'host_operation':{'kind':'launch','vm':asdict(host),'deployment_receipt':str(receipt)}}
            with self.assertRaises(RuntimeError):import_deployment_receipt(state,path)
            self.assertFalse(state['host_operation'].get('receipt_imported',False))

    def test_changed_attached_disk_blocks_tier_delete(self):
        from lib.spot_runner_autoscaler import delete_drained_vm
        from dataclasses import replace
        import json
        host=replace(vm('a',8,16,'1'),disks=({'source':'old','diskId':'10'},))
        with patch('lib.spot_runner_autoscaler.subprocess.run',return_value=SimpleNamespace(stdout=json.dumps({'id':'1','disks':[{'source':'new'}]}))) as provider:
            with self.assertRaisesRegex(RuntimeError,'disk'):
                delete_drained_vm(host,SimpleNamespace(project='p',scaling_mode='tiered'))
        self.assertEqual(provider.call_count,1)

    def test_dynamic_readiness_falls_back_only_for_legacy_receipts(self):
        host=vm('a',16,64,'1')
        self.assertEqual(expected_slots(host,{}),8)
        self.assertEqual(expected_slots(host,{'host_deployments':{'z/1':{'expected_runner_count':20}}}),20)

    def test_persisted_source_waits_for_two_replacement_health_windows(self):
        old,new=vm('old',32,128,'1'),vm('new',8,16,'2')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json'; state={}
            begin_migration([old],('starting',),state,path,'migration')
            state['migration'].update(pending_tiers=[],replacement_ids=['2'],phase='healthy')
            state['host_deployments']={'z/2':{'expected_runner_count':8}}
            runners=[GitHubRunner(i,f'gha-{new.name}-{i}','online',False,('spot','X64')) for i in range(1,9)]
            args=SimpleNamespace(org='org',remote_builder=False,maximum_hosts=5,scaling_mode='tiered',tier_job_memory_gib=2)
            for tick in (1000,1300):
                state['latest_resources']=[dict(instance_id=identity,zone='z',observed_at=tick,cpu_fraction=.1,memory_fraction=.2,peak_cpu_cores=.1,peak_ram_gib=.5,
                    io_complete=True,io_pressure_peaks=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0)) for identity in ('1','2')]
                state['latest_workload']={'complete':True}
                state['host_health']={f'z/{identity}':{'instance_id':identity,'boot_id':'boot-'+identity,'boot_oom_complete':True,
                    'boot_host_oom_ids':[],'complete':True,'observed_at':tick,'events':[]} for identity in ('1','2')}
                with patch('lib.fleet_migration.time.time',return_value=tick), patch('lib.spot_runner_autoscaler.begin_host_operation') as drain:
                    reconcile_migration([old,new],runners,args,state,path)
                    self.assertEqual(drain.call_count,0 if tick==1000 else 1)
                    if tick==1300:self.assertEqual(drain.call_args.args[1].instance_id,'1')

    def test_identity_change_and_five_host_limit_cannot_authorize_delete_or_launch(self):
        old=vm('old',32,128,'1')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';state={}
            begin_migration([old],('starting',),state,path,'migration')
            args=SimpleNamespace(maximum_hosts=5)
            with patch('lib.spot_runner_autoscaler.scale_up') as launch:
                reconcile_migration([vm('old',32,128,'changed')],[],args,state,path)
                launch.assert_not_called()
            with patch('lib.spot_runner_autoscaler.scale_up') as launch:
                reconcile_migration([old]+[vm(str(i),16,64,str(i+2)) for i in range(4)],[],args,state,path)
                launch.assert_not_called()

    def test_launch_intent_is_linked_before_provider_submission(self):
        from lib.spot_runner_autoscaler import begin_host_operation
        host=vm('new',8,16,'')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json'
            state={'migration':{'pending_tiers':['starting']}}
            begin_host_operation('launch',host,[],[],state,path)
            self.assertEqual(state['migration']['awaiting_launch']['name'],host.name)

    def test_awaiting_launch_reused_identity_holds_without_accepting_replacement(self):
        old,new=vm('old',16,64,'1'),vm('new',8,16,'different')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';state={}
            begin_migration([old],('starting',),state,path,'migration')
            state['migration']['awaiting_launch']={**asdict(new),'instance_id':'recorded'}
            with patch('lib.spot_runner_autoscaler.scale_up',side_effect=AssertionError('launch')):
                reconcile_migration([old,new],[],SimpleNamespace(maximum_hosts=5),state,path)
            self.assertEqual(state['migration']['pending_tiers'],['starting'])
            self.assertEqual(state['migration']['replacement_ids'],[])

    def test_failed_tier_canary_switches_to_drain_instead_of_force_delete(self):
        from lib.spot_runner_autoscaler import abandon_launch
        host=vm('new',8,16,'1')
        state={'host_operation':{'kind':'launch','vm':asdict(host),'abandon':'partial registration'}}
        with tempfile.TemporaryDirectory() as folder:
            with patch('lib.spot_runner_autoscaler.list_gcloud_vms',return_value=[host]),\
                 patch('lib.spot_runner_autoscaler.list_github_runners',return_value=[]),\
                 patch('lib.spot_runner_autoscaler.subprocess.run',side_effect=AssertionError('delete')):
                self.assertFalse(abandon_launch(SimpleNamespace(project='p',org='o',scaling_mode='tiered'),state,Path(folder)/'state.json'))
        self.assertEqual(state['host_operation']['kind'],'drain')

    def test_failed_tier_launch_cannot_adopt_a_reused_identity(self):
        from lib.spot_runner_autoscaler import abandon_launch
        host=vm('new',8,16,'1');changed=vm('new',8,16,'2')
        state={'host_operation':{'kind':'launch','vm':asdict(host),'abandon':'failed'}}
        with tempfile.TemporaryDirectory() as folder:
            with patch('lib.spot_runner_autoscaler.list_gcloud_vms',return_value=[changed]),\
                 patch('lib.spot_runner_autoscaler.subprocess.run',side_effect=AssertionError('delete')):
                self.assertFalse(abandon_launch(SimpleNamespace(project='p',org='o',scaling_mode='tiered'),state,Path(folder)/'state.json'))
        self.assertEqual(state['host_operation']['vm']['instance_id'],'1')

    def test_cpu_overlap_boundary_uses_safe_drain_first_below_five_hosts(self):
        hosts=[vm('a',32,128,'1'),vm('b',32,128,'2'),vm('c',16,64,'3')]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';state={}
            begin_migration(hosts,('bulky','bulky','expanded'),state,path,'shrink')
            state['latest_workload']={'complete':True}
            args=SimpleNamespace(maximum_hosts=5,remote_builder=False,tier_job_memory_gib=15)
            runners=[GitHubRunner(i,f'gha-{host.name}-{i}','online',False,()) for host in hosts[:2] for i in range(1,9)]
            for tick in (1000,1300):
                state['latest_resources']=[dict(instance_id=host.instance_id,zone='z',observed_at=tick,
                    peak_cpu_cores=1,peak_ram_gib=2,cpu_fraction=.1,memory_fraction=.1,
                    io_complete=True,io_pressure_peaks=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0)) for host in hosts]
                state['host_health']={f'z/{host.instance_id}':dict(instance_id=host.instance_id,boot_id='boot-'+host.instance_id,
                    complete=True,boot_oom_complete=True,boot_host_oom_ids=[],observed_at=tick,events=[]) for host in hosts}
                with patch('lib.spot_runner_autoscaler.begin_host_operation') as drain,\
                     patch('lib.fleet_migration.time.time',return_value=tick),\
                     patch('lib.spot_runner_autoscaler.check_running_prices',side_effect=AssertionError('launch pricing')):
                    reconcile_migration(hosts,runners,args,state,path)
                    self.assertEqual(drain.call_count,0 if tick==1000 else 1)
                    if tick==1300:self.assertEqual(drain.call_args.args[1].instance_id,'3')
