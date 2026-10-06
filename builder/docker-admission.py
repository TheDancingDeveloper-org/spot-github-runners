#!/usr/bin/python3
"""Root Docker adapter for the existing pinned remote-launcher protocol.

Other Docker commands exec the vendor binary unchanged. Only the dedicated
remote build's fixed label acquires a host memory reservation before starting.
Installed only on tier hosts; runner containers never receive a Docker socket.
"""
import base64
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack,contextmanager


ROOT='/mnt/runner-data/remote-build'


@contextmanager
def build_lock_files(project):
    """Lexically own both descriptors throughout creation, execution and cleanup."""
    with open('/run/oxidex-build.lock','a') as global_lock:
        with open(f'/run/oxidex-cache-active-{project}.lock','a') as active:
            yield global_lock,active


def build_identity(arguments):
    """Only assign cancellation identities to the launcher's fixed mount pair."""
    mounts=[arguments[i+1] for i,value in enumerate(arguments[:-1]) if value=='-v']
    targets=[value for value in mounts if value.endswith(':/target')]
    if len(targets)!=1:
        raise ValueError('Expected exactly one builder target mount')
    match=re.fullmatch(re.escape(ROOT)+r'/targets/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63}):/target',targets[0])
    if not match:
        raise ValueError('Unexpected builder target mount')
    project=match[1]
    if mounts.count(f'{ROOT}/sources/{project}:/src')!=1:
        raise ValueError('Builder source and cache must match')
    if any(value=='--name' or value.startswith('--name=') or
           value.startswith('--label=oxidex.cache-project=') or
           value.startswith('--label=oxidex.run-id=') for value in arguments):
        raise ValueError('Builder identity is assigned by the root adapter')
    run='run-'+uuid.uuid4().hex
    marker='/src/tools/remote-build/container_job.py'
    if marker in arguments:
        index=arguments.index(marker)
        if index!=len(arguments)-2:
            raise ValueError('Unexpected remote transaction payload')
        payload=json.loads(base64.urlsafe_b64decode(arguments[index+1]))
        run=payload['run_id']
        if not isinstance(run,str) or not re.fullmatch(r'run-[0-9a-f]{32}',run):
            raise ValueError('Invalid remote run identity')
    return project,run,'oxidex-run-'+run


def container_id(name):
    """A successful complete listing, not an inspect error, proves absence."""
    result=subprocess.run(['/usr/bin/docker','container','ls','--all',
        '--filter',f'name=^/{name}$','--format','{{.ID}}'],check=True,
        text=True,capture_output=True,timeout=30)
    ids=result.stdout.split()
    if len(ids)>1 or any(not re.fullmatch('[0-9a-f]{12,64}',value) for value in ids):
        raise RuntimeError('Unexpected Docker container inventory')
    return ids[0] if ids else None


def stop_owned(name,project,run):
    identity=container_id(name)
    if identity is None:
        return False
    records=json.loads(subprocess.check_output(['/usr/bin/docker','inspect',identity],text=True,timeout=30))
    if len(records)!=1:
        raise RuntimeError('Incomplete Docker identity')
    record=records[0];labels=record.get('Config',{}).get('Labels') or {}
    mounts=record.get('Mounts',[])
    if (record.get('Name')!='/'+name or labels.get('oxidex.remote-build')!='true'
        or labels.get('oxidex.cache-project')!=project or labels.get('oxidex.run-id')!=run
        or [m.get('Source') for m in mounts if m.get('Destination')=='/target']!=[f'{ROOT}/targets/{project}']
        or [m.get('Source') for m in mounts if m.get('Destination')=='/src']!=[f'{ROOT}/sources/{project}']):
        raise RuntimeError('Refusing to stop a different builder')
    subprocess.run(['/usr/bin/docker','rm','--force',identity],check=True,
                   text=True,capture_output=True,timeout=60)
    return True


def run_build(arguments,admission):
    try:
        project,run,name=build_identity(arguments)
    except (ValueError,KeyError,TypeError):
        return 78
    previous={};cancelled=[];acquired=False
    def cancel(signum,frame):
        cancelled.append(signum)
    try:
        with ExitStack() as stack:
            try:
                global_lock,active=stack.enter_context(build_lock_files(project))
                fcntl.flock(global_lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
                # Reconcile before taking our own cache lock: otherwise that
                # lock could protect a stopped orphan whose old lease blocks us.
                admission.reconcile_stopped_builders()
                fcntl.flock(active,fcntl.LOCK_EX|fcntl.LOCK_NB)
                if os.path.exists('/run/oxidex-build-draining') or container_id(name):
                    return 75
            except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError):
                return 75
            try:
                admission.acquire_build()
            except (OSError,RuntimeError,SystemExit):
                return 75
            acquired=True
            for signum in (signal.SIGHUP,signal.SIGTERM,signal.SIGINT):
                previous[signum]=signal.signal(signum,cancel)
            # Split creation from execution. If killed while the daemon creates
            # a container, a late orphan is stopped and cannot write this cache.
            command=['/usr/bin/docker','create','--name='+name,
                     '--label=oxidex.cache-project='+project,'--label=oxidex.run-id='+run,*arguments[1:]]
            child=None;startup_failed=False
            try:
                # No ordinary create timeout: killing the client cannot cancel
                # a daemon request. An orphaned client keeps both locks until
                # it exits, just as start --attach does below.
                subprocess.run(command,check=True,text=True,capture_output=True,
                               pass_fds=(global_lock.fileno(),active.fileno()))
                child=None if cancelled else subprocess.Popen(['/usr/bin/docker','start','--attach',name],
                    pass_fds=(global_lock.fileno(),active.fileno()))
            except (OSError,subprocess.SubprocessError):
                # A disconnected create can still complete in the daemon later.
                # Refresh/drain reconcile stopped orphans once no client owns
                # the cache lock; present containers are removed below first.
                startup_failed=True
            stopped=False
            while child is not None and child.poll() is None:
                if cancelled:
                    try:
                        stopped=stop_owned(name,project,run) or stopped
                        if stopped:
                            child.terminate()
                    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
                        # Keep both locks and the memory lease while Docker state is unknown.
                        pass
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    # Poll again so cancellation stays responsive while both
                    # inherited locks and the memory reservation remain held.
                    continue
            # Even a failed/disconnected Docker client can leave a daemon container.
            while True:
                try:
                    if container_id(name) is None:
                        # Current absence is not proof a failed create cannot
                        # finish later. Host reconciliation covers that case.
                        break
                    stop_owned(name,project,run)
                except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
                    time.sleep(1)
            if cancelled:return 128+cancelled[0]
            return 75 if startup_failed else child.returncode
    finally:
        for signum,handler in previous.items():
            signal.signal(signum,handler)
        if acquired:
            try:
                admission.release_build()
            except (OSError,RuntimeError,SystemExit):
                # A completed container no longer writes the cache. The admission
                # ledger reclaims the dead wrapper's identity on its next refresh.
                print('Builder finished; admission lease release deferred',file=sys.stderr)


def main():
    arguments=sys.argv[1:]
    if arguments and arguments[0]=='run' and '--label=oxidex.remote-build=true' in arguments:
        if os.geteuid()!=0:
            raise SystemExit('Remote builder admission requires root')
        sys.path.insert(0,'/opt/gha-runners')
        import host_admission
        raise SystemExit(run_build(arguments,host_admission))
    os.execv('/usr/bin/docker',['docker',*arguments])


if __name__=='__main__':
    main()
