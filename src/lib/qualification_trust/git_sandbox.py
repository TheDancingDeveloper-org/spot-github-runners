"""Host-owned isolated helper; launched as nobody in one bounded process group."""
import json
import os
from pathlib import Path, PurePosixPath
import resource
import subprocess
import sys

MAX_OBJECT = 256 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
MAX_OBJECTS = 100000
GIT = '/usr/bin/git'


def limits():
    # Inherited by Git/index-pack/cat-file, not merely the Python parent.
    for key, value in ((resource.RLIMIT_AS, 1024 * 1024 * 1024),
                       (resource.RLIMIT_CPU, 90), (resource.RLIMIT_FSIZE, 512 * 1024 * 1024),
                       (resource.RLIMIT_NOFILE, 128), (resource.RLIMIT_NPROC, 32),
                       (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(key, (value, value))


def clean_environment():
    options = {'core.hooksPath': '/dev/null', 'core.fsmonitor': 'false',
               'protocol.allow': 'never', 'protocol.file.allow': 'always',
               'gc.auto': '0', 'maintenance.auto': 'false', 'pack.threads': '1',
               'pack.windowMemory': '32m', 'pack.packSizeLimit': '256m',
               'gpg.ssh.program': '/usr/bin/ssh-keygen'}
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_CONFIG_COUNT': str(len(options))}
    for index, (key, value) in enumerate(options.items()):
        env['GIT_CONFIG_KEY_' + str(index)] = key
        env['GIT_CONFIG_VALUE_' + str(index)] = value
    return env


def safe_tree_entries(raw):
    rows = []
    for row in raw.split(b'\0'):
        if not row:
            continue
        header, path = row.split(b'\t', 1)
        mode, kind, oid = header.split()
        name = path.decode('utf-8')
        parts = PurePosixPath(name).parts
        if (mode not in (b'100644', b'100755') or kind != b'blob'
                or not parts or name.startswith('/') or '\\' in name
                or any(part in ('', '.', '..', '.git') for part in name.split('/'))
                or any(part == '__pycache__' for part in parts)
                or name.endswith(('.pyc', '.pyo'))):
            raise ValueError('Unapproved source tree mode, path, submodule or bytecode')
        rows.append((name, oid.decode('ascii'), 0o755 if mode == b'100755' else 0o644))
        if len(rows) > MAX_OBJECTS:
            raise ValueError('Source tree entry limit')
    return rows


def import_source(incoming, work, head, bundle_sha, run_id, anchor, *, verifier_module=None):
    env = clean_environment()
    os.environ.clear(); os.environ.update(env)
    repo = work / 'repository.git'
    def git(*args, limit=MAX_OBJECT):
        # RLIMIT_AS bounds subprocess and parent allocations; output also has a fixed ceiling.
        output = subprocess.check_output([GIT, *args], env=env, timeout=60)
        if len(output) > limit:
            raise ValueError('Git response limit')
        return output
    git('init', '--bare', '--template=', str(repo))
    git('-C', str(repo), '-c', 'fetch.fsckObjects=true', 'fetch', '--no-tags',
        '--no-recurse-submodules', str(incoming / 'repository.bundle'), head)
    # Bound every unpacked object's declared size, including history unrelated to HEAD.
    inventory = git('-C', str(repo), 'cat-file', '--batch-all-objects',
                    '--batch-check=%(objectname) %(objecttype) %(objectsize)', limit=16*1024*1024)
    total = 0
    for count, row in enumerate(inventory.splitlines(), 1):
        oid, kind, size = row.split(); size = int(size)
        total += size
        if count > MAX_OBJECTS or size > MAX_OBJECT or total > MAX_EXPANDED:
            raise ValueError('Expanded bundle object limit')
    # Load only the installed module next to this installed helper, never incoming.
    if verifier_module is None:
        import importlib.util
        module_path = Path(__file__).with_name('qualification_source.py')
        spec = importlib.util.spec_from_file_location('trusted_source', module_path)
        verifier = importlib.util.module_from_spec(spec); spec.loader.exec_module(verifier)
    else:
        verifier = verifier_module
    attestation = incoming / 'source-attestation'
    facts = verifier.verify_source(repo, head, anchor, attestation if attestation.exists() else None,
                                   bundle_sha256=bundle_sha, run_id=run_id)
    entries = safe_tree_entries(git('-C', str(repo), 'ls-tree', '-rz', '--full-tree', head))
    checkout = work / 'checkout'; checkout.mkdir(mode=0o755)
    for name, oid, mode in entries:
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # No checkout/smudge hooks, attributes, submodules, or filter drivers run.
        with target.open('xb') as output:
            output.write(git('-C', str(repo), 'cat-file', 'blob', oid))
        target.chmod(mode)
    (repo / 'HEAD').write_text(head + '\n')
    # Runtime Git index must match the authenticated immutable worktree.
    git('--git-dir=' + str(repo), 'read-tree', head)
    (work / 'identity.json').write_text(json.dumps(facts, sort_keys=True) + '\n')


if __name__ == '__main__':
    if os.geteuid() == 0:
        raise SystemExit('Never import a bundle as root')
    limits()
    incoming, work, head, bundle_sha, run_id, anchor = sys.argv[1:]
    import_source(Path(incoming), Path(work), head, bundle_sha, run_id, Path(anchor))
