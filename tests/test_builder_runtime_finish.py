"""Small synthetic checks for the installed-runtime finish prerequisite."""
import base64
import hashlib
import inspect
import os
import time
import json
from contextlib import ExitStack
from unittest.mock import patch
import fcntl
import tempfile
import subprocess
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lib import builder_runtime_update as runtime

class InstalledRuntimeTests(unittest.TestCase):
    def fixture(self):
        raw=b'bounded synthetic runtime\n'
        digest=hashlib.sha256(raw).hexdigest()
        assets={path:{'data':base64.b64encode(raw).decode(),'sha256':digest} for path in runtime.TARGETS}
        files={path:{'sha256':digest,'mode':0o644 if path==runtime.LAUNCHER_TARGET else mode} for path,mode in runtime.TARGETS.items()}
        return assets,files,digest

    def verify(self,assets,files,pin):
        self.assertTrue(callable(getattr(runtime,'verify_installed_runtime',None)), 'Installed runtime verifier is missing')
        return runtime.verify_installed_runtime(assets,files,client_launcher_sha256=pin)

    def test_exact_held_install_matches_client_pin(self):
        assets,files,pin=self.fixture()
        self.assertEqual(self.verify(assets,files,pin),pin)

    def test_changed_file_denied(self):
        assets,files,pin=self.fixture();files['/usr/local/bin/docker']['sha256']='0'*64
        with self.assertRaises(ValueError):self.verify(assets,files,pin)

    def test_open_launcher_denied_before_finish(self):
        assets,files,pin=self.fixture();files[runtime.LAUNCHER_TARGET]['mode']=0o755
        with self.assertRaises(ValueError):self.verify(assets,files,pin)

    def test_missing_and_extra_runtime_denied(self):
        for extra in (False,True):
            assets,files,pin=self.fixture()
            if extra:files['/etc/unrelated']={}
            else:files.pop('/usr/local/bin/docker')
            with self.subTest(extra=extra),self.assertRaises(ValueError):self.verify(assets,files,pin)

    def test_old_or_missing_client_pin_denied(self):
        for pin in (None,'0'*64,'invalid'):
            assets,files,_=self.fixture()
            with self.subTest(pin=pin),self.assertRaises(ValueError):self.verify(assets,files,pin)

    def test_missing_file_state_denied(self):
        assets,files,pin=self.fixture();files['/usr/local/bin/docker']=None
        with self.assertRaises(ValueError):self.verify(assets,files,pin)

class RecoveryLockTests(unittest.TestCase):
    def ensure(self,path):
        self.assertTrue(callable(getattr(runtime,'ensure_recovery_lock',None)), 'Checked reboot lock initialization is missing')
        runtime.ensure_recovery_lock(path)

    def test_missing_lock_created_and_existing_inode_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'lock';self.ensure(path);inode=path.stat().st_ino
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.ensure(path);self.assertEqual(path.stat().st_ino,inode)

    def test_foreign_symlink_or_writable_lock_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();target=root/'target';target.touch();path=root/'lock';path.symlink_to(target)
            with self.assertRaises((ValueError,OSError)):self.ensure(path)
            path.unlink();path.touch();path.chmod(0o666)
            with self.assertRaises(ValueError):self.ensure(path)
            self.assertEqual(path.stat().st_mode&0o777,0o666)

class ExclusiveMarkerTests(unittest.TestCase):
    def test_racing_foreign_marker_is_not_replaced(self):
        self.assertIn('exclusive',inspect.signature(runtime.atomic_write).parameters)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'marker'
            def appears():path.write_bytes(b'foreign')
            with self.assertRaises(FileExistsError):runtime.atomic_write(path,b'owned',0o600,before_replace=appears,exclusive=True)
            self.assertEqual(path.read_bytes(),b'foreign')
            self.assertEqual(list(path.parent.iterdir()),[path])

class MarkerRemovalBoundaryTests(unittest.TestCase):
    def test_slow_canonical_guard_expiry_preserves_marker(self):
        self.assertTrue(callable(getattr(runtime,'unlink_selected_marker',None)), 'Shared marker release boundary is missing')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'marker';path.write_bytes(b'owned');late=[False]
            expected=dict(inode=path.stat().st_ino)
            def guard():late[0]=True
            def fresh():
                if late[0]:raise ValueError('expired during canonical lookup')
            with patch.object(runtime,'root_file_state',side_effect=lambda selected:dict(inode=Path(selected).stat().st_ino)):
                with self.assertRaises(ValueError):runtime.unlink_selected_marker(path,expected,guard,fresh)
            self.assertEqual(path.read_bytes(),b'owned')

    def test_content_reads_finish_before_canonical_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'marker';path.write_bytes(b'owned')
            expected=dict(inode=path.stat().st_ino);guarded=[False]
            def read(_):
                self.assertFalse(guarded[0], 'Blocking content read followed authority check')
                return expected
            def guard():guarded[0]=True
            with patch.object(runtime,'root_file_state',side_effect=read):
                runtime.unlink_selected_marker(path,expected,guard,lambda:None)
            self.assertTrue(guarded[0]);self.assertFalse(path.exists())

    def test_ancestor_custody_changed_during_guard_preserves_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'marker';path.write_bytes(b'owned')
            expected=dict(inode=path.stat().st_ino)
            def guard():path.parent.chmod(0o777)
            try:
                with patch.object(runtime,'root_file_state',return_value=expected):
                    with self.assertRaises(ValueError):runtime.unlink_selected_marker(path,expected,guard,lambda:None)
                self.assertEqual(path.read_bytes(),b'owned')
            finally:path.parent.chmod(0o700)

    def test_marker_changed_during_freshness_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'marker';path.write_text('owned');expected=dict(inode=path.stat().st_ino)
            def freshness():
                other=path.parent/'foreign';other.write_text('foreign');other.replace(path)
            with patch.object(runtime,'root_file_state',return_value=expected):
                with self.assertRaises(ValueError):runtime.unlink_selected_marker(path,expected,lambda:None,freshness)
            self.assertEqual(path.read_text(),'foreign')

    def test_absent_marker_retry_still_syncs_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'marker';synced=[]
            with patch.object(runtime,'root_file_state',return_value=None),patch.object(runtime.os,'fsync',side_effect=lambda fd:synced.append(fd)):
                runtime.unlink_selected_marker(path,None,lambda:None,lambda:None)
            self.assertEqual(len(synced),1);self.assertFalse(path.exists())

    def test_marker_replaced_during_canonical_guard_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'marker';path.write_text('owned')
            def state(selected):
                info=Path(selected).stat();return dict(inode=info.st_ino,sha256=hashlib.sha256(Path(selected).read_bytes()).hexdigest())
            expected=state(path)
            def replaced():
                other=path.parent/'other';other.write_text('foreign');other.replace(path)
            with patch.object(runtime,'root_file_state',side_effect=state):
                with self.assertRaises(ValueError):runtime.unlink_selected_marker(path,expected,replaced,lambda:None)
            self.assertEqual(path.read_text(),'foreign')

