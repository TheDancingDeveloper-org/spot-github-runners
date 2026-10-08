"""Focused source controls; no host mutation or live reservation."""
import importlib.util
import hashlib
import os
from pathlib import Path
import pwd
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/lib'))
from qualification_trust import reservation as custody
from qualification_trust import control_engine as engine

spec=importlib.util.spec_from_file_location('reservation_control_recovery',ROOT/'builder/dedicated/reservation-control.py')
control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)


class ActiveControlTests(unittest.TestCase):
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
                info=actual(fd);values=list(info);values[4]=0
                return os.stat_result(values)
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
