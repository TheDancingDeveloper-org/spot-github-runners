import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.host_admission import LeasePool, heavy_runner_policy, runner_reservation

CID='a'*64
START='2026-10-04T14:22:17Z'
OWNER=f'runner:{CID}:{START}:123:456'
NAME='gha-oxidex-runners-asiaeast2-b-1791123056-1'
OTHER='gha-oxidex-runners-asiaeast2-b-1791123056-2'
GIB=1024**3


def config(heavy=True):
    row={'runner_names':[NAME,OTHER],'runner_gib':15,'builder_gib':25,'work_gib':57}
    if heavy:row['heavy_runner']={'name':NAME,'memory_gib':32,'cpus':8}
    return row


class HeavyRunnerAdmissionTests(unittest.TestCase):
    def test_default_preserves_lagging_cap_reservation(self):
        ordinary=config(False);ordinary['runner_gib']=30
        with patch('lib.host_admission.docker_identity') as inspect:
            self.assertEqual(runner_reservation(OWNER,ordinary),30)
            inspect.assert_not_called()

    def test_exact_heavy_caps_and_worker_are_required(self):
        row={'Id':CID,'Name':'/'+NAME,'State':{'StartedAt':START,'Running':True},
             'HostConfig':{'Memory':32*GIB,'MemorySwap':32*GIB,'NanoCpus':8_000_000_000}}
        with patch('lib.host_admission.docker_identity',return_value=(f'container:{CID}:{START}',row)), \
             patch('lib.host_admission.owner_alive',return_value=True):
            self.assertEqual(runner_reservation(OWNER,config()),32)
            row['HostConfig']['MemorySwap']=64*GIB
            with self.assertRaisesRegex(ValueError,'memory cap'):
                runner_reservation(OWNER,config())
            row['HostConfig']['MemorySwap']=32*GIB
            row['HostConfig']['NanoCpus']=0
            with self.assertRaisesRegex(ValueError,'CPU cap'):
                runner_reservation(OWNER,config())
        with patch('lib.host_admission.docker_identity',return_value=(f'container:{CID}:{START}',row)), \
             patch('lib.host_admission.owner_alive',return_value=False):
            with self.assertRaisesRegex(ValueError,'Worker exited'):
                runner_reservation(OWNER,config())

    def test_invalid_override_and_ordinary_cap_above_reservation_hold(self):
        bad=config();bad['heavy_runner']['memory_gib']=31
        with self.assertRaisesRegex(ValueError,'Invalid single'):
            heavy_runner_policy(bad)
        bad=config();bad['runner_gib']=float('nan')
        with self.assertRaisesRegex(ValueError,'Invalid single'):
            heavy_runner_policy(bad)
        bad=config();bad['heavy_runner']=None
        with self.assertRaisesRegex(ValueError,'Invalid single'):
            heavy_runner_policy(bad)
        row={'Id':CID,'Name':'/'+OTHER,'State':{'StartedAt':START,'Running':True},
             'HostConfig':{'Memory':16*GIB,'MemorySwap':16*GIB,'NanoCpus':0}}
        with patch('lib.host_admission.docker_identity',return_value=(f'container:{CID}:{START}',row)), \
             patch('lib.host_admission.owner_alive',return_value=True):
            with self.assertRaisesRegex(ValueError,'Ordinary runner memory cap'):
                runner_reservation(OWNER,config())

    def test_actual_ordinary_15_gib_memory_30_gib_combined_swap_is_allowed(self):
        row={'Id':CID,'Name':'/'+OTHER,'State':{'StartedAt':START,'Running':True},
             'HostConfig':{'Memory':15*GIB,'MemorySwap':30*GIB,'NanoCpus':0}}
        with patch('lib.host_admission.docker_identity',return_value=(f'container:{CID}:{START}',row)), \
             patch('lib.host_admission.owner_alive',return_value=True):
            self.assertEqual(runner_reservation(OWNER,config()),15)

    def test_aggregate_budget_and_owned_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'leases.json'
            pool=LeasePool(path,57,alive=lambda _:True,heavy_builder_isolation=True)
            heavy=OWNER
            normal=f'runner:{"b"*64}:{START}:124:457'
            second=f'runner:{"c"*64}:{START}:125:458'
            self.assertTrue(pool.acquire_runner(heavy,32))
            self.assertTrue(pool.acquire_runner(normal,15))
            self.assertFalse(pool.acquire_runner(second,32))
            self.assertFalse(pool.acquire('host:123:456',25))
            self.assertEqual(json.loads(path.read_text()),{heavy:32,normal:15})
            pool.release_runner(heavy)
            self.assertEqual(json.loads(path.read_text()),{normal:15})
            self.assertTrue(pool.acquire('host:123:456',25))

    def test_existing_builder_lease_blocks_heavy_grant_under_same_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'leases.json'
            pool=LeasePool(path,57,alive=lambda _:True,heavy_builder_isolation=True)
            self.assertTrue(pool.acquire('host:123:456',25))
            self.assertFalse(pool.acquire_runner(OWNER,32))
            self.assertEqual(json.loads(path.read_text()),{'host:123:456':25})
            pool.release('host:123:456')
            self.assertTrue(pool.acquire_runner(OWNER,32))





