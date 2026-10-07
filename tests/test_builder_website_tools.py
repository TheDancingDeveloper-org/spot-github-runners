"""Bounded source/probe controls; no Node, browser, image or application run."""
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROBE = runpy.run_path(str(ROOT / 'builder/standalone/toolchain_probe.py'))


class WebsiteToolsTests(unittest.TestCase):
    def record(self):
        return dict(node='v24.21.0', playwright='1.63.0', chromium_revision='1243',
                    chromium_version='153.0.8010.12', browser_path='/opt/playwright-browsers',
                    uid=1001, executable_readable=True, libraries_available=True)

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

    def test_browser_dependency_probe_validates_actual_output(self):
        with patch('subprocess.check_output', return_value=json.dumps(self.record())) as run:
            self.assertEqual(PROBE['website_tools'](), self.record())
            self.assertEqual(run.call_args.kwargs['timeout'], 15)

    def test_probe_does_not_start_a_browser_session(self):
        import inspect
        source = inspect.getsource(PROBE['website_tools'])
        self.assertNotIn('chromium.launch', source)
        self.assertIn("['--version']", source)
        self.assertIn("'/usr/bin/ldd'", source)
