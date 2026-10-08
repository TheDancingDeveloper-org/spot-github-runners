"""Focused source controls; no host mutation or live reservation."""
import importlib.util
import contextlib
import hashlib
import io
import os
from pathlib import Path
import pwd
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/lib'))
from qualification_trust import reservation as custody
from qualification_trust import control_engine as engine

spec=importlib.util.spec_from_file_location('reservation_control_recovery',ROOT/'builder/dedicated/reservation-control.py')
control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)


class ActiveControlTests(unittest.TestCase):
    def test_root_control_help_states_mutations_are_enabled(self):
        output=io.StringIO()
        with patch.object(control.os,'geteuid',return_value=0),\
             patch.object(sys,'argv',['reservation-control','--help']),\
             contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as result:control.main()
        self.assertEqual(result.exception.code,0)
        self.assertIn('enabled fixed-scope mutations',output.getvalue())
        self.assertNotIn('mutation remains deliberately disabled',output.getvalue())

    def test_reconcile_log_reproves_file_and_directory_durability_on_retry(self):
        home=Path(pwd.getpwuid(os.getuid()).pw_dir)
        actual_fstat=os.fstat;actual_fsync=os.fsync
        def root_info(fd):
            info=actual_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode,st_uid=0,st_nlink=info.st_nlink,
                st_size=info.st_size,st_dev=info.st_dev,st_ino=info.st_ino,
                st_mtime_ns=info.st_mtime_ns,st_ctime_ns=info.st_ctime_ns)
        with tempfile.TemporaryDirectory(dir=home) as directory:
            for failed_sync in (1,2):
                with self.subTest(failed_sync=failed_sync):
                    path=Path(directory)/('failed-'+str(failed_sync))
                    attempted=[]
                    def fail_at(fd):
                        attempted.append('directory' if stat.S_ISDIR(actual_fstat(fd).st_mode) else 'file')
                        if len(attempted)==failed_sync:raise OSError('injected fsync failure')
                        return actual_fsync(fd)
                    with patch.object(control.os,'fstat',side_effect=root_info),\
                         patch.object(control.os,'fsync',side_effect=fail_at):
                        with self.assertRaisesRegex(OSError,'injected fsync'):
                            control.reconcile_log(path)
                    self.assertEqual(path.read_bytes(),control.RECONCILE_LOG)
                    proved=[]
                    def sync(fd):
                        proved.append('directory' if stat.S_ISDIR(actual_fstat(fd).st_mode) else 'file')
                        return actual_fsync(fd)
                    with patch.object(control.os,'fstat',side_effect=root_info),\
                         patch.object(control.os,'fsync',side_effect=sync):
                        self.assertEqual(control.reconcile_log(path),hashlib.sha256(control.RECONCILE_LOG).hexdigest())
                    self.assertEqual(proved,['file','directory'])
            partial=Path(directory)/'partial'
            partial.write_bytes(control.RECONCILE_LOG[:17])
            proved=[]
            def sync(fd):
                proved.append('directory' if stat.S_ISDIR(actual_fstat(fd).st_mode) else 'file')
                return actual_fsync(fd)
            with patch.object(control.os,'fstat',side_effect=root_info),\
                 patch.object(control.os,'fsync',side_effect=sync):
                control.reconcile_log(partial)
            self.assertEqual(proved,['file','directory'])
            self.assertEqual(partial.read_bytes(),control.RECONCILE_LOG)

    def test_reconcile_log_rejects_named_file_swap_after_sync(self):
        home=Path(pwd.getpwuid(os.getuid()).pw_dir)
        actual_fstat=os.fstat;actual_fsync=os.fsync
        with tempfile.TemporaryDirectory(dir=home) as directory:
            path=Path(directory)/'reconcile-cleanup.log'
            path.write_bytes(control.RECONCILE_LOG)
            replacement=Path(directory)/'replacement'
            replacement.write_bytes(control.RECONCILE_LOG)
            def root_info(fd):
                info=actual_fstat(fd)
                return SimpleNamespace(st_mode=info.st_mode,st_uid=0,st_nlink=info.st_nlink,
                    st_size=info.st_size,st_dev=info.st_dev,st_ino=info.st_ino,
                    st_mtime_ns=info.st_mtime_ns,st_ctime_ns=info.st_ctime_ns)
            def swap_at_directory_sync(fd):
                actual_fsync(fd)
                if stat.S_ISDIR(actual_fstat(fd).st_mode):
                    path.rename(Path(directory)/'old')
                    replacement.rename(path)
            with patch.object(control.os,'fstat',side_effect=root_info),\
                 patch.object(control.os,'fsync',side_effect=swap_at_directory_sync):
                with self.assertRaisesRegex(ValueError,'changed after durability proof'):
                    control.reconcile_log(path)

    def test_stage_and_export_reobserve_enrollment_after_shared_fence(self):
        for operation in ('stage','export'):
            with self.subTest(operation=operation):
                argv=['reservation-control',operation,'--project','p','--reservation','r']
                if operation=='stage':argv.extend(['--ordinal','0'])
                order=[]
                def lock(*_):order.append('fence')
                def enrolled():order.append('enrollment');raise ValueError('Enrolled cache mount unavailable or replaced')
                with patch.object(sys,'argv',argv),patch.object(control.os,'geteuid',return_value=0),\
                     patch.object(control.os,'open',return_value=19),patch.object(control.os,'close'),\
                     patch.object(control.fcntl,'flock',side_effect=lock),\
                     patch.object(control.signal,'signal'),patch.object(custody,'enrollment',side_effect=enrolled),\
                     patch.object(engine,'stage') as stage,patch.object(engine,'export') as export:
                    with self.assertRaisesRegex(ValueError,'Enrolled cache mount'):
                        control.main()
                    self.assertEqual(order,['fence','enrollment'])
                    stage.assert_not_called();export.assert_not_called()
                with patch.object(sys,'argv',argv),patch.object(control.os,'geteuid',return_value=0),\
                     patch.object(control.os,'open',return_value=19),patch.object(control.os,'close'),\
                     patch.object(control.fcntl,'flock'),patch.object(control.signal,'signal'),\
                     patch.object(custody,'enrollment',return_value=None),\
                     patch.object(engine,'stage') as stage,patch.object(engine,'export') as export:
                    with self.assertRaisesRegex(ValueError,'requires enrolled storage'):
                        control.main()
                    stage.assert_not_called();export.assert_not_called()
                with patch.object(sys,'argv',argv),patch.object(control.os,'geteuid',return_value=0),\
                     patch.object(control.os,'open',return_value=19),patch.object(control.os,'close'),\
                     patch.object(control.fcntl,'flock'),patch.object(control.signal,'signal'),\
                     patch.object(custody,'enrollment',return_value={'enabled':True}) as enrolled_ok,\
                     patch.object(engine,'own_unit',return_value='owned.service'),\
                     patch.object(engine,'stage',return_value={'state':'ok'}) as stage,\
                     patch.object(engine,'export') as export:
                    self.assertEqual(control.main(),0)
                    enrolled_ok.assert_called_once()
                    (stage if operation=='stage' else export).assert_called_once()

    def test_reconcile_log_exact_bytes_and_nofollow(self):
        home=Path(pwd.getpwuid(os.getuid()).pw_dir)
        with tempfile.TemporaryDirectory(dir=home) as directory:
            path=Path(directory)/'reconcile-cleanup.log'
            actual=os.fstat
            def root_info(fd):
                info=actual(fd)
                return SimpleNamespace(st_mode=info.st_mode,st_uid=0,st_nlink=info.st_nlink,
                    st_size=info.st_size,st_dev=info.st_dev,st_ino=info.st_ino,
                    st_mtime_ns=info.st_mtime_ns,st_ctime_ns=info.st_ctime_ns)
            with patch.object(control.os,'fstat',side_effect=root_info):
                self.assertEqual(control.reconcile_log(path),hashlib.sha256(control.RECONCILE_LOG).hexdigest())
                self.assertEqual(path.read_bytes(),control.RECONCILE_LOG)
                control.reconcile_log(path)
                path.write_bytes(control.RECONCILE_LOG[:17])
                control.reconcile_log(path)
                self.assertEqual(path.read_bytes(),control.RECONCILE_LOG)
                path.write_bytes(b'forged')
                with self.assertRaisesRegex(ValueError,'log differs'):
                    control.reconcile_log(path)
                path.unlink()
                other=Path(directory)/'other';other.write_bytes(control.RECONCILE_LOG)
                path.symlink_to(other)
                with self.assertRaises(OSError):control.reconcile_log(path)


