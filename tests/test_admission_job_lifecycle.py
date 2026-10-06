"""Regression coverage for canceled jobs on persistent runner containers."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib.host_admission import LeasePool, owner_alive, peer_owner, worker_owner


CID = 'a' * 64
START = '2026-10-04T14:22:37.894724945Z'
OLD = f'container:{CID}:{START}'
NEW = f'runner:{CID}:{START}:123:456'
RUNNING = SimpleNamespace(stdout=json.dumps([{
    'Id': CID, 'Name': '/runner-1', 'State': {'Running': True, 'StartedAt': START, 'Pid': 1}
}]))
BASELINE = SimpleNamespace(stdout='PID COMMAND\n1 run.sh\n2 run-helper.sh\n3 Runner.Listener\n')
CGROUP=f'0::/system.slice/docker-{CID}.scope'


class AdmissionJobLifecycleTests(unittest.TestCase):
    def test_old_lease_reclaimed_only_after_complete_idle_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / 'leases.json'
            ledger.write_text(json.dumps({OLD: 15}))
            with patch('lib.host_admission.subprocess.run', side_effect=[RUNNING, BASELINE]), \
                 patch('lib.host_admission.process_record',side_effect=[
                     (1,'10','run-helper.sh',CGROUP),(2,'11','Runner.Listener',CGROUP)]):
                pool = LeasePool(ledger, 15, alive=owner_alive)
                self.assertTrue(pool.acquire('new', 15))
            self.assertEqual(json.loads(ledger.read_text()), {'new': 15})

    def test_live_legacy_lease_transfers_without_double_count_and_releases(self):
        later=f'runner:{CID}:{START}:124:999'
        with tempfile.TemporaryDirectory() as directory:
            ledger=Path(directory)/'leases.json'
            pool=LeasePool(ledger,15,alive=lambda _:True)
            self.assertTrue(pool.acquire(OLD,15))
            self.assertTrue(pool.acquire_runner(NEW,15))
            self.assertEqual(json.loads(ledger.read_text()),{NEW:15})
            pool.release_runner(NEW)
            self.assertEqual(json.loads(ledger.read_text()),{})
            self.assertTrue(pool.acquire_runner(later,15))
            self.assertEqual(json.loads(ledger.read_text()),{later:15})

    def test_completion_releases_only_its_exact_legacy_container(self):
        other='container:'+'b'*64+':2026-10-04T14:23:20Z'
        with tempfile.TemporaryDirectory() as directory:
            ledger=Path(directory)/'leases.json'
            pool=LeasePool(ledger,30,alive=lambda _:True)
            self.assertTrue(pool.acquire(OLD,15))
            self.assertTrue(pool.acquire(other,15))
            pool.release_runner(NEW)
            self.assertEqual(json.loads(ledger.read_text()),{other:15})

    def test_larger_new_reservation_refuses_without_losing_legacy_amount(self):
        other='container:'+'b'*64+':2026-10-04T14:23:20Z'
        with tempfile.TemporaryDirectory() as directory:
            ledger=Path(directory)/'leases.json'
            pool=LeasePool(ledger,20,alive=lambda _:True)
            self.assertTrue(pool.acquire(OLD,15))
            self.assertTrue(pool.acquire(other,5))
            self.assertFalse(pool.acquire_runner(NEW,16))
            self.assertEqual(json.loads(ledger.read_text()),{OLD:15,other:5})

    def test_smaller_new_reservation_cannot_discount_old_container_cap(self):
        other='container:'+'b'*64+':2026-10-04T14:23:20Z'
        with tempfile.TemporaryDirectory() as directory:
            ledger=Path(directory)/'leases.json'
            pool=LeasePool(ledger,40,alive=lambda _:True)
            self.assertTrue(pool.acquire(OLD,32))
            self.assertTrue(pool.acquire(other,5))
            self.assertFalse(pool.acquire_runner(NEW,15))
            self.assertEqual(json.loads(ledger.read_text()),{OLD:32,other:5})

    def test_old_lease_retained_with_worker_or_unknown_docker_top(self):
        active = SimpleNamespace(stdout=BASELINE.stdout + '4 Runner.Worker\n')
        with patch('lib.host_admission.subprocess.run', side_effect=[RUNNING, active]):
            self.assertTrue(owner_alive(OLD))
        with patch('lib.host_admission.subprocess.run', side_effect=[RUNNING, subprocess.TimeoutExpired('docker top',10),
                                                                      SimpleNamespace(stdout=CID+'\n')]):
            self.assertTrue(owner_alive(OLD))
        with patch('lib.host_admission.subprocess.run', side_effect=[RUNNING,
                SimpleNamespace(stdout='unexpected format\n'), SimpleNamespace(stdout=CID+'\n')]):
            self.assertTrue(owner_alive(OLD))
        with patch('lib.host_admission.subprocess.run', side_effect=[RUNNING,BASELINE]), \
             patch('lib.host_admission.process_record',return_value=(99,'10','run-helper.sh',CGROUP)):
            self.assertTrue(owner_alive(OLD))

    def test_hook_is_bound_to_live_worker_and_orphan_refused(self):
        cgroup=f'0::/system.slice/docker-{CID}.scope'
        with patch('lib.host_admission.process_record', side_effect=[
            (123, '789', 'python3', cgroup), (9, '456', 'Runner.Worker', cgroup)]):
            self.assertEqual(worker_owner(234, CID, START), NEW)
        with patch('lib.host_admission.process_record', side_effect=[
            (1, '789', 'python3', cgroup)]):
            with self.assertRaisesRegex(ValueError, 'no live Worker'):
                worker_owner(234, CID, START)

    def test_worker_lease_expires_on_worker_exit_or_pid_reuse(self):
        with patch('lib.host_admission.process_record', side_effect=FileNotFoundError):
            self.assertFalse(owner_alive(NEW))
        with patch('lib.host_admission.process_record', return_value=(9, '457', 'Runner.Worker',
                                                                      f'0::/docker-{CID}.scope')):
            self.assertFalse(owner_alive(NEW))
        with patch('lib.host_admission.process_record', return_value=(9, '456', 'Runner.Worker',
                                                                      f'0::/docker-{CID}.scope')), \
             patch('lib.host_admission.subprocess.run', return_value=RUNNING):
            self.assertTrue(owner_alive(NEW))

    def test_peer_owner_rejects_orphan_before_an_admission_grant(self):
        cgroup=f'0::/system.slice/docker-{CID}.scope'
        with patch('pathlib.Path.read_text', return_value=cgroup), \
             patch('lib.host_admission.docker_identity', return_value=(OLD, {
                 'Name': '/runner-1', 'State': {'StartedAt': START}})), \
             patch('lib.host_admission.process_record', side_effect=[(1, '789', 'python3', cgroup)]):
            with self.assertRaisesRegex(ValueError, 'no live Worker'):
                peer_owner(234, 1001, {'runner_names': ['runner-1']})
        with patch('pathlib.Path.read_text', return_value=cgroup), \
             patch('lib.host_admission.docker_identity', return_value=(OLD, {
                 'Name': '/runner-1', 'State': {'StartedAt': START}})), \
             patch('lib.host_admission.process_record', side_effect=[
                 (123, '789', 'python3', cgroup), (9, '456', 'Runner.Worker', cgroup)]):
            self.assertEqual(peer_owner(234, 1001, {'runner_names': ['runner-1']}), (NEW, 'runner'))


if __name__ == '__main__':
    unittest.main()
