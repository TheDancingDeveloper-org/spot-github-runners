"""Portable fresh-host regression controls; never launch a VM or Docker."""
import base64
import hashlib
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from lib.builder_access import canonical_sha, approved_mapping, restricted_group_guard
from lib.builder_only_assets import ASSETS, STARTUP
from lib.builder_startup import render_builder_startup
from lib.host_admission import builder_memory_budget, builder_capacity_record, execution_limits
from render_builder_startup import render_files


def inputs():
    # Synthetic public-key wire bytes, never an operational key or identity.
    key = 'ssh-ed25519 ' + base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'x' * 32).decode()
    mapping = dict(schema='root-approved-ssh-mapping-v1', principal='test@example.invalid',
                   username='oxidex-uploader', uid=2000, identity_receipt_sha256='1' * 64,
                   public_key=key, public_key_sha256=hashlib.sha256(key.encode()).hexdigest())
    return dict(authorized_uploader=mapping, authorized_uploader_sha256=canonical_sha(mapping),
                boot_disk_iops=3000, boot_disk_size_gb=40, boot_disk_throughput=140,
                builder_cpus=16, builder_memory_gib=28, cpu=16, ram=32, role='builder',
                runner_count=0, data_disk_iops=3000, data_disk_throughput=140,
                disk_device_name='builder-cache', disk_size_gb=100, disk_type='hyperdisk-balanced',
                instance_name='builder-test', project='test-project', zone='asia-east2-a')


class DedicatedBuilderStartupTests(unittest.TestCase):
    def test_complete_payload_limits_tools_mount_order_and_no_runners(self):
        script = render_builder_startup(SimpleNamespace(**inputs()), 'unit-test')
        self.assertNotIn('{{', script)
        self.assertLess(len(script.encode()), 262144)
        self.assertEqual(STARTUP, (ROOT / 'cloud-init/builder-startup.sh').read_text())
        self.assertLess(script.index('mount "$DEVICE" "$MOUNT_POINT"'), script.index('DOCKER_BUILDKIT=0'))
        self.assertIn('--memory=28g --memory-swap=28g --cpu-period=100000 --cpu-quota=1600000', script)
        self.assertIn('install -d -o0 -g1001 -m0710 "$MOUNT_POINT/remote-build/targets"', script)
        self.assertNotIn('usermod -aG docker', script)
        self.assertNotIn('--privileged', script)
        for path, content in ASSETS.items():
            if path.startswith('/opt/oxidex-builder/image/'):
                self.assertEqual(content, (ROOT / 'builder/standalone' / Path(path).name).read_text())

    def test_rendered_cloud_config_and_one_command_are_same_payload(self):
        with tempfile.TemporaryDirectory() as parent:
            out = render_files(inputs(), 'unit-test', Path(parent) / 'payload')
            cloud = json.loads((out / 'cloud-config.yaml').read_text().split('\n', 1)[1])
            self.assertEqual(base64.b64decode(cloud['write_files'][0]['content']), (out / 'startup.sh').read_bytes())
            argv = shlex.split((out / 'launch-command.sh').read_text().split('exec ', 1)[1])
            self.assertEqual(argv[:5], ['gcloud', 'compute', 'instances', 'create', 'builder-test'])
            self.assertIn('--metadata-from-file=startup-script=' + str(out / 'startup.sh'), argv)
            self.assertIn('auto-delete=no', next(x for x in argv if x.startswith('--create-disk=')))
            with self.assertRaises(FileExistsError):
                render_files(inputs(), 'unit-test', out)

    def test_policy_shape_mapping_and_performance_refusals(self):
        for key, value in [('runner_count', 1), ('cpu', 32), ('ram', 64), ('builder_memory_gib', 32),
                           ('instance_name', 'runner-test'), ('data_disk_iops', True),
                           ('authorized_uploader_sha256', '0' * 64)]:
            config = inputs(); config[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                render_builder_startup(SimpleNamespace(**config), 'unit-test')
        for pair in [(3000, 140), (4000, 250)]:
            config = inputs(); config.update(data_disk_iops=pair[0], data_disk_throughput=pair[1])
            self.assertTrue(render_builder_startup(SimpleNamespace(**config), 'unit-test'))

    def test_uploader_key_and_actual_group_collisions(self):
        mapping = inputs()['authorized_uploader']
        self.assertEqual(approved_mapping(mapping)['uid'], 2000)
        group = SimpleNamespace(gr_name='oxidex-build', gr_gid=1001, gr_mem=[])
        self.assertEqual(restricted_group_guard(mapping, [], [group])['members'], [])
        with self.assertRaises(ValueError):
            restricted_group_guard(mapping, [SimpleNamespace(pw_name='foreign', pw_uid=2500, pw_gid=1001)], [group])
        changed = dict(mapping, public_key_sha256='0' * 64)
        with self.assertRaises(ValueError):
            approved_mapping(changed)

    def test_aggregate_headroom_and_exact_execution_schema(self):
        self.assertEqual(builder_memory_budget(31.34, 30, 0), 28)
        self.assertEqual(builder_memory_budget(31.34, 27, 0), 0)
        self.assertEqual(builder_memory_budget(31.34, 40, 0), 0)
        capacity = builder_capacity_record(dict(role='builder', runner_names=[], host_cpus=16,
                                               builder_cpus=16, work_gib=28, builder_gib=28, runner_gib=0))
        self.assertEqual(execution_limits(capacity), (16, 0, 0))
        with self.assertRaises(ValueError):
            execution_limits(dict(capacity, builder_gib=32))


if __name__ == '__main__':
    unittest.main()
