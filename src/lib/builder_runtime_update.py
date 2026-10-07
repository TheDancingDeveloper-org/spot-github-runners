"""Checked runtime-file updates for an idle dedicated builder; no provisioning."""
import base64
import hashlib
import json
import math
import re
import uuid
import os
from pathlib import Path
import stat
import tempfile
from contextlib import contextmanager, ExitStack

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




def verify_installed_runtime(assets, files, *, client_launcher_sha256):
    """Check a complete held installation against the selected client pin.

    The caller must obtain file states through root_file_read. This pure check
    does not establish custody, run compatibility tests or reopen the builder.
    """
    decoded = validate_assets(assets)
    launcher_sha = hashlib.sha256(decoded[LAUNCHER_TARGET]).hexdigest()
    if client_launcher_sha256 != launcher_sha:
        raise ValueError('Client launcher pin differs from installed runtime')
    if not isinstance(files, dict) or set(files) != set(TARGETS):
        raise ValueError('Complete installed runtime observations required')
    for target, raw in decoded.items():
        state = files[target]
        expected_mode = 0o644 if target == LAUNCHER_TARGET else TARGETS[target]
        if (not isinstance(state, dict)
                or state.get('sha256') != hashlib.sha256(raw).hexdigest()
                or type(state.get('mode')) is not int
                or state['mode'] != expected_mode):
            raise ValueError('Installed runtime bytes or held mode differ: ' + target)
    return launcher_sha


def validate_plan(plan, now):
    fields = {'schema', 'nonce', 'source_main', 'vm_id', 'boot', 'image_id',
              'config_sha256', 'admission_sha256', 'service_sha256', 'service_pid', 'service_startticks',
              'observed_at', 'assets', 'before'}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan['schema']) is not int or plan['schema'] != 1:
        raise ValueError('Unknown runtime update plan')
    for field in ('service_pid', 'service_startticks'):
        if type(plan[field]) is not int or plan[field] <= 0:
            raise ValueError('Invalid admission process identity')
    return validate_runtime_observation(plan, now)


def validate_runtime_observation(plan, now):
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



def validate_committed_observation(observation, now):
    fields = {'schema', 'nonce', 'source_main', 'vm_id', 'boot', 'image_id',
              'config_sha256', 'admission_sha256', 'service_sha256', 'service_state',
              'observed_at', 'assets', 'before'}
    if (not isinstance(observation, dict) or set(observation) != fields
            or type(observation['schema']) is not int or observation['schema'] != 2):
        raise ValueError('Unknown committed reboot observation')
    validate_runtime_observation(observation, now)


