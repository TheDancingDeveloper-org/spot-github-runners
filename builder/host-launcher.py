#!/usr/bin/env python3
"""Root-owned remote builder launcher: fixed mounts, image, UID and limits."""
import json
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path('/mnt/runner-data/remote-build')
ALLOWED = {'cargo', 'rustc', 'rustfmt', 'clippy-driver', 'just', 'uv', 'python3', 'cbindgen'}


def validate_project(project):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', project):
        raise ValueError('Project must be a simple identifier, never a path')
    return project


def container_command(project, command, cpus, memory_gib):
    validate_project(project)
    if not command or command[0] not in ALLOWED:
        raise ValueError('Unsupported build command')
    if cpus < 1 or memory_gib < 1:
        raise ValueError('Resource limits must be positive')
    return ['docker', 'run', '--rm', '--user=1001:1001', f'--cpus={cpus}',
            '--label=oxidex.remote-build=true',
            f'--memory={memory_gib}g', f'--memory-swap={memory_gib}g', '--pids-limit=4096',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--entrypoint=/usr/bin/time',
            '-v', f'{ROOT}/sources/{project}:/src',
            '-v', f'{ROOT}/targets/{project}:/target', '-v', f'{ROOT}/cargo:/cargo',
            'oxidex-remote-builder:1.97.1', '-v', '/usr/bin/python3',
            '/usr/local/bin/oxidex-build-entrypoint', *command]


def checked_path(path):
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlinked builder paths are forbidden')


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Invoke this root-owned launcher with sudo')
    project = validate_project(sys.argv[1])
    config = json.loads(Path('/etc/oxidex-remote-build.json').read_text())
    for path in (ROOT, ROOT/'sources', ROOT/'targets', ROOT/'cargo',
                 ROOT/'sources'/project, ROOT/'targets'/project):
        checked_path(path)
        path.mkdir(parents=True, exist_ok=True)
    if sys.argv[2:] == ['prepare']:
        # SSH uploader and container user share access without world-writable data.
        source = ROOT/'sources'/project
        os.chown(source, int(os.environ.get('SUDO_UID', '1001')), 1001)
        source.chmod(0o2770)
        for path in (ROOT/'targets'/project, ROOT/'cargo'):
            os.chown(path, 1001, 1001)
        print(source)
        return
    command = sys.argv[2:] or ['cargo', 'build', '--release', '--locked', '--bin', 'oxidex']
    with open('/run/oxidex-build.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        if Path('/run/oxidex-build-draining').exists():
            raise SystemExit(75)
        raise SystemExit(subprocess.run(container_command(project, command, config['cpus'], config['memory_gib'])).returncode)


if __name__ == '__main__':
    main()
