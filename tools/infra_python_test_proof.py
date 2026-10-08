"""Fixed, source-owned unittest measurement for the admitted Linux recipe."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import unittest


SOURCE = Path('/src')
TARGET = Path('/target')
HEAD = SOURCE / 'fleet-source-head'
MANIFEST = SOURCE / 'infra-python-test-manifest.json'
PROOF_NAME = 'infra-python-test-proof.json'
LOG_NAME = 'infra-python-test.log'
MIN_CASES = 854
MIN_MODULES = 82
MAX_PROOF = 64 * 1024
MAX_LOG = 16 * 1024 * 1024
RUNNER_ARGV = ['python3', '-B', 'tools/infra_python_test_proof.py']
UNITTEST_ARGV = ['python3', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-v']
DISCOVERY = {'start_dir': 'tests', 'pattern': 'test_*.py', 'top_level_dir': None}
SHA40 = re.compile(r'[0-9a-f]{40}\Z')
SHA64 = re.compile(r'[0-9a-f]{64}\Z')
MODULE = re.compile(r'test_[A-Za-z0-9_]+\Z')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError('nonfinite JSON value')


def _read_regular(path, maximum, *, directory_fd=None, inode=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > maximum or
                (inode is not None and (info.st_dev, info.st_ino) != inode)):
            raise ValueError(f'invalid source file: {path}')
        with os.fdopen(os.dup(fd), 'rb') as file:
            data = file.read(maximum + 1)
        if len(data) > maximum or os.fstat(fd).st_size != len(data):
            raise ValueError(f'changing or oversized source file: {path}')
        return data
    finally:
        os.close(fd)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _source_snapshot():
    head_bytes = _read_regular(HEAD, 128)
    head = head_bytes.decode('ascii').removesuffix('\n')
    if not SHA40.fullmatch(head) or head_bytes not in (head.encode(), (head + '\n').encode()):
        raise ValueError('invalid source HEAD')
    raw = _read_regular(MANIFEST, MAX_PROOF)
    manifest = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_constant)
    if type(manifest) is not dict or set(manifest) != {'schema', 'kind', 'source_commit', 'modules'}:
        raise ValueError('invalid manifest fields')
    if type(manifest['schema']) is not int or manifest['schema'] != 1 or manifest['kind'] != 'infra_python_test_manifest_v1' or manifest['source_commit'] != head:
        raise ValueError('manifest HEAD mismatch')
    modules = manifest['modules']
    if type(modules) is not list or not modules:
        raise ValueError('empty manifest')
    expected = []
    names = []
    for item in modules:
        if type(item) is not dict or set(item) != {'path', 'sha256', 'module'}:
            raise ValueError('invalid manifest module')
        path, digest, name = item['path'], item['sha256'], item['module']
        if type(name) is not str or not MODULE.fullmatch(name) or path != f'tests/{name}.py' or type(digest) is not str or not SHA64.fullmatch(digest):
            raise ValueError('invalid manifest path or digest')
        expected.append(path)
        names.append(name)
    if expected != sorted(set(expected)):
        raise ValueError('unsorted or duplicate manifest modules')
    actual = sorted(f'tests/{p.name}' for p in (SOURCE / 'tests').glob('test_*.py'))
    if actual != expected:
        raise ValueError('source test membership mismatch')
    for item in modules:
        if _sha(_read_regular(SOURCE / item['path'], 16 * 1024 * 1024)) != item['sha256']:
            raise ValueError('source test bytes mismatch')
    return head, _sha(raw), names


def _cases(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from _cases(case)
        else:
            yield case


class _BoundedLog:
    def __init__(self, file):
        self.file = file
        self.size = 0

    def write(self, value):
        data = value.encode('utf-8', errors='replace')
        remaining = MAX_LOG - self.size
        if remaining > 0:
            written = self.file.write(data[:remaining])
            self.size += written
        return len(value)

    def flush(self):
        self.file.flush()


def _target_dir():
    fd = os.open(TARGET, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('invalid target directory')
    return fd


def _exclusive(directory_fd, name):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory_fd)
    os.fchmod(fd, 0o644)
    return fd


def _publish(directory_fd, name, data):
    fd = _exclusive(directory_fd, name)
    try:
        with os.fdopen(os.dup(fd), 'wb') as file:
            file.write(data)
            file.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory_fd)


def run():
    # Refuse old evidence before running any tests. O_EXCL remains the final race guard.
    directory_fd = _target_dir()
    try:
        for name in (PROOF_NAME, LOG_NAME):
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(f'preexisting artifact: {name}')
        head, manifest_digest, names = _source_snapshot()
        log_fd = _exclusive(directory_fd, LOG_NAME)
        log_info = os.fstat(log_fd)
        try:
            try:
                with os.fdopen(os.dup(log_fd), 'wb') as log_file:
                    stream = _BoundedLog(log_file)
                    suite = unittest.TestLoader().discover(**DISCOVERY)
                    discovered = suite.countTestCases()
                    counts = {name: 0 for name in names}
                    ids = set()
                    complete = True
                    for case in _cases(suite):
                        if not isinstance(case, unittest.TestCase):
                            complete = False
                            continue
                        case_id = case.id()
                        if case_id in ids:
                            complete = False
                        ids.add(case_id)
                        module = type(case).__module__
                        if module not in counts:
                            complete = False
                        else:
                            counts[module] += 1
                    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
                    stream.flush()
                    log_file.flush()
            finally:
                os.fsync(log_fd)
                os.fsync(directory_fd)
        finally:
            os.close(log_fd)
        # Recheck the signed snapshot after execution and before publishing proof.
        if _source_snapshot() != (head, manifest_digest, names):
            raise ValueError('source changed during measurement')
        if os.stat(LOG_NAME, dir_fd=directory_fd, follow_symlinks=False).st_size > MAX_LOG:
            raise ValueError('diagnostic log oversized')
        log_bytes = _read_regular(LOG_NAME, MAX_LOG, directory_fd=directory_fd,
                                  inode=(log_info.st_dev, log_info.st_ino))
        ok = (complete and discovered == len(ids) == result.testsRun
              and discovered >= MIN_CASES and len(names) >= MIN_MODULES
              and all(count > 0 for count in counts.values())
              and result.wasSuccessful() and not any((result.failures, result.errors,
                  result.skipped, result.expectedFailures, result.unexpectedSuccesses)))
        exit_code = 0 if ok else 1
        proof = {
            'schema': 1, 'kind': 'infra_python_unittest_v1',
            'source_commit': head, 'manifest_sha256': manifest_digest,
            'runner_argv': RUNNER_ARGV, 'unittest_argv': UNITTEST_ARGV,
            'discovery': DISCOVERY, 'python_executable': sys.executable,
            'discovered_tests': discovered, 'tests_run': result.testsRun,
            'module_counts': counts, 'failures': len(result.failures),
            'errors': len(result.errors), 'skipped': len(result.skipped),
            'expected_failures': len(result.expectedFailures),
            'unexpected_successes': len(result.unexpectedSuccesses),
            'success': result.wasSuccessful(), 'exit_code': exit_code,
            'status': 'PASS' if ok else 'FAIL',
            'diagnostic_log_sha256': _sha(log_bytes),
        }
        data = (json.dumps(proof, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
        if len(data) > MAX_PROOF:
            raise ValueError('proof oversized')
        _publish(directory_fd, PROOF_NAME, data)
        return exit_code
    finally:
        os.close(directory_fd)


if __name__ == '__main__':
    try:
        sys.exit(run())
    except Exception as error:
        print(f'infra Python test proof refused: {error}', file=sys.stderr)
        sys.exit(2)
