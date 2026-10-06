"""Execute the root budget program against temporary config/ledger fixtures."""
import copy
import io
import json
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from lib.host_admission import LeasePool
from lib.runner_fleet import GceVm
from lib.spot_runner_autoscaler import adjust_admission

GIB=1024**3


class AdmissionCapRecoveryTests(unittest.TestCase):
    def run_program(self,*,leases=None,barrier=None,fail_update=None,envelope=32,kind='runner',fail_limits=False,reservation=2,docker_delay=0,resume=None):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config_path=root/'admission.json';ledger=root/'leases.json'
            builder=root/'builder.json'
            config=dict(work_gib=32,runner_gib=reservation,builder_gib=6,runner_names=['r1','r2'])
            if resume:config=copy.deepcopy(resume['config'])
            config_path.write_text(json.dumps(config));ledger.write_text(json.dumps(leases if leases is not None else (resume['ledger'] if resume else {})))
            builder.write_text(json.dumps(resume['builder'] if resume else dict(memory_gib=6,cpus=4)))
            records={name:dict(Id=letter*64,Name='/'+name,
                State=dict(Running=True,StartedAt='2026-10-04T00:00:00Z'),
                HostConfig=dict(Memory=2*GIB,MemorySwap=4*GIB))
                for name,letter in [('r1','a'),('r2','b')]}
            if resume:records=copy.deepcopy(resume['records'])
            updates=[];events=[];elapsed=[0]
            host=GceVm('vm','z','RUNNING','n2-standard-32',True,32,128,instance_id='12345')
            with patch('lib.spot_runner_autoscaler.subprocess.run',return_value=SimpleNamespace(returncode=75,stdout='')) as ssh:
                adjust_admission(host,SimpleNamespace(project='p'),kind)
            outer=next(arg.removeprefix('--command=') for arg in ssh.call_args.args[0] if arg.startswith('--command='))
            shell=shlex.split(outer)[-1]
            self.assertIn('= 12345 || exit 75;',shell)
            self.assertNotIn('systemctl',shell)
            program=shlex.split(shell.split('python3 -c ',1)[1])[0]
            for original,replacement in [('/etc/gha-admission.json',config_path),
                ('/etc/oxidex-remote-build.json',builder),('/var/lib/gha-admission/leases.json',ledger),
                ('/run/gha-admission-adjust.lock',root/'adjust.lock')]:
                program=program.replace(original,str(replacement))

            def root_request(action,who):
                self.assertEqual((action,who),('release','builder'))
                events.append('barrier')
                self.assertEqual(json.loads(config_path.read_text())[kind+'_gib'],4 if kind=='runner' else 12)
                if barrier:barrier(config_path,ledger,records)
                return True

            def docker(command,**kwargs):
                self.assertEqual(command[0],'/usr/bin/docker')
                elapsed[0]+=min(docker_delay,kwargs['timeout'])
                if docker_delay>kwargs['timeout']:
                    import subprocess
                    raise subprocess.TimeoutExpired(command,kwargs['timeout'])
                key=command[-1]
                row=records.get(key) or next((value for value in records.values() if value['Id']==key),None)
                if row is None:raise RuntimeError('Container identity no longer exists')
                if command[1]=='inspect':return SimpleNamespace(stdout=json.dumps([copy.deepcopy(row)]))
                self.assertEqual(command[1],'update')
                self.assertIn('barrier',events)
                self.assertEqual(key,row['Id'])
                target=float(command[2].split('=',1)[1][:-1])*GIB
                self.assertGreater(target,row['HostConfig']['Memory'])
                if fail_update==key:raise RuntimeError('Docker update failed')
                row['HostConfig'].update(Memory=int(target),MemorySwap=int(target));updates.append(key)
                return SimpleNamespace(stdout='',returncode=0)

            original_replace=Path.replace
            def replace(path,target):
                if fail_limits and path==builder.with_suffix('.tmp'):
                    raise OSError('Builder config disk write failed')
                return original_replace(path,target)
            module=SimpleNamespace(observed_envelope=lambda _:envelope,request=root_request)
            stdout=io.StringIO();status=0
            with patch.dict(sys.modules,{'host_admission':module}),patch('subprocess.run',side_effect=docker),\
                 patch.object(Path,'replace',replace),patch('time.monotonic',side_effect=lambda:elapsed[0]),redirect_stdout(stdout):
                try:exec(program,{})
                except SystemExit as result:status=result.code
            return dict(status=status,config=json.loads(config_path.read_text()),
                ledger=json.loads(ledger.read_text()),builder=json.loads(builder.read_text()),
                records=records,updates=updates,receipt=json.loads(stdout.getvalue().splitlines()[-1]))

    def owner(self,letter):
        return 'container:'+letter*64+':2026-10-04T00:00:00Z'

    def test_active_job_keeps_original_cap_and_reservation_while_idle_runner_grows(self):
        leases={self.owner('a'):2,'host:999:1':6}
        result=self.run_program(leases=leases)
        self.assertEqual(result['status'],75)
        self.assertEqual(result['ledger'],leases)
        self.assertEqual(result['records']['r1']['HostConfig'],dict(Memory=2*GIB,MemorySwap=4*GIB))
        self.assertEqual(result['records']['r2']['HostConfig']['Memory'],4*GIB)
        self.assertEqual(result['updates'],['b'*64])
        self.assertEqual(result['config']['runner_gib'],4)
        self.assertEqual(result['receipt']['reserved_gib'],8)
        self.assertEqual(result['receipt']['pending_runner_ids'],['a'*64])

    def test_controller_retains_seen_oom_until_a_second_pass_observes_all_caps(self):
        from lib import spot_runner_autoscaler as controller
        from lib import tier_controller
        vm=GceVm('vm','z','RUNNING','n2-standard-32',True,32,128,instance_id='12345')
        state={'pending_oom':{'z/12345':'runner'},'seen_oom_ids':['already-seen']}
        previous=[]
        def remote_adjust(*args):
            result=self.run_program(leases={self.owner('a'):2} if not previous else {},
                                    resume=previous[-1] if previous else None)
            previous.append(result)
            return result['status']==0
        with tempfile.TemporaryDirectory() as folder,patch.object(controller,'spot_instances',return_value=[vm]),\
             patch.object(tier_controller,'evidence_for',return_value=([],{},1000)),\
             patch.object(tier_controller,'plan_capacity',return_value=dict(action='budget',target=('bulky',),reason='pending cap repair')),\
             patch.object(controller,'adjust_admission',side_effect=remote_adjust) as adjust:
            path=Path(folder)/'state.json';args=SimpleNamespace(project='p',migrate_oversized=False)
            tier_controller.check_tiers([vm],[],args,state,path)
            self.assertEqual(previous[0]['updates'],['b'*64])
            self.assertEqual(state['pending_oom'],{'z/12345':'runner'})
            self.assertEqual(json.loads(path.read_text())['pending_oom'],state['pending_oom'])
            tier_controller.check_tiers([vm],[],args,state,path)
            self.assertEqual(adjust.call_count,2)
            self.assertEqual(previous[1]['updates'],['a'*64])
            self.assertEqual(previous[1]['config']['runner_gib'],4)
            self.assertTrue(all(row['HostConfig']['Memory']==4*GIB for row in previous[1]['records'].values()))
            self.assertEqual(state['pending_oom'],{})
            self.assertEqual(state['seen_oom_ids'],['already-seen'])

    def test_old_config_acquiring_hook_finishes_at_barrier_and_its_container_is_not_raised(self):
        leases={self.owner('a'):2}
        def old_request(config,ledger,records):
            # This hook captured2 before the new config was published. The
            # serialized barrier must complete it before any cap changes.
            self.assertTrue(LeasePool(ledger,32,alive=lambda _:True).acquire(self.owner('b'),2))
        result=self.run_program(leases=leases,barrier=old_request)
        self.assertEqual(result['status'],75)
        self.assertEqual(result['updates'],[])

        self.assertEqual(result['ledger'],{self.owner('a'):2,self.owner('b'):2})
        self.assertEqual(result['records']['r2']['HostConfig']['Memory'],2*GIB)
        self.assertEqual(result['config']['runner_gib'],4)

    def test_migration_repairs_idle_caps_with_busy_sibling_and_retries_expired_event(self):
        from lib import spot_runner_autoscaler as controller
        from lib.fleet_migration import begin_migration,reconcile_migration
        from lib.runner_fleet import GitHubRunner
        old=GceVm('oxidex-runners-old','z','RUNNING','n2-standard-32',True,32,128,instance_id='old')
        new=GceVm('oxidex-runners-new','z','RUNNING','n2-standard-32',True,32,128,instance_id='12345',
            disks=({'source':'disk','diskId':'d','boot':False,'deviceName':'runner-data-new',
                    'diskType':'pd-balanced','diskSizeGb':100},))
        runners=[GitHubRunner(i,f'gha-{new.name}-{i}','online',i==1,()) for i in (1,2)]
        previous=[]
        def remote_adjust(host,args,kind):
            self.assertEqual((host,kind),(new,'runner'))
            result=self.run_program(leases={self.owner('a'):2} if len(previous)<2 else {},
                                    resume=previous[-1] if previous else None)
            previous.append(result)
            return result['status']==0
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';state={}
            begin_migration([old],('bulky',),state,path,'replacement',force_ids=['old'])
            state['migration'].update(pending_tiers=[],pending_slots=[],replacement_ids=['12345'],phase='healthy')
            state['host_deployments']={'z/12345':dict(expected_runner_count=2)}
            state['pending_oom']={'z/12345':'runner'};state['seen_oom_ids']=['oom-r']
            state['latest_workload']={'complete':True}
            state['latest_resources']=[dict(zone='z',instance_id=identity,observed_at=1000,
                cpu_fraction=.1,memory_fraction=.06,peak_cpu_cores=1,peak_ram_gib=3,
                io_complete=True,io_pressure_peaks=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0))
                for identity in ('old','12345')]
            state['host_health']={'z/12345':dict(instance_id='12345',boot_id='boot',observed_at=1000,complete=True,
                boot_oom_complete=True,boot_host_oom_ids=[],
                events=[dict(id='oom-r',kind='container',workload='runner',at=999)])}
            for index,tick in enumerate((1000,3100,3400)):
                for sample in state['latest_resources']:sample['observed_at']=tick
                state['host_health']['z/12345']['observed_at']=tick
                if index:state['host_health']['z/12345']['events']=[]
                with patch('lib.fleet_migration.time.time',return_value=tick),\
                     patch.object(controller,'adjust_admission',side_effect=remote_adjust) as adjust,\
                     patch.object(controller,'begin_host_operation') as drain:
                    reconcile_migration([old,new],runners,SimpleNamespace(remote_builder=False),state,path)
                adjust.assert_called_once()
                drain.assert_not_called()
                saved=json.loads(path.read_text())
                if index<2:
                    self.assertEqual(previous[-1]['status'],75)
                    self.assertEqual(saved['pending_oom'],{'z/12345':'runner'})
                    self.assertNotIn('oom-r',state['migration'].get('oom_budget_repairs',[]))
                    self.assertEqual(state['migration']['healthy_checks']['z/12345']['count'],0)
                else:
                    self.assertEqual(previous[-1]['status'],0)
                    self.assertEqual(saved['pending_oom'],{})
                    self.assertIn('oom-r',state['migration']['oom_budget_repairs'])
                    self.assertEqual(state['migration']['healthy_checks']['z/12345']['count'],1)
            self.assertEqual(previous[0]['updates'],['b'*64])
            self.assertEqual(previous[1]['updates'],[])
            self.assertEqual(previous[2]['updates'],['a'*64])
            self.assertEqual(previous[2]['config']['runner_gib'],4)
            self.assertTrue(all(row['HostConfig']['Memory']==4*GIB for row in previous[2]['records'].values()))
            self.assertEqual(state['seen_oom_ids'],['oom-r'])

    def test_partial_docker_failure_keeps_higher_reservation_and_all_lease_rows(self):
        result=self.run_program(fail_update='b'*64)
        self.assertEqual(result['status'],75)
        self.assertEqual(result['config']['runner_gib'],4)
        self.assertEqual(result['records']['r1']['HostConfig']['Memory'],4*GIB)
        self.assertEqual(result['records']['r2']['HostConfig']['Memory'],2*GIB)
        self.assertEqual(result['ledger'],{})
        self.assertEqual(result['receipt']['reserved_gib'],0)

    def test_slow_docker_bounds_ledger_hold_and_defers_remaining_caps(self):
        result=self.run_program(docker_delay=3)
        self.assertEqual(result['status'],75)
        self.assertEqual(result['config']['runner_gib'],4)
        self.assertEqual(result['updates'],['a'*64])
        self.assertEqual(result['records']['r2']['HostConfig']['Memory'],2*GIB)
        self.assertEqual(result['receipt']['updated'],['a'*64])

    def test_failed_barrier_changes_no_caps_and_leaves_conservative_config(self):
        def unavailable(*args):raise OSError('Admission restarted')
        result=self.run_program(barrier=unavailable)
        self.assertEqual(result['status'],75)
        self.assertEqual(result['updates'],[])
        self.assertEqual(result['config']['runner_gib'],4)

    def test_retry_catches_up_to_published_reservation_before_another_increase(self):
        result=self.run_program(reservation=4)
        self.assertEqual(result['status'],0)
        self.assertEqual(result['config']['runner_gib'],4)
        self.assertEqual(result['updates'],['a'*64,'b'*64])
        self.assertEqual(result['records']['r1']['HostConfig']['Memory'],4*GIB)
        self.assertEqual(result['records']['r2']['HostConfig']['Memory'],4*GIB)

    def test_changed_container_identity_is_never_updated(self):
        def replace_identity(config,ledger,records):records['r2']['Id']='c'*64
        result=self.run_program(barrier=replace_identity)
        self.assertEqual(result['status'],75)
        self.assertNotIn('b'*64,result['updates'])
        self.assertEqual(result['records']['r2']['HostConfig']['Memory'],2*GIB)

    def test_observed_envelope_reports_capacity_without_publishing_an_unfit_reservation(self):
        result=self.run_program(envelope=2.5)
        self.assertEqual(result['status'],76)
        self.assertEqual(result['config']['runner_gib'],2)
        self.assertEqual(result['updates'],[])

    def test_builder_future_cap_changes_without_touching_active_reservation(self):
        leases={'host:999:1':6}
        result=self.run_program(kind='builder',leases=leases)
        self.assertEqual(result['status'],0)
        self.assertEqual(result['config']['builder_gib'],12)
        self.assertEqual(result['builder']['memory_gib'],12)
        self.assertEqual(result['ledger'],leases)
        self.assertEqual(result['updates'],[])

    def test_builder_limits_write_failure_keeps_admission_higher_than_future_cap(self):
        result=self.run_program(kind='builder',fail_limits=True)
        self.assertEqual(result['status'],75)
        self.assertEqual(result['config']['builder_gib'],12)
        self.assertEqual(result['builder']['memory_gib'],6)

    def test_launch_parsers_pin_successful_cap_without_claiming_a_peak(self):
        from lib.runner_manager import args_parser as manager_parser,select_sizing
        from lib.spot_runner_autoscaler import args_parser as controller_parser
        self.assertEqual(controller_parser().parse_args([]).tier_job_memory_gib,15)
        args=manager_parser().parse_args(['--max-cost','.125','--tier','large','--cpu','16',
            '--ram','64','--instance-type','n2-standard-16'])
        select_sizing(args)
        self.assertEqual(args.runner_count,20)
        self.assertEqual(args.runner_memory_gb,15)
        self.assertEqual(int(args.admission_work_gib//15),3)
        for invalid in ('nan','inf','0','-1'):
            with self.subTest(invalid=invalid),redirect_stdout(io.StringIO()),patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit):controller_parser().parse_args(['--tier-job-memory-gib',invalid])

    def test_launch_agent_persists_explicit_memory_override(self):
        import plistlib
        from lib import spot_runner_autoscaler as controller
        with tempfile.TemporaryDirectory() as directory:
            args=controller.args_parser().parse_args(['--tier-job-memory-gib','17'])
            with patch.object(Path,'home',return_value=Path(directory)),patch.object(controller.subprocess,'run'):
                path=controller.install_launch_agent(args)
            argv=plistlib.loads(path.read_bytes())['ProgramArguments']
            self.assertEqual(argv[argv.index('--tier-job-memory-gib')+1],'17.0')

    def test_tier_launch_forwards_reservation_and_rejects_invalid_runtime_field(self):
        from lib import spot_runner_autoscaler as controller
        from lib.machine_profiles import CpuOffer
        offer=CpuOffer('us-west3','c2d-standard-8',8,32,.03,'AMD Milan',5,zone='us-west3-a')
        args=controller.args_parser().parse_args(['--scaling-mode','tiered','--tier-job-memory-gib','17'])
        args.launch_tier='expanded'
        def manager(argv,**kwargs):
            self.assertEqual(argv[argv.index('--tier-job-memory-gib')+1],'17.0')
            raise RuntimeError('manager argv captured')
        with patch.object(controller,'begin_host_operation'),patch.object(controller.subprocess,'run',side_effect=manager):
            with self.assertRaisesRegex(RuntimeError,'manager argv captured'):
                controller.launch_offer(offer,args,{},Path('/tmp/unused-cap-test-state'))
        for value in (float('nan'),float('inf'),0,-1):
            args.tier_job_memory_gib=value
            with self.subTest(value=value),patch.object(controller.subprocess,'run',side_effect=AssertionError('provider mutation')):
                with self.assertRaisesRegex(RuntimeError,'finite and positive'):
                    controller.launch_offer(offer,args,{},Path('/tmp/unused-cap-test-state'))


if __name__=='__main__':unittest.main()
