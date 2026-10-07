"""Bounded client handoff controls; no cloud or build effects."""
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from render_builder_startup import render_files
from test_dedicated_builder_startup import inputs

class BuilderClientConfigTests(unittest.TestCase):
    def test_renderer_emits_endpoint_without_inventing_live_identity(self):
        with tempfile.TemporaryDirectory() as parent:
            out = render_files(inputs(), 'client-config', Path(parent) / 'payload')
            text = (out / 'cargo-remote-env.toml').read_text()
            env = tomllib.loads(text)['env']
            self.assertEqual(env, {'OXIDEX_REMOTE_PROJECT':'test-project',
                'OXIDEX_REMOTE_INSTANCE':'builder-test',
                'OXIDEX_REMOTE_ZONE':'asia-east2-a',
                'OXIDEX_REMOTE_SSH_USER':'oxidex-uploader'})
            self.assertIn('OXIDEX_REMOTE_INSTANCE_ID', text)
            self.assertIn('OXIDEX_REMOTE_SSH_KEY', text)
            self.assertIn('OXIDEX_REMOTE_SSH_KNOWN_HOSTS', text)
            self.assertEqual((out / 'cargo-remote-env.toml').stat().st_mode & 0o777, 0o600)

if __name__ == '__main__':
    unittest.main()