class ApplyPublicationBoundaryTests(unittest.TestCase):
    def test_target_replaced_during_lock_lookup_is_preserved(self):
        self.assertTrue(callable(getattr(runtime,'apply_replacement_fence',None)), 'Shared apply publication boundary is missing')
        _,plan,_,_=InstalledHistoryTests().fixture();plan['observed_at']=time.time();target='/usr/local/bin/docker'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'target';path.write_bytes(b'old')
            def state(selected):return dict(inode=path.stat().st_ino,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            plan['before'][target]=state(target)
            def guard():
                other=path.parent/'foreign';other.write_bytes(b'foreign');other.replace(path)
            with patch.object(runtime,'Path',side_effect=lambda value:path if str(value)==target else Path(value)),patch.object(runtime,'mutation_idle_fence',return_value=None),patch.object(runtime,'file_mutation_fence',return_value=None),patch.object(runtime,'root_file_state',side_effect=state):
                with self.assertRaises(ValueError):runtime.apply_replacement_fence(plan,target,None,guard)
            self.assertEqual(path.read_bytes(),b'foreign')

    def test_target_replaced_during_final_validation_is_preserved(self):
        _,plan,_,_=InstalledHistoryTests().fixture();plan['observed_at']=time.time();target='/usr/local/bin/docker'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'target';path.write_bytes(b'old')
            def state(selected):return dict(inode=path.stat().st_ino,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            plan['before'][target]=state(target);validate=runtime.validate_plan
            def freshness(*args):
                result=validate(*args)
                other=path.parent/'foreign';other.write_bytes(b'foreign');other.replace(path)
                return result
            with patch.object(runtime,'Path',side_effect=lambda value:path if str(value)==target else Path(value)),patch.object(runtime,'mutation_idle_fence',return_value=None),patch.object(runtime,'file_mutation_fence',return_value=None),patch.object(runtime,'root_file_state',side_effect=state),patch.object(runtime,'validate_plan',side_effect=freshness):
                with self.assertRaises(ValueError):runtime.apply_replacement_fence(plan,target,None,lambda:None)
            self.assertEqual(path.read_bytes(),b'foreign')

class InactiveServiceTests(unittest.TestCase):
    def parse(self,raw):
        self.assertTrue(callable(getattr(runtime,'parse_inactive_service_state',None)), 'Native inactive state parser is missing')
        return runtime.parse_inactive_service_state(raw)

    def test_exact_inactive_service_record(self):
        raw='ControlPID=0\nMainPID=0\nSubState=dead\nActiveState=inactive\nLoadState=loaded\n'
        self.assertEqual(self.parse(raw),dict(LoadState='loaded',ActiveState='inactive',SubState='dead',MainPID='0',ControlPID='0'))

    def test_unknown_duplicate_missing_or_running_state_refused(self):
        good='LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n'
        for raw in (good+'MainPID=0\n',good+'Unknown=0\n',good.replace('ControlPID=0\n',''),good.replace('MainPID=0','MainPID=123'),good.replace('loaded','not-found')):
            with self.subTest(raw=raw),self.assertRaises(ValueError):self.parse(raw)

    def test_recovery_parser_accepts_exact_running_record(self):
        self.assertTrue(callable(getattr(runtime,'parse_recovery_service_state',None)), 'Recoverable service parser is missing')
        raw='LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=50\nControlPID=0\n'
        self.assertEqual(runtime.parse_recovery_service_state(raw)['MainPID'],'50')
        with self.assertRaises(ValueError):runtime.parse_recovery_service_state(raw.replace('running','start'))

class OrdinaryFinishClockTests(unittest.TestCase):
    def setUp(self):
        custody = patch.object(runtime,'authenticate_ancestor_custody')
        custody.start();self.addCleanup(custody.stop)

    def exercise(self,expire_at,*,recovery=False):
        _,plan,_,pin=InstalledHistoryTests().fixture();plan['observed_at']=time.time();clock=[plan['observed_at']];opened=[];stages=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();lock=root/'lock';lock.touch();drain=root/'drain';drain.write_text(plan['nonce']);drain.chmod(0o600)
            pending=root/'pending';pending.write_text(json.dumps(dict(nonce=plan['nonce'],boot=plan['boot'],source_main=plan['source_main'])));permit=root/'permit';permit.write_text(plan['nonce']);launcher=root/'launcher';launcher.write_bytes(b'bounded synthetic runtime\n');launcher.chmod(0o644)
            def state(path):
                path=launcher if str(path)==runtime.LAUNCHER_TARGET else lock if str(path)=='/run/oxidex-build.lock' else Path(path)
                if not path.exists():return None
                info=path.stat()
                if expire_at=='permit-read' and path==permit and 'FINISH_START' in stages:clock[0]=plan['observed_at']+121
                return dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),mode=info.st_mode&0o777,uid=info.st_uid,gid=info.st_gid,device=info.st_dev,inode=info.st_ino)
            publishing=[False];permit_seen=[False]
            if expire_at=='recovery-publication-lock':permit.unlink()
            plan['before']={k:dict(v) for k,v in plan['before'].items()};plan['before'][runtime.LAUNCHER_TARGET]=state(launcher)
            if expire_at=='selected-gid-changed':plan['before'][runtime.LAUNCHER_TARGET]['gid']+=1
            guard=root/'guard';guard.write_bytes(runtime.BOOT_GUARD_BYTES);guard.chmod(0o644)
            def snapshot(expected):return dict(expected,image_pin=expected['image_id'],admission_valid=True,cpu=16,role='builder',cpus=16,memory_gib=28,tools_ready=True,leases={},containers=[],upload_processes=[],pending_nonce=plan['nonce'] if pending.exists() else None,drain_token=drain.read_text())
            def record(path,row,**kwargs):
                if expire_at=='authority-stage-expiry' and row['stage']=='FINISH_RELEASE_COMMITTED':clock[0]=plan['observed_at']+121
                if kwargs.get('before_publish') is not None:kwargs['before_publish']()
                if kwargs.get('freshness') is not None:kwargs['freshness']()
                stages.append(row['stage'])
                if expire_at in ('lock-during-hold','selected-gid-changed') and row['stage']=='FINISH_START':raise ValueError('force hold')
                if (expire_at=='lock-during-hold' and row['stage']=='HOLD_FINISH_INTENT') or (expire_at=='lock-during-commit' and row['stage']=='FINISH_RELEASE_COMMITTED'):
                    replacement=root/'replacement';replacement.touch();replacement.replace(lock)
                if row['stage']==expire_at:clock[0]=plan['observed_at']+121
                return None,b''
            def read(path):
                if publishing[0] and Path(path)==permit:permit_seen[0]=True
                selected=drain if str(path)=='/run/oxidex-build-draining' else Path(path)
                return (None,b'') if selected.name=='journal.jsonl' else (state(selected),selected.read_bytes() if selected.exists() else b'')
            closed=[]
            real_fsync,real_owned,real_restore,real_close=os.fsync,runtime.finish_owned_drain,runtime.restore_launcher_execution,runtime.close_launcher_execution
            def sync(fd):
                real_fsync(fd)
                if expire_at=='pending-fsync' and not pending.exists():clock[0]=plan['observed_at']+121
            def restore(path,expected,**kwargs):
                if expire_at=='require-commit':self.assertIn('FINISH_RELEASE_COMMITTED',stages)
                real_restore(launcher,expected,**kwargs);opened.append(1)
            def close(path,**kwargs):real_close(launcher,**kwargs);closed.append(1)
            validate=runtime.validate_plan;write=runtime.atomic_write
            def checked_validation(*args):
                result=validate(*args)
                if expire_at=='recovery-publication-lock' and publishing[0] and permit_seen[0]:
                    other=root/'changed-lock';other.touch();other.replace(lock)
                return result
            def selected_write(path,*args,**kwargs):
                if expire_at=='recovery-publication-lock' and Path(path)==permit:
                    publishing[0]=True;permit_seen[0]=False
                return write(path,*args,**kwargs)
            with ExitStack() as stack:
                replacements=dict(validate_plan=checked_validation,atomic_write=selected_write,PENDING_UPDATE=pending,CURRENT_BOOT_PERMIT=permit,BOOT_GUARD=guard,installed_history=lambda *args,**kwargs:(root,(None,b'')) if kwargs.get('with_history') else root,live_snapshot=snapshot,mutation_idle_fence=lambda plan:None,admission_ready=lambda *args,**kwargs:None,verify_boot_hold=lambda plan:None,root_file_state=state,root_file_read=read,append_journal_record=record,restore_launcher_execution=restore,close_launcher_execution=close,finish_owned_drain=lambda _lock,_drain,*args,**kwargs:real_owned(lock,drain,*args,**kwargs))
                for name,value in replacements.items():stack.enter_context(patch.object(runtime,name,value))
                stack.enter_context(patch.object(time,'time',side_effect=lambda:clock[0]));stack.enter_context(patch.object(runtime.os,'fsync',side_effect=sync))
                if expire_at=='require-commit':
                    result=runtime.finish_update(plan,pin)
                    self.assertEqual(result['status'],'REOPENED_UNQUALIFIED')
                else:
                    with self.assertRaises(ValueError):
                        (runtime.recover_finish_update if recovery else runtime.finish_update)(plan,pin)
            if expire_at=='require-commit':
                self.assertFalse(drain.exists());self.assertEqual(launcher.stat().st_mode&0o777,0o755)
            else:
                self.assertEqual(drain.read_text(),plan['nonce']);self.assertEqual(launcher.stat().st_mode&0o777,0o644)
                if expire_at=='permit-read':self.assertTrue(permit.exists())
                if expire_at=='recovery-publication-lock':self.assertFalse(permit.exists())
                if expire_at in ('lock-during-hold','selected-gid-changed'):self.assertEqual(closed,[])
                if expire_at=='lock-during-commit':self.assertEqual(opened,[]);self.assertTrue(pending.exists())
                if expire_at=='authority-stage-expiry':self.assertNotIn('FINISH_RELEASE_COMMITTED',stages)
            return closed if recovery else opened

    def test_same_boot_marker_publication_refuses_lost_final_mutex(self):self.exercise('recovery-publication-lock',recovery=True)

    def test_failure_hold_refuses_unselected_group(self):self.exercise('selected-gid-changed')

    def test_expired_authority_staging_never_publishes_commit(self):self.exercise('authority-stage-expiry')

    def test_lost_lock_during_forward_commit_prevents_effects(self):self.exercise('lock-during-commit')

    def test_lost_lock_during_hold_publication_prevents_chmod(self):self.exercise('lock-during-hold')

    def test_expired_permit_read_preserves_permit(self):self.exercise('permit-read')

    def test_release_authority_precedes_execution_permission(self):self.exercise('require-commit')

    def test_recovery_journal_expiry_prevents_chmod(self):self.assertEqual(self.exercise('FINISH_RECOVERY_START',recovery=True),[])

    def test_start_journal_expiry_does_not_restore_execution(self):self.assertEqual(self.exercise('FINISH_START'),[])
    def test_pending_directory_fsync_expiry_keeps_drain(self):self.exercise('pending-fsync')

