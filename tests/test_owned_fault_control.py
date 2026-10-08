"""Source-only exact actor fault API checks; no host signal is sent."""
import importlib.util
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
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
    def scope(self):
        now=time.time()
        return dict(BASE,observed_at=now,deadline=now+120)

    def test_observe_requires_stable_post_observation_snapshot(self):
        with patch.object(fault,'snapshot',side_effect=[(BASE,'a'),(BASE,'a')]):
            self.assertEqual(fault.observe(PROJECT,IDENTITY,NONCE)['actor_start_ticks'],ACTOR['start'])
        with patch.object(fault,'snapshot',side_effect=[(BASE,'a'),(BASE,'b')]):
            with self.assertRaisesRegex(ValueError,'changed during observation'):
                fault.observe(PROJECT,IDENTITY,NONCE)

    def test_interruption_is_fixed_pidfd_signal_after_durable_hold(self):
        scope=self.scope()
        record={'reservation_id':IDENTITY,'host':list(HOST),'state':'SOURCE_VERIFIED',
                'active':{'ordinal':4,'command':'task20-execute','actor':ACTOR}}
        store=Mock();store.locked.return_value=nullcontext();store.read.return_value=record
        fd=os.open('/dev/null',os.O_RDONLY)
        try:
            with patch.object(fault.custody,'Store',return_value=store), \
                 patch.object(fault.custody,'host_identity',return_value=HOST), \
                 patch.object(fault,'snapshot',return_value=(BASE,'digest')) as snapshot, \
                 patch.object(fault.engine,'dossier',return_value=Path('/uninstalled/source-test')), \
                 patch.object(fault.engine,'write') as write, \
                 patch.object(fault,'unit_identity',return_value=BASE['cgroup']), \
                 patch.object(fault.os,'pidfd_open',return_value=fd,create=True) as pidfd_open, \
                 patch.object(fault.signal,'pidfd_send_signal',create=True) as send:
                result=fault.interrupt_owned(scope)
            self.assertEqual(result['status'],'INTERRUPTION_REQUESTED_HELD')
            self.assertEqual(record['state'],'HELD')
            self.assertEqual(snapshot.call_count,2)
            self.assertEqual(pidfd_open.call_args.args,(ACTOR['pid'],0))
            self.assertEqual(send.call_args.args,(fd,signal.SIGKILL,None,0))
            self.assertEqual(write.call_count,1)
            self.assertEqual(store.write.call_count,1)
        finally:
            # interrupt_owned closed the selected descriptor.
            pass

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
        for path in (fault.CONTROL,fault.ENGINE,fault.FAULT):
            raw=('reviewed '+path.name).encode();sha=hashlib.sha256(raw).hexdigest()
            assets[str(path)]={'data':base64.b64encode(raw).decode(),'sha256':sha}
            files[str(path)]={'sha256':sha};files[path]=raw
        plan={'schema':1,'nonce':NONCE,'vm_id':HOST[0],'boot':HOST[1],'assets':assets}
        journal=[{'stage':'FILES_AND_SERVICE_VERIFIED'},
                 {'stage':'FINISH_RELEASE_COMMITTED','qualification_ready':False,'files':{
                     str(path):files[str(path)] for path in (fault.CONTROL,fault.ENGINE,fault.FAULT)}}]
        backup=fault.RUNTIME/('runtime-update-'+NONCE)
        files[backup/'plan.json']=json.dumps(plan).encode()
        files[backup/'journal.jsonl']=b'\n'.join(json.dumps(row).encode() for row in journal)+b'\n'
        files[fault.custody.CONFIG]=b'{"reservation_enrollment":"fixture"}'
        with patch.object(fault,'root_bytes',side_effect=lambda path,maximum=1048576,mode=None:files[Path(path)]):
            self.assertEqual(fault.runtime_binding(NONCE,HOST),
                             hashlib.sha256(files[fault.custody.CONFIG]).hexdigest())
            with self.assertRaisesRegex(ValueError,'Committed installation custody'):
                fault.runtime_binding(NONCE,(HOST[0],'other-boot'))

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