def verify_committed_reboot(original, journal, observation, client_pin, now):
    """Validate explicit reboot completion; no fabricated running-process facts."""
    validate_committed_observation(observation, now)
    if not isinstance(original, dict):raise ValueError('Missing original update plan')
    validate_plan(original, original.get('observed_at', now))
    for field in ('nonce', 'source_main', 'vm_id', 'image_id', 'config_sha256',
                  'admission_sha256', 'service_sha256', 'assets'):
        if observation[field] != original[field]:
            raise ValueError('Committed recovery identity changed: ' + field)
    if observation['boot'] == original['boot']:
        raise ValueError('Use same-boot finish recovery without rewriting boot history')
    inactive = dict(LoadState='loaded', ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0')
    if observation['service_state'] != inactive:
        state = observation['service_state']
        active_keys = set(inactive) | {'StartTicks'}
        if (not isinstance(state, dict) or set(state) != active_keys
                or state['LoadState'] != 'loaded' or state['ActiveState'] != 'active'
                or state['SubState'] != 'running' or state['ControlPID'] != '0'
                or not isinstance(state['MainPID'], str)
                or not re.fullmatch('[1-9][0-9]{0,9}', state['MainPID'])
                or type(state['StartTicks']) is not int or state['StartTicks'] <= 0):
            raise ValueError('Committed recovery service state is unknown')
    if not isinstance(journal, list) or not journal or not isinstance(journal[-1], dict):
        raise ValueError('Missing durable release decision')
    final = journal[-1]
    comparison_files = observation['before']
    transitions = {'HOLD_FINISH_RECOVERY', 'HOLD_FINISH_INTENT', 'COMMITTED_LAUNCHER_RESTORE_INTENT', 'FINISH_RECOVERY_START', 'FINISH_RECOVERED_HELD', 'FINISH_START', 'FINISH_READY_TO_RELEASE'}
    if final.get('stage') in transitions:
        indices = [index for index, row in enumerate(journal)
                   if isinstance(row, dict) and row.get('stage') == 'FINISH_RELEASE_COMMITTED']
        if (not indices or any(not isinstance(row, dict) or row.get('stage') not in transitions
                               for row in journal[indices[-1]+1:])):
            raise ValueError('Unknown post-commit transition history')
        selected = comparison_files.get(LAUNCHER_TARGET)
        if not isinstance(selected, dict) or selected.get('mode') not in (0o644, 0o755):
            raise ValueError('Unknown committed launcher permission state')
        for row in journal[indices[-1]+1:]:
            if row['stage'] == 'FINISH_START' and row.get('client_launcher_sha256') != client_pin:
                raise ValueError('Finish retry selected a different client')
            if row['stage'] in ('FINISH_RECOVERED_HELD', 'FINISH_READY_TO_RELEASE'):

                recovered = row.get('files')
                if (not isinstance(recovered, dict) or set(recovered) != set(comparison_files)
                        or not isinstance(recovered.get(LAUNCHER_TARGET), dict)
                        or recovered[LAUNCHER_TARGET].get('mode') != 0o644
                        or dict(recovered, **{LAUNCHER_TARGET:dict(recovered[LAUNCHER_TARGET], mode=0o755)})
                           != dict(comparison_files, **{LAUNCHER_TARGET:dict(selected, mode=0o755)})):
                    raise ValueError('Completed rehold belongs to changed files')
            if row['stage'] in ('HOLD_FINISH_INTENT', 'COMMITTED_LAUNCHER_RESTORE_INTENT', 'FINISH_RECOVERY_START'):
                recorded = row.get('launcher')
                if (not isinstance(recorded, dict) or recorded.get('mode') not in (0o644, 0o755)
                        or dict(recorded, mode=0o755) != dict(selected, mode=0o755)):
                    raise ValueError('Permission intent belongs to a different launcher')
        final = journal[indices[-1]]
        comparison_files = dict(comparison_files, **{LAUNCHER_TARGET:dict(selected, mode=0o755)})
    if final.get('stage') == 'COMMITTED_REBOOT_RECOVERED':
        historical_boot = final.get('boot')
        try:
            valid_boot = isinstance(historical_boot, str) and str(uuid.UUID(historical_boot)) == historical_boot
        except (ValueError, TypeError, AttributeError):
            valid_boot = False
        if (not valid_boot
                or type(final.get('service_pid')) is not int or final['service_pid'] <= 0
                or type(final.get('service_startticks')) is not int or final['service_startticks'] <= 0
                or not any(isinstance(row, dict) and row.get('stage') == 'FINISH_RELEASE_COMMITTED'
                           for row in journal[:-1])):
            raise ValueError('Recovered checkpoint boot or historical identity is invalid')
    elif final.get('stage') != 'FINISH_RELEASE_COMMITTED':
        raise ValueError('No durable committed release to recover')
    # A release decision records observed held files separately from the
    # authorized executable state. Crash before chmod is still protected.
    selected = comparison_files.get(LAUNCHER_TARGET)
    if (final.get('stage') == 'FINISH_RELEASE_COMMITTED' and isinstance(selected, dict)
            and selected.get('mode') == 0o644 and final.get('observed_files') == comparison_files):
        comparison_files = dict(comparison_files, **{LAUNCHER_TARGET:dict(selected, mode=0o755)})
    if (final.get('files') != comparison_files
            or final.get('client_launcher_sha256') != client_pin
            or final.get('qualification_ready') is not False):
        raise ValueError('No matching durable unqualified release commit')
    launcher = comparison_files.get(LAUNCHER_TARGET)
    if not isinstance(launcher, dict) or launcher.get('mode') != 0o755:
        raise ValueError('Committed launcher must match its executable release state')
    held = dict(comparison_files, **{LAUNCHER_TARGET:dict(launcher, mode=0o644)})
    return verify_installed_runtime(observation['assets'], held, client_launcher_sha256=client_pin)


def verify_installed_history(plan, original, journal, client_pin, now, *, recovery=False):
    """Join fresh finish observations to the original successful installation.

    Original timestamps stay historical. Root custody of the saved plan and
    journal is checked by the caller before parsing these bounded records.
    """
    validate_plan(plan, now)
    if not isinstance(original, dict):
        raise ValueError('Missing original installation plan')
    validate_plan(original, original.get('observed_at', now))
    for field in ('nonce', 'source_main', 'vm_id', 'boot', 'image_id',
                  'config_sha256', 'admission_sha256', 'service_sha256', 'assets'):
        if original[field] != plan[field]:
            raise ValueError('Finish differs from installed update: ' + field)
    if not isinstance(journal, list) or not journal or not isinstance(journal[-1], dict):
        raise ValueError('Missing successful installation journal')
    success = ('FILES_AND_SERVICE_VERIFIED', 'FINISH_RECOVERED_HELD')
    final = journal[-1]
    observed = plan['before']
    if recovery:
        indices = [index for index, row in enumerate(journal)
                   if isinstance(row, dict) and row.get('stage') in success]
        if not indices:
            raise ValueError('Recovery requires a verified installation')
        index = indices[-1]
        allowed = {'FINISH_START', 'FINISH_READY_TO_RELEASE', 'FINISH_RELEASE_COMMITTED',
                   'HOLD_FINISH_RECOVERY', 'HOLD_FINISH_INTENT', 'FINISH_REOPENED_UNQUALIFIED', 'FINISH_RECOVERY_START'}
        if any(not isinstance(row, dict) or row.get('stage') not in allowed
               for row in journal[index+1:]):
            raise ValueError('Recovery cannot bypass failed apply or unknown history')
        final = journal[index]
        state = observed.get(LAUNCHER_TARGET)
        if not isinstance(state, dict) or state.get('mode') not in (0o644, 0o755):
            raise ValueError('Unknown launcher state for recovery')
        observed = dict(observed, **{LAUNCHER_TARGET:dict(state, mode=0o644)})
    if (final.get('stage') not in success
            or final.get('service_pid') != plan['service_pid']
            or final.get('service_startticks') != plan['service_startticks']
            or final.get('files') != observed
            or plan['service_pid'] == original['service_pid']):
        raise ValueError('Installation incomplete, failed or changed')
    return verify_installed_runtime(plan['assets'], observed,
                                    client_launcher_sha256=client_pin)


def verify_descriptor_selection(fd, path, selected):
    """One metadata-only final guard for selected file descriptors and paths."""
    retained = os.fstat(fd);canonical = Path(path).lstat()
    fields = ('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink',
              'st_size','st_mtime_ns','st_ctime_ns')
    if (any(getattr(selected,key) != getattr(retained,key) for key in fields)
            or any(getattr(retained,key) != getattr(canonical,key) for key in fields)):
        raise ValueError('Selected descriptor or canonical file changed')


def cleanup_selected_staging(fd, path, selected, verify_parent):
    """Remove only the selected private staging inode; preserve foreign evidence."""
    try:
        Path(path).lstat()
        verify_parent()
        verify_descriptor_selection(fd,path,selected)
    except (FileNotFoundError,ValueError):
        return None
    os.unlink(path)
    return None


def atomic_write(path, data, mode, *, before_replace=None, exclusive=False):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlinked runtime path')
    if path.exists() and not path.is_file():
        raise ValueError('Runtime target is not a regular file')
    with retained_ancestor_selection(path) as verify_parent:
        descriptor, temporary = tempfile.mkstemp(prefix='.runtime-', dir=path.parent)
        with os.fdopen(descriptor, 'wb') as stream:
            staged = os.fstat(stream.fileno())
            try:
                verify_parent()
                verify_descriptor_selection(stream.fileno(),temporary,staged)
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                staged = os.fstat(stream.fileno())
                if before_replace is not None:before_replace()
                verify_parent()
                verify_descriptor_selection(stream.fileno(),temporary,staged)
                if exclusive:rename_exclusive(temporary,path)
                else:os.replace(temporary,path)
                parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:os.fsync(parent)
                finally:os.close(parent)
                return {'sha256': hashlib.sha256(data).hexdigest(),
                        'mode': stat.S_IMODE(staged.st_mode), 'uid': staged.st_uid,
                        'gid': staged.st_gid, 'device': staged.st_dev, 'inode': staged.st_ino}
            finally:
                cleanup_selected_staging(stream.fileno(),temporary,staged,verify_parent)



def append_journal_record(path, record, *, before_publish=None, freshness=None, selected_history=None):
    """Atomically publish a complete bounded history, retaining the old prefix."""
    state, old = root_file_read(path)
    if selected_history is not None and (state, old) != selected_history:
        raise ValueError('Journal differs from previously authorized history')
    if old and not old.endswith(b'\n'):
        raise ValueError('Incomplete historical journal needs explicit inspection')
    for line in old.splitlines():
        if not isinstance(json.loads(line), dict):
            raise ValueError('Invalid historical journal record')
    if not isinstance(record, dict):
        raise ValueError('Journal record must be an object')
    raw = old + (json.dumps(record, sort_keys=True)+'\n').encode()
    if len(raw) > 1048576:
        raise ValueError('Journal size bound exceeded')
    def unchanged():
        with retained_file_selection(path, state, lambda:root_file_read(path)[0]) as verify:
            if before_publish is not None:before_publish()
            verify()
            if freshness is not None:freshness()
            if before_publish is not None:before_publish()
            verify()
    published = atomic_write(path, raw, 0o600, before_replace=unchanged)
    updated = root_file_read(path)
    if (updated[1] != raw or updated[0] is None
            or any(updated[0].get(key) != value for key,value in published.items())):
        raise ValueError('Own journal publication changed before receipt')
    return updated


def authenticate_ancestor_custody(info):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Untrusted selected ancestor')


@contextmanager
def retained_ancestor_selection(path, *, authenticate=False):
    """Retain directory custody for metadata-only checks at mutation boundaries."""
    path = Path(path)
    ancestors = []
    try:
        for parent in path.parents:
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(directory)
                ancestors.append((parent, directory, info))
            except BaseException:
                os.close(directory)
                raise
            if authenticate:authenticate_ancestor_custody(info)
        def verify():
            custody = ('st_dev','st_ino','st_uid','st_gid','st_mode')
            for parent, directory, original in ancestors:
                retained = os.fstat(directory)
                canonical = parent.lstat()
                if (any(getattr(original,key) != getattr(retained,key) for key in custody)
                        or any(getattr(retained,key) != getattr(canonical,key) for key in custody)):
                    raise ValueError('Selected ancestor custody changed')
        verify()
        yield verify
    finally:
        for _, directory, _ in reversed(ancestors):os.close(directory)


@contextmanager
def retained_file_selection(path, expected, read_state):
    """Read before authority checks; retain identity for metadata-only revalidation."""
    path = Path(path)
    with retained_ancestor_selection(path) as verify_ancestors:
        fd = None
        try:
            if expected is not None:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                selected = os.fstat(fd)
            if read_state() != expected:
                raise ValueError('Selected file changed before authority check')
            def verify():
                verify_ancestors()
                if fd is None:
                    try:path.lstat()
                    except FileNotFoundError:return
                    raise ValueError('Absent selected file appeared')
                verify_descriptor_selection(fd,path,selected)
            verify()
            yield verify
        finally:
            if fd is not None:os.close(fd)



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
    if not isinstance(raw, bytes) or len(raw) > 1048576 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('JSON bytes differ from selected digest')
    return json.loads(raw)


def active_work_processes(records):
    return [row['pid'] for row in records if
            row['comm'] in ('scp', 'rsync', 'sftp-server', 'cargo', 'rustc')
            or b'@internal-sftp' in b'\0'.join(row['argv'])
            or b'/usr/local/bin/oxidex-remote-build' in row['argv']]


def rename_exclusive(source, target):
    """Atomic no-replace rename; unsupported kernels fail without publication."""
    import ctypes
    import sys
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'linux':
        operation = libc.renameat2
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    elif sys.platform == 'darwin':
        # The bounded synthetic controls also run on macOS. SDK RENAME_EXCL=4.
        operation = libc.renamex_np
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(os.fsencode(source), os.fsencode(target), 4)
    else:
        raise ValueError('Exclusive atomic rename is unsupported on this platform')
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def publish_owned_drain(path, token, *, before_publish=None):
    """Publish a complete durable token exclusively; never expose an empty drain."""
    if not isinstance(token,str) or not re.fullmatch('[a-zA-Z0-9_-]{1,128}',token):
        raise ValueError('Invalid drain token')
    path = Path(path)
    with retained_ancestor_selection(path) as verify_parent:
        fd, temporary = tempfile.mkstemp(prefix='.drain-',dir=path.parent)
        staged = None
        try:
            staged = os.fstat(fd)
            verify_parent()
            verify_descriptor_selection(fd,temporary,staged)
            os.fchmod(fd,0o600)
            raw = token.encode('ascii')
            if os.write(fd,raw) != len(raw):raise OSError('Incomplete private drain write')
            os.fsync(fd);staged = os.fstat(fd)
            if before_publish is not None:before_publish()
            verify_parent()
            verify_descriptor_selection(fd,temporary,staged)
            rename_exclusive(temporary,path)
            parent = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
            os.lseek(fd,0,os.SEEK_SET)
            return fd
        except BaseException:
            try:
                if staged is not None:cleanup_selected_staging(fd,temporary,staged,verify_parent)
            finally:os.close(fd)
            raise



class CommittedReleaseError(RuntimeError):
    """Unlink succeeded; persistence is unconfirmed and no hold is claimed."""
    def __init__(self, result):
        super().__init__('Owned drain was unlinked; directory sync unconfirmed; inspect actual state before any recovery')
        self.result = dict(result if isinstance(result, dict) else {'action_result':result},
                           drain_unlinked=True, directory_sync_confirmed=False,
                           qualification_ready=False, protected_hold=False,
                           status='RELEASE_UNLINKED_SYNC_UNCONFIRMED')


def verify_owned_drain(fd, path, token, canonical_owned):
    """Read the token, then recheck both canonical custody and descriptor changes."""
    canonical_owned()
    before = os.fstat(fd);current = Path(path).lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.geteuid() or before.st_mode & 0o077
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
        raise ValueError('Owned drain custody differs')
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 129)
    canonical_owned()
    current = Path(path).lstat();after = os.fstat(fd)
    attributes = ('st_uid', 'st_gid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if ((after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or after.st_nlink != 1 or current.st_nlink != 1
            or current.st_uid != os.geteuid() or current.st_mode & 0o077
            or any(getattr(before, name) != getattr(after, name) for name in attributes)
            or raw != token.encode('ascii')):
        raise ValueError('Owned drain changed during token read')
    def metadata_guard():
        canonical_owned()
        verify_descriptor_selection(fd,path,after)
    return metadata_guard



