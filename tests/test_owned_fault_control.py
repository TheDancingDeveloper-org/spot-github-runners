"""Source-only exact actor fault API checks; no host signal is sent."""
import importlib.util
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from lib.qualification_trust import control_fault as fault

HOST=('4381893288159455056','0b99c4d2-ca5f-4471-bf84-ff4ab8dbefd3')
PROJECT='qualification-'+'a'*12+'-'+'b'*32
IDENTITY='c'*32
NONCE='d'*32
ACTOR={'pid':12345,'start':67890}
BASE=dict(schema=1,vm_id=HOST[0],boot=HOST[1],installation_nonce=NONCE,
          project=PROJECT,reservation=IDENTITY,actor_pid=ACTOR['pid'],
          actor_start_ticks=ACTOR['start'],cgroup='/system.slice/oxidex-reservation-'+IDENTITY+'-4.service',
          enrollment_sha256='e'*64)


class OwnedFaultTests(unittest.TestCase):
    def test_interrupted_signal_resumes_same_actor_with_fresh_scope(self):
        record={'reservation_id':IDENTITY,'host':list(HOST),'state':'SOURCE_VERIFIED',
                'active':{'ordinal':4,'command':'task20-execute','actor':ACTOR}}
        store=Mock();store.locked.return_value=nullcontext();store.read.return_value=record
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            with patch.object(fault.custody,'Store',return_value=store), \
                 patch.object(fault.custody,'host_identity',return_value=HOST), \
                 patch.object(fault,'snapshot',return_value=(BASE,'digest')), \
                 patch.object(fault.engine,'dossier',return_value=path), \
                 patch.object(fault,'root_bytes',side_effect=lambda p,*a,**k:Path(p).read_bytes()), \
                 patch.object(fault,'unit_identity',return_value=BASE['cgroup']), \
                 patch.object(fault.os,'pidfd_open',side_effect=lambda *a:os.open('/dev/null',os.O_RDONLY),create=True), \
                 patch.object(fault.signal,'pidfd_send_signal',side_effect=[OSError('interrupted before signal'),None],create=True):
                with self.assertRaisesRegex(OSError,'interrupted before signal'):
                    fault.interrupt_owned(self.scope())
                self.assertEqual(record['state'],'HELD')
                intent=(path/'interrupt-owned-4.json').read_bytes()
                original=json.loads(intent)['scope']
                later=original['observed_at']+121
                fresh=dict(BASE,observed_at=later,deadline=later+120)
                failure=None;result=None
                with patch.object(fault.time,'time',return_value=later):
                    try:result=fault.interrupt_owned(fresh)
                    except ValueError as error:failure=error
                self.assertIsNone(failure,'Durable interruption must be resumable for the same verified actor')
                self.assertEqual(result['status'],'INTERRUPTION_REQUESTED_HELD')
                self.assertEqual((path/'interrupt-owned-4.json').read_bytes(),intent)
                self.assertEqual(record['state'],'HELD')

    def test_durable_root_read_syncs_file_and_parent_and_propagates_failure(self):
        raw=b'{}';path=Path('/trusted/intent')
        info=SimpleNamespace(st_mode=stat.S_IFREG|0o400,st_uid=0,st_nlink=1,
            st_size=len(raw),st_dev=1,st_ino=2,st_mtime_ns=3,st_ctime_ns=4)
        directory=SimpleNamespace(st_mode=stat.S_IFDIR|0o700,st_uid=0)
        for failed_fd in (None,11,12):
            with self.subTest(failed_fd=failed_fd):
                synced=[]
                def sync(fd):
                    synced.append(fd)
                    if fd==failed_fd:raise OSError('sync failed')
                with patch.object(Path,'lstat',lambda p:info if p==path else directory), \
                     patch.object(fault.os,'open',side_effect=[11,12]), \
                     patch.object(fault.os,'fstat',return_value=info), \
                     patch.object(fault.os,'read',return_value=raw), \
                     patch.object(fault.os,'fsync',side_effect=sync), \
                     patch.object(fault.os,'close') as close:
                    if failed_fd is None:
                        self.assertEqual(fault.root_bytes(path,mode=0o400,durable=True),raw)
                    else:
                        with self.assertRaisesRegex(OSError,'sync failed'):
                            fault.root_bytes(path,mode=0o400,durable=True)
                self.assertEqual(synced,[11] if failed_fd==11 else [11,12])
                self.assertEqual([c.args[0] for c in close.call_args_list],
                                 [11] if failed_fd==11 else [12,11])

    def test_retained_retry_syncs_intent_and_republishes_hold_before_signal(self):
        for state in ('SOURCE_VERIFIED','HELD'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as folder:
                scope=self.scope();path=Path(folder);events=[]
                record={'reservation_id':IDENTITY,'host':list(HOST),'state':state,
                        'active':{'ordinal':4,'command':'task20-execute','actor':ACTOR}}
                intent=dict(schema=1,scope=scope,scope_sha256=fault.custody.digest(scope),
                            actor=ACTOR,host=list(HOST),reservation_id=IDENTITY,
                            ordinal=4,action='PIDFD_SIGKILL')
                raw=fault.custody.canonical(intent)
                (path/'interrupt-owned-4.json').write_bytes(raw)
                store=Mock();store.locked.return_value=nullcontext();store.read.return_value=record
                store.write.side_effect=lambda r:events.append('hold-durable')
                def read(*args,**kwargs):
                    if kwargs.get('durable'):events.append('intent-durable')
                    return raw
                def signal_after_barriers(*args):
                    self.assertEqual(events,['intent-durable','hold-durable'])
                with patch.object(fault.custody,'Store',return_value=store), \
                     patch.object(fault.custody,'host_identity',return_value=HOST), \
                     patch.object(fault,'snapshot',return_value=(BASE,'digest')), \
                     patch.object(fault.engine,'dossier',return_value=path), \
                     patch.object(fault,'root_bytes',side_effect=read), \
                     patch.object(fault,'unit_identity',return_value=BASE['cgroup']), \
                     patch.object(fault.os,'pidfd_open',side_effect=lambda *a:os.open('/dev/null',os.O_RDONLY),create=True), \
                     patch.object(fault.signal,'pidfd_send_signal',side_effect=signal_after_barriers,create=True):
                    fault.interrupt_owned(scope)
                self.assertEqual((path/'interrupt-owned-4.json').read_bytes(),raw)

    def test_held_resume_requires_matching_durable_intent_before_pidfd(self):
        record={'reservation_id':IDENTITY,'host':list(HOST),'state':'HELD',
                'active':{'ordinal':4,'command':'task20-execute','actor':ACTOR}}
        scope=self.scope()
        valid=dict(schema=1,scope=scope,scope_sha256=fault.custody.digest(scope),
                   actor=ACTOR,host=list(HOST),reservation_id=IDENTITY,ordinal=4,action='PIDFD_SIGKILL')
        changed=copy.deepcopy(valid)
        changed['scope']['installation_nonce']='f'*32
        changed['scope_sha256']=fault.custody.digest(changed['scope'])
        cases=[None,{},dict(valid,actor={'pid':12345,'start':67891}),
               dict(valid,reservation_id='f'*32),dict(valid,action='OTHER'),changed]
        for previous in cases:
            with self.subTest(previous=previous),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)
                if previous is not None:fault.engine.write(path/'interrupt-owned-4.json',previous)
                store=Mock();store.locked.return_value=nullcontext();store.read.return_value=record
                with patch.object(fault.custody,'Store',return_value=store), \
                     patch.object(fault.custody,'host_identity',return_value=HOST), \
                     patch.object(fault,'snapshot',return_value=(BASE,'digest')), \
                     patch.object(fault.engine,'dossier',return_value=path), \
                     patch.object(fault,'root_bytes',side_effect=lambda p,*a,**k:Path(p).read_bytes()), \
                     patch.object(fault.os,'pidfd_open',create=True) as opened, \
                     patch.object(fault.signal,'pidfd_send_signal',create=True) as send:
                    with self.assertRaisesRegex(ValueError,'intent'):
                        fault.interrupt_owned(scope)
                opened.assert_not_called();send.assert_not_called();store.write.assert_not_called()

    def scope(self):
        now=time.time()
        return dict(BASE,observed_at=now,deadline=now+120)

    def test_observe_requires_stable_post_observation_snapshot(self):
        with patch.object(fault,'snapshot',side_effect=[(BASE,'a'),(BASE,'a')]):
            self.assertEqual(fault.observe(PROJECT,IDENTITY,NONCE)['actor_start_ticks'],ACTOR['start'])
        with patch.object(fault,'snapshot',side_effect=[(BASE,'a'),(BASE,'b')]):
            with self.assertRaisesRegex(ValueError,'changed during observation'):
                fault.observe(PROJECT,IDENTITY,NONCE)

    def test_observer_does_not_block_stage_finish_in_either_read_window(self):
        request={'session':{'project':PROJECT},'stages':[]}
        active={'ordinal':4,'command':'task20-execute','actor':ACTOR}
        original={'reservation_id':IDENTITY,'host':list(HOST),'state':'SOURCE_VERIFIED',
                  'active':active,'session':request['session'],'stages':request['stages'],
                  'events':[],'next_ordinal':4}
        original['descriptor_sha256']=fault.custody.digest(request)
        store_type=fault.custody.Store
        for window in ('record-read','identity-join'):
            with self.subTest(window=window), tempfile.TemporaryDirectory(dir=Path.home()) as folder:
                store=store_type(Path(folder),owner_uid=os.getuid())
                store.write(original)
                finished=[];triggered=[False]
                def finish_once():
                    if not triggered[0]:
                        triggered[0]=True
                        finished.append(store.finish(IDENTITY,4,host=HOST,actor=ACTOR,
                                                     exit_code=0,log_sha256='a'*64,
                                                     quiescent=lambda:True))
                def read():
                    if window=='record-read':finish_once()
                    return copy.deepcopy(original)
                def bytes_for(path,maximum=1048576,*,mode=None,with_identity=False):
                    if Path(path)==store.root/'record.json':
                        raw=Path(path).read_bytes();info=Path(path).stat()
                        return (raw,(info.st_dev,info.st_ino)) if with_identity else raw
                    return json.dumps(request).encode()
                def join(*args):
                    if window=='identity-join':finish_once()
                    return BASE['cgroup']
                with patch.object(fault.custody,'Store',return_value=store) as store_class, \
                     patch.object(store,'read',side_effect=read), \
                     patch.object(fault,'root_bytes',side_effect=bytes_for), \
                     patch.object(fault.custody,'enrollment',return_value={'enabled':True}), \
                     patch.object(fault.custody,'host_identity',return_value=HOST), \
                     patch.object(fault,'runtime_binding',return_value=BASE['enrollment_sha256']), \
                     patch.object(fault.engine,'dossier',return_value=Path(folder)), \
                     patch.object(fault.engine,'validate_request'), \
                     patch.object(fault,'unit_identity',side_effect=join):
                    store_class.match=store_type.match
                    with self.assertRaisesRegex(ValueError,'Reservation changed during observation'):
                        fault.snapshot(PROJECT,IDENTITY,NONCE)
                self.assertEqual(len(finished),1)
                self.assertIsNone(finished[0]['active'])
                self.assertEqual(finished[0]['state'],'SOURCE_VERIFIED')

    def test_stable_unlocked_record_still_yields_exact_scope(self):
        request={'session':{'project':PROJECT},'stages':[]}
        record={'reservation_id':IDENTITY,'host':list(HOST),'state':'SOURCE_VERIFIED',
                'active':{'ordinal':4,'command':'task20-execute','actor':ACTOR},
                'session':request['session'],'stages':request['stages'],'events':[],
                'next_ordinal':4,'descriptor_sha256':fault.custody.digest(request)}
        store_type=fault.custody.Store
        with tempfile.TemporaryDirectory(dir=Path.home()) as folder:
            store=store_type(Path(folder),owner_uid=os.getuid())
            store.write(record)
            def bytes_for(path,maximum=1048576,*,mode=None,with_identity=False):
                if Path(path)==store.root/'record.json':
                    raw=Path(path).read_bytes();info=Path(path).stat()
                    return (raw,(info.st_dev,info.st_ino)) if with_identity else raw
                return json.dumps(request).encode()
            with patch.object(fault.custody,'Store',return_value=store) as store_class, \
                 patch.object(store,'read',return_value=copy.deepcopy(record)), \
                 patch.object(fault,'root_bytes',side_effect=bytes_for), \
                 patch.object(fault.custody,'enrollment',return_value={'enabled':True}), \
                 patch.object(fault.custody,'host_identity',return_value=HOST), \
                 patch.object(fault,'runtime_binding',return_value=BASE['enrollment_sha256']), \
                 patch.object(fault.engine,'dossier',return_value=Path(folder)), \
                 patch.object(fault.engine,'validate_request'), \
                 patch.object(fault,'unit_identity',return_value=BASE['cgroup']):
                store_class.match=store_type.match
                scope=fault.observe(PROJECT,IDENTITY,NONCE)
            self.assertEqual({key:scope[key] for key in BASE},BASE)
            self.assertEqual(scope['deadline']-scope['observed_at'],120)

    def test_same_bytes_republished_during_join_refuses_changed_inode(self):
        request={'session':{'project':PROJECT},'stages':[]}
        record={'reservation_id':IDENTITY,'host':list(HOST),'state':'SOURCE_VERIFIED',
                'active':{'ordinal':4,'command':'task20-execute','actor':ACTOR},
                'session':request['session'],'stages':request['stages'],'events':[],
                'next_ordinal':4,'descriptor_sha256':fault.custody.digest(request)}
        store_type=fault.custody.Store
        with tempfile.TemporaryDirectory(dir=Path.home()) as folder:
            store=store_type(Path(folder),owner_uid=os.getuid());store.write(record)
            def bytes_for(path,maximum=1048576,*,mode=None,with_identity=False):
                if Path(path)==store.root/'record.json':
                    raw=Path(path).read_bytes();info=Path(path).stat()
                    return (raw,(info.st_dev,info.st_ino)) if with_identity else raw
                return json.dumps(request).encode()
            def replace(*args):
                store.write(record)
                return BASE['cgroup']
            with patch.object(fault.custody,'Store',return_value=store) as store_class, \
                 patch.object(store,'read',return_value=copy.deepcopy(record)), \
                 patch.object(fault,'root_bytes',side_effect=bytes_for), \
                 patch.object(fault.custody,'enrollment',return_value={'enabled':True}), \
                 patch.object(fault.custody,'host_identity',return_value=HOST), \
                 patch.object(fault,'runtime_binding',return_value=BASE['enrollment_sha256']), \
                 patch.object(fault.engine,'dossier',return_value=Path(folder)), \
                 patch.object(fault.engine,'validate_request'), \
                 patch.object(fault,'unit_identity',side_effect=replace):
                store_class.match=store_type.match
                with self.assertRaisesRegex(ValueError,'Reservation changed during observation'):
                    fault.snapshot(PROJECT,IDENTITY,NONCE)

    def test_expired_after_final_join_retains_hold_without_signal(self):
        clock=[119.0]
        scope=dict(BASE,observed_at=0.0,deadline=120.0)
        record={'reservation_id':IDENTITY,'host':list(HOST),'state':'SOURCE_VERIFIED',
                'active':{'ordinal':4,'command':'task20-execute','actor':ACTOR}}
        store=Mock();store.locked.return_value=nullcontext();store.read.return_value=record
        opened=[]
        def open_selected(*args):
            selected=os.open('/dev/null',os.O_RDONLY)
            opened.append(selected)
            return selected
        def join(*args):
            clock[0]=121.0
            return BASE['cgroup']
        with patch.object(fault.custody,'Store',return_value=store), \
             patch.object(fault.custody,'host_identity',return_value=HOST), \
             patch.object(fault,'snapshot',return_value=(BASE,'digest')), \
             patch.object(fault.engine,'dossier',return_value=Path('/uninstalled/source-test')), \
             patch.object(fault.engine,'write') as intent, \
             patch.object(fault,'unit_identity',side_effect=join), \
             patch.object(fault.time,'time',side_effect=lambda:clock[0]), \
             patch.object(fault.os,'pidfd_open',side_effect=open_selected,create=True), \
             patch.object(fault.signal,'pidfd_send_signal',create=True) as send:
            with self.assertRaisesRegex(ValueError,'Stale fault scope'):
                fault.interrupt_owned(scope)
        self.assertEqual(record['state'],'HELD')
        intent.assert_called_once();store.write.assert_called_once();send.assert_not_called()
        self.assertEqual(len(opened),1)
        with self.assertRaises(OSError):os.fstat(opened[0])

    def test_interruption_is_fixed_pidfd_signal_after_durable_hold(self):
        scope=self.scope()
        record={'reservation_id':IDENTITY,'host':list(HOST),'state':'SOURCE_VERIFIED',
                'active':{'ordinal':4,'command':'task20-execute','actor':ACTOR}}
        store=Mock();store.locked.return_value=nullcontext();store.read.return_value=record
        opened=[]
        def open_selected(*args):
            selected=os.open('/dev/null',os.O_RDONLY)
            opened.append(selected)
            return selected
        try:
            with patch.object(fault.custody,'Store',return_value=store), \
                 patch.object(fault.custody,'host_identity',return_value=HOST), \
                 patch.object(fault,'snapshot',return_value=(BASE,'digest')) as snapshot, \
                 patch.object(fault.engine,'dossier',return_value=Path('/uninstalled/source-test')), \
                 patch.object(fault.engine,'write') as write, \
                 patch.object(fault,'unit_identity',return_value=BASE['cgroup']), \
                 patch.object(fault.os,'pidfd_open',side_effect=open_selected,create=True) as pidfd_open, \
                 patch.object(fault.signal,'pidfd_send_signal',create=True) as send:
                result=fault.interrupt_owned(scope)
            self.assertEqual(result['status'],'INTERRUPTION_REQUESTED_HELD')
            self.assertEqual(record['state'],'HELD')
            self.assertEqual(snapshot.call_count,2)
            self.assertEqual(pidfd_open.call_args.args,(ACTOR['pid'],0))
            self.assertEqual(send.call_args.args,(opened[0],signal.SIGKILL,None,0))
            self.assertEqual(write.call_count,1)
            self.assertEqual(store.write.call_count,1)
        finally:
            # The mock allocates only at the selected pidfd_open call; the
            # production handler owns and closes that descriptor immediately.
            if opened:
                with self.assertRaises(OSError):os.fstat(opened[0])

    def test_reused_actor_refuses_without_hold_or_signal(self):
        scope=self.scope()
        record={'reservation_id':IDENTITY,'host':list(HOST),'state':'SOURCE_VERIFIED',
                'active':{'ordinal':4,'command':'task20-execute','actor':ACTOR}}
        changed=dict(BASE,actor_start_ticks=ACTOR['start']+1)
        store=Mock();store.locked.return_value=nullcontext();store.read.return_value=record
        with patch.object(fault.custody,'Store',return_value=store), \
             patch.object(fault.custody,'host_identity',return_value=HOST), \
             patch.object(fault,'snapshot',return_value=(changed,'digest')), \
             patch.object(fault.os,'pidfd_open',create=True) as pidfd_open, \
             patch.object(fault.signal,'pidfd_send_signal',create=True) as send:
            with self.assertRaisesRegex(ValueError,'differs from current owned actor'):
                fault.interrupt_owned(scope)
        pidfd_open.assert_not_called();send.assert_not_called();store.write.assert_not_called()
        self.assertEqual(record['state'],'SOURCE_VERIFIED')

    def test_non_execute_and_stale_scope_refuse(self):
        scope=self.scope();scope['observed_at']-=121
        with self.assertRaisesRegex(ValueError,'Stale fault scope'):fault.validate_scope(scope,time.time())
        scope=self.scope()
        record={'reservation_id':IDENTITY,'host':list(HOST),'state':'SOURCE_VERIFIED',
                'active':{'ordinal':3,'command':'task20-bootstrap','actor':ACTOR}}
        store=Mock();store.locked.return_value=nullcontext();store.read.return_value=record
        with patch.object(fault.custody,'Store',return_value=store), \
             patch.object(fault.custody,'host_identity',return_value=HOST), \
             patch.object(fault.os,'pidfd_open',create=True), \
             patch.object(fault.signal,'pidfd_send_signal',create=True) as send:
            with self.assertRaisesRegex(ValueError,'Only active reviewed execute'):
                fault.interrupt_owned(scope)
        send.assert_not_called();store.write.assert_not_called()

    def test_systemd_main_pid_must_equal_durable_actor(self):
        names='LoadState,ActiveState,SubState,MainPID,ControlPID,ControlGroup,User,Transient,KillMode,Restart,Type'.split(',')
        values=['loaded','active','running','99999','0',BASE['cgroup'],'','yes','control-group','no','exec']
        output='\n'.join(key+'='+value for key,value in zip(names,values))+'\n'
        active={'ordinal':4,'command':'task20-execute','actor':ACTOR}
        with patch.object(fault,'command',return_value=output):
            with self.assertRaisesRegex(ValueError,'differs from selected owner'):
                fault.unit_identity(PROJECT,IDENTITY,active)

    def test_runtime_nonce_requires_committed_boot_and_exact_installed_assets(self):
        assets={};files={}
        for path in (fault.CONTROL,fault.ENGINE,fault.FAULT,fault.RESERVATION,fault.OBSERVATION,fault.RECEIVER,fault.PACKAGE):
            raw=('reviewed '+path.name).encode();sha=hashlib.sha256(raw).hexdigest()
            assets[str(path)]={'data':base64.b64encode(raw).decode(),'sha256':sha}
            files[str(path)]={'sha256':sha};files[path]=raw
        plan={'schema':1,'nonce':NONCE,'vm_id':HOST[0],'boot':HOST[1],'assets':assets}
        journal=[{'stage':'FILES_AND_SERVICE_VERIFIED'},
                 {'stage':'FINISH_RELEASE_COMMITTED','qualification_ready':False,'files':{
                     str(path):files[str(path)] for path in (fault.CONTROL,fault.ENGINE,fault.FAULT,fault.RESERVATION,fault.OBSERVATION,fault.RECEIVER,fault.PACKAGE)}}]
        backup=fault.RUNTIME/('runtime-update-'+NONCE)
        files[backup/'plan.json']=json.dumps(plan).encode()
        files[backup/'journal.jsonl']=b'\n'.join(json.dumps(row).encode() for row in journal)+b'\n'
        files[fault.custody.CONFIG]=b'{"reservation_enrollment":"fixture"}'
        with patch.object(fault,'root_bytes',side_effect=lambda path,maximum=1048576,mode=None:files[Path(path)]):
            self.assertEqual(fault.runtime_binding(NONCE,HOST),
                             hashlib.sha256(files[fault.custody.CONFIG]).hexdigest())
            with self.assertRaisesRegex(ValueError,'Committed installation custody'):
                fault.runtime_binding(NONCE,(HOST[0],'other-boot'))
            for imported in (fault.RESERVATION,fault.OBSERVATION,fault.RECEIVER,fault.PACKAGE):
                original=files[imported]
                files[imported]=b'drifted selected actor dependency'
                with self.subTest(imported=imported), self.assertRaisesRegex(ValueError,'Installed control asset differs'):
                    fault.runtime_binding(NONCE,HOST)
                files[imported]=original

    def test_root_control_active_fence_uses_shared_noncreating_lock(self):
        script=Path(__file__).resolve().parents[1]/'builder/dedicated/reservation-control.py'
        spec=importlib.util.spec_from_file_location('reservation_control_fault_test',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        info=Mock(st_mode=0o100600,st_uid=0,st_nlink=1)
        with patch.object(module.os,'open',return_value=9) as opened, \
             patch.object(module.os,'fstat',return_value=info), \
             patch.object(module.fcntl,'flock') as locked:
            self.assertEqual(module.active_fence(),9)
        self.assertFalse(opened.call_args.args[1]&os.O_CREAT)
        locked.assert_called_once_with(9,module.fcntl.LOCK_SH|module.fcntl.LOCK_NB)


if __name__=='__main__':unittest.main()
