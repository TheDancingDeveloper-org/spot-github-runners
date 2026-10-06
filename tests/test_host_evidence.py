import unittest
import subprocess
import json
import hashlib
from types import SimpleNamespace
from unittest.mock import patch
from lib.host_evidence import classify_events, sample,workload_inventory,inspected_workloads

IDLE_IO='some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'


class HostEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.inventory=patch('lib.host_evidence.inspected_workloads',return_value={})
        self.inventory.start();self.addCleanup(self.inventory.stop)
    def test_real_cgroup_kernel_sequence_never_promotes_context_to_host_oom(self):
        messages = [
            'python3 invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=500',
            'CPU: 7 PID: 41071 Comm: python3 Not tainted 6.1.0-39-cloud-amd64 #1 Debian',
            'oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=docker-abc.scope,mems_allowed=0,oom_memcg=/system.slice/docker-abc.scope,task_memcg=/system.slice/docker-abc.scope,task=python3,pid=41071,uid=1001',
            'Memory cgroup out of memory: Killed process 41071 (python3) total-vm:2553896kB, anon-rss:1919192kB, file-rss:0kB, shmem-rss:0kB, UID:1001 pgtables:6000kB oom_score_adj:500',
            'oom_reaper: reaped process 41071 (python3), now anon-rss:0kB, file-rss:0kB, shmem-rss:0kB',
        ]
        journal = [{'__REALTIME_TIMESTAMP':str((1000+i)*1000000),'MESSAGE':message}
                   for i,message in enumerate(messages)]
        result = classify_events(journal, [], 'boot', 1010)
        self.assertEqual([row['kind'] for row in result], ['container','container'])

    def test_explicit_global_kernel_decision_survives_context_filter(self):
        journal = [{'__REALTIME_TIMESTAMP':'1000000000','MESSAGE':
                    'oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=docker-abc.scope,mems_allowed=0,global_oom,task_memcg=/system.slice/docker-abc.scope,task=rustc,pid=4242,uid=1001'}]
        self.assertEqual(classify_events(journal, [], 'boot', 1010)[0]['kind'], 'host')

    def test_sampler_retains_true_boot_global_oom_older_than_recent_window(self):
        global_row={'__REALTIME_TIMESTAMP':'1000000000','MESSAGE':
                    'oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),global_oom,task=rustc,pid=42,uid=1001'}
        with patch('lib.host_evidence.time.time',return_value=10000), \
             patch('lib.host_evidence.Path.read_text',side_effect=['boot','MemTotal: 131072000 kB\nMemAvailable: 123000000 kB\n',IDLE_IO,'boot']), \
             patch('lib.host_evidence.command_json_lines',side_effect=[[],[],[global_row]]):
            observed=sample()
        self.assertEqual(observed['events'],[])
        self.assertIn('boot_oom_complete',observed)
        self.assertTrue(observed['boot_oom_complete'])
        self.assertEqual(len(observed['boot_host_oom_ids']),1)

    def test_unparseable_boot_global_row_cannot_prove_absence(self):
        row={'__REALTIME_TIMESTAMP':'invalid','MESSAGE':'Out of memory: Killed process 42 (rustc)'}
        with patch('lib.host_evidence.time.time',return_value=10000), \
             patch('lib.host_evidence.Path.read_text',side_effect=['boot','MemTotal: 131072000 kB\nMemAvailable: 123000000 kB\n',IDLE_IO,'boot']), \
             patch('lib.host_evidence.command_json_lines',side_effect=[[],[],[row]]):
            observed=sample()
        self.assertFalse(observed['boot_oom_complete'])

    def test_empty_boot_grep_exit_one_proves_no_match(self):
        def run(command,**kwargs):
            result=SimpleNamespace(returncode=1 if any(arg.startswith('--grep=') for arg in command) else 0,
                                   stdout='',stderr='')
            if kwargs.get('check') and result.returncode:
                raise subprocess.CalledProcessError(result.returncode,command,output=result.stdout,stderr=result.stderr)
            return result
        with patch('lib.host_evidence.time.time',return_value=10000), \
             patch('lib.host_evidence.Path.read_text',side_effect=['boot','MemTotal: 131072000 kB\nMemAvailable: 123000000 kB\n',IDLE_IO,'boot']), \
             patch('lib.host_evidence.subprocess.run',side_effect=run):
            try:observed=sample()
            except subprocess.CalledProcessError:self.fail('confirmed empty journal grep exit 1 must be accepted')
        self.assertTrue(observed['boot_oom_complete'])
        self.assertEqual(observed['boot_host_oom_ids'],[])

    def test_boot_grep_permission_failure_cannot_prove_no_match(self):
        def run(command,**kwargs):
            result=SimpleNamespace(returncode=1 if any(arg.startswith('--grep=') for arg in command) else 0,
                                   stdout='',stderr='Permission denied' if any(arg.startswith('--grep=') for arg in command) else '')
            if kwargs.get('check') and result.returncode:
                raise subprocess.CalledProcessError(result.returncode,command,output=result.stdout,stderr=result.stderr)
            return result
        with patch('lib.host_evidence.time.time',return_value=10000), \
             patch('lib.host_evidence.Path.read_text',return_value='boot'), \
             patch('lib.host_evidence.subprocess.run',side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):sample()

    def test_kernel_and_cgroup_oom_are_distinct_and_exit137_is_not_proof(self):
        journal = [{ '__REALTIME_TIMESTAMP':'1000000000','MESSAGE':'Out of memory: Killed process 42 (cargo)'},
                   {'__REALTIME_TIMESTAMP':'1001000000','MESSAGE':'Memory cgroup out of memory: Killed process 43'},
                   {'__REALTIME_TIMESTAMP':'1002000000','MESSAGE':'cargo exited 137'}]
        events = [{'Action':'oom','id':'abc','timeNano':1003000000000},
                  {'Action':'die','id':'def','Actor':{'Attributes':{'exitCode':'137'}},'timeNano':1004000000000}]
        result = classify_events(journal,events,'boot1',1005)
        self.assertEqual(len(result),3)
        self.assertEqual([row['kind'] for row in result],['host','container','container'])
        self.assertNotEqual(result[0]['id'],classify_events(journal,[], 'boot2',1005)[0]['id'])

    def test_old_and_future_events_do_not_trigger_capacity(self):
        rows = [{'__REALTIME_TIMESTAMP':'1000000000','MESSAGE':'Out of memory: Killed process 42'}]
        self.assertEqual(classify_events(rows,[],'boot',999),[])
        self.assertEqual(classify_events(rows,[],'boot',3000),[])


