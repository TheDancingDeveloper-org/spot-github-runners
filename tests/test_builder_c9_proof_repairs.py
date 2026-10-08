"""Portable fault controls for the C9 source repair; no host mutation."""
import base64
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from lib import builder_runtime_update as update
from lib import builder_access
from lib import host_admission
from lib.builder_access import uploader_policy_bytes
from lib.qualification_trust import control_observation as observation
from lib.qualification_trust import control_engine as engine
from lib.qualification_trust import reservation as custody

spec=importlib.util.spec_from_file_location('uploader_session',ROOT/'builder/dedicated/uploader-session.py')
uploader=importlib.util.module_from_spec(spec);spec.loader.exec_module(uploader)
PROJECT='qualification-'+'a'*12+'-'+'b'*32


class ProofRepairControls(unittest.TestCase):
    def test_reboot_refreshes_only_proven_dead_old_stage_lease_with_canonical_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Path(directory)/'leases.json'
            owner='host:987654:12345'
            record={'reservation_id':'a'*32,'active':{'ordinal':3,'command':'task20-bootstrap'},
                    'host':['123','old-boot']}
            original_pool=host_admission.LeasePool
            def selected_pool(path,*args,**kwargs):
                self.assertEqual(str(path),'/var/lib/gha-admission/leases.json')
                return original_pool(ledger,*args,**kwargs)
            def selected_json(path):
                if str(path).endswith('/leases.json'):return json.loads(ledger.read_text())
                if str(path)=='/etc/gha-admission.json':return {'role':'builder','runner_names':[],
                    'host_cpus':16,'builder_cpus':16,'work_gib':28,'builder_gib':28,'runner_gib':0}
                raise AssertionError(path)
            def absent(pid):raise FileNotFoundError(pid)
            inactive='ActiveState=inactive\nMainPID=0\nControlGroup=\n'
            with patch.object(engine,'root_json',side_effect=selected_json),\
                 patch.object(engine,'quiescent',return_value=True),\
                 patch.object(engine,'own_unit',return_value=None),\
                 patch.object(observation,'command',return_value=inactive),\
                 patch.object(host_admission,'LeasePool',side_effect=selected_pool),\
                 patch.object(host_admission,'process_identity',side_effect=absent) as identity:
                ledger.write_text(json.dumps({owner:28}))
                engine.reconcile_old_boot_leases(PROJECT,record,('123','new-boot'))
                self.assertEqual(json.loads(ledger.read_text()),{})
                self.assertGreaterEqual(identity.call_count,2)
                for observed in (owner,'host:987654:99999'):
                    ledger.write_text(json.dumps({owner:28}))
                    with patch.object(host_admission,'process_identity',return_value=observed):
                        with self.assertRaisesRegex(ValueError,'PID remains or was reused'):
                            engine.reconcile_old_boot_leases(PROJECT,record,('123','new-boot'))
                    self.assertEqual(json.loads(ledger.read_text()),{owner:28})
                ledger.write_text(json.dumps({owner:28}))
                with patch.object(host_admission,'process_identity',side_effect=PermissionError('unknown')):
                    with self.assertRaisesRegex(ValueError,'observation uncertain'):
                        engine.reconcile_old_boot_leases(PROJECT,record,('123','new-boot'))
                self.assertEqual(json.loads(ledger.read_text()),{owner:28})
                ledger.write_text(json.dumps({'runner:foreign':28}))
                with self.assertRaisesRegex(ValueError,'Foreign admission owner'):
                    engine.reconcile_old_boot_leases(PROJECT,record,('123','new-boot'))
                self.assertEqual(json.loads(ledger.read_text()),{'runner:foreign':28})
                ledger.write_text(json.dumps({owner:28}))
                with patch.object(observation,'command',return_value='ActiveState=active\nMainPID=3\nControlGroup=/live\n'):
                    with self.assertRaisesRegex(ValueError,'unit/cgroup'):
                        engine.reconcile_old_boot_leases(PROJECT,record,('123','new-boot'))
                self.assertEqual(json.loads(ledger.read_text()),{owner:28})
                with self.assertRaisesRegex(ValueError,'No selected old container stage'):
                    engine.reconcile_old_boot_leases(PROJECT,{**record,'active':None},('123','new-boot'))

    def test_completed_cleanup_is_reused_with_its_original_actor_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);original={'pid':11,'start':22};new={'pid':33,'start':44}
            stage_log=path/'stage-7.log';stage_log.write_bytes(b'actual completed cleanup\n')
            stages=[{'command':name} for name in ('prepare','upload-receive','verify-source',
                'task20-bootstrap','task20-execute','task20-pack','task20-replay','cleanup')]
            event={'ordinal':7,'command':'cleanup','actor':original,'exit_code':0,
                   'log_sha256':hashlib.sha256(stage_log.read_bytes()).hexdigest()}
            record={'reservation_id':'r','session':{'project':PROJECT},'host':['123','old-boot'],
                    'state':'SOURCE_VERIFIED','active':None,'events':[event],'stages':stages,'next_ordinal':8}
            class Store:
                def locked(self):
                    from contextlib import nullcontext
                    return nullcontext()
                def read(self):return record
                def write(self,value):record.update(value)
            original_fstat=os.fstat
            def root_fstat(fd):
                info=original_fstat(fd);values=list(info);values[4]=0
                return os.stat_result(values)
            actor=[new]
            with patch.object(engine,'idle',return_value=True),patch.object(engine,'dossier',return_value=path),\
                 patch.object(custody,'Store',return_value=Store()),\
                 patch.object(custody,'host_identity',return_value=('123','new-boot')),\
                 patch.object(custody,'enrollment',return_value={}),\
                 patch.object(custody,'current_actor',side_effect=lambda:actor[0]),\
                 patch.object(engine,'root_json',side_effect=lambda file:json.loads(Path(file).read_text())),\
                 patch.object(engine.os,'fstat',side_effect=root_fstat):
                self.assertEqual(engine.reconcile_reboot(PROJECT,'r','old-boot')['state'],'RELEASED')
                self.assertEqual(record['events'],[event])
                evidence=json.loads((path/'reboot-reconciliation.json').read_text())
                self.assertEqual(evidence['actor'],new)
                self.assertNotEqual(event['log_sha256'],hashlib.sha256((path/'reboot-cleanup.log').read_bytes()).hexdigest())
                actor[0]={'pid':55,'start':66}
                self.assertEqual(engine.reconcile_reboot(PROJECT,'r','old-boot')['events'],[event])
                stage_log.write_bytes(b'tampered')
                with self.assertRaisesRegex(ValueError,'retained stage log'):
                    engine.reconcile_reboot(PROJECT,'r','old-boot')

    def test_stage_capture_bounds_pipe_before_file_growth_and_holds_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'stage.log'
            # Test-only bound; stage_locked always uses the fixed production default.
            with output.open('wb') as stream:
                command=[sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*8192)']
                def stop(process):
                    process.wait()
                with patch.object(engine,'stop_stage_children',side_effect=stop) as stopped:
                    with self.assertRaisesRegex(ValueError,'during capture'):
                        engine.capture_stage(command,stream,limit=1024,
                                             scope_processes=lambda:{os.getpid()})
                    self.assertEqual(stopped.call_count,1)
            self.assertEqual(output.stat().st_size,1024)
            with output.open('wb') as stream:
                self.assertEqual(engine.capture_stage([sys.executable,'-c',
                    'import sys;sys.stdout.write("ok");sys.stderr.write("err")'],stream,
                    limit=1024,scope_processes=lambda:{os.getpid()}),0)
            self.assertIn(output.read_bytes(),(b'okerr',b'errok'))
            with output.open('wb') as stream:
                with self.assertRaisesRegex(ValueError,'descendants remain'):
                    engine.capture_stage([sys.executable,'-c','pass'],stream,limit=1024,
                                         scope_processes=lambda:{os.getpid(),999999})
            lock=os.open(Path(directory)/'parent.lock',os.O_CREAT|os.O_RDWR,0o600)
            try:
                fcntl.flock(lock,fcntl.LOCK_EX)
                os.set_inheritable(lock,True)
                with output.open('wb') as stream:
                    script='import os,sys\ntry: os.fstat('+str(lock)+'); sys.stdout.write("leaked")\nexcept OSError: sys.stdout.write("closed")\n'
                    self.assertEqual(engine.capture_stage([sys.executable,'-c',script],stream,
                        limit=1024,scope_processes=lambda:{os.getpid()}),0)
                self.assertEqual(output.read_bytes(),b'closed')
                challenger=os.open(Path(directory)/'parent.lock',os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(challenger,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:os.close(challenger)
            finally:os.close(lock)

    def test_capture_observes_real_process_lifetimes_and_stops_local_group(self):
        # ps is a local stand-in for the installed root cgroup observer. It
        # reads actual processes; stop_stage_children still performs the kill.
        def live_group(path):
            group=int(path.read_text())
            rows=subprocess.check_output(['ps','-axo','pid=,pgid=,stat='],text=True)
            return {int(fields[0]) for row in rows.splitlines()
                    if len(fields:=row.split())>=3 and int(fields[1])==group
                    and not fields[2].startswith('Z')}

        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            for name,script,expected,retained in (
                ('pipe-owner',
                 'import os,time\nopen({marker!r},"w").write(str(os.getpgrp()))\n'
                 'if os.fork()==0: time.sleep(1);os._exit(0)\n'
                 'os.write(1,b"parent-exit\\n");os._exit(0)\n',
                 'descendants remain',b'parent-exit\n'),
                ('overflow',
                 'import os,time\nopen({marker!r},"w").write(str(os.getpgrp()))\n'
                 'os.write(1,b"x"*1025);time.sleep(1)\n',
                 'during capture',b'x'*1024),
                ('eof-live',
                 'import os,time\nopen({marker!r},"w").write(str(os.getpgrp()))\n'
                 'os.close(1);os.close(2);time.sleep(1)\n',
                 'after stage pipe EOF',b''),
            ):
                with self.subTest(name=name):
                    marker=path/(name+'.group');log=path/(name+'.log')
                    command=[sys.executable,'-c',script.format(marker=str(marker))]
                    def observed():return {os.getpid()}|live_group(marker)
                    started=time.monotonic()
                    with patch.object(engine,'stage_unit_processes',side_effect=observed),\
                         patch.object(engine,'POST_EOF_EXIT_OBSERVATION_SECONDS',.12),\
                         log.open('wb') as output:
                        with self.assertRaisesRegex(ValueError,expected):
                            engine.capture_stage(command,output,limit=1024,
                                                 scope_processes=observed)
                    self.assertLess(time.monotonic()-started,.7)
                    self.assertEqual(log.read_bytes(),retained)
                    self.assertEqual(live_group(marker),set())

            with (path/'normal.log').open('wb') as output:
                command=[sys.executable,'-c',
                    'import os,time\nos.write(1,b"first");time.sleep(.05);'
                    'os.write(2,b"-stderr");time.sleep(.05);os.write(1,b"-last")']
                self.assertEqual(engine.capture_stage(command,output,limit=1024,
                    scope_processes=lambda:{os.getpid()}),0)
            self.assertEqual((path/'normal.log').read_bytes(),b'first-stderr-last')

    def test_capture_drains_eof_when_launcher_exits_after_empty_poll(self):
        # The selector can time out just before the launcher exits. Wait for
        # the real subprocess at that seam so the timing is deterministic.
        actual_popen=subprocess.Popen
        def launch(*args,**kwargs):
            process=actual_popen(*args,**kwargs)
            actual_poll=process.poll
            def poll_after_exit():
                process.wait(timeout=1)
                return actual_poll()
            process.poll=poll_after_exit
            return process

        with tempfile.TemporaryDirectory() as directory:
            log=Path(directory)/'stage.log'
            observations=[]
            def observed():
                observations.append(True)
                return {os.getpid()}
            command=[sys.executable,'-c',
                     'import os,time;time.sleep(.08);os.write(1,b"complete\\n")']
            with patch.object(engine.subprocess,'Popen',side_effect=launch),\
                 patch.object(engine,'CAPTURE_POLL_SECONDS',.01),\
                 log.open('wb') as output:
                self.assertEqual(engine.capture_stage(command,output,limit=1024,
                    scope_processes=observed),0)
            self.assertEqual(log.read_bytes(),b'complete\n')
            self.assertTrue(observations)

    def test_capture_holds_if_pipe_never_reaches_eof_after_exit(self):
        # Simulate a pipe owner outside the observed stage scope. Even an
        # otherwise empty scope cannot make the drain deadline a success.
        def stop(process):
            os.killpg(process.pid,9)
            process.wait(timeout=1)

        with tempfile.TemporaryDirectory() as directory:
            log=Path(directory)/'stage.log'
            command=[sys.executable,'-c',
                     'import os,time\nif os.fork()==0: time.sleep(1);os._exit(0)\nos._exit(0)']
            started=time.monotonic()
            with patch.object(engine,'POST_EXIT_PIPE_DRAIN_SECONDS',.12),\
                 patch.object(engine,'stop_stage_children',side_effect=stop) as stopped,\
                 log.open('wb') as output:
                with self.assertRaisesRegex(ValueError,'pipe remains open after launcher exit'):
                    engine.capture_stage(command,output,limit=1024,
                        scope_processes=lambda:{os.getpid()})
            self.assertEqual(stopped.call_count,1)
            self.assertGreaterEqual(time.monotonic()-started,.12)
            self.assertEqual(log.read_bytes(),b'')

    def test_all_four_container_ordinals_use_bounded_capture_and_forward_failure(self):
        actor={'pid':11,'start':22};host=('123','new-boot')
        session={'project':PROJECT,'head':'a'*40,'bundle_sha256':'b'*64}
        names=['prepare','upload-receive','verify-source','task20-bootstrap',
               'task20-execute','task20-pack','task20-replay','cleanup']
        record={'session':session,'stages':[{'command':name,'argv_sha256':'c'*64}
                  for name in names]}
        request={'session':session,'argv':[['fixed-'+str(index)] for index in range(8)]}
        class Store:
            def begin(self,*args,**kwargs):pass
            def finish(self,identity,ordinal,**kwargs):
                return {'ordinal':ordinal,'exit_code':kwargs['exit_code']}
        for ordinal in (3,4,5,6):
            with self.subTest(ordinal=ordinal),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)
                def capture(argv,output):
                    self.assertEqual(argv,['/usr/local/bin/oxidex-remote-build',PROJECT,'fixed-'+str(ordinal)])
                    output.write(b'bounded output\n')
                    return 17 if ordinal==6 else 0
                with patch.object(engine,'selected',return_value=(path,request,record)),\
                     patch.object(engine,'idle',return_value=True),\
                     patch.object(engine,'capture_stage',side_effect=capture) as captured,\
                     patch.object(engine,'capture_pack'),\
                     patch.object(engine,'file_digest',return_value='d'*64),\
                     patch.object(custody,'Store',return_value=Store()),\
                     patch.object(custody,'host_identity',return_value=host),\
                     patch.object(custody,'current_actor',return_value=actor):
                    result=engine.stage_locked(PROJECT,'r',ordinal,None)
                self.assertEqual(captured.call_count,1)
                self.assertEqual(result['exit_code'],17 if ordinal==6 else 0)
                self.assertEqual((path/('stage-'+str(ordinal)+'.log')).read_bytes(),b'bounded output\n')

    def test_stage_capture_failure_retains_partial_log_and_held_owner(self):
        actor={'pid':11,'start':22};host=('123','new-boot')
        record={'state':'SOURCE_VERIFIED','reservation_id':'r','active':{'ordinal':3,'actor':actor}}
        class Store:
            def locked(self):
                from contextlib import nullcontext
                return nullcontext()
            def begin(self,*args,**kwargs):pass
            def read(self):return record
            def match(self,selected,identity,observed):
                assert selected is record and identity=='r' and observed==host
            def write(self,value):record.update(value)
            def finish(self,*args,**kwargs):raise AssertionError('uncertain stage must not finish')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            session={'project':PROJECT,'head':'a'*40,'bundle_sha256':'b'*64}
            selected_record={**record,'session':session,'stages':[
                {'command':name,'argv_sha256':'c'*64} for name in
                ('prepare','upload-receive','verify-source','task20-bootstrap')]}
            request={'session':session,'argv':[[],[],[],['fixed']]}
            def overflow(argv,output):
                output.write(b'bounded partial\n')
                raise ValueError('Bounded control log exceeded during capture; custody held')
            with patch.object(engine,'selected',return_value=(path,request,selected_record)),\
                 patch.object(engine,'idle',return_value=True),\
                 patch.object(engine,'capture_stage',side_effect=overflow),\
                 patch.object(engine,'file_digest',return_value='d'*64),\
                 patch.object(custody,'Store',return_value=Store()),\
                 patch.object(custody,'host_identity',return_value=host),\
                 patch.object(custody,'current_actor',return_value=actor):
                with self.assertRaisesRegex(ValueError,'during capture'):
                    engine.stage_locked(PROJECT,'r',3,None)
            self.assertEqual(record['state'],'HELD')
            self.assertEqual(record['active'],{'ordinal':3,'actor':actor})
            self.assertEqual((path/'stage-3.log').read_bytes(),b'bounded partial\n')

    def test_new_stage_log_directory_sync_precedes_terminal_record(self):
        actor={'pid':11,'start':22};host=('123','new-boot')
        record={'state':'SOURCE_VERIFIED','reservation_id':'r',
                'active':{'ordinal':7,'actor':actor,'command':'cleanup'}}
        class Store:
            def locked(self):
                from contextlib import nullcontext
                return nullcontext()
            def begin(self,*args,**kwargs):pass
            def read(self):return record
            def match(self,selected,identity,observed):
                assert selected is record and identity=='r' and observed==host
            def write(self,value):record.update(value)
            def finish(self,*args,**kwargs):raise AssertionError('terminal record preceded directory sync')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            session={'project':PROJECT,'head':'a'*40,'bundle_sha256':'b'*64}
            selected={**record,'session':session,'stages':[{'command':'cleanup'}]*8}
            request={'session':session,'argv':[[]]*8}
            actual_fsync=os.fsync;actual_fstat=os.fstat;order=[]
            def fail_directory_sync(fd):
                kind='directory' if stat.S_ISDIR(actual_fstat(fd).st_mode) else 'file'
                order.append(kind)
                if kind=='directory':raise OSError('injected dossier directory fsync failure')
                return actual_fsync(fd)
            with patch.object(engine,'selected',return_value=(path,request,selected)),\
                 patch.object(engine,'idle',return_value=True),\
                 patch.object(custody,'Store',return_value=Store()),\
                 patch.object(custody,'host_identity',return_value=host),\
                 patch.object(custody,'current_actor',return_value=actor),\
                 patch.object(engine.os,'fsync',side_effect=fail_directory_sync):
                with self.assertRaisesRegex(OSError,'dossier directory fsync failure'):
                    engine.stage_locked(PROJECT,'r',7,None)
            self.assertEqual(order,['file','directory','file'])
            self.assertEqual(record['state'],'HELD')
            self.assertEqual(record['active']['ordinal'],7)
            self.assertEqual((path/'stage-7.log').read_bytes(),
                             b'Actual custody cleanup only; source/cache/evidence retained.\n')

    def test_runtime_updater_isolated_standalone_entry_and_policy_paths(self):
        updater=ROOT/'src/lib/builder_runtime_update.py'
        helpers=(ROOT/'tests/fixtures/builder_access_15asset.py',
                 ROOT/'src/lib/builder_access.py')
        probe=ROOT/'tests/isolated_builder_runtime_probe.py'
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'builder_access.py').write_text('raise RuntimeError("cwd poison")\n')
            reviewed=Path(directory,'reviewed_builder_access.py')
            reviewed.write_bytes(helpers[1].read_bytes()+b'\n# Distinct reviewed replacement fixture.\n')
            self.assertNotIn(hashlib.sha256(reviewed.read_bytes()).hexdigest(),
                             update.TRUSTED_BUILDER_ACCESS_SHA256)
            environment=dict(os.environ,PYTHONPATH=directory)
            for helper in helpers:
                for mode in (0o600,0o644):
                    with self.subTest(helper=helper.name,mode=oct(mode)):
                        process=subprocess.run([sys.executable,'-I','-B',str(probe),str(updater),
                                                str(helper),str(reviewed),oct(mode)],
                                               cwd=directory,env=environment,capture_output=True,text=True)
                        self.assertEqual(process.returncode,0,str(helper)+' '+oct(mode)+'\n'+process.stdout+process.stderr)
                        self.assertIn('checks: PASS',process.stdout)
            entry=subprocess.run([sys.executable,'-I','-B',str(updater),'--help'],
                                 cwd=directory,env=environment,capture_output=True,text=True)
            self.assertEqual(entry.returncode,0,entry.stdout+entry.stderr)
            self.assertIn('--recover-policy',entry.stdout)

    def test_permanent_guard_requires_exact_bytes_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'guard.conf'
            path.write_bytes(observation.BOOT_GUARD_BYTES);path.chmod(0o644)
            original=os.fstat
            def root_fstat(fd):
                info=original(fd);values=list(info);values[4]=0
                return os.stat_result(values)
            with patch.object(observation,'BOOT_GUARD',str(path)),patch.object(observation.os,'fstat',side_effect=root_fstat):
                observation.verify_permanent_boot_guard()
                path.write_bytes(b'foreign hold\n')
                with self.assertRaisesRegex(ValueError,'boot guard differs'):
                    observation.verify_permanent_boot_guard()

    def test_supervisor_waits_after_shell_exits_with_background_child(self):
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory)/'build.lock';active=Path(directory)/'active'
            worker=Path(directory)/'worker.py'
            worker.write_text('from pathlib import Path\nimport time\n'
                'p=Path('+repr(str(active))+');p.write_text("live")\n'
                'time.sleep(.35)\np.unlink()\n')
            command=('python3 '+str(worker)+' & while [ ! -f '+str(active)+
                     ' ]; do sleep .01; done')
            user=pwd.getpwuid(os.getuid())
            original_fstat=os.fstat;original_open=os.open;lock_fd=[None]
            def selected_open(path,flags,*args,**kwargs):
                fd=original_open(path,flags,*args,**kwargs)
                if str(path)==str(lock):lock_fd[0]=fd
                return fd
            def root_fstat(fd):
                info=original_fstat(fd)
                if fd==lock_fd[0]:
                    values=list(info);values[4]=0
                    return os.stat_result(values)
                return info
            fake=types.ModuleType('qualification_trust.reservation');fake.route_guard=lambda **kwargs:None
            package=types.ModuleType('qualification_trust');package.__path__=[]
            def members(path):return {os.getpid(),999999} if active.exists() else {os.getpid()}
            with patch.object(uploader,'LOCK',str(lock)),patch.object(uploader,'own_scope',return_value=Path(directory)),\
                 patch.object(uploader,'scope_processes',side_effect=members),\
                 patch.object(uploader.os,'open',side_effect=selected_open),\
                 patch.object(uploader.os,'fstat',side_effect=root_fstat),\
                 patch.object(uploader.os,'setgroups'),patch.object(uploader.os,'setgid'),patch.object(uploader.os,'setuid'),\
                 patch.dict(sys.modules,{'qualification_trust':package,'qualification_trust.reservation':fake}):
                started=time.monotonic()
                self.assertEqual(uploader.supervise(command,user),0)
                self.assertGreaterEqual(time.monotonic()-started,.3)

    def test_supervisor_does_not_give_child_unlockable_fd(self):
        # Run an actual child which tries LOCK_UN on every inherited FD. An
        # independent EX contender must remain blocked until the child exits.
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory)/'build.lock'
            ready=Path(directory)/'ready'
            child_script=Path(directory)/'unlock.py'
            child_script.write_text('import fcntl,os,time\n'
                'for n in range(3,64):\n'
                '    try: fcntl.flock(n,fcntl.LOCK_UN)\n'
                '    except OSError: pass\n'
                'open('+repr(str(ready))+',"w").close()\n'
                'time.sleep(.4)\n')
            user=pwd.getpwuid(os.getuid())
            command='python3 '+str(child_script)
            original_fstat=os.fstat
            def root_fstat(fd):
                info=original_fstat(fd)
                if fd == lock_fd[0]:
                    values=list(info);values[4]=0
                    return os.stat_result(values)
                return info
            lock_fd=[None]
            original_open=os.open
            def selected_open(path,flags,*args,**kwargs):
                fd=original_open(path,flags,*args,**kwargs)
                if str(path)==str(lock):lock_fd[0]=fd
                return fd
            fake=types.ModuleType('qualification_trust.reservation')
            fake.route_guard=lambda **kwargs:None
            package=types.ModuleType('qualification_trust');package.__path__=[]
            with patch.object(uploader,'LOCK',str(lock)),patch.object(uploader,'own_scope',return_value=Path(directory)),\
                 patch.object(uploader,'scope_processes',side_effect=lambda path:{os.getpid()}),\
                 patch.object(uploader.os,'open',side_effect=selected_open),\
                 patch.object(uploader.os,'fstat',side_effect=root_fstat),\
                 patch.object(uploader.os,'setgroups'),patch.object(uploader.os,'setgid'),patch.object(uploader.os,'setuid'),\
                 patch.dict(sys.modules,{'qualification_trust':package,'qualification_trust.reservation':fake}):
                # The supervisor itself runs in a separate process to permit a
                # genuinely independent lock contender in this test process.
                pid=os.fork()
                reaped=False
                if pid==0:
                    try:os._exit(uploader.supervise(command,user))
                    except BaseException:os._exit(88)
                try:
                    until=time.monotonic()+3
                    while not ready.exists() and time.monotonic()<until:time.sleep(.01)
                    self.assertTrue(ready.exists())
                    with lock.open('rb') as contender:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    status=os.waitpid(pid,0)[1];reaped=True
                    self.assertEqual(status,0)
                finally:
                    if not reaped:
                        try:os.kill(pid,9)
                        except ProcessLookupError:pass

    def test_permanent_guard_allows_idle_but_unknown_uploader_uid_holds(self):
        username='synthetic-uploader';uid=2000
        with tempfile.TemporaryDirectory() as directory:
            process=Path(directory)/'1234';process.mkdir()
            (process/'status').write_text('Name:\tpython3\nUid:\t2000\t2000\t2000\t2000\n')
            original_iterdir=Path.iterdir
            def iterdir(path):
                return iter([process]) if str(path)=='/proc' else original_iterdir(path)
            def root_json(path,maximum=65536):
                if path=='/etc/oxidex-remote-build.json':return {'role':'builder'}
                if path=='/etc/oxidex-builder-uploader.json':return {'username':username,'uid':uid}
                return {}
            def command(argv):
                if argv[0]=='/usr/sbin/sshd':return ('forcecommand /usr/local/sbin/oxidex-uploader-session\n'
                    'disableforwarding yes\n')
                return ''
            with patch.object(observation,'root_json',side_effect=root_json),\
                 patch.object(observation,'command',side_effect=command),\
                 patch.object(Path,'iterdir',iterdir):
                with self.assertRaisesRegex(ValueError,'uploader UID'):
                    observation.quiescent(PROJECT)
                (process/'status').write_text('Name:\tpython3\nUid:\t3000\t3000\t3000\t3000\n')
                self.assertTrue(observation.quiescent(PROJECT))

    def test_policy_bytes_must_equal_root_mapping(self):
        mapping={'schema':'root-approved-ssh-mapping-v1','principal':'test@example.invalid',
                 'username':'oxidex-uploader','uid':2000,'identity_receipt_sha256':'1'*64,
                 'public_key':'ssh-ed25519 '+base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\0 '+b'x'*32).decode()}
        mapping['public_key_sha256']=hashlib.sha256(mapping['public_key'].encode()).hexdigest()
        policy=uploader_policy_bytes(mapping)
        assets={path:{'data':base64.b64encode(policy.get(path,b'fixture')).decode(),
                 'sha256':hashlib.sha256(policy.get(path,b'fixture')).hexdigest()}
                for path in update.TARGETS}
        with patch.object(update,'root_file_read',return_value=({'mode':0o600},json.dumps(mapping).encode())),\
             patch.object(pwd,'getpwnam',return_value=types.SimpleNamespace(pw_uid=2000)),\
             patch.object(builder_access,'restricted_group_guard',return_value={}):
            self.assertEqual(update.validate_policy_assets(assets)['uid'],2000)
            target=next(iter(policy));raw=b'oxidex-uploader ALL=(root) NOPASSWD: ALL\n'
            assets[target]={'data':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()}
            with self.assertRaisesRegex(ValueError,'Uploaded policy differs'):
                update.validate_policy_assets(assets)

    def test_policy_recovery_preserves_foreign_file(self):
        nonce='a'*32;boot='old-boot';target=next(iter(update.POLICY_TARGETS))
        helper='/opt/gha-runners/builder_access.py';reviewed_helper=b'reviewed helper\n'
        helper_state={'sha256':hashlib.sha256(reviewed_helper).hexdigest(),
                      'mode':update.TARGETS[helper],'uid':0,'gid':0}
        plan={'nonce':nonce,'boot':boot,'source_main':'b'*40,'vm_id':'123',
              'config_sha256':'c'*64,'assets':{},'before':{target:None,helper:None},'observed_at':1.0}
        journal=b'{"stage":"BACKUPS_COMPLETE"}\n'
        def read(path):
            name=str(path)
            if name.endswith('/plan.json'):return {'sha256':'saved'},json.dumps(plan).encode()
            if name.endswith('/journal.jsonl'):return {'sha256':'journal'},journal
            if name.endswith('runtime-update-pending.json'):
                return {'sha256':'pending'},json.dumps({'nonce':nonce,'boot':boot,'source_main':'b'*40}).encode()
            if name.endswith('oxidex-build-draining'):return {'sha256':'drain'},nonce.encode()
            if name.endswith('leases.json'):return {'sha256':'leases'},b'{}'
            return None,b''
        def state(path):
            name=str(path)
            if name.endswith('oxidex-builder-uploader.json'):return {'sha256':'mapping'}
            if name.endswith('oxidex-remote-build.json'):return {'sha256':'c'*64}
            if name==helper:return dict(helper_state)
            if name==target:return {'sha256':'f'*64,'mode':0o644,'uid':0,'gid':0}
            return None
        def command(argv):return '123' if argv[0]=='/usr/bin/curl' else ''
        def finish(lock,drain,token,check,action,**kwargs):
            kwargs['lock_bound'](lambda:None)
            check();result=action();check();return result
        selected=[]
        @contextmanager
        def selection(path,expected,read_state):
            if str(path)==helper:
                self.assertEqual(expected,helper_state)
                selected.append((str(path),expected['sha256']))
            self.assertEqual(read_state(),expected)
            yield lambda:self.assertEqual(read_state(),expected)
        with patch.object(update,'validate_plan'),patch.object(update,'validate_policy_assets'),\
             patch.object(update,'validate_assets',return_value={target:b'approved',helper:reviewed_helper}),\
             patch.object(update,'root_file_read',side_effect=read),\
             patch.object(update,'root_file_state',side_effect=state),\
             patch.object(update,'verify_boot_guard'),patch.object(update,'checked_command',side_effect=command),\
             patch.object(update,'observed_work_processes',return_value=[]),\
             patch.object(update,'retained_file_selection',side_effect=selection),\
             patch.object(update,'finish_owned_drain',side_effect=finish):
            with self.assertRaisesRegex(ValueError,'Foreign policy state'):
                update.recover_policy_update(plan)
        self.assertTrue(selected)

    def test_policy_recovery_restores_selected_backup_and_is_retryable(self):
        target='/etc/ssh/sshd_config.d/40-oxidex-uploader-fence.conf'
        helper='/opt/gha-runners/builder_access.py';reviewed_helper=b'reviewed helper\n'
        old=b'old fixed policy\n';new=b'new fixed policy\n'
        sha=lambda raw:hashlib.sha256(raw).hexdigest()
        nonce='a'*32;boot='old-boot';journal=[b'{"stage":"BACKUPS_COMPLETE"}\n']
        before={'sha256':sha(old),'mode':0o644,'uid':0,'gid':0}
        current=[{'sha256':sha(new),'mode':0o644,'uid':0,'gid':0}]
        helper_state={'sha256':sha(reviewed_helper),'mode':update.TARGETS[helper],
                      'uid':0,'gid':0}
        plan={'nonce':nonce,'boot':boot,'source_main':'b'*40,'vm_id':'123',
              'config_sha256':'c'*64,'assets':{},'before':{target:before,helper:None},'observed_at':1.0}
        def read(path):
            name=str(path)
            if name.endswith('/plan.json'):return {'sha256':'saved'},json.dumps(plan).encode()
            if name.endswith('/journal.jsonl'):return {'sha256':sha(journal[0])},journal[0]
            if name.endswith('/0.before'):return {'sha256':sha(old)},old
            if name.endswith('runtime-update-pending.json'):
                return {'sha256':'pending'},json.dumps({'nonce':nonce,'boot':boot,'source_main':'b'*40}).encode()
            if name.endswith('oxidex-build-draining'):return {'sha256':'drain'},nonce.encode()
            if name.endswith('leases.json'):return {'sha256':'leases'},b'{}'
            return None,b''
        def state(path):
            name=str(path)
            if name.endswith('oxidex-builder-uploader.json'):return {'sha256':'mapping'}
            if name.endswith('oxidex-remote-build.json'):return {'sha256':'c'*64}
            if name==helper:return dict(helper_state)
            return current[0] if name==target else None
        def command(argv):return '123' if argv[0]=='/usr/bin/curl' else ''
        def write(path,raw,mode,**kwargs):
            kwargs['before_replace']()
            self.assertEqual(raw,old)
            current[0]=dict(before)
        def append(path,row,**kwargs):
            self.assertEqual(kwargs['selected_history'],({'sha256':sha(journal[0])},journal[0]))
            journal[0]+=json.dumps(row).encode()+b'\n'
            return {'sha256':sha(journal[0])},journal[0]
        def finish(lock,drain,token,check,action,**kwargs):
            kwargs['lock_bound'](lambda:None)
            check();result=action();check();return result
        selected=[]
        @contextmanager
        def selection(path,expected,read_state):
            if str(path)==helper:
                self.assertEqual(expected,helper_state)
                selected.append((str(path),expected['sha256']))
            self.assertEqual(read_state(),expected)
            yield lambda:self.assertEqual(read_state(),expected)
        with patch.object(update,'TARGETS',{target:0o644,helper:0o644}),patch.object(update,'POLICY_TARGETS',{target:0o644}),\
             patch.object(update,'validate_plan'),patch.object(update,'validate_policy_assets'),\
             patch.object(update,'validate_assets',return_value={target:new,helper:reviewed_helper}),\
             patch.object(update,'root_file_read',side_effect=read),patch.object(update,'root_file_state',side_effect=state),\
             patch.object(update,'verify_boot_guard'),patch.object(update,'checked_command',side_effect=command),\
             patch.object(update,'observed_work_processes',return_value=[]),\
             patch.object(update,'finish_owned_drain',side_effect=finish),\
             patch.object(update,'retained_file_selection',side_effect=selection),\
             patch.object(update,'atomic_write',side_effect=write),\
             patch.object(update,'append_journal_record',side_effect=append):
            self.assertEqual(update.recover_policy_update(plan)['status'],'POLICY_ROLLED_BACK_HELD')
            self.assertEqual(update.recover_policy_update(plan)['status'],'POLICY_ROLLED_BACK_HELD')
        self.assertGreaterEqual(len(selected),2)
        self.assertEqual(set(selected),{(helper,sha(reviewed_helper))})

    def test_reboot_reconcile_resumes_published_evidence_and_refuses_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            host=['123','new-boot'];old=['123','old-boot']
            record={'reservation_id':'r','session':{'project':PROJECT},'host':old,
                    'state':'HELD','active':None,'events':[],'stages':[{'command':'cleanup'}],
                    'next_ordinal':0}
            original_record=copy.deepcopy(record)
            stored=[record]
            class Store:
                def __init__(self):self.fail_once=True
                def locked(self):
                    from contextlib import nullcontext
                    return nullcontext()
                def read(self):return copy.deepcopy(stored[0])
                def write(self,value):
                    if self.fail_once:
                        self.fail_once=False;raise RuntimeError('crash after evidence')
                    stored[0]=copy.deepcopy(value)
            store=Store()
            actors=[{'pid':1,'start':1}]
            original_fstat=os.fstat
            def root_fstat(fd):
                info=original_fstat(fd);values=list(info);values[4]=0
                return os.stat_result(values)
            with patch.object(engine,'idle',return_value=True),patch.object(engine,'dossier',return_value=path),\
                 patch.object(custody,'Store',return_value=store),patch.object(custody,'host_identity',return_value=host),\
                 patch.object(custody,'enrollment',return_value={}),\
                 patch.object(custody,'current_actor',side_effect=lambda:actors[0]),\
                 patch.object(engine,'root_json',side_effect=lambda file:json.loads(Path(file).read_text())),\
                 patch.object(engine.os,'fstat',side_effect=root_fstat):
                with self.assertRaisesRegex(RuntimeError,'crash'):
                    engine.reconcile_reboot(PROJECT,'r','old-boot')
                evidence=(path/'reboot-reconciliation.json').read_bytes()
                log=(path/'reboot-cleanup.log').read_bytes()
                actors[0]={'pid':2,'start':2}
                result=engine.reconcile_reboot(PROJECT,'r','old-boot')
                self.assertEqual(result['state'],'RELEASED')
                self.assertEqual(result['events'][-1]['actor'],{'pid':1,'start':1})
                self.assertEqual((path/'reboot-reconciliation.json').read_bytes(),evidence)
                self.assertEqual((path/'reboot-cleanup.log').read_bytes(),log)
                actors[0]={'pid':3,'start':3}
                self.assertEqual(engine.reconcile_reboot(PROJECT,'r','old-boot')['state'],'RELEASED')
                actors[0]={'pid':0,'start':3}
                with self.assertRaisesRegex(ValueError,'Actual PID'):
                    engine.reconcile_reboot(PROJECT,'r','old-boot')
                actors[0]={'pid':3,'start':3}
                stored[0]=copy.deepcopy(original_record)
                (path/'reboot-cleanup.log').chmod(0o600)
                (path/'reboot-cleanup.log').write_bytes(b'foreign\n')
                with self.assertRaisesRegex(ValueError,'cleanup log differs'):
                    engine.reconcile_reboot(PROJECT,'r','old-boot')

    def test_reboot_reconcile_interrupted_cleanup_lost_ack_and_duplicate_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            actor={'pid':1,'start':1}
            record={'reservation_id':'r','session':{'project':PROJECT},
                    'host':['123','old-boot'],'state':'HELD',
                    'active':{'ordinal':0,'command':'cleanup','actor':actor},
                    'events':[{'ordinal':0,'command':'cleanup','actor':actor,
                               'exit_code':None,'log_sha256':None,'disposition':'FINISH_REFUSED'}],
                    'stages':[{'command':'cleanup'}],'next_ordinal':0}
            class Store:
                def locked(self):
                    from contextlib import nullcontext
                    return nullcontext()
                def read(self):return record
                def write(self,value):record.update(value)
            original_fstat=os.fstat
            def root_fstat(fd):
                info=original_fstat(fd);values=list(info);values[4]=0
                return os.stat_result(values)
            with patch.object(engine,'idle',return_value=True),patch.object(engine,'dossier',return_value=path),\
                 patch.object(custody,'Store',return_value=Store()),\
                 patch.object(custody,'host_identity',return_value=('123','new-boot')),\
                 patch.object(custody,'enrollment',return_value={}),\
                 patch.object(custody,'current_actor',return_value=actor),\
                 patch.object(engine,'root_json',side_effect=lambda file:json.loads(Path(file).read_text())),\
                 patch.object(engine.os,'fstat',side_effect=root_fstat):
                self.assertEqual(engine.reconcile_reboot(PROJECT,'r','old-boot')['state'],'RELEASED')
                self.assertEqual([row.get('disposition') for row in record['events']],
                                 ['FINISH_REFUSED','INTERRUPTED',None])
                evidence=(path/'reboot-reconciliation.json').read_bytes()
                log=(path/'reboot-cleanup.log').read_bytes()
                self.assertEqual(engine.reconcile_reboot(PROJECT,'r','old-boot')['state'],'RELEASED')
                self.assertEqual((path/'reboot-reconciliation.json').read_bytes(),evidence)
                self.assertEqual((path/'reboot-cleanup.log').read_bytes(),log)
                record['events'][-1]['actor']={'pid':2,'start':2}
                with self.assertRaisesRegex(ValueError,'cleanup differs'):
                    engine.reconcile_reboot(PROJECT,'r','old-boot')
                record['events'][-1]['actor']=actor
                record['events'].append(copy.deepcopy(record['events'][-1]))
                with self.assertRaisesRegex(ValueError,'cleanup differs'):
                    engine.reconcile_reboot(PROJECT,'r','old-boot')