class RecoveryStoreTests(unittest.TestCase):
    def test_ordinary_cleanup_dead_actor_recovers_without_reboot(self):
        from test_whole_run_reservation import ReservationTests, reservation as isolated_custody
        for refused_finish in (False,True):
            with self.subTest(refused_finish=refused_finish):
                fixture=ReservationTests();fixture.setUp()
                try:
                    record=fixture.claim();identity=record['reservation_id']
                    for ordinal in range(7):fixture.step(record,ordinal)
                    old=fixture.actor;new={'pid':300,'start':400}
                    fixture.store.begin(identity,7,host=fixture.host,actor=old,argv=fixture.argv[7])
                    if refused_finish:
                        with self.assertRaisesRegex(ValueError,'quiescence'):
                            fixture.store.finish(identity,7,host=fixture.host,actor=old,
                                exit_code=0,log_sha256='f'*64,quiescent=lambda:False)
                    self.assertEqual(fixture.store.read()['state'],
                                     'HELD' if refused_finish else 'SOURCE_VERIFIED')
                    with patch.object(isolated_custody,'process',return_value=(old,1)):
                        with self.assertRaisesRegex(ValueError,'Prior cleanup actor remains'):
                            fixture.store.cleanup_after_failure(identity,host=fixture.host,
                                actor=new,argv=fixture.argv[7],quiescent=lambda:True)
                    with patch.object(isolated_custody,'process',side_effect=FileNotFoundError):
                        with self.assertRaisesRegex(ValueError,'quiescence'):
                            fixture.store.cleanup_after_failure(identity,host=fixture.host,
                                actor=new,argv=fixture.argv[7],quiescent=lambda:False)
                        recovered=fixture.store.cleanup_after_failure(identity,host=fixture.host,
                            actor=new,argv=fixture.argv[7],quiescent=lambda:True)
                    self.assertEqual(recovered['state'],'RECOVERING')
                    self.assertEqual(recovered['active']['actor'],new)
                    self.assertEqual(recovered['events'][-1]['disposition'],'INTERRUPTED')
                    self.assertEqual(recovered['events'][-1]['actor'],old)
                    fixture.store.finish(identity,7,host=fixture.host,actor=new,exit_code=0,
                                         log_sha256='f'*64,quiescent=lambda:True)
                    self.assertEqual(fixture.store.release(identity,host=fixture.host,
                        quiescent=lambda:True)['state'],'RELEASED')
                finally:fixture.tearDown()

    def test_dead_cleanup_actor_rebinds_only_after_quiescence_and_retains_interruption(self):
        from test_whole_run_reservation import ReservationTests, reservation as isolated_custody
        fixture=ReservationTests();fixture.setUp()
        try:
            record=fixture.claim();identity=record['reservation_id']
            old=fixture.actor;new={'pid':300,'start':400}
            fixture.store.cleanup_after_failure(identity,host=fixture.host,actor=old,
                                                argv=fixture.argv[-1],quiescent=lambda:True)
            with patch.object(isolated_custody,'process',return_value=(old,1)):
                with self.assertRaisesRegex(ValueError,'Prior cleanup actor remains'):
                    fixture.store.cleanup_after_failure(identity,host=fixture.host,actor=new,
                                                        argv=fixture.argv[-1],quiescent=lambda:True)
            with patch.object(isolated_custody,'process',side_effect=PermissionError('unknown')):
                with self.assertRaises(PermissionError):
                    fixture.store.cleanup_after_failure(identity,host=fixture.host,actor=new,
                                                        argv=fixture.argv[-1],quiescent=lambda:True)
            with patch.object(isolated_custody,'process',side_effect=FileNotFoundError):
                with self.assertRaisesRegex(ValueError,'quiescence'):
                    fixture.store.cleanup_after_failure(identity,host=fixture.host,actor=new,
                                                        argv=fixture.argv[-1],quiescent=lambda:False)
                recovered=fixture.store.cleanup_after_failure(identity,host=fixture.host,actor=new,
                                                               argv=fixture.argv[-1],quiescent=lambda:True)
            self.assertEqual(recovered['active']['actor'],new)
            self.assertEqual(recovered['events'][-1]['disposition'],'INTERRUPTED')
            self.assertEqual(recovered['events'][-1]['actor'],old)
            fixture.store.finish(identity,7,host=fixture.host,actor=new,exit_code=0,
                                 log_sha256='f'*64,quiescent=lambda:True)
            self.assertEqual(fixture.store.release(identity,host=fixture.host,
                                                   quiescent=lambda:True)['state'],'RELEASED')
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
