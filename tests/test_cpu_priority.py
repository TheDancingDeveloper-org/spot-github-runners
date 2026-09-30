"""Exercise selection and provisioning without accessing live cloud resources."""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib.compute_api import Offer
from lib.runner_fleet import GitHubRunner
from lib.spot_runner_autoscaler import eligible_offers, runner_is_managed
from lib.runner_manager import args_parser, select_sizing, apply_startup


class CpuPriorityTests(unittest.TestCase):
    def setUp(self):
        blocker = patch('subprocess.run', side_effect=AssertionError('Unexpected live command'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_newer_cpu_candidates_win_within_cap_and_memory_floor(self):
        offers = [Offer('old', 'e2-standard-32', 32, 128, .10),
                  Offer('old', 'n2-standard-32', 32, 128, .12),
                  Offer('new', 'c4-standard-32', 32, 120, .19),
                  Offer('new', 'c4d-standard-32', 32, 124, .18),
                  Offer('small', 'c4d-highcpu-32', 32, 60, .08),
                  Offer('other', 'c4a-standard-32', 32, 128, .08),
                  Offer('lssd', 'c4-standard-32-lssd', 32, 120, .15),
                  Offer('expensive', 'c4d-standard-32', 32, 124, .22)]
        self.assertEqual([o.machine_type for o in eligible_offers(offers, Decimal('5'))],
                         ['c4d-standard-32', 'c4-standard-32', 'n2-standard-32', 'e2-standard-32'])

    def test_ram_floor_accepts_110_and_keeps_exact_cpu_count(self):
        offers = [Offer('a', 'n2-custom-32-112640', 32, 110, .15),
                  Offer('a', 'n2-custom-32-111616', 32, 109, .14),
                  Offer('a', 'n2-standard-64', 64, 256, .15)]
        self.assertEqual([o.ram_gib for o in eligible_offers(offers, Decimal('5'))], [110])

    def test_exact_32_cpu_generation_tiers_use_cheapest_peer(self):
        offers = [Offer('a', 'c4-standard-32', 32, 120, .19),
                  Offer('b', 'n4-standard-32', 32, 128, .17),
                  Offer('c', 'c4d-standard-32', 32, 124, .18),
                  Offer('d', 'n4d-standard-32', 32, 128, .16)]
        self.assertEqual([o.machine_type for o in eligible_offers(offers, Decimal('5'))],
                         ['n4d-standard-32', 'c4d-standard-32',
                          'n4-standard-32', 'c4-standard-32'])

    def test_new_families_are_counted_as_managed_x64_runners(self):
        for family in ('c4', 'c4d', 'n4', 'n4d', 'n2d'):
            runner = GitHubRunner(1, 'gha-worker-1', 'online', False, ('spot', 'X64', family + '-standard-32'))
            self.assertTrue(runner_is_managed(runner), family)

    def test_hyperdisk_boot_and_data_creation_have_nvme_and_baseline_performance(self):
        args = args_parser().parse_args(['--max-cost', '.2', '--instance-type', 'c4-standard-32',
                                        '--ram', '120', '--instance-name', 'test-vm'])
        select_sizing(args)
        calls = []
        def cloud(command, **kwargs):
            calls.append(command)
            return '[]' if command[:3] == ['compute', 'disks', 'list'] else ''
        with patch('lib.runner_manager.subprocess.run', return_value=SimpleNamespace(stdout='', returncode=1)), \
             patch('lib.runner_manager.gcloud', side_effect=cloud):
            apply_startup(args, 'zone-a', 'safe-test-script')
        create_disk = next(c for c in calls if c[:3] == ['compute', 'disks', 'create'])
        create_vm = next(c for c in calls if c[:3] == ['compute', 'instances', 'create'])
        self.assertIn('--type=hyperdisk-balanced', create_disk)
        self.assertIn('--provisioned-iops=3000', create_disk)
        self.assertIn('--provisioned-throughput=140', create_disk)
        self.assertIn('--boot-disk-type=hyperdisk-balanced', create_vm)
        self.assertIn('--boot-disk-interface=NVME', create_vm)
        self.assertIn('--boot-disk-provisioned-iops=3000', create_vm)
        self.assertIn('--boot-disk-provisioned-throughput=140', create_vm)
        self.assertIn('--network-interface=network=default,nic-type=GVNIC', create_vm)
        self.assertTrue(any('interface=NVME' in item for item in create_vm if item.startswith('--disk=')))
        self.assertEqual((args.runner_count, args.runner_memory_gb), (8, 14))

    def test_legacy_hosts_keep_pd_balanced_disks(self):
        args = args_parser().parse_args(['--max-cost', '.2'])
        select_sizing(args)
        self.assertEqual(args.disk_type, 'pd-balanced')

    def test_hyperdisk_family_rejects_incompatible_explicit_disk_type(self):
        args = args_parser().parse_args(['--max-cost', '.2', '--instance-type', 'c4d-standard-32',
                                        '--ram', '124', '--disk-type', 'pd-balanced'])
        with self.assertRaisesRegex(RuntimeError, 'requires Hyperdisk'):
            select_sizing(args)

    def test_hyperdisk_family_price_preflight_uses_actual_ram_and_rejects_cost(self):
        from lib.runner_manager import verify_price_and_capacity
        args = args_parser().parse_args(['--max-cost', '.2', '--instance-type', 'c4d-standard-32', '--ram', '124'])
        with patch('lib.runner_manager.gcloud', return_value=json.dumps({'guestCpus':32,'memoryMb':126976})), \
             patch('lib.runner_manager.spot_rates', return_value=(Decimal('.005'),Decimal('.001'))):
            with self.assertRaisesRegex(RuntimeError, 'no resources changed'):
                verify_price_and_capacity(args,'zone-a')

    def test_matching_arm_label_does_not_count_toward_x64_capacity(self):
        self.assertFalse(runner_is_managed(GitHubRunner(1,'arm','online',False,('spot','ARM64','c4a-standard-32'))))

    def test_fractional_custom_ram_cannot_understate_the_compute_price(self):
        from lib.runner_manager import verify_price_and_capacity
        args = args_parser().parse_args(['--max-cost', str(Decimal('5') / 24),
                                        '--instance-type', 'n2-custom-32-112896', '--ram', '110'])
        with patch('lib.runner_manager.gcloud', return_value=json.dumps({'guestCpus': 32, 'memoryMb': 112896})), \
             patch('lib.runner_manager.spot_rates', return_value=(Decimal('.005'), Decimal('.0004389'))):
            with self.assertRaisesRegex(RuntimeError, 'no resources changed'):
                verify_price_and_capacity(args, 'zone-a')

    def test_family_like_non_machine_label_does_not_count_as_managed(self):
        self.assertFalse(runner_is_managed(GitHubRunner(1,'other','online',False,('spot','X64','n4-region'))))

    def test_hyperdisk_adoption_attaches_data_using_nvme(self):
        args = args_parser().parse_args(['--max-cost','.2','--instance-type','c4d-standard-32',
                                        '--ram','124','--existing-instance'])
        select_sizing(args)
        vm = {'machineType':'machineTypes/c4d-standard-32','status':'TERMINATED',
              'scheduling':{'provisioningModel':'SPOT'},'disks':[]}
        replies = [SimpleNamespace(stdout=json.dumps(vm),returncode=0),
                   SimpleNamespace(stdout='',returncode=1)]
        calls=[]
        def cloud(command, **kwargs):
            calls.append(command)
            return '[]' if command[:3]==['compute','disks','list'] else ''
        with patch('lib.runner_manager.subprocess.run',side_effect=replies), \
             patch('lib.runner_manager.gcloud',side_effect=cloud):
            apply_startup(args,'zone-a','safe-test-script')
        attach=next(c for c in calls if c[:3]==['compute','instances','attach-disk'])
        self.assertIn('--interface=NVME',attach)

    def test_existing_incompatible_data_disk_is_rejected_before_attachment(self):
        args=args_parser().parse_args(['--max-cost','.2','--instance-type','c4-standard-32','--ram','120'])
        select_sizing(args)
        replies=[SimpleNamespace(stdout='',returncode=1),
                 SimpleNamespace(stdout=json.dumps({'type':'diskTypes/pd-balanced','users':[]}),returncode=0)]
        with patch('lib.runner_manager.subprocess.run',side_effect=replies), \
             patch('lib.runner_manager.gcloud',return_value='[]') as cloud:
            with self.assertRaisesRegex(RuntimeError,'disk type'):
                apply_startup(args,'zone-a','safe-test-script')
        self.assertFalse(any('create' in c.args[0] or 'attach-disk' in c.args[0] for c in cloud.call_args_list))


if __name__ == '__main__':
    unittest.main()
