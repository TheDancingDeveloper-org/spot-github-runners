#!/usr/bin/python3
"""Root-supervised legacy SSH transfer lifetime under the canonical fence."""
import fcntl
import errno
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import sys
import time
import uuid

SELF = '/usr/local/sbin/oxidex-uploader-session'
LOCK = '/run/oxidex-build.lock'
CGROUP = Path('/sys/fs/cgroup')


def scope_processes(path):
    """Read every process in this root-owned cgroup subtree; ambiguity holds."""
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Uploader scope custody unavailable')
    result = set();pending = [path]
    while pending:
        current = pending.pop()
        info = current.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Uploader descendant scope custody changed')
        for line in (current / 'cgroup.procs').read_text().splitlines():
            if not line.isdecimal():
                raise ValueError('Unknown uploader scope process')
            result.add(int(line))
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):pending.append(Path(entry.path))
    return result


def own_scope():
    rows = Path('/proc/self/cgroup').read_text().splitlines()
    if len(rows) != 1 or not rows[0].startswith('0::'):
        raise ValueError('Unified uploader scope required')
    path = rows[0][3:]
    if not re.fullmatch(r'/.*oxidex-uploader-[0-9a-f]{32}\.scope', path):
        raise ValueError('Root-owned uploader scope required')
    selected = CGROUP / path.lstrip('/')
    if os.getpid() not in scope_processes(selected):
        raise ValueError('Supervisor absent from selected scope')
    return selected


def supervise(command, account):
    # The scope is systemd-owned and survives the SSH transport. Only this
    # root process opens the canonical FD; the untrusted child gets none.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    scope = own_scope()
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    if fd<3:
        selected=fcntl.fcntl(fd,fcntl.F_DUPFD_CLOEXEC,3)
        os.close(fd);fd=selected
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ValueError('Untrusted canonical build fence')
        def selected_lock():
            current=os.stat(LOCK,follow_symlinks=False)
            if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):
                raise ValueError('Canonical uploader fence changed')
        selected_lock()
        fcntl.flock(fd, fcntl.LOCK_SH)
        selected_lock()
        sys.path.insert(0, '/opt/oxidex-builder')
        from qualification_trust.reservation import route_guard
        route_guard(pid=os.getpid())
        def drop_privileges():
            # close_fds also closes the canonical FD; close explicitly before
            # executing any uploader-controlled code.
            try:os.close(fd)
            except OSError as error:
                if error.errno!=errno.EBADF:raise
            for signum in (signal.SIGHUP,signal.SIGTERM,signal.SIGINT):
                signal.signal(signum,signal.SIG_DFL)
            os.setgroups(os.getgrouplist(account.pw_name, account.pw_gid))
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
        child = subprocess.Popen(['/bin/sh', '-c', command], close_fds=True,
            preexec_fn=drop_privileges,
            env={'PATH':'/usr/local/bin:/usr/bin:/bin', 'HOME':account.pw_dir,
                 'USER':account.pw_name, 'LANG':'C'})
        status = child.wait()
        # A shell may exit after forking, closing FDs or disconnecting. Its
        # cgroup descendants still hold the fence until each has exited.
        while scope_processes(scope) != {os.getpid()}:
            time.sleep(0.1)
        selected_lock()
        return status
    except BaseException:
        # An unknown post-start lifetime cannot release the fence. A root
        # operator must inspect the scope and recover custody explicitly.
        if 'child' in locals():
            while True:signal.pause()
        raise
    finally:
        os.close(fd)


def main():
    if os.geteuid() != 0:
        command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
        if not command:raise ValueError('Interactive uploader sessions not admitted')
        os.execv('/usr/bin/sudo', ['sudo', '-n', SELF, '--request', command])
    sys.path.insert(0, '/opt/oxidex-builder')
    from qualification_trust.control_observation import root_json
    config = root_json('/etc/oxidex-builder-uploader.json')
    account = pwd.getpwnam(config['username'])
    if (len(sys.argv) != 3 or sys.argv[1] not in ('--request', '--supervise')
            or not sys.argv[2] or len(sys.argv[2].encode()) > 16384):
        raise ValueError('Bounded uploader command required')
    if os.environ.get('SUDO_UID') != str(account.pw_uid) or account.pw_uid != config['uid']:
        raise ValueError('Actual sudo uploader identity differs')
    if sys.argv[1] == '--request':
        unit = 'oxidex-uploader-' + uuid.uuid4().hex
        return subprocess.run(['/usr/bin/systemd-run', '--scope', '--quiet', '--collect',
            '--unit=' + unit, '--property=KillMode=control-group',
            SELF, '--supervise', sys.argv[2]], check=False).returncode
    return supervise(sys.argv[2], account)


if __name__ == '__main__':raise SystemExit(main())
