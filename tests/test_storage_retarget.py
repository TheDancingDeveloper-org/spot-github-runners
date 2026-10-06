import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from lib import fleet_migration
from lib.runner_fleet import GceVm,GitHubRunner


class StorageRetargetTests(unittest.TestCase):
    def fixture(self,path):
        hosts=[GceVm('oxidex-runners-'+name,'z','RUNNING','n2-standard-32',True,32,128,
                      instance_id=identity,disks=({'source':'disk-'+identity,'diskId':identity+'0','deviceName':'runner-data-'+name,
                          'boot':False,'diskType':'pd-balanced','diskSizeGb':100},))
               for name,identity in (('old','1'),('bad','2'))]
        state={}
        fleet_migration.begin_migration(hosts[:1],('bulky',),state,path,'oversized migration',force_ids=['1'])
        state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['2'],phase='healthy')
        state['host_deployments']={'z/1':dict(expected_runner_count=8,runner_memory_gib=15),
                                   'z/2':dict(expected_runner_count=16,runner_memory_gib=2)}
        state['latest_workload']=dict(complete=True,minimum_job_memory_gib=15)
        state['latest_resources']=[dict(instance_id=host.instance_id,zone='z',observed_at=2500,windows=6,
            cpu_cores=2,peak_cpu_cores=3,used_ram_gib=8,peak_ram_gib=12,cpu_fraction=.1,memory_fraction=.1,
            io_complete=True,io_pressure_peaks=dict(full_avg10=90 if host.instance_id=='2' else 0,
                full_avg60=90 if host.instance_id=='2' else 0,some_avg10=0,some_avg60=0)) for host in hosts]
        state['host_health']={f'z/{host.instance_id}':dict(instance_id=host.instance_id,boot_id='boot-'+host.instance_id,
            complete=True,boot_oom_complete=True,boot_host_oom_ids=[],observed_at=2500,events=[],io_complete=True)
            for host in hosts}
        state['io_history']={'z/2':dict(boot_id='boot-2',windows=[dict(observed_at=tick,
            full_avg10=90,full_avg60=90,some_avg10=0,some_avg60=0) for tick in (2200,2500)])}
        return hosts,state,SimpleNamespace(tier_job_memory_gib=15,maximum_hosts=5)

    def retarget(self,hosts,args,state,path):
        helper=getattr(fleet_migration,'retarget_unhealthy_storage_canary',lambda *args:False)
        return helper(hosts,args,state,path)

    def test_bad_two_host_canary_becomes_one_large_plan_without_draining(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';hosts,state,args=self.fixture(path)
            before=copy.deepcopy(state['migration'])
            tracker=state.setdefault('job_tracking',{})
            with patch('lib.fleet_migration.time.time',return_value=2500),\
                 patch('lib.spot_runner_autoscaler.begin_host_operation',side_effect=AssertionError('drain')):
                self.assertTrue(self.retarget(hosts,args,state,path))
            self.assertEqual(state['migration']['target'],['large'])
            self.assertEqual(state['migration']['sources'][0]['instance_id'],'2')
            self.assertEqual(state['migration']['pending_tiers'],['large'])
            self.assertEqual(state['migration']['pending_slots'],[20])
            self.assertEqual({row['instance_id'] for row in state['migration']['sources']},{'1','2'})
            self.assertEqual(state['migration_history'][-1]['migration'],before)
            self.assertIs(state['job_tracking'],tracker)

    def test_missing_identity_pressure_history_peak_headroom_or_global_proof_holds(self):
        variants=('operation','awaiting','reused','disk_changed','short_history','stale','one_io_window',
                  'unproven_global','actual_global','quarantined_host','big_ram','reservation_two','incomplete_jobs')
        for variant in variants:
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'state.json';hosts,state,args=self.fixture(path)
                if variant=='operation':state['host_operation']={'kind':'drain'}
                if variant=='awaiting':state['migration']['awaiting_launch']={'instance_id':'3'}
                if variant=='reused':state['migration']['sources'][0]['instance_id']='different'
                if variant=='disk_changed':state['migration']['sources'][0]['disks']=({'source':'changed','diskId':'99'},)
                if variant=='short_history':state['latest_resources'][0]['windows']=5
                if variant=='stale':state['latest_resources'][0]['observed_at']=1900
                if variant=='one_io_window':state['io_history']['z/2']['windows'].pop()
                if variant=='unproven_global':state['host_health']['z/2']['boot_oom_complete']=False
                if variant=='actual_global':state['host_health']['z/2']['boot_host_oom_ids']=['global']
                if variant=='quarantined_host':
                    state['pending_oom']={'z/2':'host'}
                    state['pending_oom_origins']={'z/2':dict(kind='host',quarantined=True)}
                if variant=='big_ram':state['latest_resources'][0]['peak_ram_gib']=100
                if variant=='reservation_two':args.tier_job_memory_gib=2
                if variant=='incomplete_jobs':state['latest_workload']['complete']=False
                before=copy.deepcopy(state)
                with patch('lib.fleet_migration.time.time',return_value=2500):
                    self.assertFalse(self.retarget(hosts,args,state,path))
                self.assertEqual(state,before)

    def test_source_io_pressure_can_retire_only_after_new_destination_passes_health(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';hosts,state,args=self.fixture(path)
            with patch('lib.fleet_migration.time.time',return_value=2500):self.retarget(hosts,args,state,path)
            new=GceVm('oxidex-runners-good','z','RUNNING','n2-standard-16',True,16,64,instance_id='3',disks=({'source':'disk-3','diskId':'30','deviceName':'runner-data-good',
                'boot':False,'diskType':'pd-balanced','diskSizeGb':100},))
            state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['3'],phase='healthy')
            state['host_deployments']['z/3']=dict(expected_runner_count=20,runner_memory_gib=15)
            row=copy.deepcopy(state['latest_resources'][0]);row.update(instance_id='3',io_pressure_peaks=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0))
            state['latest_resources'].append(row)
            state['host_health']['z/3']=dict(instance_id='3',boot_id='boot-3',complete=True,events=[],observed_at=2800,
                boot_oom_complete=True,boot_host_oom_ids=[])
            runners=[GitHubRunner(i,f'gha-{new.name}-{i}','online',False,()) for i in range(1,21)]
            for tick in (2800,3100):
                for row in state['latest_resources']:row['observed_at']=tick
                for health in state['host_health'].values():health['observed_at']=tick
                with patch('lib.fleet_migration.time.time',return_value=tick),\
                     patch('lib.spot_runner_autoscaler.begin_host_operation') as drain:
                    fleet_migration.reconcile_migration([*hosts,new],runners,SimpleNamespace(remote_builder=False),state,path)
                    self.assertEqual(drain.call_count,0 if tick==2800 else 1)
            state['host_health']['z/1']['boot_host_oom_ids']=['new-real-global']
            for row in state['latest_resources']:row['observed_at']=3400
            for health in state['host_health'].values():health['observed_at']=3400
            with patch('lib.fleet_migration.time.time',return_value=3400),\
                 patch('lib.spot_runner_autoscaler.begin_host_operation') as drain:
                fleet_migration.reconcile_migration([*hosts,new],runners,SimpleNamespace(remote_builder=False),state,path)
                drain.assert_not_called()