class CommittedFlowTests(unittest.TestCase):
    def setUp(self):
        # Synthetic root-service flows use real temporary files under user ancestors.
        custody = patch.object(runtime,'authenticate_ancestor_custody')
        custody.start();self.addCleanup(custody.stop)

    def exercise(self,*,failure=None,existing=False,already_running=False,missing_lock=False,held=False,missing_pending=False):
        original,_,plan,pin=InstalledHistoryTests().reboot_fixture();plan['observed_at']=time.time()
        if failure=='missing-schema':plan.pop('service_state')
        if already_running:plan['service_state']=dict(LoadState='loaded',ActiveState='active',SubState='running',MainPID='50',ControlPID='0',StartTicks=60)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();lock=root/'lock'
            binding_paths={}
            for name,field in (('/etc/oxidex-remote-build.json','config_sha256'),('/etc/gha-admission.json','admission_sha256'),('/etc/systemd/system/gha-admission.service','service_sha256')):
                path=root/field;path.write_bytes(field.encode());binding_paths[name]=path
                plan[field]=hashlib.sha256(path.read_bytes()).hexdigest()
            if not missing_lock:lock.touch()
            drain=root/'drain';pending=root/'pending';permit=root/'permit'
            if not missing_pending:pending.write_text('retained')
            if failure=='foreign-permit':permit.write_text('foreign');permit.chmod(0o600)
            if failure=='permit-swapped':permit.write_text(plan['nonce']);permit.chmod(0o600)
            launcher=root/'launcher';launcher.write_bytes(b'bounded synthetic runtime\n');launcher.chmod(0o644 if held else 0o755)
            if held:
                info=launcher.stat();plan['before']={k:dict(v) for k,v in plan['before'].items()}
                plan['before'][runtime.LAUNCHER_TARGET].update(mode=0o644,uid=info.st_uid,gid=info.st_gid,device=info.st_dev,inode=info.st_ino)
            if existing:drain.write_text(plan['nonce']);drain.chmod(0o600)
            running=already_running;executed=already_running;events=[];snapshots=0;captures=0;marker_reads=0;clock=[plan['observed_at']]
            def file_state(path):
                nonlocal marker_reads
                path=lock if str(path)=='/run/oxidex-build.lock' else binding_paths.get(str(path),Path(path))
                if path in (pending,lock,permit,*binding_paths.values()):
                    if path==pending:
                        marker_reads+=1
                        if failure=='final-marker-expiry' and marker_reads>1:clock[0]=plan['observed_at']+121
                    if not path.exists():return None
                    info=path.stat();return dict(inode=info.st_ino,device=info.st_dev,uid=info.st_uid,gid=info.st_gid,mode=info.st_mode&0o777,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                return None
            def read(path):
                if str(path)=='/run/oxidex-build-draining':return None,drain.read_bytes() if drain.exists() else b''
                if Path(path).name=='journal.jsonl':return None,b''
                if Path(path)==pending:return file_state(pending),pending.read_bytes() if pending.exists() else b''
                if Path(path)==permit:
                    selected=file_state(permit);raw=permit.read_bytes() if permit.exists() else b''
                    if failure=='permit-swapped' and raw==plan['nonce'].encode():
                        other=root/'foreign-permit';other.write_text('foreign');other.chmod(0o600);other.replace(permit)
                    return selected,raw
                raise AssertionError('Unexpected fixture read '+str(path))
            def capture(*args,**kwargs):
                nonlocal captures
                captures+=1
                if failure=='capture-expiry' and captures>1:clock[0]=plan['observed_at']+121
                if running and not already_running:raise ValueError('Inactive capture used after service start')
                return (dict(plan),(None,b''),original) if kwargs.get('with_history') else dict(plan)
            def command(argv,**kwargs):
                nonlocal running
                self.assertEqual(argv,['/usr/bin/systemctl','start','gha-admission.service'])
                self.assertEqual(drain.read_text(),plan['nonce']);self.assertTrue(pending.exists());self.assertEqual(permit.read_text(),plan['nonce'])
                if missing_pending:self.assertEqual(json.loads(pending.read_text()),dict(nonce=original['nonce'],boot=original['boot'],source_main=original['source_main']))
                events.append('start')
                if failure=='start':raise OSError('service start failed')
                running=True;return ''
            def identity(*,strict=True):
                if strict and not executed:raise ValueError('Unexpected admission process')
                return 50,60
            def ready(active,pid,ticks,**kwargs):
                nonlocal executed
                executed=True
                self.assertTrue(running);self.assertEqual((pid,ticks),(50,60));events.append('ready')
                if failure=='readiness':raise ConnectionRefusedError('not ready')
            def snapshot(active, *, committed_original=None):
                nonlocal snapshots
                self.assertEqual(committed_original,original, 'Recovery snapshot lost original marker authority')
                snapshots+=1
                if failure=='final' and snapshots>1:raise ValueError('final host observation failed')
                return dict(active,image_pin=active['image_id'],admission_valid=True,cpu=16,role='builder',cpus=16,memory_gib=28,tools_ready=True,leases={},containers=[],upload_processes=[],pending_nonce=plan['nonce'] if pending.exists() else None,drain_token=drain.read_text())
            real_new,real_owned,real_init,real_restore=runtime.drained_update,runtime.finish_owned_drain,runtime.ensure_recovery_lock,runtime.restore_launcher_execution
            real_sync=os.fsync
            def sync(fd):
                real_sync(fd)
                if failure=='pending-expiry' and not pending.exists():clock[0]=plan['observed_at']+121
                if failure=='lock-pending-fsync' and not pending.exists():
                    replacement=root/'new-lock';replacement.touch();replacement.replace(lock)
            def restore_selected(path,expected,**kwargs):
                real_restore(launcher,expected,**kwargs)
                if failure=='marker-replaced':
                    replacement=root/'foreign';replacement.write_text('foreign');replacement.replace(pending)
            def new_transaction(_lock,_drain,*args,**kwargs):return real_new(lock,drain,*args,**kwargs)
            def owned_transaction(_lock,_drain,*args,**kwargs):return real_owned(lock,drain,*args,**kwargs)
            def journal(path,row,**kwargs):
                events.append(row['stage'])
                if failure=='checkpoint-expiry':clock[0]=plan['observed_at']+121
                return None,b''
            with ExitStack() as stack:
                stack.enter_context(patch.object(runtime,'Path',side_effect=lambda value:binding_paths.get(str(value),Path(value))))
                stack.enter_context(patch.object(time,'time',side_effect=lambda:clock[0]))
                stack.enter_context(patch.object(runtime.os,'fsync',side_effect=sync))
                replacements=dict(verify_boot_guard=lambda:None,restore_launcher_execution=restore_selected,ensure_recovery_lock=lambda path:real_init(lock),PENDING_UPDATE=pending,CURRENT_BOOT_PERMIT=permit,capture_committed_recovery=capture,root_file_state=file_state,root_file_read=read,checked_command=command,service_identity=identity,admission_ready=ready,live_snapshot=snapshot,append_journal_record=journal,drained_update=new_transaction,finish_owned_drain=owned_transaction)
                for name,value in replacements.items():stack.enter_context(patch.object(runtime,name,value))
                if failure:
                    with self.assertRaises((OSError,ValueError,KeyError)):runtime.complete_committed_recovery(plan,pin)
                    self.assertEqual(drain.read_text(),plan['nonce'])
                    if failure=='capture-expiry':self.assertEqual(launcher.stat().st_mode&0o777,0o644)
                    if failure=='marker-replaced':self.assertEqual(pending.read_text(),'foreign')
                    if failure in ('final-marker-expiry','missing-schema','foreign-permit','permit-swapped'):self.assertNotIn('start',events)
                    if failure in ('foreign-permit','permit-swapped'):self.assertEqual(permit.read_text(),'foreign')
                    if failure in ('pending-expiry','lock-pending-fsync'):
                        self.assertIn('COMMITTED_REBOOT_RECOVERED',events);self.assertIn('ready',events)
                    if failure in ('missing-schema','start','readiness','final'):self.assertTrue(pending.exists(), 'Persistent hold lost before successful postflight')
                else:
                    result=runtime.complete_committed_recovery(plan,pin)
                    self.assertEqual(result['status'],'REBOOT_RECOVERED_UNQUALIFIED');self.assertIs(result['qualification_ready'],False)
                    self.assertFalse(drain.exists());self.assertTrue(running);self.assertGreaterEqual(snapshots,2)
                    if held:self.assertEqual(launcher.stat().st_mode&0o777,0o755)
            return events

    def test_incomplete_selected_plan_refused_before_effects(self):self.exercise(failure='missing-schema',existing=True)

    def test_expired_final_marker_read_retains_drain(self):self.exercise(failure='final-marker-expiry',existing=True)

    def test_lost_lock_during_pending_fsync_prevents_start(self):self.exercise(failure='lock-pending-fsync',existing=True)

    def test_missing_pending_hold_is_recreated_before_start(self):self.exercise(missing_pending=True)
    def test_validated_permit_replacement_is_preserved(self):self.exercise(failure='permit-swapped',existing=True)
    def test_foreign_boot_permit_is_preserved(self):self.exercise(failure='foreign-permit',existing=True)
    def test_missing_pending_hold_survives_start_failure(self):self.exercise(failure='start',existing=True,missing_pending=True)

    def test_foreign_marker_replacement_is_preserved(self):self.exercise(failure='marker-replaced',existing=True,held=True)
    def test_pending_fsync_expiry_retains_drain_after_postflight(self):self.exercise(failure='pending-expiry',existing=True)

    def test_slow_capture_expiry_does_not_restore_launcher(self):self.exercise(failure='capture-expiry',existing=True,held=True)

    def test_held_postcommit_launcher_restored_under_owned_transaction(self):self.exercise(held=True,missing_lock=True)

    def test_recovery_initializes_missing_reboot_lock(self):self.exercise(missing_lock=True)
    def test_checkpoint_expiry_keeps_owned_drain(self):self.exercise(failure='checkpoint-expiry')

    def test_running_retry_uses_exact_service_without_restart(self):
        events=self.exercise(existing=True,already_running=True)
        self.assertNotIn('start',events)

    def test_success_releases_new_drain_after_actual_active_checks(self):self.exercise()
    def test_success_releases_existing_owned_drain(self):self.exercise(existing=True)
    def test_start_failure_keeps_owned_drain(self):self.exercise(failure='start')
    def test_readiness_failure_keeps_owned_drain(self):self.exercise(failure='readiness')
    def test_late_postflight_failure_keeps_owned_drain(self):
        events=self.exercise(failure='final')
        self.assertNotIn('COMMITTED_REBOOT_RECOVERED',events)

class ManagedDropinTests(unittest.TestCase):
    def test_changed_dropin_bytes_or_mode_refused(self):
        for mode,raw in ((0o644,b'[Service]\nExecStartPre=/bin/false\n'),(0o666,runtime.BOOT_GUARD_BYTES)):
            with self.subTest(mode=mode),patch.object(runtime,'root_file_read',return_value=(dict(mode=mode),raw)):
                with self.assertRaises(ValueError):runtime.verify_boot_guard()
        with patch.object(runtime,'root_file_read',return_value=(dict(mode=0o644),runtime.BOOT_GUARD_BYTES)):
            self.assertEqual(runtime.verify_boot_guard()['mode'],0o644)

class JournalPublicationTests(unittest.TestCase):
    def reader(self,path):
        path=Path(path)
        if not path.exists():return None,b''
        raw=path.read_bytes();info=path.stat()
        return dict(sha256=hashlib.sha256(raw).hexdigest(),inode=info.st_ino,device=info.st_dev,uid=info.st_uid,gid=info.st_gid,mode=info.st_mode&0o777),raw

    def append(self,path,row):
        self.assertTrue(callable(getattr(runtime,'append_journal_record',None)), 'Crash-safe journal publication is missing')
        # Root custody is covered separately; this adapter retains real file IO.
        with patch.object(runtime,'root_file_read',side_effect=self.reader):
            runtime.append_journal_record(path,row)

    def test_complete_records_preserve_historical_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';before=b'{"stage":"VERIFIED"}\n';path.write_bytes(before)
            self.append(path,dict(stage='FINISH_START',observed_at=1))
            self.assertTrue(path.read_bytes().startswith(before))
            self.assertEqual(len(path.read_bytes().splitlines()),2)
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_failed_publication_preserves_complete_old_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';before=b'{"stage":"VERIFIED"}\n';path.write_bytes(before)
            with patch.object(runtime.os,'replace',side_effect=OSError('interrupted publication')):
                with self.assertRaises(OSError):self.append(path,dict(stage='FINISH_START'))
            self.assertEqual(path.read_bytes(),before);self.assertEqual(list(path.parent.iterdir()),[path])

    def test_malformed_prior_history_is_never_extended(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';before=b'{"stage":"VERIFIED"}\n{"stage":';path.write_bytes(before)
            with self.assertRaises(ValueError):self.append(path,dict(stage='FINISH_START'))
            self.assertEqual(path.read_bytes(),before)

    def test_journal_changed_during_guard_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';path.write_bytes(b'{"stage":"OLD"}\n')
            def replaced():
                other=path.parent/'foreign';other.write_bytes(b'{"stage":"FOREIGN"}\n');other.replace(path)
            with patch.object(runtime,'root_file_read',side_effect=self.reader):
                with self.assertRaises(ValueError):runtime.append_journal_record(path,dict(stage='NEW'),before_publish=replaced)
            self.assertEqual(path.read_bytes(),b'{"stage":"FOREIGN"}\n')

    def test_selected_history_is_not_readopted_after_foreign_change(self):
        self.assertIn('selected_history',inspect.signature(runtime.append_journal_record).parameters)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';path.write_bytes(b'{"stage":"AUTHORIZED"}\n');selected=self.reader(path)
            path.write_bytes(b'{"stage":"HOLD_EXPLICIT_RECOVERY"}\n')
            with patch.object(runtime,'root_file_read',side_effect=self.reader):
                with self.assertRaises(ValueError):runtime.append_journal_record(path,dict(stage='COMMIT'),selected_history=selected)
            self.assertEqual(path.read_bytes(),b'{"stage":"HOLD_EXPLICIT_RECOVERY"}\n')

    def test_selected_history_advances_only_through_verified_own_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';path.write_bytes(b'{"stage":"AUTHORIZED"}\n');selected=self.reader(path)
            with patch.object(runtime,'root_file_read',side_effect=self.reader):
                selected=runtime.append_journal_record(path,dict(stage='OWN_ONE'),selected_history=selected)
                selected=runtime.append_journal_record(path,dict(stage='OWN_TWO'),selected_history=selected)
                self.assertEqual(len(selected[1].splitlines()),3)
                other=path.parent/'foreign';other.write_bytes(b'{"stage":"FOREIGN"}\n');other.replace(path)
                with self.assertRaises(ValueError):runtime.append_journal_record(path,dict(stage='OWN_THREE'),selected_history=selected)
            self.assertEqual(path.read_bytes(),b'{"stage":"FOREIGN"}\n')

    def test_publication_reads_finish_before_authority_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';path.write_bytes(b'{"stage":"OLD"}\n')
            guarded=[False];published=[False];replace=os.replace
            def read(selected):
                if not published[0]:self.assertFalse(guarded[0])
                return self.reader(selected)
            def guard():guarded[0]=True
            def publish(source,target):replace(source,target);published[0]=True
            with patch.object(runtime,'root_file_read',side_effect=read),patch.object(runtime.os,'replace',side_effect=publish):
                runtime.append_journal_record(path,dict(stage='NEW'),before_publish=guard)
            self.assertTrue(guarded[0]);self.assertTrue(published[0])

    def test_ancestor_custody_change_refuses_journal_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';old=b'{"stage":"OLD"}\n';path.write_bytes(old)
            def guard():path.parent.chmod(0o777)
            try:
                with patch.object(runtime,'root_file_read',side_effect=self.reader):
                    with self.assertRaises(ValueError):runtime.append_journal_record(path,dict(stage='NEW'),before_publish=guard)
                self.assertEqual(path.read_bytes(),old)
            finally:path.parent.chmod(0o700)

    def test_journal_changed_during_freshness_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';path.write_bytes(b'{"stage":"OLD"}\n')
            def freshness():
                other=path.parent/'foreign';other.write_bytes(b'{"stage":"FOREIGN"}\n');other.replace(path)
            with patch.object(runtime,'root_file_read',side_effect=self.reader):
                with self.assertRaises(ValueError):runtime.append_journal_record(path,dict(stage='NEW'),freshness=freshness)
            self.assertEqual(path.read_bytes(),b'{"stage":"FOREIGN"}\n')

    def test_identical_foreign_inode_is_not_accepted_as_own_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'journal';path.write_bytes(b'{"stage":"OLD"}\n');sync=os.fsync;armed=[False]
            def replaced(fd):
                sync(fd)
                if path.exists() and b'NEW' in path.read_bytes() and not armed[0]:
                    other=path.parent/'foreign';other.write_bytes(path.read_bytes());other.chmod(0o600);other.replace(path);armed[0]=True
            with patch.object(runtime,'root_file_read',side_effect=self.reader),patch.object(runtime.os,'fsync',side_effect=replaced):
                with self.assertRaises(ValueError):runtime.append_journal_record(path,dict(stage='NEW'))
            self.assertTrue(armed[0]);self.assertIn(b'NEW',path.read_bytes())

class DrainPublicationTests(unittest.TestCase):
    def publish(self,path,callback=None):
        self.assertTrue(callable(getattr(runtime,'publish_owned_drain',None)), 'Atomic complete-token publication is missing')
        return runtime.publish_owned_drain(path,'owned',before_publish=callback)

    def test_publication_exposes_only_complete_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'drain';seen=[]
            self.assertTrue(callable(getattr(runtime,'rename_exclusive',None)), 'Exclusive atomic rename is missing')
            rename=runtime.rename_exclusive
            def observe(source,target,**kwargs):
                self.assertFalse(path.exists());self.assertEqual(Path(source).read_bytes(),b'owned');seen.append(1)
                self.assertEqual(Path(source).stat().st_nlink,1)
                return rename(source,target)
            with patch.object(runtime,'rename_exclusive',side_effect=observe):fd=self.publish(path)
            try:
                self.assertEqual(path.read_bytes(),b'owned');self.assertEqual(os.fstat(fd).st_ino,path.stat().st_ino)
                self.assertEqual(path.stat().st_nlink,1);self.assertEqual(path.stat().st_mode&0o777,0o600)
            finally:os.close(fd)
            self.assertEqual(seen,[1])

    def test_foreign_token_created_before_publication_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'drain'
            with self.assertRaises(FileExistsError):self.publish(path,lambda:path.write_text('foreign'))
            self.assertEqual(path.read_text(),'foreign');self.assertEqual(list(path.parent.iterdir()),[path])

    def test_expired_publication_leaves_no_public_or_staging_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'drain'
            def expired():raise ValueError('expired')
            with self.assertRaises(ValueError):self.publish(path,expired)
            self.assertEqual(list(path.parent.iterdir()),[])

class FinishCliTests(unittest.TestCase):
    def invoke(self,*args):
        return subprocess.run([sys.executable,'-I','-B',str(Path(runtime.__file__).resolve()),*args],capture_output=True,text=True,timeout=2)

    def test_finish_is_an_explicit_cli_action(self):
        result=self.invoke('--help')
        self.assertEqual(result.returncode,0)
        self.assertIn('--finish',result.stdout)
        self.assertIn('--client-launcher-sha256',result.stdout)
        self.assertIn('--finish-nonce',result.stdout)
        self.assertIn('--recover-finish',result.stdout)
        self.assertIn('--recover-committed',result.stdout)

    def test_apply_and_finish_are_mutually_exclusive(self):
        result=self.invoke('--plan','synthetic.json','--apply','--finish')
        self.assertEqual(result.returncode,2)
        self.assertIn('not allowed with argument',result.stderr)

class LauncherRestoreTests(unittest.TestCase):
    def setUp(self):
        custody = patch.object(runtime,'authenticate_ancestor_custody')
        custody.start();self.addCleanup(custody.stop)

    def state(self,path):
        info=path.stat()
        return dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),mode=info.st_mode&0o777,uid=info.st_uid,gid=info.st_gid,device=info.st_dev,inode=info.st_ino)

    def restore(self,path,expected):
        self.assertTrue(callable(getattr(runtime,'restore_launcher_execution',None)), 'Verified launcher restore is missing')
        return runtime.restore_launcher_execution(path,expected)

    def test_only_exact_held_launcher_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(0o644)
            expected=self.state(path);self.restore(path,expected)
            self.assertEqual(path.stat().st_mode&0o777,0o755)
            self.assertEqual(path.stat().st_ino,expected['inode'])
            self.assertEqual(path.read_bytes(),b'fixture')

    def test_changed_bytes_or_inode_remain_held(self):
        for changed in ('bytes','inode'):
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(0o644);expected=self.state(path)
                if changed=='bytes':path.write_bytes(b'tampered')
                else:
                    other=path.parent/'new';other.write_bytes(b'fixture');other.chmod(0o644);other.replace(path)
                with self.subTest(changed=changed),self.assertRaises(ValueError):self.restore(path,expected)
                self.assertEqual(path.stat().st_mode&0o777,0o644)

    def test_failure_close_refuses_replaced_launcher(self):
        self.assertIn('expected',inspect.signature(runtime.close_launcher_execution).parameters)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(0o755);expected=self.state(path)
            other=path.parent/'replacement';other.write_bytes(b'foreign');other.chmod(0o755);other.replace(path)
            with self.assertRaises(ValueError):runtime.close_launcher_execution(path,expected=expected)
            self.assertEqual(path.stat().st_mode&0o777,0o755)

    def test_failure_close_changes_only_selected_launcher(self):
        self.assertIn('expected',inspect.signature(runtime.close_launcher_execution).parameters)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(0o755);expected=self.state(path)
            runtime.close_launcher_execution(path,expected=expected)
            self.assertEqual(path.stat().st_mode&0o777,0o644)
            self.assertEqual(path.stat().st_ino,expected['inode'])

    def test_permission_fence_runs_after_descriptor_read(self):
        self.assertIn('before_change',inspect.signature(runtime.restore_launcher_execution).parameters)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(0o644);expected=self.state(path);read=os.read;clock=[0]
            def slow_read(*args):
                raw=read(*args);clock[0]=121;return raw
            def fence():
                if clock[0]>120:raise ValueError('expired after read')
            with patch.object(runtime.os,'read',side_effect=slow_read):
                with self.assertRaises(ValueError):runtime.restore_launcher_execution(path,expected,freshness=fence)
            self.assertEqual(path.stat().st_mode&0o777,0o644)

    def test_permission_read_detects_changed_custody(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(0o644);expected=self.state(path);read=os.read
            def mutate(*args):
                raw=read(*args);path.chmod(0o666);return raw
            with patch.object(runtime.os,'read',side_effect=mutate):
                with self.assertRaises(ValueError):runtime.restore_launcher_execution(path,expected)
            self.assertEqual(path.stat().st_mode&0o777,0o666)

    def test_selected_group_mismatch_never_grants_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(0o644);expected=self.state(path);expected['gid']+=1
            with self.assertRaises(ValueError):runtime.restore_launcher_execution(path,expected)
            self.assertEqual(path.stat().st_mode&0o777,0o644)

    def test_permission_ancestor_change_never_grants_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(0o644);expected=self.state(path)
            try:
                with self.assertRaises(ValueError):runtime.restore_launcher_execution(path,expected,before_change=lambda:path.parent.chmod(0o777))
                self.assertEqual(path.stat().st_mode&0o777,0o644)
            finally:path.parent.chmod(0o700)

    def test_permission_freshness_custody_change_is_preserved(self):
        for mode in (0o644,0o755):
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(mode);expected=self.state(path)
                def fence():path.chmod(0o400)
                with self.subTest(mode=mode),self.assertRaises(ValueError):
                    if mode==0o644:runtime.restore_launcher_execution(path,expected,freshness=fence)
                    else:runtime.close_launcher_execution(path,expected=expected,freshness=fence)
                self.assertEqual(path.stat().st_mode&0o777,0o400)

    def test_permission_callback_custody_change_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'launcher';path.write_bytes(b'fixture');path.chmod(0o644);expected=self.state(path)
            with self.assertRaises(ValueError):runtime.restore_launcher_execution(path,expected,before_change=lambda:path.chmod(0o666))
            self.assertEqual(path.stat().st_mode&0o777,0o666)

class InstalledHistoryTests(unittest.TestCase):
    def fixture(self):
        raw=b'bounded synthetic runtime\n';sha=hashlib.sha256(raw).hexdigest()
        assets={path:{'data':base64.b64encode(raw).decode(),'sha256':sha} for path in runtime.TARGETS}
        files={path:{'sha256':sha,'mode':0o644 if path==runtime.LAUNCHER_TARGET else mode} for path,mode in runtime.TARGETS.items()}
        original=dict(schema=1,nonce='a'*32,source_main='b'*40,vm_id='123',boot='0b99c4d2-ca5f-4471-bf84-ff4ab8dbefd3',image_id='sha256:'+'c'*64,config_sha256='d'*64,admission_sha256='e'*64,service_sha256='f'*64,service_pid=10,service_startticks=20,observed_at=1000.0,assets=assets,before={path:None for path in runtime.TARGETS})
        current=dict(original,service_pid=30,service_startticks=40,observed_at=2000.0,before=files)
        journal=[dict(stage='FILES_AND_SERVICE_VERIFIED',service_pid=30,service_startticks=40,files=files)]
        return original,current,journal,sha

    def verify(self,original,current,journal,pin):
        self.assertTrue(callable(getattr(runtime,'verify_installed_history',None)), 'Successful installation history verifier is missing')
        return runtime.verify_installed_history(current,original,journal,pin,2001.0)

    def test_historical_install_and_fresh_finish_are_distinct(self):
        original,current,journal,pin=self.fixture()
        self.assertEqual(self.verify(original,current,journal,pin),pin)
        self.assertEqual(original['observed_at'],1000.0)

    def test_failed_or_partial_install_refused(self):
        for stage in ('FILE_REPLACED','HOLD_EXPLICIT_RECOVERY','HOLD_BEFORE_DRAIN_RELEASE'):
            original,current,journal,pin=self.fixture();journal[-1]['stage']=stage
            with self.subTest(stage=stage),self.assertRaises(ValueError):self.verify(original,current,journal,pin)

    def test_different_install_identity_refused(self):
        for field,value in (('nonce','9'*32),('source_main','9'*40),('vm_id','999')):
            original,current,journal,pin=self.fixture();original[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.verify(original,current,journal,pin)

    def test_changed_service_or_file_after_install_refused(self):
        for changed in ('service','file'):
            original,current,journal,pin=self.fixture()
            if changed=='service':journal[-1]['service_pid']=99
            else:journal[-1]['files']={}
            with self.subTest(changed=changed),self.assertRaises(ValueError):self.verify(original,current,journal,pin)

    def test_expired_finish_refused(self):
        original,current,journal,pin=self.fixture();current['observed_at']=1500.0
        with self.assertRaises(ValueError):self.verify(original,current,journal,pin)

    def test_checked_recovery_accepts_interrupted_finish_without_rewriting_history(self):
        self.assertIn('recovery',inspect.signature(runtime.verify_installed_history).parameters)
        original,current,journal,pin=self.fixture();journal.append(dict(stage='FINISH_START',client_launcher_sha256=pin));journal.append(dict(stage='HOLD_FINISH_RECOVERY',execution_held=True))
        prior=list(journal)
        self.assertEqual(runtime.verify_installed_history(current,original,journal,pin,2001.0,recovery=True),pin)
        self.assertEqual(journal,prior)

    def test_recovery_refuses_failed_apply_and_unknown_suffix(self):
        self.assertIn('recovery',inspect.signature(runtime.verify_installed_history).parameters)
        for stage in ('HOLD_BEFORE_DRAIN_RELEASE','HOLD_EXPLICIT_RECOVERY','UNKNOWN'):
            original,current,journal,pin=self.fixture();journal.append(dict(stage=stage))
            with self.subTest(stage=stage),self.assertRaises(ValueError):runtime.verify_installed_history(current,original,journal,pin,2001.0,recovery=True)

    def test_recovery_recognizes_exact_opened_launcher_inode(self):
        self.assertIn('recovery',inspect.signature(runtime.verify_installed_history).parameters)
        original,current,journal,pin=self.fixture()
        current['before']={k:dict(v) for k,v in current['before'].items()};current['before'][runtime.LAUNCHER_TARGET]['mode']=0o755
        journal.append(dict(stage='FINISH_READY_TO_RELEASE'))
        self.assertEqual(runtime.verify_installed_history(current,original,journal,pin,2001.0,recovery=True),pin)

    def test_recovered_held_record_can_be_finished_normally(self):
        self.assertIn('recovery',inspect.signature(runtime.verify_installed_history).parameters)
        original,current,journal,pin=self.fixture();journal[-1]['stage']='FINISH_RECOVERED_HELD'
        self.assertEqual(self.verify(original,current,journal,pin),pin)

    def reboot_fixture(self):
        original,current,journal,pin=self.fixture()
        opened={k:dict(v) for k,v in current['before'].items()};opened[runtime.LAUNCHER_TARGET]['mode']=0o755
        journal.append(dict(stage='FINISH_RELEASE_COMMITTED',files=opened,client_launcher_sha256=pin,qualification_ready=False))
        observation={k:v for k,v in current.items() if k not in ('service_pid','service_startticks')}
        observation.update(schema=2,boot='5333c11d-fbbd-42e5-9638-cff800bd7e19',before=opened,
            service_state=dict(LoadState='loaded',ActiveState='inactive',SubState='dead',MainPID='0',ControlPID='0'))
        return original,journal,observation,pin

    def reboot_verify(self,original,journal,observation,pin):
        self.assertTrue(callable(getattr(runtime,'verify_committed_reboot',None)), 'Committed reboot verifier is missing')
        return runtime.verify_committed_reboot(original,journal,observation,pin,2001.0)

    def test_reboot_recovery_requires_durable_committed_release(self):
        original,journal,observation,pin=self.reboot_fixture()
        self.assertEqual(self.reboot_verify(original,journal,observation,pin),pin)
        journal[-1]['stage']='FINISH_READY_TO_RELEASE'
        with self.assertRaises(ValueError):self.reboot_verify(original,journal,observation,pin)

    def test_reboot_recovery_refuses_changed_assets_host_or_client(self):
        for field,value in (('vm_id','999'),('config_sha256','0'*64),('source_main','0'*40)):
            original,journal,observation,pin=self.reboot_fixture();observation[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.reboot_verify(original,journal,observation,pin)
        original,journal,observation,pin=self.reboot_fixture()
        with self.assertRaises(ValueError):self.reboot_verify(original,journal,observation,'0'*64)

    def test_reboot_recovery_refuses_running_or_foreign_service(self):
        original,journal,observation,pin=self.reboot_fixture();observation['service_state']['MainPID']='99'
        with self.assertRaises(ValueError):self.reboot_verify(original,journal,observation,pin)

    def test_failed_poststart_can_be_observed_for_committed_recovery(self):
        original,journal,observation,pin=self.reboot_fixture()
        observation['service_state']=dict(LoadState='loaded',ActiveState='active',SubState='running',MainPID='50',ControlPID='0',StartTicks=60)
        try:result=self.reboot_verify(original,journal,observation,pin)
        except ValueError as error:self.fail('Committed recovery cannot resume after service started: '+str(error))
        self.assertEqual(result,pin)

    def test_committed_checkpoint_can_resume_final_drain_release(self):
        original,journal,observation,pin=self.reboot_fixture()
        observation['service_state']=dict(LoadState='loaded',ActiveState='active',SubState='running',MainPID='50',ControlPID='0',StartTicks=60)
        journal.append(dict(stage='COMMITTED_REBOOT_RECOVERED',files=observation['before'],client_launcher_sha256=pin,qualification_ready=False,boot=observation['boot'],service_pid=50,service_startticks=60))
        try:result=self.reboot_verify(original,journal,observation,pin)
        except ValueError as error:self.fail('Durable checkpoint cannot resume drain release: '+str(error))
        self.assertEqual(result,pin)

    def test_committed_checkpoint_survives_another_reboot(self):
        original,journal,observation,pin=self.reboot_fixture()
        journal.append(dict(stage='COMMITTED_REBOOT_RECOVERED',files=observation['before'],client_launcher_sha256=pin,qualification_ready=False,boot=observation['boot'],service_pid=50,service_startticks=60))
        observation['boot']='11111111-2222-3333-4444-555555555555'
        self.assertEqual(self.reboot_verify(original,journal,observation,pin),pin)

    def test_committed_reboot_accepts_held_postcommit_failure(self):
        original,journal,observation,pin=self.reboot_fixture()
        observation['before']={k:dict(v) for k,v in observation['before'].items()};observation['before'][runtime.LAUNCHER_TARGET]['mode']=0o644
        journal.append(dict(stage='HOLD_FINISH_RECOVERY',execution_held=True))
        try:result=self.reboot_verify(original,journal,observation,pin)
        except ValueError as error:self.fail('Held committed release lost recovery authority: '+str(error))
        self.assertEqual(result,pin)

    def test_committed_permission_intents_allow_either_crash_side(self):
        for stage in ('HOLD_FINISH_INTENT','COMMITTED_LAUNCHER_RESTORE_INTENT','FINISH_RECOVERY_START'):
            for mode in (0o644,0o755):
                original,journal,observation,pin=self.reboot_fixture()
                observation['before']={k:dict(v) for k,v in observation['before'].items()};observation['before'][runtime.LAUNCHER_TARGET]['mode']=mode
                journal.append(dict(stage=stage,launcher=observation['before'][runtime.LAUNCHER_TARGET]))
                with self.subTest(stage=stage,mode=mode):
                    try:result=self.reboot_verify(original,journal,observation,pin)
                    except ValueError as error:self.fail('Intent lost retry authority: '+str(error))
                    self.assertEqual(result,pin)

    def test_completed_rehold_preserves_committed_reboot_authority(self):
        original,journal,observation,pin=self.reboot_fixture()
        observation['before']={k:dict(v) for k,v in observation['before'].items()};observation['before'][runtime.LAUNCHER_TARGET]['mode']=0o644
        journal.append(dict(stage='FINISH_RECOVERED_HELD',files=observation['before'],service_pid=30,service_startticks=40))
        try:result=self.reboot_verify(original,journal,observation,pin)
        except ValueError as error:self.fail('Verified rehold lost durable authority: '+str(error))
        self.assertEqual(result,pin)

    def test_release_decision_supports_before_and_after_permission_change(self):
        for mode in (0o644,0o755):
            original,journal,observation,pin=self.reboot_fixture()
            held={k:dict(v) for k,v in observation['before'].items()};held[runtime.LAUNCHER_TARGET]['mode']=0o644
            journal[-1]['observed_files']=held
            observation['before']={k:dict(v) for k,v in observation['before'].items()};observation['before'][runtime.LAUNCHER_TARGET]['mode']=mode
            with self.subTest(mode=mode):self.assertEqual(self.reboot_verify(original,journal,observation,pin),pin)

    def test_reheld_finish_retry_preserves_committed_reboot_authority(self):
        for stage in ('FINISH_START','FINISH_READY_TO_RELEASE'):
            original,journal,observation,pin=self.reboot_fixture()
            held={k:dict(v) for k,v in observation['before'].items()};held[runtime.LAUNCHER_TARGET]['mode']=0o644
            journal.append(dict(stage='FINISH_RECOVERED_HELD',files=held,service_pid=30,service_startticks=40))
            journal.append(dict(stage=stage,files=held,client_launcher_sha256=pin))
            observation['before']=held
            with self.subTest(stage=stage):
                try:result=self.reboot_verify(original,journal,observation,pin)
                except ValueError as error:self.fail('Writer retry stage lost authority: '+str(error))
                self.assertEqual(result,pin)

    def test_checkpoint_retry_rebinds_actual_restarted_service(self):
        original,journal,observation,pin=self.reboot_fixture()
        journal.append(dict(stage='COMMITTED_REBOOT_RECOVERED',files=observation['before'],client_launcher_sha256=pin,qualification_ready=False,boot=observation['boot'],service_pid=50,service_startticks=60))
        observation['service_state']=dict(LoadState='loaded',ActiveState='active',SubState='running',MainPID='51',ControlPID='0',StartTicks=61)
        try:result=self.reboot_verify(original,journal,observation,pin)
        except ValueError as error:self.fail('Fresh verified restart lost committed retry: '+str(error))
        self.assertEqual(result,pin)

class OwnedFinishTests(unittest.TestCase):
    def run_finish(self,lock,drain,action,check=lambda:None):
        self.assertTrue(callable(getattr(runtime,'finish_owned_drain',None)), 'Owned finish transaction is missing')
        return runtime.finish_owned_drain(lock,drain,'owned',check,action)

    def fixture(self,directory):
        lock=Path(directory).resolve()/'lock';lock.touch()
        drain=lock.parent/'drain';drain.write_text('owned');drain.chmod(0o600)
        return lock,drain

    def test_success_keeps_drain_during_action_then_removes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory)
            def action():
                self.assertEqual(drain.read_text(),'owned')
                return 'verified'
            self.assertEqual(self.run_finish(lock,drain,action),'verified')
            self.assertFalse(drain.exists())

    def test_busy_build_lock_preserves_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory)
            with lock.open('r+') as holder:
                fcntl.flock(holder,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):self.run_finish(lock,drain,lambda:self.fail('ran'))
            self.assertEqual(drain.read_text(),'owned')

    def test_foreign_drain_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory);drain.write_text('foreign')
            with self.assertRaises(ValueError):self.run_finish(lock,drain,lambda:self.fail('ran'))
            self.assertEqual(drain.read_text(),'foreign')

    def test_failed_reopen_keeps_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory)
            def action():raise ValueError('failed proof')
            with self.assertRaises(ValueError):self.run_finish(lock,drain,action)
            self.assertEqual(drain.read_text(),'owned')

    def test_replaced_drain_is_never_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory)
            def action():
                drain.unlink();drain.write_text('foreign')
            with self.assertRaises(ValueError):self.run_finish(lock,drain,action)
            self.assertEqual(drain.read_text(),'foreign')

    def test_final_check_failure_keeps_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory);checks=[]
            def check():
                checks.append(1)
                if len(checks)>1:raise ValueError('state changed')
            with self.assertRaises(ValueError):self.run_finish(lock,drain,lambda:None,check)
            self.assertEqual(drain.read_text(),'owned')

    def test_lock_becoming_writable_during_finish_keeps_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory)
            def action():lock.chmod(0o666)
            with self.assertRaises(ValueError):self.run_finish(lock,drain,action)
            self.assertEqual(drain.read_text(),'owned')

    def test_failure_callback_runs_under_same_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory);seen=[]
            def action():raise ValueError('partial finish')
            def hold(error,guard=None):
                self.assertTrue(callable(guard), 'Failure callback needs canonical guard through slow steps')
                guard()
                with lock.open('r+') as other:
                    with self.assertRaises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(drain.read_text(),'owned');seen.append(type(error).__name__)
            self.assertIn('on_failure',inspect.signature(runtime.finish_owned_drain).parameters)
            with self.assertRaises(ValueError):runtime.finish_owned_drain(lock,drain,'owned',lambda:None,action,on_failure=hold)
            self.assertEqual(seen,['ValueError'])

    def test_release_commit_runs_after_final_check_while_drained(self):
        self.assertIn('commit',inspect.signature(runtime.finish_owned_drain).parameters)
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory);events=[]
            def check():events.append('check')
            def action():events.append('action');return 1
            def commit():
                self.assertEqual(drain.read_text(),'owned');events.append('commit')
            self.assertEqual(runtime.finish_owned_drain(lock,drain,'owned',check,action,commit=commit),1)
            self.assertEqual(events,['check','action','check','commit'])
            self.assertFalse(drain.exists())

    def test_failed_final_check_never_commits_release(self):
        self.assertIn('commit',inspect.signature(runtime.finish_owned_drain).parameters)
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory);events=[]
            def check():
                events.append('check')
                if len(events)>1:raise ValueError('late failure')
            with self.assertRaises(ValueError):runtime.finish_owned_drain(lock,drain,'owned',check,lambda:None,commit=lambda:self.fail('committed'))
            self.assertEqual(drain.read_text(),'owned')

    def test_recovery_transaction_retains_owned_drain(self):
        self.assertIn('release',inspect.signature(runtime.finish_owned_drain).parameters)
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory)
            self.assertEqual(runtime.finish_owned_drain(lock,drain,'owned',lambda:None,lambda:3,release=False),3)
            self.assertEqual(drain.read_text(),'owned')

    def test_lost_canonical_lock_refuses_failure_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory);called=[]
            def action():
                replacement=lock.parent/'replacement';replacement.touch();replacement.replace(lock)
            with self.assertRaises(ValueError):runtime.finish_owned_drain(lock,drain,'owned',lambda:None,action,on_failure=lambda error,guard:called.append('mutated'))
            self.assertEqual(called,[]);self.assertEqual(drain.read_text(),'owned')

    def test_new_drain_transaction_refuses_replaced_lock_release(self):
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory).resolve()/'lock';lock.touch();drain=lock.parent/'drain'
            def action():
                other=lock.parent/'other';other.touch();other.replace(lock);return 1
            with self.assertRaises(ValueError):runtime.drained_update(lock,drain,'owned',action)
            self.assertEqual(drain.read_text(),'owned')

    def test_token_read_replacement_never_deletes_foreign_drain(self):
        for transaction in ('existing','new'):
            with tempfile.TemporaryDirectory() as directory:
                lock=Path(directory).resolve()/'lock';lock.touch();drain=lock.parent/'drain';read=os.read;armed=[0]
                if transaction=='existing':drain.write_text('owned');drain.chmod(0o600)
                def action():armed[0]=3 if transaction=='existing' else 2;return 1
                def swapped(fd,size):
                    raw=read(fd,size)
                    if armed[0] and size==129:
                        armed[0]-=1
                        if armed[0]==0:
                            replacement=lock.parent/'foreign';replacement.write_text('foreign');replacement.chmod(0o600);replacement.replace(drain)
                    return raw
                with self.subTest(transaction=transaction),patch.object(runtime.os,'read',side_effect=swapped):
                    with self.assertRaises(ValueError):
                        if transaction=='existing':runtime.finish_owned_drain(lock,drain,'owned',lambda:None,action)
                        else:runtime.drained_update(lock,drain,'owned',action)
                self.assertTrue(drain.exists(),'Foreign drain was deleted')
                if drain.exists():self.assertEqual(drain.read_text(),'foreign')

    def test_final_token_read_expiry_keeps_owned_drain(self):
        self.assertIn('release_fence',inspect.signature(runtime.finish_owned_drain).parameters)
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory);read=os.read;late=[False]
            def delayed(fd,size):raw=read(fd,size);late[0]=True;return raw
            def fresh():
                if late[0]:raise ValueError('expired during final read')
            with patch.object(runtime.os,'read',side_effect=delayed):
                with self.assertRaises(ValueError):runtime.finish_owned_drain(lock,drain,'owned',lambda:None,lambda:None,release_fence=fresh)
            self.assertEqual(drain.read_text(),'owned')

    def test_postunlink_fsync_failure_never_claims_protective_hold(self):
        self.assertTrue(isinstance(getattr(runtime,'CommittedReleaseError',None),type), 'Explicit already-unlinked outcome is missing')
        with tempfile.TemporaryDirectory() as directory:
            lock,drain=self.fixture(directory);sync=os.fsync;calls=[]
            def failed(fd):
                if not drain.exists():raise OSError('directory persistence unconfirmed')
                sync(fd)
            with patch.object(runtime.os,'fsync',side_effect=failed):
                with self.assertRaises(runtime.CommittedReleaseError) as caught:
                    runtime.finish_owned_drain(lock,drain,'owned',lambda:None,lambda:dict(qualification_ready=False),on_failure=lambda error,guard:calls.append('held'))
            self.assertFalse(drain.exists());self.assertEqual(calls,[])
            self.assertIs(caught.exception.result['drain_unlinked'],True)
            self.assertIs(caught.exception.result['directory_sync_confirmed'],False)

    def test_new_drain_postunlink_failure_is_explicit_committed_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory).resolve()/'lock';lock.touch();drain=lock.parent/'drain';sync=os.fsync;armed=[False]
            def action():armed[0]=True;return dict(qualification_ready=False)
            def failed(fd):
                if armed[0] and not drain.exists():raise OSError('sync failed after unlink')
                sync(fd)
            with patch.object(runtime.os,'fsync',side_effect=failed):
                with self.assertRaises(runtime.CommittedReleaseError) as caught:runtime.drained_update(lock,drain,'owned',action)
            self.assertFalse(drain.exists());self.assertIs(caught.exception.result['protected_hold'],False)

    def test_drain_changed_during_trailing_authority_check_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            _,drain=self.fixture(directory);fd=os.open(drain,os.O_RDONLY);calls=[]
            def guard():
                calls.append(1)
                if len(calls)==2:
                    other=drain.parent/'foreign';other.write_text('foreign');other.chmod(0o600);other.replace(drain)
            try:
                with self.assertRaises(ValueError):runtime.verify_owned_drain(fd,drain,'owned',guard)
            finally:os.close(fd)
            self.assertEqual(drain.read_text(),'foreign')

    def test_expired_publication_fence_never_creates_drain(self):
        self.assertIn('publication_fence',inspect.signature(runtime.drained_update).parameters)
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory).resolve()/'lock';lock.touch();drain=lock.parent/'drain'
            def expired():raise ValueError('expired after canonical lookup')
            with self.assertRaises(ValueError):runtime.drained_update(lock,drain,'owned',lambda:self.fail('action ran'),publication_fence=expired)
            self.assertFalse(drain.exists());self.assertEqual(list(lock.parent.iterdir()),[lock])

