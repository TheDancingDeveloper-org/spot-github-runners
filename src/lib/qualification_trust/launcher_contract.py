"""Host-owned sealed qualification lifecycle under namespace and cache locks."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
from .host_gate import (copy_sealed, verify_sealed, execution_arguments, regular_bytes,
                        tree_manifest, write_owned, validate_identity, PROTECTIVE_FDS)
from .mounts import qualification_project, volume_arguments


def trusted_directory(path):
    if (path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_dir() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
        raise ValueError('Untrusted sealed namespace ancestor')


def load_binding(sealed, project):
    trusted_directory(sealed)
    binding = json.loads(regular_bytes(sealed / 'execution-binding.json', 32 * 1024 * 1024))
    validate_identity(binding['head'], binding['bundle_sha256'], binding['run_id'])
    if binding.get('schema') != 2 or binding['run_id'] != project:
        raise ValueError('Host binding differs from namespace')
    manifest = {name: tree_manifest(sealed / name) for name in ('incoming', 'checkout', 'repository.git')}
    if manifest != binding.get('manifest'):
        raise ValueError('Sealed source manifest differs from successful admission')
    return binding


def create_binding(root, project, head, bundle_sha):
    trust = root / 'qualification-sealed'
    trusted_directory(trust)
    sealed = trust / project
    # An existing namespace cannot be re-admitted or replaced. Invalidate any old
    # authorization first; explicit cleanup is required before another attempt.
    if sealed.exists() or sealed.is_symlink():
        trusted_directory(sealed)
        (sealed / 'execution-binding.json').unlink(missing_ok=True)
        raise ValueError('Qualification namespace already attempted; cleanup required')
    stage = Path(tempfile.mkdtemp(prefix='.' + project + '-', dir=trust))
    stage.chmod(0o755)
    try:
        copy_sealed(root / 'sources' / project, stage / 'incoming')
        binding = verify_sealed(stage, head, bundle_sha, project)
        binding['manifest'] = {name: tree_manifest(stage / name) for name in ('incoming', 'checkout', 'repository.git')}
        # Mutable Git state is separate from the authority repo. It is never read by
        # root Git. All stage execution is UID1001 with immutable source mounts.
        metadata = stage / 'runtime-git'
        shutil.copytree(stage / 'repository.git', metadata)
        # Remove bare-only host paths/config. Preserve only object/index/ref data.
        config = metadata / 'config'; config.chmod(0o600)
        config.write_text('[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tlogallrefupdates = true\n'
                          '[gpg]\n\tformat = ssh\n[gpg "ssh"]\n\tallowedSignersFile = /src/maintainer.allowed_signers\n')
        for path in [metadata, *metadata.rglob('*')]:
            if path.is_symlink():
                raise ValueError('Unsafe runtime metadata')
            os.chown(path, 1001, 1001)
            path.chmod(0o700 if path.is_dir() else 0o600)
        write_owned(stage / 'execution-binding.json', (json.dumps(binding, sort_keys=True) + '\n').encode())
        # Single publication point. A failed import/copy/verifier never creates sealed.
        stage.rename(sealed)
        fd = os.open(trust, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return binding
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def assert_not_mounted(sealed, inventory):
    """A complete actual Docker inventory must prove every sealed submount absent."""
    if not isinstance(inventory, list):
        raise ValueError('Unknown Docker mount inventory')
    for row in inventory:
        if not isinstance(row, dict) or not isinstance(row.get('Mounts'), list):
            raise ValueError('Incomplete Docker mount inventory')
        for mount in row['Mounts']:
            if not isinstance(mount, dict) or not isinstance(mount.get('Source'), str):
                raise ValueError('Malformed Docker mount inventory')
            source = Path(mount['Source'])
            if source == sealed or source.is_relative_to(sealed):
                raise ValueError('Qualification sealed state still mounted')


def cleanup(root, project, inventory):
    sealed = root / 'qualification-sealed' / project
    assert_not_mounted(sealed, inventory)
    # Interrupted admissions can leave unpublished staging trees. They are owned
    # by this exact namespace and are reclaimed only under the same admission lock.
    candidates = [sealed, *(root / 'qualification-sealed').glob('.' + project + '-*')]
    for candidate in candidates:
        if candidate.exists():
            trusted_directory(candidate)
            assert_not_mounted(candidate, inventory)
            # Only owned sealed source/metadata. /target evidence remains.
            shutil.rmtree(candidate)


def dispatch(root, project, arguments, container_command, config, execute, *, protective_fds=()):
    if not qualification_project(project):
        raise ValueError('Qualification run namespace required')
    trust = root / 'qualification-sealed'
    trusted_directory(trust)
    fd = os.open('/run/oxidex-qualification-' + project + '.lock',
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    token = PROTECTIVE_FDS.set((*protective_fds, fd))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sealed = trust / project
        if arguments[:1] == ['verify-source']:
            if len(arguments) != 7 or arguments[1::2] != ['--head', '--bundle-sha256', '--run-id'] or arguments[6] != project:
                raise ValueError('Exact verify-source arguments required')
            return {'verified': True, 'binding': create_binding(root, project, arguments[2], arguments[4])}
        if not sealed.exists() and arguments[:2] == ['python3', '/src/qualification_bootstrap.py']:
            if (len(arguments) != 10 or arguments[2::2] != ['--head', '--output', '--bundle-sha256', '--signers-sha256']
                    or (root / 'sources' / project / 'source-attestation').exists()):
                raise ValueError('Only exact strict-SSH bootstrap supports implicit admission')
            # Prevalidate all arguments before the implicit gate creates state.
            provisional = dict(head=arguments[3], run_id=project, bundle_sha256=arguments[7],
                               uploaded_signers_sha256=arguments[9])
            validate_identity(arguments[3], arguments[7], project)
            execution_arguments(arguments, provisional)
            create_binding(root, project, arguments[3], arguments[7])
        binding = load_binding(sealed, project)
        execution_arguments(arguments, binding)
        stage = 'bootstrap' if arguments[1] == '/src/qualification_bootstrap.py' else 'execute'
        safe = ['python3', '-I', '-B', '/qualification-runtime.py', stage, binding['head'], project]
        if stage == 'execute':
            safe += arguments[2:]
        invocation = container_command(project, safe, config['cpus'], config['memory_gib'], config['image'], root=root)
        image_index = invocation.index(config['image'])
        before = invocation[:image_index]
        replaced = []; index = 0
        while index < len(before):
            if before[index] == '-v':
                index += 2
            else:
                replaced.append(before[index]); index += 1
        invocation = replaced + volume_arguments(root, project) + invocation[image_index:]
        return execute(invocation)
    finally:
        PROTECTIVE_FDS.reset(token)
        os.close(fd)
