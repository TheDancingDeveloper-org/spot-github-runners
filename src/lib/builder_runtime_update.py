"""Checked runtime-file updates for an idle dedicated builder; no provisioning."""
import base64
import hashlib
import math
import re
import uuid
import os
from pathlib import Path
import stat
import tempfile

PENDING_UPDATE = Path('/var/lib/oxidex-builder/runtime-update-pending.json')
CURRENT_BOOT_PERMIT = Path('/run/oxidex-runtime-update-allow')
BOOT_GUARD = Path('/etc/systemd/system/gha-admission.service.d/20-runtime-update-hold.conf')
BOOT_GUARD_BYTES = (b'[Unit]\n'
    b'ConditionPathExists=|!/var/lib/oxidex-builder/runtime-update-pending.json\n'
    b'ConditionPathExists=|/run/oxidex-runtime-update-allow\n')


LAUNCHER_TARGET = '/usr/local/bin/oxidex-remote-build'

QUALIFICATION = '/opt/oxidex-builder/qualification_trust/'
TARGETS = {
    '/usr/local/bin/oxidex-remote-build': 0o755,
    '/usr/local/bin/docker': 0o755,
    '/opt/gha-runners/host_admission.py': 0o600,
    '/opt/gha-runners/builder_access.py': 0o644,
    '/etc/oxidex-remote-build/qualification.gitconfig': 0o444,
    '/etc/oxidex-remote-build/qualification-maintainer.allowed_signers': 0o444,
    **{QUALIFICATION + name + '.py': 0o644 for name in (
        '__init__', 'git_sandbox', 'host_gate', 'import_supervisor',
        'launcher_contract', 'mounts', 'qualification_source', 'runtime')},
    QUALIFICATION + 'runtime_git.py': 0o555,
}


def validate_assets(assets):
    if not isinstance(assets, dict) or set(assets) != set(TARGETS):
        raise ValueError('Exactly the reviewed fifteen runtime targets are required')
    result = {}
    total = 0
    for target, row in assets.items():
        if not isinstance(row, dict) or set(row) != {'data', 'sha256'}:
            raise ValueError('Unexpected runtime asset fields')
        try:
            raw = base64.b64decode(row['data'], validate=True)
        except (TypeError, ValueError) as error:
            raise ValueError('Invalid runtime asset encoding') from error
        total += len(raw)
        if (not raw or len(raw) > 262144 or total > 1048576
                or hashlib.sha256(raw).hexdigest() != row['sha256']):
            raise ValueError('Runtime asset differs from reviewed bytes')
        result[target] = raw
    return result



def validate_plan(plan, now):
    fields = {'schema', 'nonce', 'source_main', 'vm_id', 'boot', 'image_id',
              'config_sha256', 'admission_sha256', 'service_sha256', 'service_pid', 'service_startticks',
              'observed_at', 'assets', 'before'}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan['schema']) is not int or plan['schema'] != 1:
        raise ValueError('Unknown runtime update plan')
    for field in ('service_pid', 'service_startticks'):
        if type(plan[field]) is not int or plan[field] <= 0:
            raise ValueError('Invalid admission process identity')
    observed = plan['observed_at']
    if (type(observed) not in (int, float) or not math.isfinite(observed)
            or not 0 <= now - observed <= 120):
        raise ValueError('Expired or invalid runtime observation')
    for key, pattern in (('nonce', r'[a-f0-9]{32}'), ('source_main', r'[a-f0-9]{40}'),
                         ('vm_id', r'[0-9]{1,20}'), ('image_id', r'sha256:[a-f0-9]{64}'),
                         ('config_sha256', r'[a-f0-9]{64}'), ('admission_sha256', r'[a-f0-9]{64}'), ('service_sha256', r'[a-f0-9]{64}')):
        if not isinstance(plan[key], str) or not re.fullmatch(pattern, plan[key]):
            raise ValueError('Unbound runtime identity: ' + key)
    try:
        if str(uuid.UUID(plan['boot'])) != plan['boot']:
            raise ValueError('Noncanonical boot identity')
    except (TypeError, AttributeError, ValueError) as error:
        raise ValueError('Invalid boot identity') from error
    if not isinstance(plan['before'], dict) or set(plan['before']) != set(TARGETS):
        raise ValueError('Complete current file observations required')
    return validate_assets(plan['assets'])


