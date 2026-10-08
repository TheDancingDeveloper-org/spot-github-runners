"""Root-owned admission of qualification sources; never execute uploaded code."""
import hashlib
import fcntl
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from . import qualification_source as verifier

MODULE_SHA = 'b895fd0614537eda9fd8cb922964b779baca50e5eeccfe80f7177f381ba31933'
ANCHOR = Path('/etc/oxidex-remote-build/qualification-maintainer.allowed_signers')
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 100000
PROTECTIVE_FDS = ContextVar('qualification_protective_fds', default=())
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def regular_bytes(path, limit=MAX_FILE):
    fd = os.open(path, READ_FLAGS)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('Unsafe or oversized source file')
        chunks = []; size = 0
        while block := os.read(fd, min(1024 * 1024, limit + 1 - size)):
            size += len(block)
            if size > limit:
                raise ValueError('Source file grew beyond limit')
            chunks.append(block)
        if identity(os.fstat(fd)) != identity(before) or size != before.st_size:
            raise ValueError('Source file changed during copy')
        return b''.join(chunks)
    finally:
        os.close(fd)


def write_owned(path, data, mode=0o444):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def validate_identity(head, bundle_sha, run_id):
    if (not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40}', head)
            or not isinstance(bundle_sha, str) or not re.fullmatch('[0-9a-f]{64}', bundle_sha)):
        raise ValueError('Full source and bundle identities required')
    if not isinstance(run_id, str) or not re.fullmatch('qualification-' + head[:12] + '-[0-9a-f]{32}', run_id):
        raise ValueError('Run identity mismatch')