class StrictHeavyPoolTests(unittest.TestCase):
    def pool_config(self):
        return dict(role='runner',heavy_runners=[NAME,OTHER],runner_names=[NAME,OTHER],runner_gib=32,work_gib=64)

    def test_both_members_require_exact_caps(self):
        for name in (NAME,OTHER):
            row={'Id':CID,'Name':'/'+name,'State':{'StartedAt':START,'Running':True},
                 'HostConfig':{'Memory':32*GIB,'MemorySwap':32*GIB,'NanoCpus':8_000_000_000}}
            with patch('lib.host_admission.docker_identity',return_value=(f'container:{CID}:{START}',row)),patch('lib.host_admission.owner_alive',return_value=True):
                self.assertEqual(runner_reservation(OWNER,self.pool_config()),32)
                for field,value in [('Memory',15*GIB),('MemorySwap',64*GIB),('NanoCpus',0)]:
                    original=row['HostConfig'][field];row['HostConfig'][field]=value
                    with self.assertRaises(ValueError):runner_reservation(OWNER,self.pool_config())
                    row['HostConfig'][field]=original

    def test_changed_owner_and_membership_refuse(self):
        row={'Id':CID,'Name':'/'+NAME,'State':{'StartedAt':START},'HostConfig':{'Memory':32*GIB,'MemorySwap':32*GIB,'NanoCpus':8_000_000_000}}
        with patch('lib.host_admission.docker_identity',return_value=(f'container:{CID}:{START}',row)),patch('lib.host_admission.owner_alive',return_value=False):
            with self.assertRaisesRegex(ValueError,'Worker exited'):runner_reservation(OWNER,self.pool_config())
        for change in [dict(runner_names=None),dict(runner_names=[NAME,OTHER,OTHER]),dict(runner_names=[NAME,{}]),dict(role='builder'),dict(work_gib=57),dict(heavy_runners=[NAME,NAME])]:
            with self.assertRaises(ValueError):heavy_runner_policy(dict(self.pool_config(),**change))

    def test_64_gib_budget_admits_two_and_refuses_third(self):
        with tempfile.TemporaryDirectory() as folder:
            pool=LeasePool(Path(folder)/'leases.json',64,alive=lambda _:True,heavy_builder_isolation=True)
            first=OWNER;second=f'runner:{"b"*64}:{START}:124:457';third=f'runner:{"c"*64}:{START}:125:458'
            self.assertTrue(pool.acquire_runner(first,32));self.assertTrue(pool.acquire_runner(second,32))
            self.assertFalse(pool.acquire_runner(third,32));pool.release_runner(first)
            self.assertTrue(pool.acquire_runner(third,32))

if __name__ == "__main__":
    unittest.main()
