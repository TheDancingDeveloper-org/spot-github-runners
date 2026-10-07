"""Tiny configuration controls; no provider, compilation, or guest effects."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from render_builder_startup import builder_config, render_files
from test_dedicated_builder_startup import inputs

class BuilderConfigDefaultsTests(unittest.TestCase):
    def test_minimal_config_retains_identity_without_mutating_input(self):
        original = {'instance_name': 'builder-test', 'project': 'test-project',
                    'zone': 'asia-east2-a', 'authorized_uploader': {'test': True},
                    'authorized_uploader_sha256': 'explicit'}
        result = builder_config(original)
        self.assertEqual(result['cpu'], 16)
        self.assertEqual(result['builder_memory_gib'], 28)
        self.assertEqual(result['runner_count'], 0)
        self.assertEqual(result['disk_device_name'], 'builder-cache')
        self.assertEqual(result['authorized_uploader_sha256'], 'explicit')
        self.assertNotIn('cpu', original)

    def test_explicit_values_remain_for_existing_validator(self):
        self.assertEqual(builder_config({'cpu': 32})['cpu'], 32)
        self.assertIsNone(builder_config({'cpu': None})['cpu'])
        self.assertEqual(builder_config({'data_disk_iops': 4000,
                                        'data_disk_throughput': 250})['data_disk_iops'], 4000)

    def test_identity_and_authority_are_never_defaulted(self):
        result = builder_config({})
        for field in ('instance_name', 'project', 'zone', 'authorized_uploader',
                      'authorized_uploader_sha256'):
            self.assertNotIn(field, result)

    def test_minimal_and_full_configs_render_identical_payloads(self):
        full = inputs()
        minimal = {key: full[key] for key in ('instance_name', 'project', 'zone',
                   'authorized_uploader', 'authorized_uploader_sha256')}
        with tempfile.TemporaryDirectory() as parent:
            a = render_files(full, 'comparison', Path(parent) / 'full')
            b = render_files(minimal, 'comparison', Path(parent) / 'minimal')
            for name in ('startup.sh', 'cloud-config.yaml'):
                self.assertEqual((a / name).read_bytes(), (b / name).read_bytes())
            self.assertEqual((a / 'launch-command.sh').read_text().replace(str(a), '<output>'),
                             (b / 'launch-command.sh').read_text().replace(str(b), '<output>'))

    def test_invalid_override_refuses_before_output_creation(self):
        for key, value in (('cpu', 32), ('ram', None), ('runner_count', 1),
                           ('authorized_uploader_sha256', '0' * 64)):
            config = inputs()
            config[key] = value
            with self.subTest(key=key), tempfile.TemporaryDirectory() as parent:
                output = Path(parent) / 'payload'
                with self.assertRaises(ValueError):
                    render_files(config, 'invalid', output)
                self.assertFalse(output.exists())

if __name__ == '__main__':
    unittest.main()