def copy_sealed(source, destination):
    """Descriptor-relative, streaming bounded copy into an exclusively new tree."""
    source = Path(source)
    if source.resolve() != source:
        raise ValueError('Symlinked upload ancestor')
    top = os.open(source, READ_FLAGS | os.O_DIRECTORY)
    count = [0]; total = [0]; device = os.fstat(top).st_dev
    def copy(directory, target, depth):
        if depth > 64:
            raise ValueError('Source depth bound')
        target.mkdir(mode=0o755); target.chmod(0o755)
        before_dir = identity(os.fstat(directory))
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                count[0] += 1
                if count[0] > MAX_ENTRIES:
                    raise ValueError('Source entry bound')
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if info.st_dev != device:
                    raise ValueError('Foreign upload filesystem')
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, READ_FLAGS | os.O_DIRECTORY, dir_fd=directory)
                    try:
                        if os.fstat(child).st_dev != device:
                            raise ValueError('Foreign upload directory')
                        copy(child, target / name, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    fd = os.open(name, READ_FLAGS, dir_fd=directory)
                    try:
                        before = os.fstat(fd)
                        if (not stat.S_ISREG(before.st_mode) or before.st_dev != device
                                or before.st_size > MAX_FILE):
                            raise ValueError('Unsafe file')
                        out = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
                        size = 0
                        with os.fdopen(out, 'wb') as stream:
                            os.fchmod(stream.fileno(), 0o444)
                            while block := os.read(fd, 1024 * 1024):
                                size += len(block); total[0] += len(block)
                                if size > MAX_FILE or total[0] > MAX_TOTAL:
                                    raise ValueError('Source byte bound')
                                stream.write(block)
                            stream.flush(); os.fsync(stream.fileno())
                        if identity(os.fstat(fd)) != identity(before) or size != before.st_size:
                            raise ValueError('Upload changed during copy')
                    finally:
                        os.close(fd)
                else:
                    raise ValueError('Special or symlinked upload member')
        if identity(os.fstat(directory)) != before_dir:
            raise ValueError('Upload directory changed during copy')
    try:
        copy(top, destination, 0)
    finally:
        os.close(top)


def validate_upload_layout(incoming, *, profile='task19-v1', session=None):
    if profile=='task20-v1':
        from .task20_receiver import MEMBERS
        files={str(path.relative_to(incoming)) for path in incoming.rglob('*') if not path.is_dir()}
        if files!=MEMBERS or any(path.is_symlink() for path in incoming.rglob('*')):
            raise ValueError('Exact twelve-member Task20 profile required')
        if not isinstance(session,dict):raise ValueError('Root Task20 claim required')
        claim=json.loads(regular_bytes(incoming/'task20-round.json',4096),object_pairs_hook=verifier._pairs)
        expected=dict(schema=1,round=session['attempt'],head=session['head'],tree=session['tree'],
                      run_id=session['project'],bundle_sha256=session['bundle_sha256'])
        if claim!=expected:raise ValueError('Task20 round differs from root claim')
        return incoming/'source-attestation'
    if profile!='task19-v1':raise ValueError('Unknown root source profile')
    required = {'repository.bundle', 'maintainer.allowed_signers', 'rust-toolchain.toml',
                'qualification_bootstrap.py', 'qualification_source.py', 'reference'}
    names = {p.name for p in incoming.iterdir()}
    if names - required - {'source-attestation'} or not required <= names:
        raise ValueError('Unexpected upload entry or missing source input')
    for name in required - {'reference'}:
        if not (incoming / name).is_file() or (incoming / name).is_symlink():
            raise ValueError('Source input must be regular')
    reference = incoming / 'reference'
    if reference.is_symlink() or not reference.is_dir():
        raise ValueError('Reference must be a data directory')
    # References are JSON data only. No Python/module/bytecode lookup uses this tree.
    for path in reference.rglob('*'):
        if path.is_symlink() or (not path.is_dir() and (not path.is_file() or path.suffix != '.json')):
            raise ValueError('Reference contains a non-JSON data entry')
    attestation = incoming / 'source-attestation'
    if attestation.exists():
        expected = {'document.json', 'document.sig', 'github-commit.json', 'pgp-proof.txt', 'integration-ref.json'}
        if (not attestation.is_dir() or attestation.is_symlink()
                or {p.name for p in attestation.iterdir()} != expected
                or any(p.is_symlink() or not p.is_file() for p in attestation.iterdir())):
            raise ValueError('Unexpected attestation layout')
        return attestation
    return None


def preauthenticate(attestation, head, bundle_sha, run_id):
    """Reject unattested bundles before Git parsing when a detached proof exists."""
    if attestation is None:
        return
    raw = regular_bytes(attestation / 'document.json', 65536)
    signature = regular_bytes(attestation / 'document.sig', 16384)
    doc = json.loads(raw, object_pairs_hook=verifier._pairs)
    if (not isinstance(doc, dict) or doc.get('head') != head
            or doc.get('bundle_sha256') != bundle_sha or doc.get('run_id') != run_id
            or doc.get('namespace') != verifier.NAMESPACE or doc.get('mode') != verifier.ATTESTED):
        raise ValueError('Detached bundle admission identity differs')
    verifier._trusted_key(ANCHOR)
    with tempfile.TemporaryDirectory() as folder:
        frozen = Path(folder) / 'signature'
        write_owned(frozen, signature)
        subprocess.run(['/usr/bin/ssh-keygen', '-Y', 'verify', '-f', str(ANCHOR),
                        '-I', verifier.PRINCIPAL, '-n', verifier.NAMESPACE, '-s', str(frozen)],
                       input=raw, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=True)


@contextmanager
def import_slot():
    """At most one 1 GiB verifier may use the host reserve across all namespaces."""
    fd = os.open('/run/oxidex-qualification-import.lock',
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


def wait_guardian(command, locks, diagnostics):
    """Owner endpoint, also used by real process lifecycle controls."""
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C'}
    reader, writer = os.pipe()
    cancelled = []
    previous = {sig: signal.signal(sig, lambda number, frame: cancelled.append(number))
                for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    process = None
    try:
        argv = command(reader, locks)
        process = subprocess.Popen(argv, env=env, cwd='/', stdin=subprocess.DEVNULL,
                                   stdout=diagnostics, stderr=diagnostics, start_new_session=True,
                                   pass_fds=(reader, *locks))
        os.close(reader); reader = None
        while process.poll() is None:
            if cancelled and writer is not None:
                os.close(writer); writer = None
            # Guardian owns the hard deadline. Never kill it or drop its locks
            # while it is still reaping children, even after owner cancellation.
            try:
                process.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                pass  # Keep guardian locks held while it finishes reaping descendants.
        code = process.returncode
        if cancelled:
            raise InterruptedError('Source import cancelled by launcher signal')
        if code:
            diagnostics.seek(0)
            raise ValueError('Bounded source import refused: ' + diagnostics.read(4096).decode(errors='replace'))
    finally:
        if writer is not None:
            os.close(writer)
        if reader is not None:
            os.close(reader)
        if process is not None:
            process.wait()  # EOF cancellation; guardian closes locks only after reap.
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def sandbox_import(stage, head, bundle_sha, run_id, attestation):
    """One unprivileged, bounded process group; no root Git parses the upload."""
    if os.geteuid() != 0:
        raise ValueError('Host source import requires root launcher')
    account = pwd.getpwnam('nobody')
    work = stage / 'importer'; work.mkdir(mode=0o700)
    os.chown(work, account.pw_uid, account.pw_gid); work.chmod(0o700)
    with import_slot() as slot, tempfile.TemporaryFile(dir=stage) as diagnostics:
        def command(reader, locks):
            return ['/usr/bin/python3', '-I', '-B', str(Path(__file__).with_name('import_supervisor.py')),
                    str(reader), ','.join(map(str, locks)), str(account.pw_uid), str(account.pw_gid),
                    str(stage), head, bundle_sha, run_id, str(ANCHOR)]
        wait_guardian(command, (*PROTECTIVE_FDS.get(), slot), diagnostics)
    return json.loads(regular_bytes(work / 'identity.json', 65536), object_pairs_hook=verifier._pairs)


def seal_owned_tree(root):
    """Do not follow links or share inodes when promoting sandbox output."""
    for path in [root, *root.rglob('*')]:
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            mode = 0o755
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            mode = 0o555 if info.st_mode & 0o111 else 0o444
        else:
            raise ValueError('Authenticated materialization contains a link or special entry')
        os.chown(path, 0, 0, follow_symlinks=False); path.chmod(mode)


def tree_manifest(root):
    result = {}
    count = 0; total = 0
    for path in sorted(root.rglob('*')):
        info = path.lstat(); count += 1
        if count > MAX_ENTRIES or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Untrusted sealed tree ownership or entry count')
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o222:
            raise ValueError('Untrusted sealed tree member')
        total += info.st_size
        if total > 4 * MAX_TOTAL:
            raise ValueError('Sealed tree size bound')
        value = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                value.update(block)
        result[str(path.relative_to(root))] = [info.st_size, stat.S_IMODE(info.st_mode), value.hexdigest()]
    return result


def verify_sealed(stage, head, bundle_sha, run_id, *, profile='task19-v1', session=None):
    """Stage is host-created; incoming is the only uploader-controlled subtree."""
    validate_identity(head, bundle_sha, run_id)
    if digest(Path(verifier.__file__).read_bytes()) != MODULE_SHA:
        raise ValueError('Host verifier digest differs')
    info = ANCHOR.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o222:
        raise ValueError('Anchor must be root-owned readonly')
    incoming = stage / 'incoming'
    attestation = validate_upload_layout(incoming,profile=profile,session=session)
    bundle = incoming / 'repository.bundle'
    # Intake bounded this file; digest streaming avoids another compressed-input allocation.
    if verifier._sha_file(bundle) != bundle_sha:
        raise ValueError('Bundle digest differs')
    preauthenticate(attestation, head, bundle_sha, run_id)
    facts = sandbox_import(stage, head, bundle_sha, run_id, attestation)
    work = stage / 'importer'
    checkout = work / 'checkout'
    bootstrap='task20_bootstrap.py' if profile=='task20-v1' else 'qualification_bootstrap.py'
    for uploaded, tracked in [(bootstrap, 'tools/remote-build/'+bootstrap),
                              ('qualification_source.py', 'tools/remote-build/qualification_source.py'),
                              ('rust-toolchain.toml', 'rust-toolchain.toml')]:
        if regular_bytes(incoming / uploaded) != regular_bytes(checkout / tracked):
            raise ValueError('Uploaded executable/pin differs from authenticated tree: ' + uploaded)
    # .git is an empty mountpoint, never uploaded content. Metadata is prepared separately.
    (checkout / '.git').mkdir(mode=0o755)
    if profile=='task20-v1':
        for name in ('task20_worker.py','task20_results.py','task20_context.py','task20_gates.py'):
            regular_bytes(checkout/'tools/remote-build'/name,MAX_FILE)
        if facts.get('tree')!=session['tree']:raise ValueError('Task20 selected tree differs')
    checkout.rename(stage / 'checkout')
    (work / 'repository.git').rename(stage / 'repository.git')
    shutil.rmtree(work)
    for path in (stage / 'checkout', stage / 'repository.git', incoming):
        seal_owned_tree(path)
    result={'schema': 2, 'head': head, 'bundle_sha256': bundle_sha, 'run_id': run_id,
            'source_identity': facts,
            'uploaded_signers_sha256': digest(regular_bytes(incoming / 'maintainer.allowed_signers', 16384))}
    if profile=='task20-v1':result['profile']=profile
    return result


def execution_arguments(arguments, binding):
    """Only the four exact controller stages may execute authenticated source."""
    head = binding['head']; run = binding['run_id']
    output = '/target/ops/evidence/remote-qualification/' + run
    bootstrap = ['python3', '/src/qualification_bootstrap.py', '--head', head, '--output', output,
                 '--bundle-sha256', binding['bundle_sha256'],
                 '--signers-sha256', binding['uploaded_signers_sha256']]
    base = ['python3', '/target/checkout/tools/remote-build/qualification.py']
    tails = [['--remote-run', '--output', output, '--expected-head', head],
             ['--pack-results', '--output', output, '--expected-head', head, '--result-file', '/target/pack-result.json'],
             ['--verify-results-tar', '/target/results.tar.gz', '--output', output,
              '--expected-head', head, '--result-file', '/target/replay-result.json']]
    if arguments == bootstrap or arguments in [base + tail for tail in tails]:
        return arguments
    raise ValueError('Qualification execution is outside exact verified command contract')
