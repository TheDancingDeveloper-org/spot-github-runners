"""Installed, isolated entrypoint; only an authenticated source tree is imported."""
import os
from pathlib import Path
import re
import runpy
import sys

CHECKOUT = Path('/target/checkout')


def main(arguments):
    # Root launcher already proved exact arguments and sealed content. The root-owned
    # runtime does not inspect uploader code or add /src or writable /target to imports.
    if not arguments or arguments[0] not in ('bootstrap', 'execute'):
        raise ValueError('Unknown qualification stage')
    stage, head, run_id = arguments[:3]
    if (not re.fullmatch('[0-9a-f]{40}', head)
            or not re.fullmatch('qualification-' + head[:12] + '-[0-9a-f]{32}', run_id)):
        raise ValueError('Malformed bound execution identity')
    for key in list(os.environ):
        if key.startswith(('PYTHON', 'GIT_')):
            del os.environ[key]
    # Git may write only metadata. Never honor writable metadata for executables,
    # hooks, filesystem monitoring, replace refs or arbitrary network transports.
    options = {'safe.directory': str(CHECKOUT), 'core.hooksPath': '/dev/null',
               'core.fsmonitor': 'false', 'protocol.allow': 'never',
               'protocol.file.allow': 'always', 'protocol.https.allow': 'never', 'gpg.program': '/usr/bin/gpg',
               'gpg.ssh.program': '/usr/bin/ssh-keygen', 'core.sshCommand': '/bin/false'}
    # Readonly wrapper permits only the authenticated pinned oracle clone.
    os.environ['PATH'] = '/qualification-bin:' + os.environ.get('PATH', '/usr/bin:/bin')
    os.environ.update(GIT_CONFIG_GLOBAL='/dev/null',
                      GIT_NO_REPLACE_OBJECTS='1', GIT_CONFIG_COUNT=str(len(options)),
                      PYTHONDONTWRITEBYTECODE='1', OXIDEX_OPS_DIR='/target/ops',
                      OXIDEX_TARGET_ROOT='/target')
    for index, (key, value) in enumerate(options.items()):
        os.environ['GIT_CONFIG_KEY_' + str(index)] = key
        os.environ['GIT_CONFIG_VALUE_' + str(index)] = value
    os.chdir('/')
    sys.dont_write_bytecode = True
    sys.path[:] = [str(CHECKOUT / 'tools/remote-build'),
                   *[p for p in sys.path if p.startswith(('/usr/', '/opt/'))]]
    if stage == 'bootstrap':
        output = '/target/ops/evidence/remote-qualification/' + run_id
        args = ['--prepare-inputs', '--reference', '/src/reference', '--output', output, '--expected-head', head]
    else:
        args = arguments[3:]
    sys.argv = [str(CHECKOUT / 'tools/remote-build/qualification.py'), *args]
    runpy.run_path(sys.argv[0], run_name='__main__')


if __name__ == '__main__':
    main(sys.argv[1:])