class InitialAncestorCustodyTests(unittest.TestCase):
    def test_directory_descriptor_closes_if_initial_stat_fails(self):
        opened=[];real_open=os.open;real_stat=os.fstat
        def capture(*args):
            fd=real_open(*args);opened.append(fd);return fd
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'marker'
            with patch.object(runtime.os,'open',side_effect=capture),patch.object(runtime.os,'fstat',side_effect=OSError('failed metadata')):
                with self.assertRaises(OSError):
                    with runtime.retained_ancestor_selection(path):self.fail('failed metadata accepted')
            self.assertEqual(len(opened),1)
            with self.assertRaises(OSError):real_stat(opened[0])

    def test_only_root_nonwritable_directory_is_trusted(self):
        from types import SimpleNamespace
        import stat
        for uid, mode, allowed in ((0,stat.S_IFDIR|0o755,True),(1,stat.S_IFDIR|0o755,False),(0,stat.S_IFDIR|0o777,False),(0,stat.S_IFREG|0o644,False)):
            with self.subTest(uid=uid,mode=mode):
                info=SimpleNamespace(st_uid=uid,st_mode=mode)
                if allowed:runtime.authenticate_ancestor_custody(info)
                else:
                    with self.assertRaises(ValueError):runtime.authenticate_ancestor_custody(info)

    def test_token_read_directory_change_retains_owned_drain(self):
        for existing in (True,False):
            with tempfile.TemporaryDirectory() as directory:
                lock=Path(directory).resolve()/'lock';lock.touch();drain=lock.parent/'drain'
                if existing:drain.write_text('owned');drain.chmod(0o600)
                read=os.read;armed=[False]
                def action():armed[0]=True;return 1
                def changed(fd,size):
                    raw=read(fd,size)
                    if armed[0] and size==129:lock.parent.chmod(0o777)
                    return raw
                try:
                    with patch.object(runtime.os,'read',side_effect=changed),self.assertRaises(ValueError):
                        if existing:runtime.finish_owned_drain(lock,drain,'owned',lambda:None,action)
                        else:runtime.drained_update(lock,drain,'owned',action)
                    self.assertEqual(drain.read_text(),'owned')
                finally:lock.parent.chmod(0o700)

