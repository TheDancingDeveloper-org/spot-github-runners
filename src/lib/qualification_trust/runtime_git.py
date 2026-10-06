#!/usr/bin/python3 -I
"""Readonly Git entrypoint: one HTTPS exception for the pinned oracle clone.

All other Git operations retain the runtime's transport deny policy. The one
exception ignores every mutable Git config/environment and runs outside any
repository, into the consumer's freshly created empty oracle staging directory.
"""
import json
import os
from pathlib import Path
import re
import sys

CHECKOUT = Path('/target/checkout')
ORIGIN = 'https://github.com/exiftool/exiftool.git'


def oracle_clone(arguments):
    if len(arguments) != 4 or arguments[:3] != ['clone', '--no-checkout', ORIGIN]:
        raise ValueError('Only exact pinned oracle clone is allowed')
    pin = (CHECKOUT / '.exiftool-version').read_text().strip()
    item = json.loads((CHECKOUT / 'tools/release/oracle-lock.json').read_text())['exiftool']
    if (not re.fullmatch(r'[0-9]+\.[0-9]+', pin) or item['version'] != pin
            or item['repo'] != ORIGIN or not re.fullmatch('[0-9a-f]{40}', item['tag_object'])):
        raise ValueError('Authenticated oracle identity differs')
    destination = Path(arguments[3])
    parent = Path('/target/ops/cache/exiftool') / pin
    if (destination.parent != parent or not re.fullmatch(r'\.exiftool\.staging-[0-9]+-[A-Za-z0-9_-]+', destination.name)
            or destination.resolve() != destination or not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('Oracle clone requires fresh empty staging under pinned cache')
    # Executables/config come only from the image. In particular, do not retain
    # GIT_CONFIG_*, GIT_EXEC_PATH, GIT_SSL_*, credential helpers or URL rewrites.
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1'}
    policy = ['core.hooksPath=/dev/null', 'core.fsmonitor=false', 'core.sshCommand=/bin/false',
              'protocol.allow=never', 'protocol.https.allow=always', 'http.followRedirects=false',
              'http.sslVerify=true', 'credential.helper=', 'fetch.fsckObjects=true']
    return ['/usr/bin/git', *[v for option in policy for v in ('-c', option)],
            'clone', '--template=', '--no-checkout', ORIGIN, str(destination)], env


def main(arguments):
    if arguments[:1] == ['clone'] and any('://' in item for item in arguments):
        command, env = oracle_clone(arguments)
        os.chdir('/')
        os.execve('/usr/bin/git', command, env)
    else:
        os.execve('/usr/bin/git', ['/usr/bin/git', *arguments], dict(os.environ))


if __name__ == '__main__':
    main(sys.argv[1:])