class KernelAttributionTests(unittest.TestCase):
    identity='40df51e9af61870fcd315de41c854055647478e2bf32ca8eb3e4b6e1acc98ac7'
    boot='6c5d3d5e-6694-43ec-b222-9dbd441c3df3'
    name='gha-oxidex-runners-asianortheast2-a-1791098074-4'

    def rows(self):
        return [dict(__REALTIME_TIMESTAMP='1791109266782726',_BOOT_ID=self.boot.replace('-',''),MESSAGE=
            'oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=docker-'+self.identity+'.scope,mems_allowed=0,oom_memcg=/system.slice/docker-'+self.identity+'.scope,task_memcg=/system.slice/docker-'+self.identity+'.scope,task=python3,pid=138343,uid=1001'),
            dict(__REALTIME_TIMESTAMP='1791109266800989',_BOOT_ID=self.boot.replace('-',''),MESSAGE=
            'Memory cgroup out of memory: Killed process 138343 (python3) total-vm:4322072kB, anon-rss:3951532kB, file-rss:10932kB, shmem-rss:0kB, UID:1001 pgtables:7868kB oom_score_adj:500')]

    def mapping(self,role='runner'):
        row=dict(Id=self.identity,Name='/'+self.name,Config=dict(Labels={}))
        if role=='builder':row.update(Name='/builder',Config=dict(Labels={'oxidex.remote-build':'true'}))
        return workload_inventory([row],[self.name])

    def test_actual_kernel_pair_maps_exact_runner_id_and_preserves_both_event_ids(self):
        events=classify_events(self.rows(),[],self.boot,1791109300,workloads=self.mapping())
        self.assertEqual([e['workload'] for e in events],['runner','runner'])
        self.assertEqual([e['id'] for e in events],[
            '8dc324ea7d8a9b9fa8604979c74639216515cc28842ebd7a17a9c07907340dad',
            'fa0180a9652e997572c0698fde54692b6785b0f22517ff7356d1eeed55ba5449'])
        self.assertTrue(all(set(e)=={'id','kind','workload','at'} for e in events),'emit no process/cgroup payload')
        self.assertEqual([e['workload'] for e in classify_events(self.rows(),[],self.boot,1791109300,workloads=self.mapping('builder'))],['builder','builder'])

    def test_pid_reuse_and_multiple_decisions_leave_kill_unattributed(self):
        rows=self.rows();another=dict(rows[0]);another['__REALTIME_TIMESTAMP']='1791109266000000'
        for decision in ('CONSTRAINT_MEMCG','CONSTRAINT_NONE'):
            with self.subTest(decision=decision):
                another['MESSAGE']=rows[0]['MESSAGE'].replace('CONSTRAINT_MEMCG',decision)
                events=classify_events([*rows,another],[],self.boot,1791109300,workloads=self.mapping())
                self.assertEqual(events[-1]['workload'],'unknown')

    def test_missing_contradictory_or_short_cgroups_cannot_guess_a_workload(self):
        for message in [self.rows()[0]['MESSAGE'].replace('task_memcg=/system.slice/docker-'+self.identity,'task_memcg=/system.slice/docker-'+'f'*64),
                        self.rows()[0]['MESSAGE'].replace(self.identity,'short'),
                        self.rows()[0]['MESSAGE'].replace('oom_memcg=','missing_memcg=')]:
            rows=self.rows();rows[0]['MESSAGE']=message
            self.assertEqual([e['workload'] for e in classify_events(rows,[],self.boot,1791109300,workloads=self.mapping())],['unknown','unknown'])
        self.assertEqual([e['workload'] for e in classify_events(self.rows(),[],self.boot,1791109300)],['unknown','unknown'])

    def test_global_decision_and_wrong_task_kill_remain_vetoes(self):
        rows=self.rows();rows[0]['MESSAGE']=rows[0]['MESSAGE'].replace('CONSTRAINT_MEMCG','CONSTRAINT_NONE')
        events=classify_events(rows,[],self.boot,1791109300,workloads=self.mapping())
        self.assertEqual((events[0]['kind'],events[0]['workload']),('host','unknown'))
        self.assertEqual(events[1]['workload'],'unknown')
        rows=self.rows();rows[1]['MESSAGE']=rows[1]['MESSAGE'].replace('(python3)','(rustc)')
        self.assertEqual(classify_events(rows,[],self.boot,1791109300,workloads=self.mapping())[-1]['workload'],'unknown')

    def test_inventory_requires_full_unique_ids_and_exact_configured_runner_names(self):
        row=dict(Id=self.identity,Name='/'+self.name,Config=dict(Labels={}))
        self.assertEqual(workload_inventory([row],['different-runner']),{})
        row['Config']['Labels']['oxidex.remote-build']='true'
        self.assertEqual(workload_inventory([row],[self.name]),{},'conflicting runner/builder identity is unknown')
        for records in ([dict(row,Id='short')],[row,row],[None]):
            with self.assertRaises(ValueError):workload_inventory(records,[self.name])

    def test_docker_inspection_uses_full_ids_and_rejects_changed_inventory(self):
        row=dict(Id=self.identity,Name='/'+self.name,Config=dict(Labels={}))
        with patch('lib.host_evidence.Path.read_text',return_value=json.dumps({'runner_names':[self.name]})),\
             patch('lib.host_evidence.subprocess.run',side_effect=[SimpleNamespace(stdout=self.identity+'\n'),SimpleNamespace(stdout=json.dumps([row]))]) as command:
            self.assertEqual(inspected_workloads(),{self.identity:'runner'})
            self.assertEqual(command.call_args.args[0],['/usr/bin/docker','inspect',self.identity])
        with patch('lib.host_evidence.Path.read_text',return_value=json.dumps({'runner_names':[self.name]})),\
             patch('lib.host_evidence.subprocess.run',side_effect=[SimpleNamespace(stdout=self.identity+'\n'),SimpleNamespace(stdout=json.dumps([dict(row,Id='f'*64)]))]):
            with self.assertRaises(ValueError):inspected_workloads()

    def test_expired_pending_ids_reclassify_only_requested_ids_from_complete_original_boot(self):
        rows=self.rows();target='fa0180a9652e997572c0698fde54692b6785b0f22517ff7356d1eeed55ba5449'
        variants=[(rows,True),([dict(rows[0],_BOOT_ID='wrong'),rows[1]],False),
                  ([dict(rows[0],__REALTIME_TIMESTAMP='invalid'),rows[1]],False),(rows*1000,False)]
        for history,complete in variants:
            with self.subTest(complete=complete,rows=len(history)),\
                 patch('lib.host_evidence.time.time',return_value=1791112000),\
                 patch('lib.host_evidence.Path.read_text',side_effect=[self.boot,'MemTotal: 131072000 kB\nMemAvailable: 123000000 kB\n',IDLE_IO,self.boot]),\
                 patch('lib.host_evidence.inspected_workloads',return_value=self.mapping()),\
                 patch('lib.host_evidence.command_json_lines',side_effect=[[],[],[],history]):
                observed=sample([target])
            self.assertEqual(observed['events'],[],'never replay the complete old boot history as current events')
            self.assertIs(observed['reclassification_complete'],complete)
            self.assertEqual([e['id'] for e in observed['reclassified_events']],[target] if complete else [])

    def test_unknown_inventory_keeps_kernel_events_unattributed(self):
        with patch('lib.host_evidence.time.time',return_value=1791109300),\
             patch('lib.host_evidence.Path.read_text',side_effect=[self.boot,'MemTotal: 131072000 kB\nMemAvailable: 123000000 kB\n',IDLE_IO,self.boot]),\
             patch('lib.host_evidence.inspected_workloads',side_effect=OSError('Docker unavailable')),\
             patch('lib.host_evidence.command_json_lines',side_effect=[self.rows(),[],[]]):
            observed=sample()
        self.assertEqual([e['workload'] for e in observed['events']],['unknown','unknown'])

    def test_wrong_boot_rows_cannot_attribute_even_with_known_full_id(self):
        for index in (0,1):
            rows=self.rows();rows[index]['_BOOT_ID']='different'
            events=classify_events(rows,[],self.boot,1791109300,workloads=self.mapping())
            self.assertEqual(events[index]['workload'],'unknown')
            if index==0:self.assertEqual(events[1]['workload'],'unknown')

    def test_actual_docker_actor_id_maps_only_inspected_identity_without_rekeying(self):
        row=dict(Action='oom',timeNano=1791109267568195753,
                 Actor=dict(ID=self.identity,Attributes=dict(name=self.name)))
        events=classify_events([], [row],self.boot,1791109300,workloads=self.mapping())
        expected=hashlib.sha256(f"{self.boot}:{int(row['timeNano'])/1e9}:None:docker-oom".encode()).hexdigest()
        self.assertEqual((events[0]['id'],events[0]['workload']),(expected,'runner'))
        for candidate in (dict(row,Actor=dict(ID='f'*64,Attributes=dict(name=self.name))),row):
            mapping=self.mapping() if candidate is not row else {}
            self.assertEqual(classify_events([],[candidate],self.boot,1791109300,workloads=mapping)[0]['workload'],'unknown')

    def test_sampler_rejects_boot_change_before_any_claim_can_leave_host(self):
        with patch('lib.host_evidence.time.time',return_value=1791109300),\
             patch('lib.host_evidence.Path.read_text',side_effect=[self.boot,'MemTotal: 131072000 kB\nMemAvailable: 123000000 kB\n',IDLE_IO,'different']),\
             patch('lib.host_evidence.inspected_workloads',return_value=self.mapping()),\
             patch('lib.host_evidence.command_json_lines',side_effect=[self.rows(),[],[]]):
            with self.assertRaisesRegex(RuntimeError,'boot changed'):sample()

    def test_malformed_docker_inspection_cannot_escape_fail_closed_inventory(self):
        with patch('lib.host_evidence.Path.read_text',return_value=json.dumps({'runner_names':[self.name]})),\
             patch('lib.host_evidence.subprocess.run',side_effect=[SimpleNamespace(stdout=self.identity+'\n'),SimpleNamespace(stdout='[null]')]):
            with self.assertRaises(ValueError):inspected_workloads()
        for names in ([{}],[self.name,self.name]):
            with self.assertRaises(ValueError):workload_inventory([],names)

    def test_legacy_absent_config_uses_only_exact_anchored_canonical_names(self):
        host='oxidex-runners-asianortheast2-a-1791098074'
        names=[f'gha-{host}-{index}' for index in (1,100,0,101)]+[f'gha-{host}-01',f'gha-{host}-foreign-1','gha-other-host-1']
        records=[dict(Id=f'{index:064x}',Name='/'+name,Config=dict(Labels={})) for index,name in enumerate(names,1)]
        with patch('lib.host_evidence.Path.read_text',side_effect=FileNotFoundError),\
             patch('lib.host_evidence.Path.lstat',side_effect=FileNotFoundError),\
             patch('lib.host_evidence.subprocess.run',side_effect=[SimpleNamespace(stdout='\n'.join(row['Id'] for row in records)),SimpleNamespace(stdout=json.dumps(records))]):
            mapping=inspected_workloads(host)
        self.assertEqual(mapping,{records[0]['Id']:'runner',records[1]['Id']:'runner'})
        for anchor in (None,'bad;name','UPPERCASE','a'*64):
            with patch('lib.host_evidence.Path.read_text',side_effect=FileNotFoundError),\
                 patch('lib.host_evidence.Path.lstat',side_effect=FileNotFoundError),\
                 patch('lib.host_evidence.subprocess.run') as command:
                with self.assertRaises(ValueError):inspected_workloads(anchor)
                command.assert_not_called()

    def test_existing_malformed_config_never_falls_back_to_legacy_names(self):
        for text in ('not json','{}','[]'):
            with patch('lib.host_evidence.Path.read_text',return_value=text),\
                 patch('lib.host_evidence.subprocess.run',return_value=SimpleNamespace(stdout='')):
                # Even an empty Docker inventory cannot turn malformed config
                # into a successful legacy proof.
                with self.assertRaises((ValueError,KeyError,TypeError)):inspected_workloads('exact-vm')
        with patch('lib.host_evidence.Path.read_text',side_effect=FileNotFoundError),\
             patch('lib.host_evidence.Path.lstat',return_value=object()),\
             patch('lib.host_evidence.subprocess.run') as command:
            with self.assertRaisesRegex(ValueError,'Existing admission'):inspected_workloads('exact-vm')
            command.assert_not_called()