class PendingMarkerIdentityTests(unittest.TestCase):
    def test_original_marker_remains_valid_across_reboots(self):
        original,_,observation,_=InstalledHistoryTests().reboot_fixture()
        marker=dict(nonce=original['nonce'],boot=original['boot'],source_main=original['source_main'])
        for boot in (observation['boot'],'11111111-2222-3333-4444-555555555555'):
            self.assertEqual(runtime.validate_pending_marker(marker,boot,committed_original=original),original['nonce'])
            with self.assertRaises(ValueError):runtime.validate_pending_marker(marker,boot)

    def test_foreign_recovery_marker_is_refused(self):
        original,_,observation,_=InstalledHistoryTests().reboot_fixture()
        marker=dict(nonce=original['nonce'],boot=original['boot'],source_main=original['source_main'])
        for field in marker:
            altered=dict(marker);altered[field]='foreign'
            with self.subTest(field=field),self.assertRaises(ValueError):runtime.validate_pending_marker(altered,observation['boot'],committed_original=original)


class FinalDrainCustodyTests(unittest.TestCase):
    def test_release_fence_replacement_is_preserved(self):
        for existing in (True,False):
            for changed in ('lock','drain'):
                with tempfile.TemporaryDirectory() as directory:
                    lock=Path(directory).resolve()/'lock';lock.touch();drain=lock.parent/'drain'
                    if existing:drain.write_text('owned');drain.chmod(0o600)
                    target=lock if changed=='lock' else drain
                    def fence():
                        other=lock.parent/'foreign';other.write_text('foreign');other.chmod(0o600);other.replace(target)
                    with self.subTest(existing=existing,changed=changed),self.assertRaises(ValueError):
                        if existing:runtime.finish_owned_drain(lock,drain,'owned',lambda:None,lambda:1,release_fence=fence)
                        else:runtime.drained_update(lock,drain,'owned',lambda:1,release_fence=fence)
                    self.assertEqual(target.read_text(),'foreign');self.assertTrue(drain.exists())

    def test_final_release_verification_does_not_reread_token(self):
        for existing in (True,False):
            with tempfile.TemporaryDirectory() as directory:
                lock=Path(directory).resolve()/'lock';lock.touch();drain=lock.parent/'drain'
                if existing:drain.write_text('owned');drain.chmod(0o600)
                read=os.read;fenced=[False]
                def checked_read(*args):
                    self.assertFalse(fenced[0], 'Blocking token read after final freshness fence')
                    return read(*args)
                def fence():fenced[0]=True
                with patch.object(runtime.os,'read',side_effect=checked_read):
                    if existing:result=runtime.finish_owned_drain(lock,drain,'owned',lambda:None,lambda:1,release_fence=fence)
                    else:result=runtime.drained_update(lock,drain,'owned',lambda:1,release_fence=fence)
                self.assertEqual(result,1);self.assertFalse(drain.exists())

    def test_publication_fence_replacement_creates_no_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory).resolve()/'lock';lock.touch();drain=lock.parent/'drain'
            def fence():
                other=lock.parent/'foreign';other.write_text('foreign');other.replace(lock)
            with self.assertRaises(ValueError):runtime.drained_update(lock,drain,'owned',lambda:self.fail('action ran'),publication_fence=fence)
            self.assertFalse(drain.exists());self.assertEqual(lock.read_text(),'foreign')


