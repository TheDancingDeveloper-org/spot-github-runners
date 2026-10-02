import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lib.runner_manager import apply_startup, args_parser, cleanup_orphan_runner_disks, render_startup, select_sizing


class RunnerDiskLifecycleTests(unittest.TestCase):
    def setUp(self):
        blocker = patch('subprocess.run', side_effect=AssertionError('Unexpected live command'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def provisioning_calls(self, machine_type, ram, vm=None, attached=False):
        options = ['--max-cost', '.2', '--instance-type', machine_type,
                   '--ram', str(ram), '--instance-name', 'test-vm']
        if vm:
            options.append('--existing-instance')
        args = args_parser().parse_args(options)
        select_sizing(args)
        results = [SimpleNamespace(returncode=0 if vm else 1,
                                   stdout=json.dumps(vm) if vm else ''),
                   SimpleNamespace(returncode=0 if attached else 1,
                                   stdout=json.dumps({'type': args.disk_type,
                                                      'users': ['test-vm']}) if attached else '')]
        calls = []
        def cloud(command, **kwargs):
            calls.append(command)
            return '[]' if command[:3] == ['compute', 'disks', 'list'] else ''
        with patch('lib.runner_manager.subprocess.run', side_effect=results), \
             patch('lib.runner_manager.gcloud', side_effect=cloud):
            apply_startup(args, 'zone-a', 'safe-test-script')
        return args, calls

    def test_new_spot_vms_delete_both_disks_on_preemption(self):
        for machine_type, ram in [('n2-standard-32', 128), ('c4d-standard-32', 124)]:
            with self.subTest(machine_type=machine_type):
                _, calls = self.provisioning_calls(machine_type, ram)
                create = next(c for c in calls if c[:3] == ['compute', 'instances', 'create'])
                self.assertIn('--boot-disk-auto-delete', create)
                self.assertIn('--instance-termination-action=DELETE', create)
                self.assertTrue(any('auto-delete=yes' in flag for flag in create if flag.startswith('--disk=')))

    def test_spot_startup_uses_one_label_set_and_never_mounts_host_docker_socket(self):
        args = SimpleNamespace(disk_device_name='runner-data', instance_type='n2-standard-32',
                               region='asia-south2', runner_label='spot', runner_prefix='gha',
                               runner_count=8, runner_cpu=4, runner_memory_gb=15,
                               instance_name='oxidex-runners-test')
        startup = render_startup(args, 'registration-token', 'oxidex-m4air', 'deployment-id')
        self.assertIn("RUNNER_LABELS='spot,n2-standard-32,asia-south2'", startup)
        self.assertNotIn('DOCKER_RUNNER_LABELS', startup)
        self.assertNotIn('DOCKER_RUNNER_COUNT', startup)
        self.assertNotIn('/var/run/docker.sock', startup)
        self.assertNotIn('--group-add', startup)

    def test_runners_share_idle_cpu_instead_of_a_hard_four_vcpu_cap(self):
        args = SimpleNamespace(disk_device_name='runner-data', instance_type='n4-standard-32',
                               region='asia-east2', runner_label='spot', runner_prefix='gha',
                               runner_count=8, runner_cpu=4, runner_memory_gb=15,
                               instance_name='oxidex-runners-test')
        startup = render_startup(args, 'registration-token', 'group', 'deployment-id')
        self.assertNotIn('--cpus', startup)
        self.assertIn('--cpu-shares=1024', startup)  # Equal weights split CPU only under contention.
        self.assertIn('--memory="${RUNNER_MEMORY_GB}g"', startup)
        self.assertIn('RUNNER_MEMORY_GB=15', startup)

    def test_adoption_enforces_boot_and_data_deletion_without_touching_extra_disk(self):
        vm = {'machineType': 'types/n2-standard-32', 'status': 'TERMINATED',
              'scheduling': {'provisioningModel': 'SPOT'},
              'disks': [{'boot': True, 'deviceName': 'boot-device',
                         'source': 'disks/different-root-name', 'autoDelete': False},
                        {'boot': False, 'deviceName': 'runner-data', 'autoDelete': False},
                        {'boot': False, 'deviceName': 'unrelated-retained', 'autoDelete': False}]}
        args = args_parser().parse_args(['--max-cost', '.2'])
        vm['disks'][1]['deviceName'] = args.disk_device_name
        _, calls = self.provisioning_calls('n2-standard-32', 128, vm, attached=True)
        settings = [c for c in calls if c[:3] == ['compute', 'instances', 'set-disk-auto-delete']]
        self.assertEqual(len(settings), 2)
        self.assertTrue(any('--device-name=boot-device' in c and '--auto-delete' in c for c in settings))
        self.assertTrue(any(f'--disk={args.disk_device_name}' in c and '--auto-delete' in c for c in settings))
        self.assertFalse(any('unrelated-retained' in flag for c in calls for flag in c))

    def test_adoption_attaches_new_data_disk_with_auto_delete(self):
        vm = {'machineType': 'types/c4d-standard-32', 'status': 'TERMINATED',
              'scheduling': {'provisioningModel': 'SPOT'},
              'disks': [{'boot': True, 'deviceName': 'boot-device', 'autoDelete': False}]}
        _, calls = self.provisioning_calls('c4d-standard-32', 124, vm)
        attach = next(c for c in calls if c[:3] == ['compute', 'instances', 'attach-disk'])
        self.assertIn('--auto-delete', attach)
        self.assertIn('--interface=NVME', attach)
        self.assertTrue(any('--device-name=boot-device' in c and '--auto-delete' in c for c in calls))

    def test_create_receipt_survives_loss_before_registration_inventory(self):
        args = args_parser().parse_args(['--max-cost', '.2', '--instance-name', 'test-vm'])
        select_sizing(args)
        with tempfile.TemporaryDirectory() as directory:
            args.deployment_receipt = Path(directory) / 'receipt.json'
            created = {'id': '123', 'name': 'test-vm', 'zone': 'zones/zone-a',
                       'status': 'RUNNING', 'disks': [
                           {'boot': True, 'source': 'disks/root', 'autoDelete': True},
                           {'boot': False, 'source': 'disks/data', 'autoDelete': True}]}
            def cloud(command, **kwargs):
                if command[:3] == ['compute', 'instances', 'create']:
                    self.assertTrue(any(flag.startswith('--format=json(') for flag in command))
                    return json.dumps([created])
                return '[]' if command[:3] == ['compute', 'disks', 'list'] else ''
            with patch('lib.runner_manager.subprocess.run', return_value=SimpleNamespace(stdout='', returncode=1)), \
                 patch('lib.runner_manager.gcloud', side_effect=cloud):
                apply_startup(args, 'zone-a', 'safe-test-script')
            receipt = json.loads(args.deployment_receipt.read_text())
            self.assertEqual(receipt['instance_id'], '123')
            self.assertEqual(receipt['name'], 'test-vm')
            self.assertEqual(receipt['disks'][0]['source'], 'disks/root')
            self.assertTrue(receipt['disks'][1]['autoDelete'])

    @patch("lib.runner_manager.datetime")
    @patch("lib.runner_manager.gcloud")
    def test_orphan_cleanup_deletes_only_unattached_owned_disks(self, gcloud, clock):
        clock.now.return_value = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)
        clock.fromisoformat.side_effect = datetime.fromisoformat
        gcloud.return_value = json.dumps([
            {"name": "runner-data-old", "status": "READY", "users": [],
             "labels": {"managed-by": "spot-runner-manager"},
             "creationTimestamp": "2026-09-28T00:00:00Z"},
            {"name": "runner-data-in-use", "status": "READY", "users": ["vm-url"],
             "labels": {"managed-by": "spot-runner-manager"},
             "creationTimestamp": "2026-09-28T00:00:00Z"},
            {"name": "runner-data-new", "status": "READY", "users": [],
             "labels": {"managed-by": "spot-runner-manager"},
             "creationTimestamp": "2026-09-30T05:00:00Z"},
            {"name": "unrelated", "status": "READY", "users": [], "labels": {},
             "creationTimestamp": "2026-09-28T00:00:00Z"},
            {"name": "manager-stopping", "status": "CREATING", "users": [],
             "labels": {"managed-by": "spot-runner-manager"},
             "creationTimestamp": "2026-09-28T00:00:00Z"},
        ])

        removed = cleanup_orphan_runner_disks("us-south1-b")

        self.assertEqual(removed, ["runner-data-old"])
        gcloud.assert_any_call([
            "compute", "disks", "delete", "runner-data-old", "--zone=us-south1-b", "--quiet"
        ])
        self.assertEqual(gcloud.call_count, 2)


if __name__ == "__main__":
    unittest.main()
