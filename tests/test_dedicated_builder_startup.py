"""Portable fresh-host regression controls; never launch a VM or Docker."""
import base64
import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from lib.builder_access import canonical_sha, approved_mapping, restricted_group_guard
from lib.builder_only_assets import ASSETS, STARTUP
from lib.builder_startup import render_builder_startup, builder_guest_sources
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
        self.assertIn('umask 077\n', script)
        self.assertIn(' >/opt/gha-runners/builder_access.py\n', script)
        self.assertNotIn('chmod 0644 /opt/gha-runners/builder_access.py', script)
        self.assertLess(script.index('mount "$DEVICE" "$MOUNT_POINT"'), script.index('DOCKER_BUILDKIT=0'))
        self.assertIn('--memory=28g --memory-swap=28g --cpu-period=100000 --cpu-quota=1600000', script)
        self.assertIn('install -d -o0 -g1001 -m0710 "$MOUNT_POINT/remote-build/targets"', script)
        self.assertLess(script.index("systemctl reload ssh"),
                        script.index("python3 -B /opt/gha-runners/builder_access.py"))
        self.assertLess(script.index("ACTIVE_UPLOAD_FENCE"),
                        script.index("python3 -B /opt/gha-runners/builder_access.py"))
        self.assertLess(script.index("systemctl reload ssh"),
                        script.index("%%oxidex-build ALL=(root)"))
        self.assertIn("'schema':2,'enabled':True", script)
        self.assertIn("'by_id':observed['by_id'],'serial':observed['serial']",script)
        self.assertNotIn('usermod -aG docker', script)
        self.assertNotIn('--privileged', script)
        for path, content in ASSETS.items():
            if path.startswith('/opt/oxidex-builder/image/'):
                self.assertEqual(content, (ROOT / 'builder/standalone' / Path(path).name).read_text())

    def test_rendered_active_ssh_fence_refuses_missing_or_wrong_policy(self):
        script = render_builder_startup(SimpleNamespace(**inputs()), 'unit-test')
        guard = script.split("python3 -B - <<'ACTIVE_UPLOAD_FENCE'\n", 1)[1].split(
            "\nACTIVE_UPLOAD_FENCE", 1)[0]
        correct = ("forcecommand /usr/local/sbin/oxidex-uploader-session\n"
                   "disableforwarding yes\npermittty no\n")
        def run_for(output, active=True):
            def observed(argv, **kwargs):
                if argv[:2] == ['systemctl', 'is-active']:
                    if not active:raise subprocess.CalledProcessError(3, argv)
                    return SimpleNamespace(stderr='', stdout='')
                self.assertEqual(argv[:2], ['/usr/sbin/sshd', '-T'])
                self.assertIn('user=oxidex-uploader', argv[-1])
                return SimpleNamespace(stderr='', stdout=output)
            with patch.object(subprocess, 'run', side_effect=observed):
                exec(compile(guard, '<rendered active uploader fence>', 'exec'), {})
        run_for(correct)
        for changed in (correct.replace('forcecommand /usr/local/sbin/oxidex-uploader-session',
                                        'forcecommand none'),
                        correct.replace('disableforwarding yes', 'disableforwarding no'),
                        correct.replace('permittty no', 'permittty yes')):
            with self.subTest(changed=changed), self.assertRaisesRegex(SystemExit, 'fence differs'):
                run_for(changed)
        with self.assertRaises(subprocess.CalledProcessError):
            run_for(correct, active=False)

    def test_actual_asset_write_pipelines_restore_all_canonical_bytes(self):
        script = render_builder_startup(SimpleNamespace(**inputs()), 'unit-test')
        self.assertEqual(script, render_builder_startup(SimpleNamespace(**inputs()), 'unit-test'))
        observed = {}
        # Execute only each emitted decoding pipeline; never parent-directory
        # creation, final absolute-path redirection or any startup side effect.
        for line in script.splitlines():
            if " | base64 -d | python3 -c " not in line:
                continue
            pipeline, destination = line.rsplit(' >', 1)
            self.assertNotIn(destination, observed)
            observed[destination] = subprocess.check_output(
                ['bash', '-euo', 'pipefail', '-c', pipeline], timeout=5)
        expected = {path: source.encode() for path, source in builder_guest_sources().items()}
        self.assertEqual(observed, expected)
        self.assertIn(b'openssh-client gnupg', observed['/opt/oxidex-builder/image/Dockerfile'])
        self.assertIn(b'Admission reply lost or incomplete', observed['/opt/gha-runners/host_admission.py'])
        self.assertIn(b'check_inspected_mounts', observed['/opt/gha-runners/host_admission.py'])

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

    def test_cli_uses_instance_identity_and_preserves_explicit_override(self):
        with tempfile.TemporaryDirectory() as parent:
            parent = Path(parent)
            config = parent / 'config.json'
            config.write_text(json.dumps(inputs()))
            for identity in (None, 'explicit-deployment'):
                output = parent / ('default' if identity is None else 'override')
                command = [sys.executable, str(ROOT / 'src/render_builder_startup.py'),
                           '--config', str(config), '--output-directory', str(output)]
                if identity is not None:
                    command.extend(['--deployment-id', identity])
                result = subprocess.run(command, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), str(output / 'launch-command.sh'))
                expected = render_builder_startup(SimpleNamespace(**inputs()),
                                                  identity or 'builder-test')
                self.assertEqual((output / 'startup.sh').read_text(), expected)

    def test_cli_mapping_file_matches_embedded_config_and_refuses_conflicts(self):
        with tempfile.TemporaryDirectory() as parent:
            parent = Path(parent)
            original = inputs()
            mapping = parent / 'uploader.json'
            mapping.write_text(json.dumps(original['authorized_uploader']))
            config = parent / 'config.json'
            minimal = {key: value for key, value in original.items()
                       if key not in ('authorized_uploader', 'authorized_uploader_sha256')}
            config.write_text(json.dumps(minimal))
            output = parent / 'payload'
            command = [sys.executable, str(ROOT / 'src/render_builder_startup.py'),
                       '--config', str(config), '--uploader-mapping', str(mapping),
                       '--output-directory', str(output)]
            subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual((output / 'startup.sh').read_text(),
                             render_builder_startup(SimpleNamespace(**original), 'builder-test'))
            config.write_text(json.dumps(original))
            conflict_output = parent / 'conflict'
            command[-1] = str(conflict_output)
            conflict = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(conflict.returncode, 2)
            self.assertIn('not both', conflict.stderr)
            self.assertFalse(conflict_output.exists())
            config.write_text(json.dumps(minimal))
            invalid = dict(original['authorized_uploader'], public_key_sha256='0' * 64)
            mapping.write_text(json.dumps(invalid))
            refused = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(conflict_output.exists())

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