class SharedFinalMetadataGuardTests(unittest.TestCase):
    def test_atomic_stage_changed_during_callback_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'target';path.write_bytes(b'old')
            def changed():next(path.parent.glob('.runtime-*')).write_bytes(b'foreign')
            with self.assertRaises(ValueError):runtime.atomic_write(path,b'new',0o600,before_replace=changed)
            self.assertEqual(path.read_bytes(),b'old')
            self.assertEqual(next(path.parent.glob('.runtime-*')).read_bytes(),b'foreign')

    def test_drain_stage_changed_during_callback_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'drain'
            def changed():next(path.parent.glob('.drain-*')).write_bytes(b'foreign')
            with self.assertRaises(ValueError):runtime.publish_owned_drain(path,'owned',before_publish=changed)
            self.assertFalse(path.exists());self.assertEqual(next(path.parent.glob('.drain-*')).read_bytes(),b'foreign')

    def test_service_binding_replacement_is_refused_by_production_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();mapping={};plan={}
            for name,field in (('/etc/oxidex-remote-build.json','config_sha256'),('/etc/gha-admission.json','admission_sha256'),('/etc/systemd/system/gha-admission.service','service_sha256')):
                path=root/field;path.write_bytes(field.encode());mapping[name]=path;plan[field]=hashlib.sha256(path.read_bytes()).hexdigest()
            def state(name):
                path=mapping.get(str(name),Path(name));info=path.stat()
                return dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),mode=info.st_mode&0o777,uid=info.st_uid,gid=info.st_gid,device=info.st_dev,inode=info.st_ino)
            with patch.object(runtime,'Path',side_effect=lambda value:mapping.get(str(value),Path(value))),patch.object(runtime,'root_file_state',side_effect=state):
                with runtime.selected_service_bindings(plan) as guard:
                    replacement=root/'foreign';replacement.write_bytes(b'foreign');replacement.replace(mapping['/etc/systemd/system/gha-admission.service'])
                    with self.assertRaises(ValueError):guard()


