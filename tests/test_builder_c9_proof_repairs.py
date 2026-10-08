"""Portable fault controls for the C9 source repair; no host mutation."""
import base64
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
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
from lib.builder_access import uploader_policy_bytes
from lib.qualification_trust import control_observation as observation
from lib.qualification_trust import control_engine as engine
from lib.qualification_trust import reservation as custody

spec=importlib.util.spec_from_file_location('uploader_session',ROOT/'builder/dedicated/uploader-session.py')
uploader=importlib.util.module_from_spec(spec);spec.loader.exec_module(uploader)
PROJECT='qualification-'+'a'*12+'-'+'b'*32


class ProofRepairControls(unittest.TestCase):
    def test_runtime_updater_isolated_standalone_entry_and_policy_paths(self):
        updater=ROOT/'src/lib/builder_runtime_update.py'
        helper=ROOT/'src/lib/builder_access.py'
        probe=ROOT/'tests/isolated_builder_runtime_probe.py'
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'builder_access.py').write_text('raise RuntimeError("cwd poison")\n')
            environment=dict(os.environ,PYTHONPATH=directory)
            process=subprocess.run([sys.executable,'-I','-B',str(probe),str(updater),str(helper)],
                                   cwd=directory,env=environment,capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stdout+process.stderr)
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
        plan={'nonce':nonce,'boot':boot,'source_main':'b'*40,'vm_id':'123',
              'config_sha256':'c'*64,'assets':{},'before':{target:None},'observed_at':1.0}
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
            if name==target:return {'sha256':'f'*64,'mode':0o644,'uid':0,'gid':0}
            return None
        def command(argv):return '123' if argv[0]=='/usr/bin/curl' else ''
        def finish(lock,drain,token,check,action,**kwargs):
            kwargs['lock_bound'](lambda:None)
            check();result=action();check();return result
        with patch.object(update,'validate_plan'),patch.object(update,'validate_policy_assets'),\
             patch.object(update,'validate_assets',return_value={target:b'approved'}),\
             patch.object(update,'root_file_read',side_effect=read),\
             patch.object(update,'root_file_state',side_effect=state),\
             patch.object(update,'verify_boot_guard'),patch.object(update,'checked_command',side_effect=command),\
             patch.object(update,'observed_work_processes',return_value=[]),\
             patch.object(update,'finish_owned_drain',side_effect=finish):
            with self.assertRaisesRegex(ValueError,'Foreign policy state'):
                update.recover_policy_update(plan)

    def test_policy_recovery_restores_selected_backup_and_is_retryable(self):
        target='/etc/ssh/sshd_config.d/40-oxidex-uploader-fence.conf'
        old=b'old fixed policy\n';new=b'new fixed policy\n'
        sha=lambda raw:hashlib.sha256(raw).hexdigest()
        nonce='a'*32;boot='old-boot';journal=[b'{"stage":"BACKUPS_COMPLETE"}\n']
        before={'sha256':sha(old),'mode':0o644,'uid':0,'gid':0}
        current=[{'sha256':sha(new),'mode':0o644,'uid':0,'gid':0}]
        plan={'nonce':nonce,'boot':boot,'source_main':'b'*40,'vm_id':'123',
              'config_sha256':'c'*64,'assets':{},'before':{target:before},'observed_at':1.0}
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
        @contextmanager
        def selection(*args,**kwargs):yield lambda:None
        with patch.object(update,'TARGETS',{target:0o644}),patch.object(update,'POLICY_TARGETS',{target:0o644}),\
             patch.object(update,'validate_plan'),patch.object(update,'validate_policy_assets'),\
             patch.object(update,'validate_assets',return_value={target:new}),\
             patch.object(update,'root_file_read',side_effect=read),patch.object(update,'root_file_state',side_effect=state),\
             patch.object(update,'verify_boot_guard'),patch.object(update,'checked_command',side_effect=command),\
             patch.object(update,'observed_work_processes',return_value=[]),\
             patch.object(update,'finish_owned_drain',side_effect=finish),\
             patch.object(update,'retained_file_selection',side_effect=selection),\
             patch.object(update,'atomic_write',side_effect=write),\
             patch.object(update,'append_journal_record',side_effect=append):
            self.assertEqual(update.recover_policy_update(plan)['status'],'POLICY_ROLLED_BACK_HELD')
            self.assertEqual(update.recover_policy_update(plan)['status'],'POLICY_ROLLED_BACK_HELD')

    def test_reboot_reconcile_resumes_published_evidence_and_refuses_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            host=['123','new-boot'];old=['123','old-boot']
            record={'reservation_id':'r','session':{'project':PROJECT},'host':old,
                    'state':'HELD','active':None,'events':[],'stages':[{'command':'cleanup'}],
                    'next_ordinal':0}
            original_record=copy.deepcopy(record)
            class Store:
                def __init__(self):self.fail_once=True
                def locked(self):
                    from contextlib import nullcontext
                    return nullcontext()
                def read(self):return record
                def write(self,value):
                    if self.fail_once:
                        self.fail_once=False;raise RuntimeError('crash after evidence')
                    record.update(value)
            store=Store()
            original_fstat=os.fstat
            def root_fstat(fd):
                info=original_fstat(fd);values=list(info);values[4]=0
                return os.stat_result(values)
            with patch.object(engine,'idle',return_value=True),patch.object(engine,'dossier',return_value=path),\
                 patch.object(custody,'Store',return_value=store),patch.object(custody,'host_identity',return_value=host),\
                 patch.object(custody,'enrollment',return_value={}),\
                 patch.object(custody,'current_actor',return_value={'pid':1,'start':1}),\
                 patch.object(engine,'root_json',side_effect=lambda file:json.loads(Path(file).read_text())),\
                 patch.object(engine.os,'fstat',side_effect=root_fstat):
                with self.assertRaisesRegex(RuntimeError,'crash'):
                    engine.reconcile_reboot(PROJECT,'r','old-boot')
                evidence=(path/'reboot-reconciliation.json').read_bytes()
                log=(path/'reboot-cleanup.log').read_bytes()
                result=engine.reconcile_reboot(PROJECT,'r','old-boot')
                self.assertEqual(result['state'],'RELEASED')
                self.assertEqual((path/'reboot-reconciliation.json').read_bytes(),evidence)
                self.assertEqual((path/'reboot-cleanup.log').read_bytes(),log)
                self.assertEqual(engine.reconcile_reboot(PROJECT,'r','old-boot')['state'],'RELEASED')
                record.clear();record.update(original_record)
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
