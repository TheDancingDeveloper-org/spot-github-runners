"""Root guardian: retains admission locks until all imported processes are reaped.

The launcher may die, including SIGKILL: EOF on its private lifetime pipe cancels
this guardian. The unprivileged importer never inherits the pipe or lock FDs.
Administrative SIGKILL of the guardian itself is outside this lifetime contract.
"""
import ctypes
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


def enable_subreaper():
    if sys.platform != 'linux':
        raise RuntimeError('Import supervision requires Linux child subreaper')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'Cannot become import subreaper')


def reap_import(process, *, subreaper):
    """Never release locks while a descendant remains, even in uninterruptible I/O."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # The group already exited; wait and adopted-child reaping still follow.
    process.wait()
    if not subreaper:
        # Portable test mode covers same-group children only; production forbids it.
        return
    while True:
        try:
            child, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if child:
            continue
        # Orphaned grandchildren (including a changed session) are adopted here.
        children = Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()
        for child in children:
            try:
                os.kill(int(child), signal.SIGKILL)
            except ProcessLookupError:
                pass  # The adopted child exited between observation and signaling.
        time.sleep(.01)


def supervise(argv, lifetime, lock_fds, *, timeout=120, credentials=None, subreaper=True):
    # Validate handoff before creating any importer. FDs remain owned here until exit.
    for fd in (lifetime, *lock_fds):
        os.fstat(fd)
    if not lock_fds:
        raise ValueError('Guardian must retain protective locks')
    if subreaper:
        enable_subreaper()
    cancelled = []
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda number, frame: cancelled.append(number))
    process = None
    try:
        # EOF can precede guardian startup; never launch after an already dead owner.
        if select.select([lifetime], [], [], 0)[0]:
            return 125
        process = subprocess.Popen(argv, env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C'},
                                   cwd='/', stdin=subprocess.DEVNULL, close_fds=True,
                                   start_new_session=True, **(credentials or {}))
        deadline = time.monotonic() + timeout
        while True:
            if cancelled or select.select([lifetime], [], [], .05)[0]:
                return 125
            if time.monotonic() >= deadline:
                return 124
            code = process.poll()
            if code is not None:
                return code if code >= 0 else 128 - code
    finally:
        if process is not None:
            reap_import(process, subreaper=subreaper)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(arguments):
    if os.geteuid() != 0:
        raise ValueError('Production guardian must be root')
    lifetime, locks, uid, gid, stage, head, bundle, run, anchor = arguments
    stage = Path(stage)
    command = ['/usr/bin/python3', '-I', '-B', str(Path(__file__).with_name('git_sandbox.py')),
               str(stage / 'incoming'), str(stage / 'importer'), head, bundle, run, anchor]
    return supervise(command, int(lifetime), tuple(map(int, locks.split(','))),
                     credentials=dict(user=int(uid), group=int(gid), extra_groups=[1001]))


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