class FinalAuthorityAfterFreshnessTests(unittest.TestCase):
    def test_replaced_mutex_after_freshness_prevents_all_selected_effects(self):
        for kind in ('journal','marker','launcher'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();lock=root/'lock';lock.touch();path=root/'selected'
                raw=b'{"stage":"OLD"}\n' if kind=='journal' else b'owned'
                path.write_bytes(raw);path.chmod(0o644)
                with lock.open('r+b') as held:
                    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    def guard():
                        if lock.stat().st_ino != os.fstat(held.fileno()).st_ino:raise ValueError('canonical mutex replaced')
                    def fresh():
                        other=root/'foreign';other.write_bytes(b'foreign');other.replace(lock)
                    with self.subTest(kind=kind),self.assertRaises(ValueError):
                        if kind=='journal':
                            with patch.object(runtime,'root_file_read',side_effect=JournalPublicationTests().reader):runtime.append_journal_record(path,dict(stage='NEW'),before_publish=guard,freshness=fresh)
                        elif kind=='marker':
                            expected=LauncherRestoreTests().state(path)
                            with patch.object(runtime,'root_file_state',side_effect=lambda value:LauncherRestoreTests().state(Path(value))):runtime.unlink_selected_marker(path,expected,guard,fresh)
                        else:
                            with patch.object(runtime,'authenticate_ancestor_custody'):runtime.restore_launcher_execution(path,LauncherRestoreTests().state(path),before_change=guard,freshness=fresh)
                    self.assertEqual(path.read_bytes(),raw);self.assertEqual(path.stat().st_mode&0o777,0o644)


if __name__ == '__main__':
    unittest.main()
