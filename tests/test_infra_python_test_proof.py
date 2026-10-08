"""Small real unittest controls for the fixed producer; never run the repo suite."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock as mock


RUNNER = Path(__file__).resolve().parents[1] / 'tools' / 'infra_python_test_proof.py'
spec = importlib.util.spec_from_file_location('infra_python_test_proof', RUNNER)
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)
HEAD = 'a' * 40


class ProducerControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'src'
        self.target = self.root / 'target'
        (self.source / 'tests').mkdir(parents=True)
        self.target.mkdir()
        (self.source / 'fleet-source-head').write_text(HEAD + '\n')
        self.old_cwd = Path.cwd()
        self.old_sys_path = sys.path[:]
        self.addCleanup(self._restore_imports)
        os.chdir(self.source)
        self.addCleanup(os.chdir, self.old_cwd)
        self.patchers = [mock.patch.object(producer, 'SOURCE', self.source),
                         mock.patch.object(producer, 'TARGET', self.target),
                         mock.patch.object(producer, 'HEAD', self.source / 'fleet-source-head'),
                         mock.patch.object(producer, 'MANIFEST', self.source / 'infra-python-test-manifest.json'),
                         mock.patch.object(producer, 'MIN_CASES', 2),
                         mock.patch.object(producer, 'MIN_MODULES', 2)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.modules = {
            'test_alpha': 'import unittest\nclass Alpha(unittest.TestCase):\n def test_one(self): self.assertTrue(True)\n',
            'test_beta': 'import unittest\nclass Beta(unittest.TestCase):\n def test_two(self): self.assertEqual(2, 2)\n',
        }
        self.write_modules()

    def _restore_imports(self):
        sys.path[:] = self.old_sys_path
        for name in self.modules:
            sys.modules.pop(name, None)

    def write_modules(self):
        for name, body in self.modules.items():
            (self.source / 'tests' / f'{name}.py').write_text(body)
        entries = [{'path': f'tests/{name}.py', 'module': name,
                    'sha256': hashlib.sha256(body.encode()).hexdigest()}
                   for name, body in sorted(self.modules.items())]
        manifest = {'schema': 1, 'kind': 'infra_python_test_manifest_v1',
                    'source_commit': HEAD, 'modules': entries}
        (self.source / 'infra-python-test-manifest.json').write_text(
            json.dumps(manifest, sort_keys=True, separators=(',', ':')))

    def proof(self):
        return json.loads((self.target / producer.PROOF_NAME).read_text())

    def test_real_result_and_log(self):
        self.assertEqual(producer.run(), 0)
        proof = self.proof()
        self.assertEqual((proof['status'], proof['discovered_tests'], proof['tests_run']), ('PASS', 2, 2))
        self.assertEqual(proof['module_counts'], {'test_alpha': 1, 'test_beta': 1})
        self.assertEqual(proof['diagnostic_log_sha256'], hashlib.sha256(
            (self.target / producer.LOG_NAME).read_bytes()).hexdigest())
        self.assertEqual(proof['runner_argv'], producer.RUNNER_ARGV)
        self.assertEqual(proof['discovery']['top_level_dir'], None)
        self.assertEqual((self.target / producer.PROOF_NAME).stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.target / producer.LOG_NAME).stat().st_mode & 0o777, 0o644)

    def test_real_failure_cannot_pass(self):
        self.modules['test_beta'] = 'import unittest\nclass Beta(unittest.TestCase):\n def test_two(self): self.fail("real failure")\n'
        self.write_modules()
        self.assertEqual(producer.run(), 1)
        self.assertEqual((self.proof()['status'], self.proof()['failures'], self.proof()['exit_code']), ('FAIL', 1, 1))

    def test_skip_cannot_pass(self):
        self.modules['test_beta'] = 'import unittest\nclass Beta(unittest.TestCase):\n @unittest.skip("skip")\n def test_two(self): pass\n'
        self.write_modules()
        self.assertEqual(producer.run(), 1)
        self.assertEqual(self.proof()['skipped'], 1)

    def test_zero_and_omitted_module_cannot_pass(self):
        self.modules['test_beta'] = 'import unittest\n'
        self.write_modules()
        self.assertEqual(producer.run(), 1)
        self.assertEqual(self.proof()['module_counts']['test_beta'], 0)

    def test_zero_tests_cannot_pass(self):
        self.modules = {'test_alpha': 'import unittest\n', 'test_beta': 'import unittest\n'}
        self.write_modules()
        self.assertEqual(producer.run(), 1)
        self.assertEqual((self.proof()['discovered_tests'], self.proof()['status']), (0, 'FAIL'))

    def test_import_error_cannot_pass(self):
        self.modules['test_beta'] = 'raise RuntimeError("import failed")\n'
        self.write_modules()
        self.assertEqual(producer.run(), 1)
        self.assertGreater(self.proof()['errors'], 0)

    def test_duplicate_case_is_incomplete(self):
        sys.path.insert(0, str(self.source / 'tests'))
        from test_alpha import Alpha
        from test_beta import Beta
        suite = unittest.TestSuite([Alpha('test_one'), Alpha('test_one'), Beta('test_two')])
        with mock.patch.object(producer.unittest, 'TestLoader') as loader:
            loader.return_value.discover.return_value = suite
            self.assertEqual(producer.run(), 1)
        self.assertEqual(self.proof()['status'], 'FAIL')

    def test_modified_source_refused(self):
        (self.source / 'tests' / 'test_alpha.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'bytes mismatch'):
            producer.run()
        self.assertFalse((self.target / producer.PROOF_NAME).exists())

    def test_deleted_source_refused(self):
        (self.source / 'tests' / 'test_beta.py').unlink()
        with self.assertRaisesRegex(ValueError, 'membership mismatch'):
            producer.run()

    def test_duplicate_manifest_key_refused(self):
        manifest = self.source / 'infra-python-test-manifest.json'
        manifest.write_text(manifest.read_text().replace('"schema":1', '"schema":1,"schema":1'))
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            producer.run()

    def test_preexisting_and_symlink_artifacts_refused(self):
        for name, symlink in ((producer.PROOF_NAME, False), (producer.LOG_NAME, True)):
            path = self.target / name
            if symlink:
                path.symlink_to(self.source / 'fleet-source-head')
            else:
                path.write_text('forged')
            with self.assertRaises(FileExistsError):
                producer.run()
            self.assertTrue(path.is_symlink() if symlink else path.read_text() == 'forged')
            path.unlink()

    def test_fsync_failure_retains_evidence_without_proof(self):
        with mock.patch.object(producer.os, 'fsync', side_effect=OSError('fsync failed')):
            with self.assertRaisesRegex(OSError, 'fsync failed'):
                producer.run()
        self.assertTrue((self.target / producer.LOG_NAME).exists())
        self.assertFalse((self.target / producer.PROOF_NAME).exists())

    def test_source_changed_during_real_test_refused(self):
        self.modules['test_beta'] = ('import unittest\nfrom pathlib import Path\n'
            'class Beta(unittest.TestCase):\n def test_two(self):\n'
            '  Path("tests/test_alpha.py").write_text("changed")\n')
        self.write_modules()
        with self.assertRaisesRegex(ValueError, 'bytes mismatch'):
            producer.run()
        self.assertTrue((self.target / producer.LOG_NAME).exists())
        self.assertFalse((self.target / producer.PROOF_NAME).exists())


if __name__ == '__main__':
    unittest.main()