def atomic_write(path, data, mode, *, before_replace=None):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlinked runtime path')
    if path.exists() and not path.is_file():
        raise ValueError('Runtime target is not a regular file')
    descriptor, temporary = tempfile.mkstemp(prefix='.runtime-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def root_file_read(path):
    """Inspect root custody before trusting a live file or its absence."""
    path = Path(path)
    for parent in path.parents:
        if parent.exists():
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or info.st_mode & 0o022):
                raise ValueError('Untrusted runtime ancestor: ' + str(parent))
        elif parent.is_symlink():
            raise ValueError('Symlinked runtime ancestor')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, b''
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 1048576):
            raise ValueError('Untrusted runtime file')
        raw = os.read(fd, 1048577)
        if len(raw) != info.st_size:
            raise ValueError('Runtime file changed while reading')
        after = os.fstat(fd)
        current = path.lstat()
        if ((info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) !=
                   (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('File changed during bounded descriptor read')
        return {'sha256': hashlib.sha256(raw).hexdigest(),
                'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid,
                'gid': info.st_gid, 'device': info.st_dev, 'inode': info.st_ino}, raw
    finally:
        os.close(fd)



def root_file_state(path):
    return root_file_read(path)[0]


def checked_json(raw, digest):
    import json
    if not isinstance(raw, bytes) or len(raw) > 1048576 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('JSON bytes differ from selected digest')
    return json.loads(raw)


def active_work_processes(records):
    return [row['pid'] for row in records if
            row['comm'] in ('scp', 'rsync', 'sftp-server', 'cargo', 'rustc')
            or b'@internal-sftp' in b'\0'.join(row['argv'])
            or b'/usr/local/bin/oxidex-remote-build' in row['argv']]


def drained_update(lock_path, drain_path, token, action, *, before_drain=None, before_release=None, keep_drain=False):
    """Run under the existing lock; failures retain the protective owned drain."""
    import fcntl
    if not isinstance(token, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,128}', token):
        raise ValueError('Invalid update token')
    lock_path, drain_path = Path(lock_path), Path(drain_path)
    lock = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    drain = None
    try:
        original = os.fstat(lock)
        if (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1
                or original.st_uid != os.geteuid()):
            raise ValueError('Untrusted build lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = lock_path.lstat()
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise ValueError('Build lock replaced')
        if before_drain is not None:
            before_drain()
        drain = os.open(drain_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        raw = token.encode('ascii')
        with os.fdopen(os.dup(drain), 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(drain_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
            result = action()
            owned, current = os.fstat(drain), drain_path.lstat()
            if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino) or current.st_nlink != 1:
                raise ValueError('Owned drain replaced; explicit recovery required')
            os.lseek(drain, 0, os.SEEK_SET)
            if os.read(drain, 129) != raw:
                raise ValueError('Owned drain changed; explicit recovery required')
            if before_release is not None:
                before_release(result)
            current = drain_path.lstat()
            os.lseek(drain, 0, os.SEEK_SET)
            if ((current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino)
                    or current.st_nlink != 1 or os.read(drain, 129) != raw):
                raise ValueError('Owned drain changed during final checks')
            if not keep_drain:
                os.unlink(drain_path)
                os.fsync(directory)
            return result
        finally:
            os.close(directory)
    finally:
        if drain is not None:
            os.close(drain)
        os.close(lock)


def check_snapshot(plan, snapshot, *, owned_drain=False):
    for field in ('vm_id', 'boot', 'image_id', 'config_sha256', 'admission_sha256', 'service_sha256',
                  'service_pid', 'service_startticks'):
        if snapshot.get(field) != plan[field]:
            raise ValueError('Live builder binding changed: ' + field)
    if snapshot.get('image_pin') != plan['image_id'] or snapshot.get('admission_valid') is not True:
        raise ValueError('Image pin or dedicated admission policy differs')
    if (snapshot.get('cpu') != 16 or snapshot.get('role') != 'builder'
            or snapshot.get('cpus') != 16 or snapshot.get('memory_gib') != 28
            or snapshot.get('tools_ready') is not True):
        raise ValueError('Builder role or host tools differ')
    if (snapshot.get('leases') != {} or snapshot.get('containers') != []
            or snapshot.get('upload_processes') != []):
        raise ValueError('Builder is not idle')
    if snapshot.get('before') != plan['before']:
        raise ValueError('Current runtime files differ from reviewed observations')
    if snapshot.get('pending_nonce') not in (None, plan['nonce'] if owned_drain else None):
        raise ValueError('Persistent pending update requires explicit recovery')
    expected_drain = plan['nonce'] if owned_drain else None
    if snapshot.get('drain_token') != expected_drain:
        raise ValueError('Foreign or unexpected builder drain')



def valid_admission(config):
    expected = dict(role='builder', host_cpus=16, builder_cpus=16, work_gib=28,
                    builder_gib=28, runner_gib=0, runner_names=[])
    if not isinstance(config, dict) or set(config) != set(expected):
        return False
    return all(config[k] == value and (type(config[k]) in (int, float)
               if type(value) is int else type(config[k]) is type(value))
               for k, value in expected.items())


def checked_command(arguments, *, timeout=20):
    import subprocess
    result = subprocess.run(arguments, capture_output=True, text=True,
                            check=True, timeout=timeout)
    if result.stderr:
        raise ValueError('Unexpected command diagnostics')
    return result.stdout.strip()



def service_identity(*, strict=True, timeout=2):
    pid = int(checked_command(['/usr/bin/systemctl', 'show', 'gha-admission.service',
                              '--property=MainPID', '--value'], timeout=timeout))
    if pid <= 0:
        raise ValueError('Admission service is not running')
    proc = Path('/proc') / str(pid)
    if proc.stat().st_uid != 0:
        raise ValueError('Admission process is not root-owned')
    if strict and (proc / 'cmdline').read_bytes().split(b'\0')[:-1] != [
            b'/usr/bin/python3', b'/opt/gha-runners/host_admission.py', b'serve']:
        raise ValueError('Unexpected admission process')
    ticks = int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[19])
    return pid, ticks


def validate_service_peer(expected_pid, credentials):
    if (len(credentials) != 3 or any(type(x) is not int for x in credentials)
            or credentials[0] != expected_pid or credentials[1] != 0):
        raise ValueError('Admission socket does not belong to the expected root process')



def validate_admission_response(raw):
    import json
    if len(raw) > 4096 or json.loads(raw) != {'error': 'Admission kind does not match peer identity'}:
        raise ValueError('Admission service did not return the expected no-action response')


def admission_ready(plan, pid, ticks, *, wait=True):
    import socket
    import struct
    import time
    remaining = plan['observed_at'] + 120 - time.time()
    if remaining <= 0:
        raise ValueError('Admission readiness observation expired')
    deadline = time.monotonic() + min(8 if wait else 1, remaining)
    path = Path('/run/gha-admission/socket')
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('Admission socket did not become ready')
        if service_identity(strict=False, timeout=min(2, remaining)) != (pid, ticks):
            raise ValueError('Admission process changed during readiness checks')
        try:
            service_identity(timeout=min(2, remaining))
            trusted_directory(path.parent)
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                raise ValueError('Untrusted admission socket')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(min(.5, remaining))
                client.connect(str(path))
                credentials = struct.unpack('3i', client.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                validate_service_peer(pid, credentials)
                # No action or lease request: root's default runner kind is rejected
                # before constructing/refreshing the admission pool.
                client.sendall(b'{}')
                response = bytearray()
                while len(response) <= 4096:
                    client.settimeout(min(.5, max(.01, deadline-time.monotonic())))
                    chunk = client.recv(4097-len(response))
                    if not chunk:
                        break
                    response.extend(chunk)
                validate_admission_response(bytes(response))
            if service_identity(timeout=min(2, max(.01, deadline-time.monotonic()))) != (pid, ticks):
                raise ValueError('Admission process changed after socket observation')
            validate_plan(plan, time.time())
            return
        except ValueError as error:
            if str(error) != 'Unexpected admission process' or not wait:
                raise
            time.sleep(min(.05, max(0, deadline-time.monotonic())))
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
            if not wait:
                raise
            time.sleep(min(.05, max(0, deadline-time.monotonic())))


def observed_work_processes():
    work_records = []
    for proc_path in Path('/proc').iterdir():
        if not proc_path.name.isdecimal():
            continue
        try:
            comm = (proc_path / 'comm').read_text().strip()
            argv = (proc_path / 'cmdline').read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        work_records.append(dict(pid=int(proc_path.name),comm=comm,argv=argv.split(b'\0')))
    return active_work_processes(work_records)


def live_snapshot(plan):
    """Read fixed host facts, never acquire leases or start containers."""
    import json
    config_path = Path('/etc/oxidex-remote-build.json')
    config_state, config_raw = root_file_read(config_path)
    config = json.loads(config_raw)
    admission_state, admission_raw = root_file_read('/etc/gha-admission.json')
    admission = json.loads(admission_raw)
    if config.get('image') != 'oxidex-remote-builder:1.99.0':
        raise ValueError('Unexpected configured builder image tag')
    ledger = Path('/var/lib/gha-admission/leases.json')
    root_file_state(ledger)
    service = root_file_state('/etc/systemd/system/gha-admission.service')
    drain = Path('/run/oxidex-build-draining')
    root_file_state(drain)
    service_pid, service_ticks = service_identity()
    executables = ('/usr/bin/git', '/usr/bin/ssh-keygen', '/usr/bin/python3',
                   '/usr/bin/docker', '/usr/bin/systemctl')
    tools_ready = all(stat.S_ISREG(Path(p).stat().st_mode) and Path(p).stat().st_uid == 0
                      and not Path(p).stat().st_mode & 0o022
                      and os.access(p, os.X_OK) for p in executables)
    uploads = observed_work_processes()
    pending_state, pending_raw = root_file_read(PENDING_UPDATE)
    pending_nonce = None
    if pending_state is not None:
        pending = json.loads(pending_raw)
        if pending.get('boot') != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
            raise ValueError('Pending update survived reboot; explicit recovery required')
        pending_nonce = pending.get('nonce')
    return dict(
        pending_nonce=pending_nonce,
        upload_processes=uploads,
        vm_id=checked_command(['/usr/bin/curl', '-q', '--fail', '--silent', '--show-error',
            '--max-time', '2', '-H', 'Metadata-Flavor:Google',
            'http://metadata.google.internal/computeMetadata/v1/instance/id']),
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        image_id=checked_command(['/usr/bin/docker', 'image', 'inspect',
                                 config['image'], '--format={{.Id}}']),
        config_sha256=config_state['sha256'], service_sha256=service['sha256'],
        image_pin=config.get('approved_image_id'), admission_sha256=admission_state['sha256'],
        admission_valid=valid_admission(admission),
        service_pid=service_pid, service_startticks=service_ticks,
        cpu=os.cpu_count(), role=config.get('role'), cpus=config.get('cpus'),
        memory_gib=config.get('memory_gib'), tools_ready=tools_ready,
        leases=json.loads(ledger.read_bytes()),
        containers=checked_command(['/usr/bin/docker', 'ps', '-aq', '--no-trunc']).split(),
        before={target: root_file_state(target) for target in TARGETS},
        drain_token=drain.read_text() if drain.exists() else None)




def close_launcher_execution(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Launcher is not a single regular file')
        os.fchmod(fd, 0o644)
        os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def mkdir_exact(path, mode):
    path = Path(path)
    path.mkdir(mode=mode)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def trusted_directory(path, *, create=False):
    path = Path(path)
    for current in reversed((path, *path.parents)):
        if not current.exists() and not current.is_symlink() and create:
            mkdir_exact(current, 0o755)
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise ValueError('Untrusted update directory: ' + str(current))



def mutation_idle_fence(plan):
    import json
    import time
    _, ledger = root_file_read('/var/lib/gha-admission/leases.json')
    _, token = root_file_read('/run/oxidex-build-draining')
    if (json.loads(ledger) != {} or token != plan['nonce'].encode()
            or checked_command(['/usr/bin/docker', 'ps', '-aq', '--no-trunc'])
            or observed_work_processes()):
        raise ValueError('Transfer, launcher, lease or container appeared before mutation')
    validate_plan(plan, time.time())



def file_mutation_fence(plan, target=None, *, expected_file=None):
    import time
    if service_identity() != (plan['service_pid'], plan['service_startticks']):
        raise ValueError('Approved admission process changed before planned restart')
    if (Path('/proc/sys/kernel/random/boot_id').read_text().strip() != plan['boot']
            or root_file_state('/etc/oxidex-remote-build.json')['sha256'] != plan['config_sha256']
            or root_file_state('/etc/gha-admission.json')['sha256'] != plan['admission_sha256']
            or root_file_state('/etc/systemd/system/gha-admission.service')['sha256'] != plan['service_sha256']
            or (target is not None and root_file_state(target) != (plan['before'][target] if expected_file is None else expected_file))):
        raise ValueError('Immediate runtime mutation bindings changed')
    validate_plan(plan, time.time())



def prepare_boot_hold(plan):
    import json
    state = root_file_state(BOOT_GUARD)
    if state is not None and state['sha256'] != hashlib.sha256(BOOT_GUARD_BYTES).hexdigest():
        raise ValueError('Unknown admission boot guard')
    if root_file_state(PENDING_UPDATE) is not None or root_file_state(CURRENT_BOOT_PERMIT) is not None:
        raise ValueError('Existing update hold requires explicit recovery')
    trusted_directory(BOOT_GUARD.parent, create=True)
    file_mutation_fence(plan)
    if state is None:
        atomic_write(BOOT_GUARD, BOOT_GUARD_BYTES, 0o644,
                     before_replace=lambda:file_mutation_fence(plan))
    checked_command(['/usr/bin/systemctl', 'daemon-reload'])
    file_mutation_fence(plan)
    atomic_write(CURRENT_BOOT_PERMIT, plan['nonce'].encode(), 0o600,
                 before_replace=lambda:file_mutation_fence(plan))
    # Must become durable before the first of the fifteen runtime replacements.
    atomic_write(PENDING_UPDATE, (json.dumps(dict(nonce=plan['nonce'], boot=plan['boot'],
                 source_main=plan['source_main']))+'\n').encode(), 0o600,
                 before_replace=lambda:file_mutation_fence(plan))



def verify_boot_hold(plan):
    import json
    guard, _ = root_file_read(BOOT_GUARD)
    _, pending = root_file_read(PENDING_UPDATE)
    _, permit = root_file_read(CURRENT_BOOT_PERMIT)
    expected = dict(nonce=plan['nonce'], boot=plan['boot'], source_main=plan['source_main'])
    if (guard is None or guard['sha256'] != hashlib.sha256(BOOT_GUARD_BYTES).hexdigest()
            or guard['mode'] != 0o644 or json.loads(pending) != expected
            or permit != plan['nonce'].encode()):
        raise ValueError('Persistent boot hold or current-boot permit differs')


def apply_update(plan):
    """Replace only approved runtime files after actual idle-state confirmation."""
    import json
    import time
    assets = validate_plan(plan, time.time())
    backup = Path('/var/lib/oxidex-builder') / ('runtime-update-' + plan['nonce'])
    trusted_directory(backup.parent)
    if backup.exists() or backup.is_symlink():
        raise ValueError('Update nonce already used')

    def install():
        snapshot = live_snapshot(plan)
        validate_plan(plan, time.time())
        check_snapshot(plan, snapshot, owned_drain=True)
        validate_plan(plan, time.time())
        mkdir_exact(backup, 0o700)
        validate_plan(plan, time.time())
        atomic_write(backup / 'plan.json', (json.dumps(plan, sort_keys=True) + '\n').encode(), 0o600)
        journal = backup / 'journal.jsonl'

        def record(stage, **facts):
            with journal.open('ab') as output:
                os.chmod(journal, 0o600)
                output.write((json.dumps(dict(stage=stage, observed_at=time.time(), **facts),
                                         sort_keys=True) + '\n').encode())
                output.flush()
                os.fsync(output.fileno())

        record('BACKUP_START')
        try:
            for index, target in enumerate(sorted(TARGETS)):
                validate_plan(plan, time.time())
                if root_file_state(target) != plan['before'][target]:
                    raise ValueError('Runtime changed before backup')
                if plan['before'][target] is not None:
                    atomic_write(backup / (str(index) + '.before'), Path(target).read_bytes(), 0o600)
            record('BACKUPS_COMPLETE', before=plan['before'])
            # Preparation has not changed live code. This durable permission
            # transition is the first live cutover, including the old launcher.
            mutation_idle_fence(plan)
            file_mutation_fence(plan, LAUNCHER_TARGET)
            close_launcher_execution(LAUNCHER_TARGET)
            held_launcher = root_file_state(LAUNCHER_TARGET)
            intended = dict(plan['before'][LAUNCHER_TARGET], mode=0o644)
            if held_launcher != intended:
                raise ValueError('Durable launcher execution hold differs')
            record('MUTATING_HELD', launcher=held_launcher)
            prepare_boot_hold(plan)
            verify_boot_hold(plan)
            record('PERSISTENT_BOOT_HOLD_INSTALLED')
            for target in sorted(TARGETS):
                validate_plan(plan, time.time())
                mutation_idle_fence(plan)
                expected_file = held_launcher if target == LAUNCHER_TARGET else None
                file_mutation_fence(plan, target, expected_file=expected_file)
                trusted_directory(Path(target).parent, create=True)
                mutation_idle_fence(plan)
                file_mutation_fence(plan, target, expected_file=expected_file)
                def before_replace():
                    mutation_idle_fence(plan)
                    file_mutation_fence(plan, target, expected_file=expected_file)
                installed_mode = 0o644 if target == LAUNCHER_TARGET else TARGETS[target]
                atomic_write(target, assets[target], installed_mode, before_replace=before_replace)
                record('FILE_REPLACED', target=target,
                       sha256=hashlib.sha256(assets[target]).hexdigest())
            validate_plan(plan, time.time())
            mutation_idle_fence(plan)
            trusted_directory('/mnt/runner-data/remote-build/qualification-sealed', create=True)
            validate_plan(plan, time.time())
            mutation_idle_fence(plan)
            file_mutation_fence(plan)
            checked_command(['/usr/bin/systemctl', 'restart', 'gha-admission.service'])
            new_pid, new_ticks = service_identity(strict=False)
            admission_ready(plan, new_pid, new_ticks)
            after = live_snapshot(plan)
            if (after['service_pid'], after['service_startticks']) != (new_pid, new_ticks):
                raise ValueError('Admission identity changed after readiness proof')
            expected = dict(plan, service_pid=after['service_pid'],
                            service_startticks=after['service_startticks'], before=after['before'])
            check_snapshot(expected, after, owned_drain=True)
            if (after['service_pid'] == plan['service_pid']
                    or Path('/proc/' + str(plan['service_pid'])).exists()):
                raise ValueError('Original admission process is still present')
            for target, data in assets.items():
                state = after['before'][target]
                if state['sha256'] != hashlib.sha256(data).hexdigest() or state['mode'] != (0o644 if target == LAUNCHER_TARGET else TARGETS[target]):
                    raise ValueError('Installed runtime proof differs')
            verify_boot_hold(plan)
            record('FILES_AND_SERVICE_VERIFIED', service_pid=after['service_pid'],
                   service_startticks=after['service_startticks'], files=after['before'])
            return dict(status='INSTALLED_DRAINED', source_main=plan['source_main'], backup=str(backup),
                        service_pid=after['service_pid'], service_startticks=after['service_startticks'],
                        qualification_ready=False)
        except BaseException as error:
            record('HOLD_EXPLICIT_RECOVERY', error_type=type(error).__name__)
            raise

    def before_drain():
        snapshot = live_snapshot(plan)
        validate_plan(plan, time.time())
        check_snapshot(plan, snapshot)
    def before_release(result):
        try:
            verify_boot_hold(plan)
            snapshot = live_snapshot(plan)
            validate_plan(plan, time.time())
            expected = dict(plan, service_pid=result['service_pid'],
                            service_startticks=result['service_startticks'], before=snapshot['before'])
            check_snapshot(expected, snapshot, owned_drain=True)
            for target, raw in assets.items():
                state = snapshot['before'][target]
                if state['sha256'] != hashlib.sha256(raw).hexdigest() or state['mode'] != (0o644 if target == LAUNCHER_TARGET else TARGETS[target]):
                    raise ValueError('Installed runtime changed before drain release')
            admission_ready(plan, result['service_pid'], result['service_startticks'], wait=False)
            records = []
            for proc in Path('/proc').iterdir():
                if proc.name.isdecimal():
                    try:
                        records.append(dict(pid=int(proc.name),comm=(proc/'comm').read_text().strip(),
                                            argv=(proc/'cmdline').read_bytes().split(b'\0')))
                    except (FileNotFoundError, ProcessLookupError):
                        continue
            if active_work_processes(records):
                raise ValueError('Launcher or upload appeared before drain release')
            validate_plan(plan, time.time())
        except BaseException as error:
            import json
            with (backup/'journal.jsonl').open('ab') as output:
                output.write((json.dumps(dict(stage='HOLD_BEFORE_DRAIN_RELEASE',
                    observed_at=time.time(),error_type=type(error).__name__),sort_keys=True)+'\n').encode())
                output.flush();os.fsync(output.fileno())
            raise
    return drained_update('/run/oxidex-build.lock', '/run/oxidex-build-draining',
                          plan['nonce'], install, before_drain=before_drain, before_release=before_release, keep_drain=True)


def plan_from_snapshot(source_main, assets, snapshot, observed_at, nonce):
    plan = dict(schema=1, nonce=nonce, source_main=source_main,
                observed_at=observed_at, assets=assets,
                **{field: snapshot[field] for field in (
                    'vm_id', 'boot', 'image_id', 'config_sha256', 'admission_sha256', 'service_sha256',
                    'service_pid', 'service_startticks', 'before')})
    validate_plan(plan, observed_at)
    check_snapshot(plan, snapshot)
    return plan


def main():
    import argparse
    import json
    import sys
    import time
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--plan', type=Path)
    inputs.add_argument('--capture-assets', type=Path)
    parser.add_argument('--plan-sha256')
    parser.add_argument('--assets-sha256')
    parser.add_argument('--source-main')
    parser.add_argument('--apply', action='store_true', help='Apply the reviewed plan; default checks only')
    args = parser.parse_args()
    if (sys.platform != 'linux' or os.geteuid() != 0
            or not sys.flags.isolated or not sys.dont_write_bytecode):
        raise ValueError('Run through the existing authenticated root control path on the builder')
    if args.capture_assets:
        if args.apply or not args.source_main or not args.assets_sha256:
            parser.error('Capture requires source-main/assets-sha256 and cannot apply')
        state, raw = root_file_read(args.capture_assets)
        if state is None or state['sha256'] != args.assets_sha256:
            raise ValueError('Candidate assets differ from selected digest')
        assets = checked_json(raw, args.assets_sha256)
        validate_assets(assets)
        observed_at = time.time()
        snapshot = live_snapshot({})
        plan = plan_from_snapshot(args.source_main, assets, snapshot, observed_at, uuid.uuid4().hex)
        validate_plan(plan, time.time())
        print(json.dumps(plan, sort_keys=True))
        return
    if not args.plan_sha256:
        parser.error('Check/apply requires plan-sha256')
    state, raw = root_file_read(args.plan)
    if state is None or state['sha256'] != args.plan_sha256:
        raise ValueError('Plan differs from the explicitly selected reviewed digest')
    plan = checked_json(raw, args.plan_sha256)
    validate_plan(plan, time.time())
    snapshot = live_snapshot(plan)
    validate_plan(plan, time.time())
    check_snapshot(plan, snapshot)
    result = apply_update(plan) if args.apply else dict(status='CHECKED_ONLY', executed=False)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
