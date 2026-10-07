"""Bounded source/probe controls; no Node, browser, image or application run."""
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROBE = runpy.run_path(str(ROOT / 'builder/standalone/toolchain_probe.py'))


class WebsiteToolsTests(unittest.TestCase):
    def record(self):
        return dict(node='v24.21.0', playwright='1.63.0', chromium_revision='1243',
                    chromium_version='153.0.8010.12', browser_path='/opt/playwright-browsers',
                    uid=1001, executable_readable=True, libraries_available=True,
                    root_owned_runtime=True, runtime_path=PROBE['WEBSITE_PATH'])

    def test_rejects_version_path_identity_and_render_mismatch(self):
        self.assertEqual(PROBE['validate_website_tools'](self.record()), self.record())
        for key in self.record():
            row = self.record(); row[key] = 'wrong'
            with self.subTest(key=key), self.assertRaises(ValueError):
                PROBE['validate_website_tools'](row)

    def test_locked_package_pair_and_browser_install_in_image(self):
        folder = ROOT / 'builder/standalone'
        package = json.loads((folder / 'website-package.json').read_text())
        lock = json.loads((folder / 'website-package-lock.json').read_text())
        self.assertEqual(package['dependencies'], {'playwright': '1.63.0'})
        self.assertEqual(set(lock['packages']), {'', 'node_modules/playwright', 'node_modules/playwright-core'})
        for name in ('playwright', 'playwright-core'):
            row = lock['packages']['node_modules/' + name]
            self.assertEqual(row['version'], '1.63.0')
            self.assertTrue(row['integrity'].startswith('sha512-'))
        dockerfile = (folder / 'Dockerfile').read_text()
        self.assertIn('npm ci --ignore-scripts', dockerfile)
        self.assertIn('install --with-deps chromium', dockerfile)
        self.assertIn('PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers', dockerfile)
        self.assertIn('/opt/node/bin:/opt/build-tools', dockerfile)
        self.assertIn('--no-same-owner', dockerfile)
        self.assertIn('chown -hR 0:0 /opt/node', dockerfile)
        self.assertIn('chmod -R go-w /opt/node', dockerfile)

    def test_browser_dependency_probe_validates_actual_output(self):
        with patch.dict(PROBE['website_tools'].__globals__, {'verify_runtime_tools': lambda: None}), \
                patch('subprocess.check_output', return_value=json.dumps(self.record())) as run:
            self.assertEqual(PROBE['website_tools'](), self.record())
            self.assertEqual(run.call_args.kwargs['timeout'], 15)
            self.assertEqual(run.call_args.kwargs['env']['PATH'], PROBE['WEBSITE_PATH'])

    def test_website_children_do_not_inherit_shadow_path_or_node_options(self):
        import os
        with patch.dict(os.environ, {'PATH': '/home/runner/.cargo/bin:/usr/bin',
                                     'NODE_OPTIONS': '--require=/target/untrusted.js'}):
            env = PROBE['website_environment']()
            self.assertEqual(env['PATH'], PROBE['WEBSITE_PATH'])
            self.assertNotIn('NODE_OPTIONS', env)

    def test_unowned_runtime_refused_before_node_execution(self):
        def refuse():
            raise ValueError('runtime owned by archive UID')
        with patch.dict(PROBE['website_tools'].__globals__, {'verify_runtime_tools': refuse}), \
                patch('subprocess.check_output') as run:
            with self.assertRaises(ValueError):
                PROBE['website_tools']()
            run.assert_not_called()

    def test_runtime_path_rejects_archive_uid_and_writable_component(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            original = Path.lstat
            for uid, writable, accepted in ((0, False, True), (1001, False, False), (0, True, False)):
                def metadata(path):
                    info = original(path)
                    mode = info.st_mode & ~0o022
                    if path == folder and writable:
                        mode |= 0o020
                    return SimpleNamespace(st_uid=uid if path == folder else 0, st_mode=mode)
                with self.subTest(uid=uid, writable=writable), patch.object(Path, 'lstat', metadata):
                    self.assertEqual(PROBE['root_owned_runtime_path'](folder), accepted)

    def test_probe_does_not_start_a_browser_session(self):
        import inspect
        source = inspect.getsource(PROBE['website_tools'])
        self.assertNotIn('chromium.launch', source)
        self.assertIn("['--version']", source)
        self.assertIn("'/usr/bin/ldd'", source)
