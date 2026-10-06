import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from dataclasses import asdict
from unittest.mock import patch
from job_coverage_fixtures import complete_job_tracking

from lib import fleet_evidence,host_evidence
from lib.runner_fleet import GceVm,GitHubRunner
from lib.runner_tiers import TIERS
from lib.spot_runner_autoscaler import eligible_tier_offers
from lib.compute_api import Offer
from lib.storage_selection import cheapest_storage
from lib.tier_policy import plan_capacity
from lib.fleet_migration import begin_migration,reconcile_migration
from lib import spot_runner_autoscaler as controller
from lib.tier_controller import check_tiers,evidence_for


def pressure(full=0,some=0):
    return dict(full_avg10=full,full_avg60=full,full_avg300=full,full_total=10,
                some_avg10=some,some_avg60=some,some_avg300=some,some_total=20)


def resources(full=0,some=0):
    return dict(instance_id='2',zone='z',observed_at=1000,windows=6,cpu_fraction=.1,memory_fraction=.1,
                cpu_cores=1,peak_cpu_cores=1,used_ram_gib=2,peak_ram_gib=2,
                io_complete=True,io_pressure_peaks=pressure(full,some))


class IoPolicyTests(unittest.TestCase):
    def test_performance_floor_keeps_cheap_boot_and_charges_balanced_data(self):
        rates={'pd-standard:capacity':Decimal('.04'),'pd-balanced:capacity':Decimal('.10')}
        self.assertEqual(cheapest_storage('c2d-standard-32',100,rates),
                         ('pd-standard','pd-balanced',Decimal('11.60')))
        with self.assertRaises(RuntimeError):
            cheapest_storage('c2d-standard-32',100,{'pd-standard:capacity':Decimal('.04')})

    def test_performance_floor_does_not_relax_tier_all_disk_caps(self):
        rates={'r':{'pd-standard:capacity':Decimal('.04'),'pd-balanced:capacity':Decimal('.10')}}
        for name in ('small','starting','expanded','large','bulky'):
            tier=TIERS[name];price=float((tier.max_daily_usd-Decimal('.3'))/24)
            offer=Offer('r',f'c2d-standard-{tier.vcpu}',tier.vcpu,tier.ram_gib,price)
            self.assertEqual(eligible_tier_offers([offer],tier,rates),[])

    def test_sample_includes_bounded_validated_io_pressure_without_breaking_oom(self):
        psi='some avg10=92.50 avg60=60.00 avg300=25.00 total=12345\nfull avg10=85.00 avg60=55.00 avg300=20.00 total=10000\n'
        def read(path,*args,**kwargs):
            return {'/proc/sys/kernel/random/boot_id':'boot','/proc/meminfo':'MemTotal: 16000000 kB\nMemAvailable: 12000000 kB\n','/proc/pressure/io':psi}[str(path)]
        with patch.object(Path,'read_text',read),patch.object(host_evidence,'command_json_lines',return_value=[]):
            data=host_evidence.sample()
        self.assertTrue(data.get('io_complete'))
        self.assertEqual(data['io_pressure']['full_avg10'],85)
        self.assertTrue(data['complete'])

    def test_io_validation_rejects_missing_partial_nonfinite_out_of_range_or_oversized(self):
        parser=getattr(host_evidence,'parse_io_pressure',lambda text:None)
        valid='some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
        self.assertEqual(parser(valid),pressure(0,0)|{'full_total':0,'some_total':0})
        for invalid in ('',valid.splitlines()[0],valid.replace('0.00','nan',1),
                        valid.replace('0.00','101.00',1),valid+'x'*1024,valid.replace('total=0','total=-1',1),
                        valid.replace('total=0','total='+'9'*400,1)):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):parser(invalid)

    def test_fresh_vm_boot_pressure_history_is_propagated_and_reset(self):
        record=getattr(fleet_evidence,'record_io_pressure',lambda *args:None)
        state={};evidence=[resources()]
        health={'z/2':dict(instance_id='2',boot_id='one',observed_at=1000,io_complete=True,io_pressure=pressure(90))}
        record(state,evidence,health,1000)
        self.assertEqual(evidence[0]['io_pressure_peaks']['full_avg10'],90)
        health['z/2'].update(observed_at=1300,io_pressure=pressure())
        record(state,evidence,health,1300)
        self.assertEqual(evidence[0]['io_pressure_peaks']['full_avg10'],90)
        health['z/2'].update(boot_id='two',observed_at=1600)
        record(state,evidence,health,1600)
        self.assertEqual(evidence[0]['io_pressure_peaks']['full_avg10'],0)
        health['z/2']['observed_at']=1000
        record(state,evidence,health,2000)
        self.assertFalse(evidence[0]['io_complete'])

    def test_stall_inside_same_window_cannot_disappear_on_next_poll(self):
        state={};rows=[resources()]
        health={'z/2':dict(instance_id='2',boot_id='boot',observed_at=1000,io_complete=True,io_pressure=pressure())}
        fleet_evidence.record_io_pressure(state,rows,health,1000)
        health['z/2'].update(observed_at=1100,io_pressure=pressure(90))
        fleet_evidence.record_io_pressure(state,rows,health,1100)
        health['z/2'].update(observed_at=1300,io_pressure=pressure())
        fleet_evidence.record_io_pressure(state,rows,health,1300)
        self.assertEqual(rows[0]['io_pressure_peaks']['full_avg10'],90)

    def test_drain_phase_rechecks_io_before_another_source_is_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            old=GceVm('oxidex-runners-old','z','RUNNING','n2-standard-32',True,32,128,instance_id='1')
            state={};path=Path(folder)/'state.json'
            begin_migration([old],('expanded',),state,path,'replacement')
            state['migration'].update(pending_tiers=[],phase='drain')
            state['latest_resources']=[resources(90)]
            with patch('lib.spot_runner_autoscaler.begin_host_operation') as drain:
                reconcile_migration([old],[],SimpleNamespace(),state,path)
                drain.assert_not_called()

    def test_high_or_missing_io_blocks_density_shrink_and_storage_duration_growth(self):
        for variant in ('high','missing'):
            row=resources(90 if variant=='high' else 0)
            if variant=='missing':row.pop('io_complete')
            for topology,workload in ((('expanded',),dict(queue_delayed=True,slots=8)),
                                      (('starting',),{}),(('starting',),dict(duration_regression=True))):
                state={}
                for tick in range(1000,2800,300):
                    result=plan_capacity(topology,[row],workload,state,tick)
                self.assertEqual(result['action'],'hold',(variant,topology,workload,result))
                self.assertIn('IO',result['reason'])

    def test_io_pressure_allows_memory_relief_and_container_budget_repair(self):
        row=resources(90)
        self.assertEqual(plan_capacity(('starting',),[row],dict(container_oom=True),{},1000)['action'],'budget')
        self.assertEqual(plan_capacity(('starting',),[row],dict(host_oom=True),{},1000)['target'],('expanded',))

    def test_known_workload_reservation_skips_tiny_empty_hosts_and_blocks_shrink(self):
        workload=dict(minimum_job_memory_gib=15)
        self.assertEqual(plan_capacity((),None,workload,{},1000)['target'],('expanded',))
        state={}
        for tick in range(1000,2800,300):
            result=plan_capacity(('expanded',),[resources()],workload,state,tick)
        self.assertEqual(result['action'],'hold')
        self.assertIn('reservation',result['reason'])

    def test_density_that_cannot_increase_admission_concurrency_grows_topology(self):
        state={};workload=dict(minimum_job_memory_gib=15,queue_delayed=True,slots=8)
        plan_capacity(('expanded',),[resources()],workload,state,1000)
        result=plan_capacity(('expanded',),[resources()],workload,state,1300)
        self.assertEqual(result['action'],'topology')
        self.assertEqual(result['target'],('large',))

    def test_queue_growth_advances_new_tail_until_known_reservation_fits(self):
        state={};workload=dict(minimum_job_memory_gib=15,queue_delayed=True,slots=20)
        plan_capacity(('large',),[resources()],workload,state,1000)
        result=plan_capacity(('large',),[resources()],workload,state,1300)
        self.assertEqual(result['target'],('large','expanded'))

    def test_composed_evidence_uses_post_ssh_clock_for_io_without_changing_cpu_window(self):
        vm=GceVm('oxidex-runners-a','z','RUNNING','n2-standard-8',True,8,32,instance_id='1')
        args=controller.args_parser().parse_args(['--scaling-mode','tiered'])
        for guest_tick in (1010,1030):
            state={'job_tracking':complete_job_tracking(args.org+'/oxidex','1970-01-01T00:16:40Z')}
            health=dict(instance_id='1',boot_id='boot',observed_at=guest_tick,complete=True,
                        events=[],io_complete=True,io_pressure=pressure())
            with patch('lib.tier_controller.time.time',side_effect=[1000,1011,1011]),\
                 patch.object(controller,'read_utilization',return_value=[(.1,.1)]),\
                 patch('lib.tier_controller.read_host_events',return_value=health):
                evidence,_,tick=evidence_for([vm],[],args,state)
            self.assertEqual(evidence[0]['io_complete'],guest_tick==1010)
            self.assertEqual(evidence[0]['observed_at'],1000)
            self.assertEqual(tick,1000)

    def test_loss_recovery_escalates_incompatible_receipt_without_requiring_io(self):
        lost=GceVm('oxidex-runners-lost','z','RUNNING','c2d-highcpu-8',True,8,16,instance_id='1')
        state=dict(desired_topology=['starting'],pending_vm_recovery={'lost':dict(**asdict(lost),_cleanup_complete=True)},
                   host_deployments={'z/1':dict(tier='starting',expected_runner_count=8)})
        args=controller.args_parser().parse_args(['--scaling-mode','tiered','--tier-job-memory-gib','15'])
        with tempfile.TemporaryDirectory() as folder,\
             patch('lib.tier_controller.evidence_for',return_value=(None,dict(complete=False,minimum_job_memory_gib=15),1000)),\
             patch.object(controller,'check_running_prices'),\
             patch.object(controller,'discover_tier_offers',return_value=[SimpleNamespace(total_daily_usd=Decimal('1'))]),\
             patch.object(controller,'scale_up') as launch:
            check_tiers([],[],args,state,Path(folder)/'state.json')
        self.assertEqual(state['migration']['target'],['expanded'])
        self.assertEqual(args.launch_slots,8)
        self.assertEqual(launch.call_count,1)

    def test_high_or_missing_io_never_qualifies_replacement_for_source_drain(self):
        for missing in (False,True):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as folder:
                old=GceVm('oxidex-runners-old','z','RUNNING','n2-standard-32',True,32,128,instance_id='1')
                new=GceVm('oxidex-runners-new','z','RUNNING','n2-standard-8',True,8,32,instance_id='2')
                state={};path=Path(folder)/'state.json'
                begin_migration([old],('expanded',),state,path,'replacement')
                state['migration'].update(pending_tiers=[],replacement_ids=['2'],phase='healthy')
                state['host_deployments']={'z/2':dict(expected_runner_count=8)}
                runners=[GitHubRunner(i,f'gha-{new.name}-{i}','online',False,()) for i in range(1,9)]
                for tick in (1000,1300):
                    rows=[resources(0 if missing else 90),resources()];rows[1]['instance_id']='1'
                    if missing:rows[0].pop('io_complete')
                    for row in rows:row['observed_at']=tick
                    state['latest_resources']=rows;state['latest_workload']={'complete':True}
                    state['host_health']={'z/2':dict(complete=True,observed_at=tick,events=[])}
                    with patch('lib.fleet_migration.time.time',return_value=tick),\
                         patch('lib.spot_runner_autoscaler.begin_host_operation') as drain:
                        reconcile_migration([old,new],runners,SimpleNamespace(remote_builder=False),state,path)
                        drain.assert_not_called()
                self.assertEqual(state['migration']['healthy_checks']['z/2']['count'],0)
