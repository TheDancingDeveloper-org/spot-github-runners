import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from lib.host_admission import LeasePool, admission_budget, owner_alive, memory_envelope
from unittest.mock import patch
from types import SimpleNamespace
from lib.runner_tiers import TIERS


class AdmissionTests(unittest.TestCase):
    def test_measured_host_overhead_reduces_admission_without_raising_configured_budget(self):
        config=dict(work_gib=12,runner_names=['r']*8)
        self.assertEqual(memory_envelope(config,16,10,['4GiB / 16GiB']),8)
        self.assertEqual(memory_envelope(config,16,4,['3GiB / 16GiB']),12)
        with self.assertRaises(ValueError):memory_envelope(config,16,4,['invalid'])

    def test_lost_builder_process_retains_lease_while_container_survives(self):
        with patch('lib.host_admission.process_identity',side_effect=FileNotFoundError),\
             patch('lib.host_admission.subprocess.run',return_value=SimpleNamespace(stdout='container-id\n')):
            self.assertTrue(owner_alive('host:999:1'))

    def test_parallel_requests_never_exceed_memory_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = LeasePool(Path(folder)/'leases.json', 6, alive=lambda identity:True)
            with ThreadPoolExecutor(max_workers=8) as workers:
                results = list(workers.map(lambda i:pool.acquire(str(i),2), range(8)))
            self.assertEqual(sum(results),3)
            self.assertFalse(pool.acquire('0',3))
            owner=next(str(i) for i,acquired in enumerate(results) if acquired)
            pool.release(owner)
            self.assertTrue(pool.acquire('new',2))

    def test_dead_identity_reclaimed_and_oversized_request_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            alive = {'old'}
            pool = LeasePool(Path(folder)/'leases.json',4,alive=lambda key:key in alive)
            self.assertTrue(pool.acquire('old',4))
            alive.clear()
            alive.add('new')
            self.assertTrue(pool.acquire('new',4))
            with self.assertRaises(ValueError):pool.acquire('new',5)
            with self.assertRaises(ValueError):pool.acquire('new',float('nan'))

    def test_listener_and_measured_overhead_leave_work_headroom(self):
        budget = admission_budget(TIERS['starting'],3,2,slots=8)
        self.assertEqual(budget['work_gib'],11)
        self.assertEqual(budget['concurrency'],5)
        self.assertLessEqual(budget['work_gib']+budget['reserve_gib'],16)
        with self.assertRaises(ValueError):admission_budget(TIERS['starting'],15,2,slots=8)

    def test_unknown_runner_docker_state_retains_existing_reservation(self):
        import subprocess
        with patch('lib.host_admission.subprocess.run',side_effect=subprocess.TimeoutExpired('docker',10)):
            self.assertTrue(owner_alive('container:abc:start'))

    def test_reused_builder_pid_retains_reservation_while_container_survives(self):
        with patch('lib.host_admission.process_identity',return_value='host:999:2'),\
             patch('lib.host_admission.builders_running',return_value=True):
            self.assertTrue(owner_alive('host:999:1'))

    def test_complete_inventory_proves_removed_runner_lease_can_be_reclaimed(self):
        import subprocess
        with patch('lib.host_admission.subprocess.run',side_effect=[
            subprocess.CalledProcessError(1,'docker',stderr='No such object'),SimpleNamespace(stdout='other\n')]):
            self.assertFalse(owner_alive('container:abc:start'))