def drained_update(lock_path, drain_path, token, action, *, before_drain=None, before_release=None, keep_drain=False, lock_bound=None, release_fence=None, publication_fence=None, root_custody=False):
    """Run under the existing lock; failures retain the protective owned drain."""
    import fcntl
    if not isinstance(token, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,128}', token):
        raise ValueError('Invalid update token')
    lock_path, drain_path = Path(lock_path), Path(drain_path)
    with retained_ancestor_selection(lock_path, authenticate=root_custody) as verify_lock_ancestors, retained_ancestor_selection(drain_path, authenticate=root_custody) as verify_drain_ancestors:
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
            def canonical_owned():
                verify_lock_ancestors()
                verify_drain_ancestors()
                current = lock_path.lstat()
                if ((current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
                        or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                        or current.st_uid != os.geteuid() or current.st_mode & 0o022):
                    raise ValueError('Canonical build lock ownership lost')
            if lock_bound is not None:lock_bound(canonical_owned)
            def before_publish():
                canonical_owned()
                if before_drain is not None:before_drain()
                canonical_owned()
                if publication_fence is not None:publication_fence()
                canonical_owned()
            before_publish()
            drain = publish_owned_drain(drain_path, token, before_publish=before_publish)
            directory = os.open(drain_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
                canonical_owned()
                result = action()
                canonical_owned()
                verify_owned_drain(drain, drain_path, token, canonical_owned)
                if before_release is not None:
                    canonical_owned()
                    before_release(result)
                    canonical_owned()
                final_drain_guard = verify_owned_drain(drain, drain_path, token, canonical_owned)
                if not keep_drain:
                    if release_fence is not None:release_fence()
                    final_drain_guard()
                    os.unlink(drain_path)
                    try:os.fsync(directory)
                    except BaseException as error:raise CommittedReleaseError(result) from error
                return result
            finally:
                os.close(directory)
        finally:
            if drain is not None:
                os.close(drain)
            os.close(lock)



def finish_owned_drain(lock_path, drain_path, token, check, action, *, on_failure=None, commit=None, release=True, lock_bound=None, release_fence=None, root_custody=False):
    """Finish under the existing lock; delete only the unchanged owned drain.

    The root caller verifies canonical paths and supplies fresh state checks.
    This function never creates a lock, replaces a drain or clears a lease.
    """
    import fcntl
    if not isinstance(token, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,128}', token):
        raise ValueError('Invalid finish token')
    lock_path, drain_path = Path(lock_path), Path(drain_path)
    with retained_ancestor_selection(lock_path, authenticate=root_custody) as verify_lock_ancestors, retained_ancestor_selection(drain_path, authenticate=root_custody) as verify_drain_ancestors:
        lock = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        drain = directory = None
        try:
            original = os.fstat(lock)
            if (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1
                    or original.st_uid != os.geteuid() or original.st_mode & 0o022):
                raise ValueError('Untrusted build lock')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            drain = os.open(drain_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            owned = os.fstat(drain)
            if (not stat.S_ISREG(owned.st_mode) or owned.st_nlink != 1
                    or owned.st_uid != os.geteuid() or owned.st_mode & 0o077):
                raise ValueError('Untrusted owned drain')
            directory = os.open(drain_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            def canonical_owned():
                verify_lock_ancestors()
                verify_drain_ancestors()
                current = lock_path.lstat()
                if ((current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
                        or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                        or current.st_uid != os.geteuid() or current.st_mode & 0o022):
                    raise ValueError('Canonical build lock ownership lost')
            if lock_bound is not None:lock_bound(canonical_owned)
            def still_owned():
                return verify_owned_drain(drain, drain_path, token, canonical_owned)
            still_owned()
            check()
            still_owned()
            released = False
            try:
                result = action()
                still_owned()
                check()
                still_owned()
                if commit is not None:
                    commit()
                final_drain_guard = still_owned()
                if release:
                    if release_fence is not None:release_fence()
                    final_drain_guard()
                    os.unlink(drain_path)
                    released = True
                    try:os.fsync(directory)
                    except BaseException as error:raise CommittedReleaseError(result) from error
                return result
            except BaseException as error:
                if on_failure is not None and not released:
                    canonical_owned()
                    on_failure(error, canonical_owned)
                raise
        finally:
            if directory is not None:os.close(directory)
            if drain is not None:os.close(drain)
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


def parse_recovery_service_state(raw):
    """Accept only an exact inactive unit or a running unit to verify in /proc."""
    expected_keys = {'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlPID'}
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError('Unavailable recovery service record')
    rows = raw.strip().splitlines()
    if len(rows) != 5 or any('=' not in row for row in rows):
        raise ValueError('Incomplete or duplicate recovery service record')
    fields = dict(row.split('=', 1) for row in rows)
    if set(fields) != expected_keys or fields['LoadState'] != 'loaded' or fields['ControlPID'] != '0':
        raise ValueError('Unknown recovery service record')
    inactive = fields['ActiveState'] == 'inactive' and fields['SubState'] == 'dead' and fields['MainPID'] == '0'
    active = fields['ActiveState'] == 'active' and fields['SubState'] == 'running' and re.fullmatch('[1-9][0-9]{0,9}', fields['MainPID'])
    if not (inactive or active):raise ValueError('Unknown recovery service state')
    return fields


def parse_inactive_service_state(raw):
    """Parse an exact native inactive unit record; unknown state is not absence."""
    expected = dict(LoadState='loaded', ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0')
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError('Unavailable inactive service record')
    rows = raw.strip().splitlines()
    if len(rows) != len(expected) or any('=' not in row for row in rows):
        raise ValueError('Incomplete or duplicate inactive service record')
    fields = dict(row.split('=', 1) for row in rows)
    if fields != expected:
        raise ValueError('Admission service is not exactly loaded and inactive')
    return fields


def validate_pending_marker(pending, current_boot, *, committed_original=None):
    if not isinstance(pending, dict):raise ValueError('Invalid pending marker')
    if committed_original is not None:
        expected = dict(nonce=committed_original['nonce'], boot=committed_original['boot'],
                        source_main=committed_original['source_main'])
        if pending != expected:raise ValueError('Committed marker differs from original update')
    elif pending.get('boot') != current_boot:
        raise ValueError('Pending update survived reboot; explicit recovery required')
    return pending.get('nonce')


def live_snapshot(plan, *, committed_original=None):
    """Read fixed host facts, never acquire leases or start containers."""
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
    if committed_original is not None:
        service_state = parse_recovery_service_state(checked_command(['/usr/bin/systemctl',
            'show', 'gha-admission.service', '--property=LoadState', '--property=ActiveState',
            '--property=SubState', '--property=MainPID', '--property=ControlPID'], timeout=2))
        if service_state['ActiveState'] == 'inactive':
            service_pid = service_ticks = None  # No running process is observed.
        else:
            service_pid, service_ticks = service_identity()
            if str(service_pid) != service_state['MainPID']:
                raise ValueError('Running recovery process changed during observation')
            service_state['StartTicks'] = service_ticks
    else:
        service_state = None
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
        current_boot = None if committed_original is not None else Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        pending_nonce = validate_pending_marker(pending, current_boot, committed_original=committed_original)
    result = dict(
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
    if service_state is not None:result['service_state'] = service_state
    return result




def close_launcher_execution(path, *, expected=None, before_change=None, freshness=None):
    """Close only an explicitly selected launcher; never adopt an unbound path."""
    if not isinstance(expected, dict):raise ValueError('Selected launcher state required')
    set_verified_launcher_mode(path, expected, 0o644, before_change=before_change, freshness=freshness)



def restore_launcher_execution(path, expected, *, before_change=None, freshness=None):
    """Restore execution only on the still-held, selected inode and bytes."""
    if expected['mode'] != 0o644:
        raise ValueError('Expected launcher must be held')
    set_verified_launcher_mode(path, expected, 0o755, before_change=before_change, freshness=freshness)


def set_verified_launcher_mode(path, expected, mode, *, before_change=None, freshness=None):
    if mode not in (0o644, 0o755) or expected['mode'] not in (0o644, 0o755):
        raise ValueError('Unexpected launcher execution mode')
    path = Path(path)
    with retained_ancestor_selection(path, authenticate=True) as verify_ancestors:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            current = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_size > 262144
                    or stat.S_IMODE(info.st_mode) != expected['mode']
                    or (info.st_dev, info.st_ino) != (expected['device'], expected['inode'])
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                    or info.st_uid != expected['uid'] or info.st_gid != expected['gid']
                    or hashlib.sha256(os.read(fd, 262145)).hexdigest() != expected['sha256']):
                raise ValueError('Held launcher changed before restoring execution')
            if before_change is not None:before_change()
            def verify_selected():
                verify_ancestors()
                verify_descriptor_selection(fd,path,info)
            verify_selected()
            if freshness is not None:freshness()
            if before_change is not None:before_change()
            verify_selected()
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)


def mkdir_exact(path, mode):
    path = Path(path)
    path.mkdir(mode=mode)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        selected = os.fstat(fd)
        if not stat.S_ISDIR(selected.st_mode) or selected.st_uid != os.geteuid():
            raise ValueError('New directory custody differs')
        verify_descriptor_selection(fd,path,selected)
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



@contextmanager
def selected_service_bindings(plan):
    """Keep the unchanged service/config identities selected through an effect."""
    bindings = (('/etc/oxidex-remote-build.json','config_sha256'),
                ('/etc/gha-admission.json','admission_sha256'),
                ('/etc/systemd/system/gha-admission.service','service_sha256'))
    with ExitStack() as stack:
        guards = []
        for path, field in bindings:
            selected = root_file_state(path)
            if selected is None or selected['sha256'] != plan[field]:
                raise ValueError('Selected service binding changed: '+path)
            guards.append(stack.enter_context(retained_file_selection(path, selected,
                          lambda path=path:root_file_state(path))))
        def verify():
            for guard in guards:guard()
        yield verify


def prepare_boot_hold(plan, canonical_guard):
    state = root_file_state(BOOT_GUARD)
    if state is not None and (state['sha256'] != hashlib.sha256(BOOT_GUARD_BYTES).hexdigest() or state['mode'] != 0o644):
        raise ValueError('Unknown admission boot guard')
    if root_file_state(PENDING_UPDATE) is not None or root_file_state(CURRENT_BOOT_PERMIT) is not None:
        raise ValueError('Existing update hold requires explicit recovery')
    trusted_directory(BOOT_GUARD.parent, create=True)
    def publication(target, selected):
        with retained_file_selection(target, selected, lambda:root_file_state(target)) as verify:
            with selected_service_bindings(plan) as verify_bindings:
                file_mutation_fence(plan)
                canonical_guard()
                verify_bindings();verify()
    if state is None:
        published = atomic_write(BOOT_GUARD, BOOT_GUARD_BYTES, 0o644,
                     before_replace=lambda:publication(BOOT_GUARD,None), exclusive=True)
        state, raw = root_file_read(BOOT_GUARD)
        if state != published or raw != BOOT_GUARD_BYTES:
            raise ValueError('Own boot guard publication changed')
    with retained_file_selection(BOOT_GUARD,state,lambda:root_file_state(BOOT_GUARD)) as verify:
        with selected_service_bindings(plan) as verify_bindings:
            file_mutation_fence(plan)
            canonical_guard()
            verify_bindings();verify()
            checked_command(['/usr/bin/systemctl', 'daemon-reload'])
    atomic_write(CURRENT_BOOT_PERMIT, plan['nonce'].encode(), 0o600,
                 before_replace=lambda:publication(CURRENT_BOOT_PERMIT,None), exclusive=True)
    # Durable before the first of the fifteen runtime replacements.
    atomic_write(PENDING_UPDATE, (json.dumps(dict(nonce=plan['nonce'], boot=plan['boot'],
                 source_main=plan['source_main']))+'\n').encode(), 0o600,
                 before_replace=lambda:publication(PENDING_UPDATE,None), exclusive=True)



def unlink_selected_marker(path, expected, canonical_guard, freshness):
    """Verify custody, then canonical authority, then time immediately before unlink."""
    path = Path(path)
    with retained_file_selection(path, expected, lambda:root_file_state(path)) as verify:
        canonical_guard()
        verify()
        freshness()
        canonical_guard()
        verify()
        if expected is not None:os.unlink(path)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:os.fsync(parent)
    finally:os.close(parent)


def verify_boot_guard():
    state, raw = root_file_read(BOOT_GUARD)
    if state is None or state['mode'] != 0o644 or raw != BOOT_GUARD_BYTES:
        raise ValueError('Managed admission drop-in custody, mode or bytes differ')
    return state


def verify_boot_hold(plan):
    verify_boot_guard()
    guard, _ = root_file_read(BOOT_GUARD)
    _, pending = root_file_read(PENDING_UPDATE)
    _, permit = root_file_read(CURRENT_BOOT_PERMIT)
    expected = dict(nonce=plan['nonce'], boot=plan['boot'], source_main=plan['source_main'])
    if (guard is None or guard['sha256'] != hashlib.sha256(BOOT_GUARD_BYTES).hexdigest()
            or guard['mode'] != 0o644 or json.loads(pending) != expected
            or permit != plan['nonce'].encode()):
        raise ValueError('Persistent boot hold or current-boot permit differs')


def transaction_lock_guard():
    """Bind the existing EX authority; unbound mutation checks fail closed."""
    holder = []
    def bind(check):holder[:] = [check]
    def verify():
        if not holder:raise ValueError('Transaction lock authority is not bound')
        holder[0]()
    return verify, bind


def apply_replacement_fence(plan, target, expected_file, canonical_guard):
    """Finish all blocking checks, recheck selected target, then selected time."""
    import time
    mutation_idle_fence(plan)
    file_mutation_fence(plan, target, expected_file=expected_file)
    selected = plan['before'][target] if expected_file is None else expected_file
    with retained_file_selection(target, selected, lambda:root_file_state(target)) as verify:
        validate_plan(plan, time.time())
        canonical_guard()
        verify()


def apply_update(plan):
    """Replace only approved runtime files after actual idle-state confirmation."""
    import time
    lock_guard, bind_lock = transaction_lock_guard()
    assets = validate_plan(plan, time.time())
    journal_history = [None]
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
        atomic_write(backup / 'plan.json', (json.dumps(plan, sort_keys=True) + '\n').encode(), 0o600, before_replace=lock_guard, exclusive=True)
        journal = backup / 'journal.jsonl'
        journal_history[0] = root_file_read(journal)

        def record(stage, **facts):
            lock_guard()
            fresh = None if stage.startswith('HOLD_') else lambda:validate_plan(plan,time.time())
            journal_history[0] = append_journal_record(journal, dict(stage=stage, observed_at=time.time(), **facts), before_publish=lock_guard, freshness=fresh, selected_history=journal_history[0])

        record('BACKUP_START')
        try:
            for index, target in enumerate(sorted(TARGETS)):
                validate_plan(plan, time.time())
                if root_file_state(target) != plan['before'][target]:
                    raise ValueError('Runtime changed before backup')
                if plan['before'][target] is not None:
                    atomic_write(backup / (str(index) + '.before'), Path(target).read_bytes(), 0o600, before_replace=lock_guard, exclusive=True)
            record('BACKUPS_COMPLETE', before=plan['before'])
            # Preparation has not changed live code. This durable permission
            # transition is the first live cutover, including the old launcher.
            mutation_idle_fence(plan)
            file_mutation_fence(plan, LAUNCHER_TARGET)
            close_launcher_execution(LAUNCHER_TARGET, expected=plan['before'][LAUNCHER_TARGET],
                                     before_change=lock_guard, freshness=lambda:validate_plan(plan,time.time()))
            held_launcher = root_file_state(LAUNCHER_TARGET)
            intended = dict(plan['before'][LAUNCHER_TARGET], mode=0o644)
            if held_launcher != intended:
                raise ValueError('Durable launcher execution hold differs')
            record('MUTATING_HELD', launcher=held_launcher)
            prepare_boot_hold(plan, lock_guard)
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
                    apply_replacement_fence(plan, target, expected_file, lock_guard)
                installed_mode = 0o644 if target == LAUNCHER_TARGET else TARGETS[target]
                atomic_write(target, assets[target], installed_mode, before_replace=before_replace)
                record('FILE_REPLACED', target=target,
                       sha256=hashlib.sha256(assets[target]).hexdigest())
            validate_plan(plan, time.time())
            mutation_idle_fence(plan)
            trusted_directory('/mnt/runner-data/remote-build/qualification-sealed', create=True)
            validate_plan(plan, time.time())
            mutation_idle_fence(plan)
            with selected_service_bindings(plan) as verify_bindings:
                mutation_idle_fence(plan)
                file_mutation_fence(plan)
                lock_guard()
                verify_bindings()
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
            verify_installed_runtime(plan['assets'], after['before'],
                client_launcher_sha256=hashlib.sha256(assets[LAUNCHER_TARGET]).hexdigest())
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
        lock_guard()
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
            verify_installed_runtime(plan['assets'], snapshot['before'],
                client_launcher_sha256=hashlib.sha256(assets[LAUNCHER_TARGET]).hexdigest())
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
            journal_history[0] = append_journal_record(backup/'journal.jsonl', dict(stage='HOLD_BEFORE_DRAIN_RELEASE',
                observed_at=time.time(), error_type=type(error).__name__), before_publish=lock_guard, selected_history=journal_history[0])
            raise
    return drained_update('/run/oxidex-build.lock', '/run/oxidex-build-draining',
                          plan['nonce'], install, before_drain=before_drain, before_release=before_release, keep_drain=True, lock_bound=bind_lock, root_custody=True, publication_fence=lambda:validate_plan(plan,time.time()))


def installed_history(plan, client_pin, *, recovery=False, with_history=False):
    """Read the original private records; do not re-date the installation."""
    backup = Path('/var/lib/oxidex-builder') / ('runtime-update-' + plan['nonce'])
    _, raw = root_file_read(backup / 'plan.json')
    journal_state, journal = root_file_read(backup / 'journal.jsonl')
    original = json.loads(raw)
    rows = [json.loads(line) for line in journal.splitlines()]
    import time
    verify_installed_history(plan, original, rows, client_pin, time.time(), recovery=recovery)
    return (backup, (journal_state, journal)) if with_history else backup


def finish_update(plan, client_pin):
    """Reopen the verified ordinary route; qualification still needs live proof."""
    import time
    lock_guard, bind_lock = transaction_lock_guard()
    validate_plan(plan, time.time())
    backup = installed_history(plan, client_pin)
    journal_history = [None]
    expected = dict(plan, before=dict(plan['before']))
    pending_state = root_file_state(PENDING_UPDATE)
    permit_state = root_file_state(CURRENT_BOOT_PERMIT)
    phase = dict(started=False, opened=False, pending=False, permit=False)

    def record(stage, *, guard=None, **facts):
        forward = guard is None
        guard = guard or lock_guard
        guard()
        if journal_history[0] is None:raise ValueError('No authorized installation history bound')
        journal_history[0] = append_journal_record(backup/'journal.jsonl', dict(stage=stage, observed_at=time.time(), **facts),
            before_publish=guard, freshness=(lambda:validate_plan(plan,time.time())) if forward else None, selected_history=journal_history[0])

    def check():
        lock_guard()
        validate_plan(plan, time.time())
        if journal_history[0] is None:
            _, journal_history[0] = installed_history(plan, client_pin, with_history=True)
        if root_file_read(backup/'journal.jsonl') != journal_history[0]:
            raise ValueError('Previously authorized installation history changed')
        snapshot = live_snapshot(expected)
        check_snapshot(expected, snapshot, owned_drain=True)
        mutation_idle_fence(plan)
        admission_ready(plan, plan['service_pid'], plan['service_startticks'], wait=False)
        guard = root_file_state(BOOT_GUARD)
        if guard is None or guard['sha256'] != hashlib.sha256(BOOT_GUARD_BYTES).hexdigest():
            raise ValueError('Admission boot guard changed during finish')
        if root_file_state(PENDING_UPDATE) != (None if phase['pending'] else pending_state):
            raise ValueError('Persistent finish marker changed')
        if root_file_state(CURRENT_BOOT_PERMIT) != (None if phase['permit'] else permit_state):
            raise ValueError('Current-boot finish permit changed')
        if not phase['pending'] and not phase['permit']:
            verify_boot_hold(plan)
        validate_plan(plan, time.time())

    def remove_marker(path, state):
        check()
        if state is None:raise ValueError('Finish does not own the selected marker')
        unlink_selected_marker(path, state, lock_guard, lambda:validate_plan(plan,time.time()))

    def reopen():
        check()
        record('FINISH_START', client_launcher_sha256=client_pin)
        validate_plan(plan, time.time())
        phase['started'] = True
        # Execution remains held until the durable release decision below.
        remove_marker(CURRENT_BOOT_PERMIT, permit_state);phase['permit'] = True
        check()
        record('FINISH_READY_TO_RELEASE', files=expected['before'])
        return dict(status='REOPENED_UNQUALIFIED', qualification_ready=False,
                    source_main=plan['source_main'], nonce=plan['nonce'],
                    client_launcher_sha256=client_pin, ordinary_build_proof_required=True)

    def commit_release():
        # All release checks have passed under EX. This fsynced decision permits
        # ordinary routing after reboot even if power is lost before /run cleanup.
        validate_plan(plan, time.time())
        release_files = dict(expected['before'])
        release_files[LAUNCHER_TARGET] = dict(expected['before'][LAUNCHER_TARGET], mode=0o755)
        record('FINISH_RELEASE_COMMITTED', files=release_files, observed_files=expected['before'],
               client_launcher_sha256=client_pin, qualification_ready=False)
        validate_plan(plan, time.time())
        restore_launcher_execution(LAUNCHER_TARGET, plan['before'][LAUNCHER_TARGET],
                                   before_change=lock_guard, freshness=lambda:validate_plan(plan,time.time()))
        phase['opened'] = True
        expected['before'] = release_files
        check()
        if pending_state is None or root_file_state(PENDING_UPDATE) != pending_state:
            raise ValueError('Persistent marker changed before committed release')
        unlink_selected_marker(PENDING_UPDATE, pending_state, lock_guard,
                               lambda:validate_plan(plan,time.time()))
        phase['pending'] = True
        validate_plan(plan, time.time())

    def hold_failure(error, canonical_guard):
        # Keep executable permission closed on the exact owned launcher after a
        # partial finish. Never restore bytes or manipulate unrelated services.
        held = False
        current = root_file_state(LAUNCHER_TARGET)
        selected = plan['before'][LAUNCHER_TARGET]
        if current is not None and current.get('mode') in (0o644, 0o755):
            if dict(current, mode=selected['mode']) != selected:
                raise ValueError('Failure holding cannot adopt changed selected custody')
            record('HOLD_FINISH_INTENT', launcher=current, guard=canonical_guard)
            close_launcher_execution(LAUNCHER_TARGET, expected=current, before_change=canonical_guard)
            held = root_file_state(LAUNCHER_TARGET)['mode'] == 0o644
        record('HOLD_FINISH_RECOVERY', error_type=type(error).__name__, execution_held=held, guard=canonical_guard)

    result = finish_owned_drain('/run/oxidex-build.lock', '/run/oxidex-build-draining',
                                plan['nonce'], check, reopen, on_failure=hold_failure, commit=commit_release,
                                lock_bound=bind_lock, root_custody=True, release_fence=lambda:validate_plan(plan,time.time()))
    # The durable release decision was recorded under EX. The returned receipt
    # observes actual drain removal; do not append outside the protected lock.
    return result


def capture_committed_recovery(nonce, source_main, assets, client_pin, *, with_history=False):
    """Capture a new boot and inactive unit without changing the saved update."""
    import time
    if not isinstance(nonce, str) or not re.fullmatch('[a-f0-9]{32}', nonce):
        raise ValueError('Exact installed nonce required for committed recovery')
    backup = Path('/var/lib/oxidex-builder') / ('runtime-update-' + nonce)
    _, raw = root_file_read(backup/'plan.json')
    original = json.loads(raw)
    validate_plan(original, original.get('observed_at', time.time()))
    journal_state, raw = root_file_read(backup/'journal.jsonl')
    journal = [json.loads(line) for line in raw.splitlines()]
    observed_at = time.time()
    verify_boot_guard()
    snapshot = live_snapshot({}, committed_original=original)
    if (snapshot['pending_nonce'] not in (None, nonce) or snapshot['drain_token'] not in (None, nonce)
            or snapshot['image_pin'] != snapshot['image_id'] or snapshot['admission_valid'] is not True
            or snapshot['cpu'] != 16 or snapshot['role'] != 'builder'
            or snapshot['cpus'] != 16 or snapshot['memory_gib'] != 28
            or snapshot['tools_ready'] is not True or snapshot['leases'] != {}
            or snapshot['containers'] != [] or snapshot['upload_processes'] != []):
        raise ValueError('Committed reboot host is not exactly idle and protected')
    observation = dict(schema=2, nonce=nonce, source_main=source_main,
        observed_at=observed_at, assets=assets,
        **{field:snapshot[field] for field in ('vm_id', 'boot', 'image_id', 'config_sha256',
            'admission_sha256', 'service_sha256', 'service_state', 'before')})
    verify_committed_reboot(original, journal, observation, client_pin, time.time())
    return (observation, (journal_state, raw), original) if with_history else observation


def ensure_recovery_lock(path):
    """Initialize only an absent volatile lock; preserve every existing inode."""
    path = Path(path)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise ValueError('Untrusted recovery lock parent')
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd);current = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o022
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('Untrusted existing recovery lock')
        os.fsync(fd)
    finally:os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:os.fsync(directory)
    finally:os.close(directory)


def complete_committed_recovery(plan, client_pin):
    """Complete an explicitly selected durable release after reboot."""
    import time
    lock_guard, bind_lock = transaction_lock_guard()
    validate_committed_observation(plan, time.time())
    # Fresh capture reads the immutable original records and exact inactive unit.
    current, authorized, original = capture_committed_recovery(plan['nonce'], plan['source_main'], plan['assets'], client_pin, with_history=True)
    journal_history = [authorized]
    validate_runtime_observation(plan, time.time())
    if any(current[key] != value for key, value in plan.items() if key != 'observed_at'):
        raise ValueError('Committed recovery observation changed')
    backup = Path('/var/lib/oxidex-builder') / ('runtime-update-' + plan['nonce'])
    pending = root_file_state(PENDING_UPDATE)
    _, raw = root_file_read('/run/oxidex-build-draining')
    if raw not in (b'', plan['nonce'].encode()):
        raise ValueError('Foreign committed-recovery drain')

    phase = {'active':None, 'lock_state':None, 'permit':None}
    def check():
        lock_guard()
        if root_file_read(backup/'journal.jsonl') != journal_history[0]:
            raise ValueError('Previously authorized committed history changed')
        if phase['lock_state'] is not None and root_file_state('/run/oxidex-build.lock') != phase['lock_state']:
            raise ValueError('Selected canonical recovery lock changed')
        verify_boot_guard()
        if phase['active'] is not None:
            active = phase['active']
            validate_plan(active, time.time())
            check_snapshot(active, live_snapshot(active, committed_original=original), owned_drain=True)
            admission_ready(active, active['service_pid'], active['service_startticks'], wait=False)
            return
        validate_runtime_observation(plan, time.time())
        fresh = capture_committed_recovery(plan['nonce'], plan['source_main'], plan['assets'], client_pin)
        if any(fresh[key] != value for key, value in plan.items() if key != 'observed_at'):
            raise ValueError('Committed recovery bindings changed under lock')
        validate_runtime_observation(plan, time.time())
        if phase['lock_state'] is not None and root_file_state('/run/oxidex-build.lock') != phase['lock_state']:
            raise ValueError('Canonical recovery lock changed during observation')
        if root_file_state(PENDING_UPDATE) != pending:
            raise ValueError('Persistent committed marker changed')
        validate_runtime_observation(plan, time.time())

    def complete():
        nonlocal pending
        check()
        # The persistent hold survives service/postflight failures and another
        # reboot. Only this boot's exact nonce permit allows the guarded start.
        def hold_publication():
            with ExitStack() as stack:
                guards = [stack.enter_context(retained_file_selection(PENDING_UPDATE, pending,
                          lambda:root_file_state(PENDING_UPDATE)))]
                if phase['permit'] is not None:
                    guards.append(stack.enter_context(retained_file_selection(CURRENT_BOOT_PERMIT,
                        phase['permit'], lambda:root_file_state(CURRENT_BOOT_PERMIT))))
                validate_runtime_observation(plan, time.time())
                lock_guard()
                for verify in guards:verify()
        if pending is None:
            pending_raw = (json.dumps(dict(nonce=original['nonce'], boot=original['boot'],
                           source_main=original['source_main']))+'\n').encode()
            published = atomic_write(PENDING_UPDATE, pending_raw, 0o600,
                                     before_replace=hold_publication, exclusive=True)
            pending, observed_raw = root_file_read(PENDING_UPDATE)
            if pending != published or observed_raw != pending_raw:
                raise ValueError('Own persistent recovery hold publication changed')
        permit, permit_raw = root_file_read(CURRENT_BOOT_PERMIT)
        if permit is not None and (permit['mode'] != 0o600 or permit_raw != plan['nonce'].encode()):
            raise ValueError('Foreign current-boot recovery permit')
        if permit is None:
            published = atomic_write(CURRENT_BOOT_PERMIT, plan['nonce'].encode(), 0o600,
                                     before_replace=hold_publication, exclusive=True)
            permit, observed_raw = root_file_read(CURRENT_BOOT_PERMIT)
            if permit != published or observed_raw != plan['nonce'].encode():
                raise ValueError('Own current-boot permit publication changed')
        phase['permit'] = permit
        released_files = dict(plan['before'])
        selected = plan['before'][LAUNCHER_TARGET]
        if selected['mode'] == 0o644:
            journal_history[0] = append_journal_record(backup/'journal.jsonl', dict(stage='COMMITTED_LAUNCHER_RESTORE_INTENT',
                observed_at=time.time(), launcher=selected), before_publish=lock_guard, freshness=lambda:validate_runtime_observation(plan,time.time()), selected_history=journal_history[0])
            validate_runtime_observation(plan, time.time())
            restore_launcher_execution(LAUNCHER_TARGET, selected,
                                       before_change=lock_guard, freshness=lambda:validate_runtime_observation(plan,time.time()))
            released_files[LAUNCHER_TARGET] = dict(selected, mode=0o755)
        remaining = plan['observed_at']+120-time.time()
        if remaining <= 0:raise ValueError('Recovery startup observation expired')
        startup_timeout = min(20,remaining)
        with selected_service_bindings(plan) as verify_bindings:
            hold_publication()
            verify_bindings()
            if plan['service_state']['ActiveState'] == 'inactive':
                checked_command(['/usr/bin/systemctl', 'start', 'gha-admission.service'],
                                timeout=startup_timeout)
        pid, ticks = service_identity(strict=False)
        if plan['service_state']['ActiveState'] == 'active' and (pid, ticks) != (
                int(plan['service_state']['MainPID']), plan['service_state']['StartTicks']):
            raise ValueError('Selected running recovery process changed')
        active = {key:value for key,value in plan.items() if key != 'service_state'}
        active.update(schema=1, service_pid=pid, service_startticks=ticks, before=released_files)
        phase['active'] = active
        admission_ready(active, pid, ticks)
        snapshot = live_snapshot(active, committed_original=original)
        check_snapshot(active, snapshot, owned_drain=True)
        return dict(status='REBOOT_RECOVERED_UNQUALIFIED', qualification_ready=False,
                    service_pid=pid, service_startticks=ticks, boot=plan['boot'])

    def publish_success():
        active = phase['active']
        journal_history[0] = append_journal_record(backup/'journal.jsonl', dict(stage='COMMITTED_REBOOT_RECOVERED',
            observed_at=time.time(), boot=plan['boot'], service_pid=active['service_pid'],
            service_startticks=active['service_startticks'], files=active['before'],
            client_launcher_sha256=client_pin, qualification_ready=False), before_publish=lock_guard, freshness=lambda:validate_runtime_observation(plan,time.time()), selected_history=journal_history[0])
        validate_runtime_observation(plan, time.time())
        unlink_selected_marker(PENDING_UPDATE, pending, lock_guard,
                               lambda:validate_runtime_observation(plan,time.time()))
        unlink_selected_marker(CURRENT_BOOT_PERMIT, phase['permit'], lock_guard,
                               lambda:validate_runtime_observation(plan,time.time()))

    # No unrelated launcher needs to recreate the volatile mutex after reboot.
    validate_runtime_observation(plan, time.time())
    ensure_recovery_lock('/run/oxidex-build.lock')
    phase['lock_state'] = root_file_state('/run/oxidex-build.lock')
    if phase['lock_state'] is None:raise ValueError('Recovery mutex disappeared')
    # Keep the drain on any failure; no lease is invented or cleared.
    if raw:
        return finish_owned_drain('/run/oxidex-build.lock', '/run/oxidex-build-draining',
                                  plan['nonce'], check, complete, commit=publish_success, lock_bound=bind_lock, root_custody=True, release_fence=lambda:validate_runtime_observation(plan,time.time()))
    return drained_update('/run/oxidex-build.lock', '/run/oxidex-build-draining', plan['nonce'],
                          complete, before_drain=check, before_release=lambda result:(check(), publish_success()), lock_bound=bind_lock, root_custody=True, release_fence=lambda:validate_runtime_observation(plan,time.time()), publication_fence=lambda:validate_runtime_observation(plan,time.time()))


def recover_finish_update(plan, client_pin):
    """Return a same-boot interrupted finish to its exact owned held state."""
    import time
    lock_guard, bind_lock = transaction_lock_guard()
    validate_plan(plan, time.time())
    backup = installed_history(plan, client_pin, recovery=True)
    journal_history = [None]
    expected = dict(plan, before=dict(plan['before']))
    _, drain_raw = root_file_read('/run/oxidex-build-draining')
    if drain_raw not in (b'', plan['nonce'].encode()):
        raise ValueError('Recovery cannot replace a foreign drain')
    existing_drain = bool(drain_raw)
    phase = dict(started=False, lock_state=root_file_state('/run/oxidex-build.lock'))
    if phase['lock_state'] is None:raise ValueError('Missing same-boot recovery mutex')

    def record(stage, **facts):
        lock_guard()
        if journal_history[0] is None:raise ValueError('Unbound recovery history')
        journal_history[0] = append_journal_record(backup/'journal.jsonl', dict(stage=stage, observed_at=time.time(), **facts), before_publish=lock_guard, freshness=lambda:validate_plan(plan,time.time()), selected_history=journal_history[0])

    def check(*, owned=True):
        lock_guard()
        if root_file_state('/run/oxidex-build.lock') != phase['lock_state']:
            raise ValueError('Canonical same-boot recovery lock changed')
        validate_plan(plan, time.time())
        if journal_history[0] is None:
            _, journal_history[0] = installed_history(plan, client_pin, recovery=True, with_history=True)
        if root_file_read(backup/'journal.jsonl') != journal_history[0]:
            raise ValueError('Authorized recovery history changed')
        snapshot = live_snapshot(expected)
        check_snapshot(expected, snapshot, owned_drain=owned)
        if owned:mutation_idle_fence(plan)
        admission_ready(plan, plan['service_pid'], plan['service_startticks'], wait=False)
        if root_file_state('/run/oxidex-build.lock') != phase['lock_state']:
            raise ValueError('Canonical recovery lock changed during checks')
        guard = root_file_state(BOOT_GUARD)
        if guard is None or guard['mode'] != 0o644 or guard['sha256'] != hashlib.sha256(BOOT_GUARD_BYTES).hexdigest():
            raise ValueError('Unknown recovery boot guard')
        pending, raw = root_file_read(PENDING_UPDATE)
        if pending is not None and json.loads(raw) != dict(nonce=plan['nonce'], boot=plan['boot'], source_main=plan['source_main']):
            raise ValueError('Foreign persistent recovery marker')
        permit, raw = root_file_read(CURRENT_BOOT_PERMIT)
        if permit is not None and raw != plan['nonce'].encode():
            raise ValueError('Foreign current-boot recovery permit')
        if root_file_state('/run/oxidex-build.lock') != phase['lock_state']:
            raise ValueError('Canonical recovery lock changed at mutation boundary')
        validate_plan(plan, time.time())
        lock_guard()

    def restore_hold():
        check()
        record('FINISH_RECOVERY_START', client_launcher_sha256=client_pin,
               launcher=plan['before'][LAUNCHER_TARGET])
        validate_plan(plan, time.time())
        phase['started'] = True
        close_launcher_execution(LAUNCHER_TARGET, expected=plan['before'][LAUNCHER_TARGET],
                                 before_change=lock_guard, freshness=lambda:validate_plan(plan,time.time()))
        expected['before'][LAUNCHER_TARGET] = dict(plan['before'][LAUNCHER_TARGET], mode=0o644)
        check()
        if root_file_state(CURRENT_BOOT_PERMIT) is None:
            atomic_write(CURRENT_BOOT_PERMIT, plan['nonce'].encode(), 0o600, before_replace=check, exclusive=True)
        if root_file_state(PENDING_UPDATE) is None:
            raw = (json.dumps(dict(nonce=plan['nonce'], boot=plan['boot'], source_main=plan['source_main']))+'\n').encode()
            atomic_write(PENDING_UPDATE, raw, 0o600, before_replace=check, exclusive=True)
        check()
        verify_boot_hold(plan)
        record('FINISH_RECOVERED_HELD', service_pid=plan['service_pid'],
               service_startticks=plan['service_startticks'], files=expected['before'])
        return dict(status='RECOVERED_HELD', nonce=plan['nonce'], qualification_ready=False,
                    requires_fresh_finish_capture=True)

    if existing_drain:
        return finish_owned_drain('/run/oxidex-build.lock', '/run/oxidex-build-draining',
                                  plan['nonce'], check, restore_hold, release=False, lock_bound=bind_lock, root_custody=True)
    return drained_update('/run/oxidex-build.lock', '/run/oxidex-build-draining', plan['nonce'],
                          restore_hold, before_drain=lambda:check(owned=False), keep_drain=True, lock_bound=bind_lock, root_custody=True, publication_fence=lambda:validate_plan(plan,time.time()))


def plan_from_snapshot(source_main, assets, snapshot, observed_at, nonce, *, owned_drain=False):
    plan = dict(schema=1, nonce=nonce, source_main=source_main,
                observed_at=observed_at, assets=assets,
                **{field: snapshot[field] for field in (
                    'vm_id', 'boot', 'image_id', 'config_sha256', 'admission_sha256', 'service_sha256',
                    'service_pid', 'service_startticks', 'before')})
    validate_plan(plan, observed_at)
    check_snapshot(plan, snapshot, owned_drain=owned_drain)
    return plan


def main():
    import argparse
    import sys
    import time
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--plan', type=Path)
    inputs.add_argument('--capture-assets', type=Path)
    parser.add_argument('--plan-sha256')
    parser.add_argument('--assets-sha256')
    parser.add_argument('--source-main')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--apply', action='store_true', help='Apply the reviewed plan; default checks only')
    actions.add_argument('--finish', action='store_true', help='Reopen an installed route; not qualification readiness')
    actions.add_argument('--recover-committed', action='store_true', help='Explicitly recover a committed release after reboot')
    actions.add_argument('--recover-finish', action='store_true', help='Restore a checked interrupted finish to held state')
    parser.add_argument('--finish-nonce', help='Capture fresh observations for this installed update nonce')
    parser.add_argument('--client-launcher-sha256', help='Explicit paired client launcher pin')
    args = parser.parse_args()
    if (sys.platform != 'linux' or os.geteuid() != 0
            or not sys.flags.isolated or not sys.dont_write_bytecode):
        raise ValueError('Run through the existing authenticated root control path on the builder')
    if args.capture_assets:
        if args.apply or args.finish or not args.source_main or not args.assets_sha256:
            parser.error('Capture requires source-main/assets-sha256 and cannot apply')
        state, raw = root_file_read(args.capture_assets)
        if state is None or state['sha256'] != args.assets_sha256:
            raise ValueError('Candidate assets differ from selected digest')
        assets = checked_json(raw, args.assets_sha256)
        validate_assets(assets)
        if args.recover_committed:
            if not args.finish_nonce or not args.client_launcher_sha256:
                parser.error('Committed recovery capture requires nonce and paired client pin')
            observation = capture_committed_recovery(args.finish_nonce, args.source_main, assets, args.client_launcher_sha256)
            print(json.dumps(observation, sort_keys=True))
            return
        observed_at = time.time()
        snapshot = live_snapshot({})
        if args.recover_finish and not args.finish_nonce:
            parser.error('Recovery capture requires the installed finish nonce')
        if args.finish_nonce and (not re.fullmatch('[a-f0-9]{32}', args.finish_nonce) or not args.client_launcher_sha256):
            parser.error('Finish capture requires exact nonce and paired client launcher pin')
        plan = plan_from_snapshot(args.source_main, assets, snapshot, observed_at,
            args.finish_nonce or uuid.uuid4().hex, owned_drain=bool(snapshot['drain_token']) if args.recover_finish else bool(args.finish_nonce))
        if args.finish_nonce:installed_history(plan, args.client_launcher_sha256, recovery=args.recover_finish)
        validate_plan(plan, time.time())
        print(json.dumps(plan, sort_keys=True))
        return
    if not args.plan_sha256:
        parser.error('Check/apply requires plan-sha256')
    state, raw = root_file_read(args.plan)
    if state is None or state['sha256'] != args.plan_sha256:
        raise ValueError('Plan differs from the explicitly selected reviewed digest')
    plan = checked_json(raw, args.plan_sha256)
    if args.recover_committed:
        if not args.client_launcher_sha256:
            parser.error('Committed recovery requires paired client pin')
        result = complete_committed_recovery(plan, args.client_launcher_sha256)
        print(json.dumps(result, sort_keys=True))
        return
    validate_plan(plan, time.time())
    snapshot = live_snapshot(plan)
    validate_plan(plan, time.time())
    if args.finish_nonce:
        parser.error('finish-nonce is only valid with capture-assets')
    if args.apply and args.client_launcher_sha256:
        parser.error('A client pin selects finish checks and cannot be combined with apply')
    finishing = args.finish or args.recover_finish or bool(args.client_launcher_sha256)
    check_snapshot(plan, snapshot, owned_drain=bool(snapshot['drain_token']) if args.recover_finish else finishing)
    if finishing:
        if not args.client_launcher_sha256:parser.error('Finish requires paired client launcher pin')
        installed_history(plan, args.client_launcher_sha256, recovery=args.recover_finish)
        if args.recover_finish:
            result = recover_finish_update(plan, args.client_launcher_sha256)
        else:
            result = (finish_update(plan, args.client_launcher_sha256) if args.finish else
                      dict(status='CHECKED_FINISH_ONLY', executed=False))
    else:
        result = apply_update(plan) if args.apply else dict(status='CHECKED_ONLY', executed=False)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:main()
    except CommittedReleaseError as error:
        print(json.dumps(error.result, sort_keys=True))
        raise SystemExit(75)
