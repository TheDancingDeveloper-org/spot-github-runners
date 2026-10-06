"""Preserve measured history across early polls without changing distinct-window gates."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from job_coverage_fixtures import complete_job_tracking
from lib import spot_runner_autoscaler as controller
from lib.fleet_evidence import record_resources,record_io_pressure
from lib.fleet_migration import begin_migration
from lib.runner_fleet import GceVm


class StopPoll(Exception):pass


def host(name,identity,storage='pd-balanced'):
    return GceVm('oxidex-runners-'+name,'z','RUNNING','n2-standard-32',True,32,128,instance_id=identity,
        disks=({'source':'https://www.googleapis.com/compute/v1/projects/p/zones/z/disks/'+name,
                'diskId':identity+'0','deviceName':'runner-data-'+name,'boot':False,'diskType':storage,'diskSizeGb':100},))


def health(vm,tick):
    return dict(instance_id=vm.instance_id,boot_id='boot-'+vm.instance_id,complete=True,observed_at=tick,
        boot_oom_complete=True,boot_host_oom_ids=[],events=[],io_complete=True,
        io_pressure={scope+'_'+name:0 for scope in ('full','some') for name in ('avg10','avg60','avg300','total')})


class ObservationContinuityTests(unittest.TestCase):
    def test_early_resource_sample_then_timely_json_restart_sample_keeps_real_peak(self):
        vm=host('old','1');state={}
        state['latest_resources']=record_resources(state,[vm],[(.4,.2)],1791116407.821197)
        state['latest_resources']=record_resources(state,[vm],[(.01,.01)],1791116554.193166)
        state=json.loads(json.dumps(state))
        result=record_resources(state,[vm],[(.01,.01)],1791116896.635038)
        self.assertEqual(result[0]['windows'],2)
        self.assertEqual(result[0]['peak_cpu_cores'],12.8)

    def test_early_io_sample_then_timely_json_restart_sample_keeps_real_pressure(self):
        vm=host('old','1');state={};resources=[dict(zone='z',instance_id='1')]
        first=health(vm,1791116413.7001245);first['io_pressure']['full_avg10']=90
        record_io_pressure(state,resources,{'z/1':first},first['observed_at']);state['host_health']={'z/1':first}
        second=health(vm,1791116560.4303892)
        record_io_pressure(state,resources,{'z/1':second},second['observed_at']);state['host_health']={'z/1':second}
        state=json.loads(json.dumps(state));third=health(vm,1791116903.1948667)
        record_io_pressure(state,resources,{'z/1':third},third['observed_at'])
        self.assertEqual(len(state['io_history']['z/1']['windows']),2)
        self.assertEqual(resources[0]['io_pressure_peaks']['full_avg10'],90)

    def test_real_controller_restart_collects_six_windows_and_retargets_without_losing_demand(self):
        hosts=[host('good','1'),host('bad','2','pd-standard')]
        anchors=[1791114647.086041,1791114996.064857,1791115350.141613,1791115707.252349,1791116058.492896,1791116407.821197]
        clock=[1791116554.193166-42.441872];observations=[];snapshots=[]
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            path=Path(folder)/'state.json';state={}
            begin_migration(hosts[:1],('bulky',),state,path,'tier price replacement',force_ids=['1'])
            state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['2'],phase='healthy')
            state['resource_roster']=['z/1']  # Old resource-mode metadata does not control tier evidence.
            for tick in anchors:
                state['latest_resources']=record_resources(state,hosts,[(.4,.2),(.03,.05)],tick)
                rows={f'z/{vm.instance_id}':health(vm,tick+6) for vm in hosts}
                record_io_pressure(state,state['latest_resources'],rows,tick+6);state['host_health']=rows
            controller.write_scale_state(path,state)
            args=controller.args_parser().parse_args(['--project','p','--scaling-mode','tiered','--tier-job-memory-gib','15','--state-file',str(path)])
            def runners(*_):clock[0]+=42.441872;return []
            class Jobs:
                def __init__(self,state,save,*_):self.state=state;self.save=save
                def poll(self,vms):
                    self.state['job_tracking']=complete_job_tracking(args.org+'/oxidex',datetime.fromtimestamp(clock[0],timezone.utc).isoformat())
                    self.save()
            def utilization(project,vms,tick):observations.append(tick);return [(.01,.01)]*len(vms)
            def guest(project,vm,ids):clock[0]+=5;return health(vm,clock[0])
            def sleep(delay):
                snapshots.append(controller.read_scale_state(path));clock[0]+=delay
                if len(observations)==7:raise StopPoll
            for target,value in [('list_github_runners',runners),('read_utilization',utilization)]:
                stack.enter_context(patch.object(controller,target,side_effect=value))
            stack.enter_context(patch.object(controller,'list_gcloud_vms',return_value=hosts))
            stack.enter_context(patch.object(controller,'JobRecovery',Jobs))
            stack.enter_context(patch.object(controller.time,'time',side_effect=lambda:clock[0]))
            stack.enter_context(patch.object(controller.time,'sleep',side_effect=sleep))
            stack.enter_context(patch('lib.tier_controller.read_host_events',side_effect=guest))
            for target,value in [('load_cpu_platform_map',{}),('clear_catalog_cache',None),('log_hourly_spend',None)]:
                stack.enter_context(patch.object(controller,target,return_value=value))
            stack.enter_context(patch.object(controller,'adjust_admission',side_effect=AssertionError('unexpected cap update')))
            stack.enter_context(patch.object(controller,'builder_ready',side_effect=AssertionError('standard disk passed readiness')))
            stack.enter_context(patch.object(controller,'scale_up',side_effect=AssertionError('no launch in history collection')))
            with self.assertRaises(StopPoll):controller._run_controller(args)
        self.assertAlmostEqual(observations[0],1791116554.193166)
        self.assertAlmostEqual(observations[1],1791116896.635038)
        self.assertEqual(snapshots[0]['latest_resources'][0]['windows'],5)
        self.assertEqual(snapshots[1]['latest_resources'][0]['windows'],5)
        self.assertEqual(snapshots[1]['latest_resources'][0]['peak_cpu_cores'],12.8)
        self.assertEqual(snapshots[-1]['latest_resources'][0]['windows'],6)
        self.assertEqual(snapshots[-1]['migration']['reason'],'storage-pressure canary replacement')
        self.assertEqual(snapshots[-1]['migration']['target'],['large'])
        self.assertEqual(snapshots[-1]['resource_roster'],['z/1'])

    def test_actual_observation_gap_and_clock_reversal_keep_strict_reset_rules(self):
        for next_tick in (1550,1551,1099):
            with self.subTest(next_tick=next_tick):
                vm=host('old','1');state={};resources=[dict(zone='z',instance_id='1')]
                for tick in (1000,1100):
                    state['latest_resources']=record_resources(state,[vm],[(.4 if tick==1000 else .01,.2)],tick)
                    current=health(vm,tick);current['io_pressure']['full_avg10']=90 if tick==1000 else 0
                    record_io_pressure(state,resources,{'z/1':current},tick);state['host_health']={'z/1':current}
                state=json.loads(json.dumps(state))
                result=record_resources(state,[vm],[(.01,.01)],next_tick)
                record_io_pressure(state,resources,{'z/1':health(vm,next_tick)},next_tick)
                allowed=next_tick==1550
                self.assertEqual(result[0]['windows'],2 if allowed else 1)
                self.assertEqual(result[0]['peak_cpu_cores'],12.8 if allowed else .32)
                self.assertEqual(resources[0]['io_pressure_peaks']['full_avg10'],90 if allowed else 0)

    def test_old_json_without_observation_metadata_uses_exact_previous_fresh_samples(self):
        vm=host('old','1');state={};resources=[dict(zone='z',instance_id='1')]
        for tick in (1000,1200):
            state['latest_resources']=record_resources(state,[vm],[(.4 if tick==1000 else .01,.2)],tick)
            current=health(vm,tick);current['io_pressure']['full_avg10']=90 if tick==1000 else 0
            record_io_pressure(state,resources,{'z/1':current},tick);state['host_health']={'z/1':current}
        for windows in (state['resource_history']['z/1'],state['io_history']['z/1']['windows']):
            for row in windows:row.pop('last_observed_at')
        state=json.loads(json.dumps(state))
        result=record_resources(state,[vm],[(.01,.01)],1640)
        record_io_pressure(state,resources,{'z/1':health(vm,1640)},1640)
        self.assertEqual(result[0]['windows'],2);self.assertEqual(result[0]['peak_ram_gib'],25.6)
        self.assertEqual(resources[0]['io_pressure_peaks']['full_avg10'],90)

    def test_invalid_inputs_clear_continuity_and_malformed_observation_metadata_cannot_hold_history(self):
        vm=host('old','1');resources=[dict(zone='z',instance_id='1')]
        for defect in ('metrics','io','future_io','marker_bool','marker_nan'):
            with self.subTest(defect=defect):
                state={};state['latest_resources']=record_resources(state,[vm],[(.4,.2)],1000)
                current=health(vm,1000);current['io_pressure']['full_avg10']=90
                record_io_pressure(state,resources,{'z/1':current},1000);state['host_health']={'z/1':current}
                if defect=='metrics':
                    self.assertIsNone(record_resources(state,[vm],None,1200));self.assertEqual(state['resource_history'],{})
                elif defect in ('io','future_io'):
                    bad=health(vm,1200 if defect=='io' else 1201)
                    if defect=='io':bad['io_pressure']['full_avg10']=-1
                    record_io_pressure(state,resources,{'z/1':bad},1200)
                    self.assertFalse(resources[0]['io_complete']);self.assertEqual(state['io_history'],{})
                else:
                    value=True if defect=='marker_bool' else float('nan')
                    state['resource_history']['z/1'][-1]['last_observed_at']=value
                    state['io_history']['z/1']['windows'][-1]['last_observed_at']=value
                    result=record_resources(state,[vm],[(.01,.01)],1300)
                    record_io_pressure(state,resources,{'z/1':health(vm,1300)},1300)
                    self.assertEqual(result[0]['windows'],1);self.assertEqual(result[0]['peak_cpu_cores'],.32)
                    self.assertEqual(resources[0]['io_pressure_peaks']['full_avg10'],0)

    def test_early_observations_do_not_add_windows_or_extend_1800_second_retention(self):
        vm=host('old','1');state={};resources=[dict(zone='z',instance_id='1')]
        for tick in range(0,1851,50):
            state['latest_resources']=record_resources(state,[vm],[(.4 if tick==0 else .01,.2 if tick==0 else .01)],tick)
            current=health(vm,tick);current['io_pressure']['full_avg10']=90 if tick==0 else 0
            record_io_pressure(state,resources,{'z/1':current},tick);state['host_health']={'z/1':current}
            if tick==250:self.assertEqual(state['latest_resources'][0]['windows'],1)
            if tick==1800:self.assertEqual(state['latest_resources'][0]['peak_cpu_cores'],12.8)
        self.assertEqual(state['latest_resources'][0]['windows'],6)
        self.assertEqual(state['latest_resources'][0]['peak_cpu_cores'],.32)
        self.assertEqual(resources[0]['io_pressure_peaks']['full_avg10'],0)
        self.assertTrue(all(1850-row['observed_at']<=1800 for row in state['resource_history']['z/1']))
        rebooted=health(vm,1900);rebooted['boot_id']='reboot'
        record_io_pressure(state,resources,{'z/1':rebooted},1900)
        self.assertEqual(len(state['io_history']['z/1']['windows']),1)

    def test_historical_promoted_oom_ids_repair_only_once_during_unsupported_disk_migration(self):
        from lib.tier_controller import check_tiers
        from lib.runner_fleet import GitHubRunner
        hosts=[host('good','1'),host('bad','2','pd-standard')];ids=[f'{index:064x}' for index in range(1,15)]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';state={};args=controller.args_parser().parse_args(['--project','p','--scaling-mode','tiered','--tier-job-memory-gib','15','--state-file',str(path)])
            begin_migration(hosts[:1],('bulky',),state,path,'tier price replacement',force_ids=['1'])
            state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['2'],phase='healthy')
            state.update(seen_oom_ids=ids,host_health={'z/2':dict(boot_id='boot-2')},pending_oom={'z/2':'unknown'},
                pending_oom_origins={'z/2':dict(kind='unknown',boot_id='boot-2',event_ids=ids,quarantined=False)})
            state['host_deployments']={'z/2':dict(tier='bulky',expected_runner_count=16,runner_memory_gib=15)}
            runners=[GitHubRunner(index,f'gha-{hosts[1].name}-{index}','online',False,()) for index in range(1,17)]
            with patch.object(controller,'adjust_admission',return_value=True) as adjust,\
                 patch.object(controller,'read_utilization',return_value=[(.01,.01),(.01,.01)]),\
                 patch.object(controller,'builder_ready',side_effect=AssertionError('standard disk passed readiness')):
                for tick in (1000,1300,1600,1900):
                    cursors=[]
                    def guest(project,vm,cursor):
                        cursors.append((vm.instance_id,cursor));result=health(vm,tick)
                        result.update(reclassification_complete=bool(cursor),reclassified_events=[dict(id=identity,kind='container',workload='runner',at=100) for identity in cursor])
                        return result
                    state['job_tracking']=complete_job_tracking(args.org+'/oxidex',datetime.fromtimestamp(tick,timezone.utc).isoformat())
                    with patch('lib.tier_controller.read_host_events',side_effect=guest),patch('lib.tier_controller.time.time',return_value=tick):
                        check_tiers(hosts,runners,args,state,path)
                    self.assertEqual(dict(cursors)['2'],ids if tick==1000 else [])
                    self.assertEqual(adjust.call_count,1)
                    self.assertEqual(state['pending_oom'],{})
                    self.assertEqual(set(state['migration']['oom_budget_repairs']),set(ids))
                    self.assertEqual(state['migration']['healthy_checks']['z/2']['count'],0)
                    state=json.loads(path.read_text())

if __name__=='__main__':unittest.main()
