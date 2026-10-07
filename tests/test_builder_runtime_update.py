"""Tiny update-plan and atomic-file controls; no guest or service effects."""
import base64
import hashlib
import fcntl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lib.builder_runtime_update import TARGETS, validate_assets, validate_plan, atomic_write, drained_update, check_snapshot, plan_from_snapshot, active_work_processes, checked_json, valid_admission, validate_service_peer, validate_admission_response, mkdir_exact, close_launcher_execution

class BuilderRuntimeUpdateTests(unittest.TestCase):
    def assets(self):
        raw=b'synthetic reviewed fixture\n'
        return {name:{'data':base64.b64encode(raw).decode(),
            'sha256':hashlib.sha256(raw).hexdigest()} for name in TARGETS}

    def test_exact_runtime_set_and_hashes(self):
        decoded=validate_assets(self.assets())
        self.assertEqual(set(decoded),set(TARGETS))
        self.assertEqual(len(decoded),15)

    def test_unknown_missing_or_tampered_asset_refused(self):
        for kind in ('unknown','missing','tampered'):
            data=self.assets()
            if kind=='unknown':data['/etc/shadow']=next(iter(data.values()))
            elif kind=='missing':data.pop(next(iter(data)))
            else:next(iter(data.values()))['sha256']='0'*64
            with self.subTest(kind=kind),self.assertRaises(ValueError):validate_assets(data)

    def test_atomic_write_preserves_requested_mode(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root).resolve()/'target';path.write_bytes(b'old')
            atomic_write(path,b'new',0o644)
            self.assertEqual(path.read_bytes(),b'new')
            self.assertEqual(path.stat().st_mode & 0o777,0o644)

    def test_atomic_write_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            root=str(Path(root).resolve())
            original=Path(root)/'original';original.write_bytes(b'keep')
            path=Path(root)/'target';path.symlink_to(original)
            with self.assertRaises(ValueError):atomic_write(path,b'new',0o644)
            self.assertEqual(original.read_bytes(),b'keep')

    def plan(self):
        return dict(schema=1, nonce='a'*32, source_main='b'*40, vm_id='123',
            boot='0b99c4d2-ca5f-4471-bf84-ff4ab8dbefd3', image_id='sha256:'+'c'*64,
            config_sha256='d'*64, service_sha256='e'*64, admission_sha256='f'*64, service_pid=123,
            service_startticks=456, observed_at=1000.0,
            assets=self.assets(), before={name:None for name in TARGETS})

    def test_expired_future_or_nonfinite_plan_refused(self):
        for observed in (800.0,1001.0,float('nan'),float('inf'),True):
            plan=self.plan();plan['observed_at']=observed
            with self.subTest(observed=observed),self.assertRaises(ValueError):
                validate_plan(plan,1000.0)

    def test_valid_plan_keeps_exact_before_set_and_authority(self):
        self.assertEqual(set(validate_plan(self.plan(),1001.0)),set(TARGETS))
        plan=self.plan();plan['before'].pop(next(iter(TARGETS)))
        with self.assertRaises(ValueError):validate_plan(plan,1001.0)
        plan=self.plan();plan['vm_id']='unbound'
        with self.assertRaises(ValueError):validate_plan(plan,1001.0)

    def test_busy_lock_does_not_create_drain_or_call_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); lock=root/'build.lock'; drain=root/'drain'
            with lock.open('a') as holder:
                fcntl.flock(holder,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    drained_update(lock,drain,'owned',lambda:self.fail('action ran'))
            self.assertFalse(drain.exists())

    def test_foreign_drain_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); lock=root/'build.lock'; lock.touch()
            drain=root/'drain'; drain.write_text('foreign')
            with self.assertRaises(FileExistsError):
                drained_update(lock,drain,'owned',lambda:self.fail('action ran'))
            self.assertEqual(drain.read_text(),'foreign')

    def test_failed_action_retains_owned_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); lock=root/'build.lock'; lock.touch(); drain=root/'drain'
            def fail():raise RuntimeError('partial update')
            with self.assertRaises(RuntimeError):drained_update(lock,drain,'owned',fail)
            self.assertEqual(drain.read_text(),'owned')

    def test_success_removes_only_exact_owned_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); lock=root/'build.lock'; lock.touch(); drain=root/'drain'
            self.assertEqual(drained_update(lock,drain,'owned',lambda:42),42)
            self.assertFalse(drain.exists())


    def snapshot(self):
        plan=self.plan()
        return dict(vm_id=plan['vm_id'],boot=plan['boot'],image_id=plan['image_id'],
            config_sha256=plan['config_sha256'],service_sha256=plan['service_sha256'],
            admission_sha256=plan['admission_sha256'],admission_valid=True,image_pin=plan['image_id'],
            service_pid=plan['service_pid'],service_startticks=plan['service_startticks'],
            cpu=16,role='builder',cpus=16,memory_gib=28,leases={},containers=[],
            before=plan['before'],drain_token=None,tools_ready=True,upload_processes=[])

    def test_busy_changed_identity_or_config_refuses(self):
        for key,value in [('leases',{'real-worker':28}),('containers',['actual-id']),
                          ('vm_id','999'),('cpu',32),('image_id','sha256:'+'f'*64),
                          ('drain_token','foreign'),('tools_ready',False),('service_pid',999),('upload_processes',[123]),('image_pin','wrong'),('admission_sha256','wrong'),('admission_valid',False)]:
            state=self.snapshot();state[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                check_snapshot(self.plan(),state)

    def test_only_exact_owned_drain_is_accepted_inside_transaction(self):
        state=self.snapshot();check_snapshot(self.plan(),state)
        state['drain_token']=self.plan()['nonce']
        with self.assertRaises(ValueError):check_snapshot(self.plan(),state)
        check_snapshot(self.plan(),state,owned_drain=True)


    def test_capture_uses_actual_snapshot_and_refuses_busy_host(self):
        snapshot=self.snapshot()
        plan=plan_from_snapshot('b'*40,self.assets(),snapshot,1000.0,'a'*32)
        self.assertEqual(plan['before'],snapshot['before'])
        self.assertEqual(plan['service_pid'],snapshot['service_pid'])
        snapshot['containers']=['actual-container']
        with self.assertRaises(ValueError):
            plan_from_snapshot('b'*40,self.assets(),snapshot,1000.0,'a'*32)


    def test_queued_python_launcher_is_work_but_inline_metadata_is_not(self):
        records=[{'pid':1,'comm':'python3','argv':[b'python3',b'/usr/local/bin/oxidex-remote-build',b'project']},
                 {'pid':2,'comm':'python3','argv':[b'python3',b'-c',b"print('/usr/local/bin/oxidex-remote-build')"]}]
        self.assertEqual(active_work_processes(records),[1])

    def test_failed_freshness_fence_precedes_drain_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();lock=root/'build.lock';lock.touch();drain=root/'drain'
            def stale():raise ValueError('expired actual observation')
            with self.assertRaises(ValueError):
                drained_update(lock,drain,'owned',lambda:self.fail('action ran'),before_drain=stale)
            self.assertFalse(drain.exists())

    def test_json_parser_uses_only_digest_bound_bytes(self):
        raw=b'{"actual": true}'
        self.assertEqual(checked_json(raw,hashlib.sha256(raw).hexdigest()),{'actual':True})
        with self.assertRaises(ValueError):checked_json(b'{"changed": true}',hashlib.sha256(raw).hexdigest())


    def test_dedicated_admission_policy_is_validated(self):
        config=dict(role='builder',host_cpus=16,builder_cpus=16,work_gib=28,
                    builder_gib=28,runner_gib=0,runner_names=[])
        self.assertTrue(valid_admission(config))
        for key,value in [('work_gib',57),('runner_names',['foreign']),('host_cpus',True)]:
            changed=dict(config);changed[key]=value
            self.assertFalse(valid_admission(changed))


    def test_admission_socket_peer_must_match_root_process(self):
        validate_service_peer(123,(123,0,0))
        for peer in ((999,0,0),(123,1001,1001),(123,False,0)):
            with self.subTest(peer=peer),self.assertRaises(ValueError):
                validate_service_peer(123,peer)


    def test_readiness_requires_exact_non_mutating_protocol_response(self):
        validate_admission_response(b'{"error":"Admission kind does not match peer identity"}')
        for response in (b'{"accepted":true}',b'{"error":"broken config"}',b'{}'):
            with self.subTest(response=response),self.assertRaises(ValueError):
                validate_admission_response(response)


    def test_final_callback_cannot_change_owned_drain_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();lock=root/'build.lock';lock.touch();drain=root/'drain'
            def replace_token(result):drain.write_text('foreign-after-check')
            with self.assertRaises(ValueError):
                drained_update(lock,drain,'owned',lambda:42,before_release=replace_token)
            self.assertEqual(drain.read_text(),'foreign-after-check')


    def test_expiry_after_staging_preserves_live_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();path=root/'target';path.write_bytes(b'old')
            def expired():raise ValueError('expired after staging')
            with self.assertRaises(ValueError):
                atomic_write(path,b'new',0o644,before_replace=expired)
            self.assertEqual(path.read_bytes(),b'old')

    def test_successful_install_can_remain_drained_for_pin_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();lock=root/'build.lock';lock.touch();drain=root/'drain'
            self.assertEqual(drained_update(lock,drain,'owned',lambda:42,keep_drain=True),42)
            self.assertEqual(drain.read_text(),'owned')


    def test_new_directory_mode_ignores_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'new'
            old=__import__('os').umask(0o077)
            try:mkdir_exact(path,0o755)
            finally:__import__('os').umask(old)
            self.assertEqual(path.stat().st_mode & 0o777,0o755)


    def test_launcher_execution_hold_preserves_bytes_and_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'launcher';path.write_bytes(b'#!/bin/sh\nexit 0\n');path.chmod(0o755)
            before=path.stat();raw=path.read_bytes()
            close_launcher_execution(path)
            self.assertEqual(path.read_bytes(),raw)
            self.assertEqual(path.stat().st_ino,before.st_ino)
            self.assertEqual(path.stat().st_mode & 0o777,0o644)
            with self.assertRaises(PermissionError):
                subprocess.run([str(path)],check=True)


if __name__=='__main__':unittest.main()
