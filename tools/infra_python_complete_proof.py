"""Complete signed-source unittest measurement for the neutral CI profile."""
import hashlib
import json
import unittest
import os
from pathlib import Path
import stat
import sys
import types

PRODUCER_SHA = '14f70ae8939ef533f86cddc6ce759972dea73f700e453421c635e2dcf0e0a6ff'

def load_producer():
    path = Path(__file__).with_name('infra_python_test_proof.py')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('Producer must be a regular file')
        data = source.read(262145)
    if len(data) > 262144 or hashlib.sha256(data).hexdigest() != PRODUCER_SHA:
        raise ValueError('Reviewed producer bytes changed')
    module = types.ModuleType('neutral_infra_producer')
    module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module

def measure(module, *, case_floor=774, module_floor=76):
    _, _, names = module._source_snapshot()
    module.MIN_CASES = max(case_floor, len(names))
    module.MIN_MODULES = max(module_floor, len(names))
    module.RUNNER_ARGV = ['python3', '-B', 'tools/infra_python_complete_proof.py']
    # Private backend namespace: tests cannot change global unittest bindings
    # through this adapter. Capture real classes before executing any test.
    loader_class = unittest.TestLoader
    runner_class = unittest.TextTestRunner
    result_class = unittest.TextTestResult
    state = {'discovered': None, 'executed': [], 'result': None}

    class IdentityLoader(loader_class):
        def discover(self, *args, **kwargs):
            suite = super().discover(*args, **kwargs)
            ids = [case.id() for case in module._cases(suite)]
            if len(ids) != len(set(ids)):
                raise ValueError('Duplicate discovered test identity')
            state['discovered'] = tuple(ids)
            return suite

    class IdentityResult(result_class):
        def startTest(self, test):
            state['executed'].append(test.id())
            super().startTest(test)

    class IdentityRunner(runner_class):
        def _makeResult(self):
            result = IdentityResult(self.stream, self.descriptions, self.verbosity)
            state['result'] = result
            return result

    module.unittest = types.SimpleNamespace(
        TestLoader=IdentityLoader, TextTestRunner=IdentityRunner,
        TestCase=unittest.TestCase, TestSuite=unittest.TestSuite)
    publish = module._publish

    def publish_complete(directory_fd, name, data):
        discovered, executed = state['discovered'], state['executed']
        result = state['result']
        if (discovered is None or result is None or len(executed) != len(set(executed))
                or set(executed) != set(discovered)
                or len(executed) != len(discovered)
                or len(executed) != result.testsRun):
            raise ValueError('Executed test identities differ from discovery')
        proof = json.loads(data)
        digest = lambda ids: hashlib.sha256(json.dumps(sorted(ids), separators=(',', ':')).encode()).hexdigest()
        proof['discovered_test_ids_sha256'] = digest(discovered)
        proof['executed_test_ids_sha256'] = digest(executed)
        data = (json.dumps(proof, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
        if len(data) > module.MAX_PROOF:
            raise ValueError('Complete proof oversized')
        publish(directory_fd, name, data)

    module._publish = publish_complete
    return module.run()

if __name__ == '__main__':
    try:
        sys.exit(measure(load_producer()))
    except Exception as error:
        print(f'Complete infra test proof refused: {error}', file=sys.stderr)
        sys.exit(2)
