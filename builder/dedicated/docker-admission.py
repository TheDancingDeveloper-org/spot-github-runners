#!/usr/bin/python3
"""Root Docker adapter for the existing pinned remote-launcher protocol.

Other Docker commands exec the vendor binary unchanged. Only the dedicated
remote build's fixed label acquires a host memory reservation before starting.
Installed only on tier hosts; runner containers never receive a Docker socket.
"""
import base64
import fcntl
import json
import math
from decimal import Decimal
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from contextlib import ExitStack,contextmanager


ROOT='/mnt/runner-data/remote-build'
LOCK_ROOT=Path('/run')
CREATED_ROOT=Path('/var/lib/oxidex-builder')
BENCHMARK=False

BUILDER_IMAGE='oxidex-remote-builder:1.99.0'


def qualification_contract():
    sys.path.insert(0, '/opt/oxidex-builder')
    from qualification_trust import mounts
    return mounts


def mount_spec(root, project):
    if project.startswith('qualification-'):
        contract = qualification_contract()
        if not contract.qualification_project(project):
            raise ValueError('Malformed qualification namespace')
        return contract.expected_mounts(root, project)
    return {'/src': (f'{root}/sources/{project}', True),
            '/target': (f'{root}/targets/{project}', True), '/cargo': (f'{root}/cargo', True)}


def expected_volumes(root, project):
    return [source + ':' + destination + ('' if writable else ':ro')
            for destination, (source, writable) in mount_spec(root, project).items()]


def inspected_mounts(mounts, project):
    expected = mount_spec(ROOT, project)
    if not isinstance(mounts, list) or len(mounts) != len(expected):
        raise ValueError('Created builder cache mounts differ')
    found = {}
    for mount in mounts:
        if (not isinstance(mount, dict) or mount.get('Destination') in found
                or mount.get('Type') != 'bind' or type(mount.get('RW')) is not bool):
            raise ValueError('Malformed created builder mount')
        found[mount['Destination']] = (mount.get('Source'), mount['RW'])
    if found != expected:
        raise ValueError('Created builder cache mounts differ')



def configured_build_limits():
    if BENCHMARK:
        import benchmark_admission
        config,_=benchmark_admission.config_and_receipt()
        return {**config,'cpus':16,'memory_gib':28}
    return json.loads(Path('/etc/oxidex-remote-build.json').read_text())


def validate_build_policy(arguments,config):
    """Prove vendor Docker caps before claiming a nominal CPU/RAM lease.

    Only the pinned launcher's pre-image option grammar is admitted. Unknown
    aliases or repeated options cannot override the measured reservation.
    Arguments after the image remain the unmodified compiler/job payload.
    """
    if not isinstance(config,dict):raise ValueError('Unknown builder configuration')
    for field in ('cpus','memory_gib'):
        value=config.get(field)
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
            raise ValueError('Invalid builder resource configuration')
    if not arguments or arguments[0]!='run' or any(not isinstance(x,str) for x in arguments):
        raise ValueError('Unknown Docker run arguments')
    allowed={'--user','--cpus','--label','--memory','--memory-swap','--pids-limit',
             '--cap-drop','--security-opt','--entrypoint'}
    values={};index=1
    while index<len(arguments):
        word=arguments[index]
        if word=='--rm':index+=1;continue
        if word=='-v':
            if index+1>=len(arguments) or not arguments[index+1]:raise ValueError('Missing bind mount')
            index+=2;continue
        if word.startswith('-'):
            key,separator,value=word.partition('=')
            if not separator or key not in allowed or not value:
                raise ValueError('Unknown/conflicting Docker option')
            values.setdefault(key,[]).append(value);index+=1;continue
        break
    if index>=len(arguments) or arguments[index]!=BUILDER_IMAGE:
        raise ValueError('Builder image differs from reviewed toolchain')
    for key in allowed-{'--label'}:
        if len(values.get(key,[]))>1:raise ValueError('Repeated Docker policy option')
    for key,field,pattern in (('--cpus','cpus',r'[0-9]+(?:\.[0-9]+)?'),
                              ('--memory','memory_gib',r'[0-9]+(?:\.[0-9]+)?g'),
                              ('--memory-swap','memory_gib',r'[0-9]+(?:\.[0-9]+)?g')):
        observed=values.get(key,[])
        if len(observed)!=1 or not re.fullmatch(pattern,observed[0]):
            raise ValueError('Missing/unknown Docker resource cap')
        amount=observed[0] if key=='--cpus' else observed[0][:-1]
        if Decimal(amount)!=Decimal(str(config[field])):
            raise ValueError('Actual Docker cap differs from reserved builder resources')
    required={'--user':'1001:1001','--cap-drop':'ALL','--security-opt':'no-new-privileges',
              '--pids-limit':'4096','--entrypoint':'/usr/bin/time'}
    if any(values.get(key)!=[value] for key,value in required.items()):
        raise ValueError('Builder privilege/runtime policy differs from launcher')
    if values.get('--label')!=['oxidex.remote-build=true']:
        raise ValueError('Builder label differs from launcher')
    mounts=[arguments[i+1] for i,word in enumerate(arguments[:index]) if word=='-v']
    target=[value for value in mounts if value.endswith(':/target')]
    if len(target)!=1:raise ValueError('Unknown builder target mount')
    match=re.fullmatch(re.escape(ROOT)+r'/targets/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63}):/target',target[0])
    if not match:raise ValueError('Unapproved builder cache path')
    project=match[1]
    if sorted(mounts)!=sorted(expected_volumes(ROOT, project)):
        raise ValueError('Builder mounts differ from fixed cache/source boundaries')


@contextmanager
def build_lock_files(project):
    """Lexically own both descriptors throughout creation, execution and cleanup."""
    opener=lambda name,flags:os.open(name,flags|os.O_NOFOLLOW,0o600)
    with open(str(LOCK_ROOT/'oxidex-build.lock'),'a',opener=opener) as global_lock:
        with open(str(LOCK_ROOT/f'oxidex-cache-active-{project}.lock'),'a',opener=opener) as active:
            yield global_lock,active


def build_identity(arguments):
    """Only assign cancellation identities to the launcher's fixed mount pair."""
    options=arguments[:arguments.index(BUILDER_IMAGE)] if BUILDER_IMAGE in arguments else arguments
    mounts=[options[i+1] for i,value in enumerate(options[:-1]) if value=='-v']
    targets=[value for value in mounts if value.endswith(':/target')]
    if len(targets)!=1:
        raise ValueError('Expected exactly one builder target mount')
    match=re.fullmatch(re.escape(ROOT)+r'/targets/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63}):/target',targets[0])
    if not match:
        raise ValueError('Unexpected builder target mount')
    project=match[1]
    if project.startswith('qualification-'):
        if sorted(mounts) != sorted(expected_volumes(ROOT, project)):
            raise ValueError('Qualification source and cache must match sealed namespace')
    elif mounts.count(f'{ROOT}/sources/{project}:/src')!=1:
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
        or [m.get('Source') for m in mounts if m.get('Destination')=='/src']!=[mount_spec(ROOT, project)['/src'][0]]):
        raise RuntimeError('Refusing to stop a different builder')
    if project.startswith('qualification-'):inspected_mounts(mounts, project)
    subprocess.run(['/usr/bin/docker','rm','--force',identity],check=True,
                   text=True,capture_output=True,timeout=60)
    return True


def record_created_container(name,project,run):
    """Root-only created-state receipt; no Env, and no running/build claim."""
    records=json.loads(subprocess.check_output(['/usr/bin/docker','inspect',name],text=True,timeout=30))
    if not isinstance(records,list) or len(records)!=1:raise ValueError('Incomplete created identity')
    row=records[0]
    if not isinstance(row,dict):raise ValueError('Malformed created identity')
    expected=configured_build_limits().get('approved_image_id')
    if not isinstance(expected,str) or not re.fullmatch('sha256:[0-9a-f]{64}',expected) or row.get('Image')!=expected:
        raise ValueError('Created container image differs from approved immutable image')
    caps=row.get('HostConfig');config=row.get('Config');state=row.get('State')
    if any(not isinstance(v,dict) for v in (caps,config,state)):raise ValueError('Malformed created policy')
    if (not re.fullmatch('[0-9a-f]{64}',row.get('Id','')) or row.get('Name')!='/'+name
        or not re.fullmatch('sha256:[0-9a-f]{64}',row.get('Image',''))
        or state.get('Status')!='created' or state.get('Running') is not False or state.get('Pid')!=0
        or config.get('Image')!=BUILDER_IMAGE or config.get('User')!='1001:1001'
        or not isinstance(config.get('Labels'),dict)
        or any(config['Labels'].get(k)!=v for k,v in {'oxidex.remote-build':'true','oxidex.cache-project':project,'oxidex.run-id':run}.items())
        or caps.get('NanoCpus')!=16*10**9 or caps.get('Memory')!=28*1024**3 or caps.get('MemorySwap')!=28*1024**3):
        raise ValueError('Created builder differs from configured policy')
    mounts=row.get('Mounts')
    inspected_mounts(mounts, project)
    directory=Path(str(CREATED_ROOT));directory.mkdir(mode=0o700,exist_ok=True)
    if any(p.is_symlink() for p in (directory,*directory.parents)) or directory.stat().st_uid!=0 or directory.stat().st_mode&0o022:
        raise ValueError('Unsafe builder receipt directory')
    record=dict(schema='builder-container-created-v1',observed_at_ns=time.time_ns(),container_id=row['Id'],
        name=row['Name'],image_id=row['Image'],created=row['Created'],state={k:state[k] for k in ('Running','Status','Pid','StartedAt')},
        labels={k:config['Labels'][k] for k in ('oxidex.remote-build','oxidex.cache-project','oxidex.run-id')},caps={k:caps[k] for k in ('NanoCpus','Memory','MemorySwap')},mounts=mounts)
    temporary=directory/('created-'+uuid.uuid4().hex+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w') as target:
            json.dump(record,target,sort_keys=True);target.flush();os.fsync(target.fileno())
        os.replace(temporary,directory/'last-created-container.json')
        if not BENCHMARK:
            sys.path.insert(0,'/opt/oxidex-builder')
            from qualification_trust.control_engine import publish_container
            publish_container(project,record)
    finally:temporary.unlink(missing_ok=True)


def run_build(arguments,admission,*,role_config=None):
    try:
        if role_config is not None:
            if (role_config.get('role') not in ('builder','benchmark-builder') or role_config.get('cpus')!=16
                or role_config.get('memory_gib')!=28 or role_config.get('image')!=BUILDER_IMAGE):
                raise ValueError('Dedicated builder role/shape differs from approved policy')
            validate_build_policy(arguments,role_config)
            expected=role_config.get('approved_image_id')
            if not isinstance(expected,str) or not re.fullmatch('sha256:[0-9a-f]{64}',expected):
                raise ValueError('Prepared/unapproved image cannot start a build')
        project,run,name=build_identity(arguments)
    except (OSError,ValueError,KeyError,TypeError,OverflowError):
        return 78
    previous={};cancelled=[];acquired=False;terminal_exit=None;canary_proof=None
    def cancel(signum,frame):
        cancelled.append(signum)
    try:
        with ExitStack() as stack:
            try:
                global_lock,active=stack.enter_context(build_lock_files(project))
                fcntl.flock(global_lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
                if role_config is not None and role_config.get('role') == 'builder':
                    sys.path.insert(0, '/opt/oxidex-builder')
                    from qualification_trust.reservation import route_guard
                    route_guard(project)
                # Reconcile before taking our own cache lock: otherwise that
                # lock could protect a stopped orphan whose old lease blocks us.
                admission.reconcile_stopped_builders()
                fcntl.flock(active,fcntl.LOCK_EX|fcntl.LOCK_NB)
                if os.path.exists(str(LOCK_ROOT/'oxidex-build-draining')) or container_id(name):
                    return 75
            except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError):
                return 75
            try:
                if BENCHMARK:
                    import benchmark_admission
                    image_index=arguments.index(BUILDER_IMAGE)
                    payload=arguments[image_index+4:]
                    claim=benchmark_admission.canary_claim(role_config,project,payload,admission.process_identity(os.getpid()),run)
                    if claim is not None:
                        session=benchmark_admission.canary_session(role_config)
                        canary_proof={'not_after':session['session_deadline_at'],'session_deadline_at':session['session_deadline_at']}
                admission.acquire_build()
            except (OSError,ValueError,RuntimeError,SystemExit):
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
                if canary_proof is not None:benchmark_admission.canary_final_freshness(canary_proof,time.time())
                subprocess.run(command,check=True,text=True,capture_output=True,
                               pass_fds=(global_lock.fileno(),active.fileno()))
                if role_config is not None:
                    record_created_container(name,project,run)
                if canary_proof is not None:benchmark_admission.canary_final_freshness(canary_proof,time.time())
                child=None if cancelled else subprocess.Popen(['/usr/bin/docker','start','--attach',name],
                    pass_fds=(global_lock.fileno(),active.fileno()))
            except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
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
            if cancelled:terminal_exit=128+cancelled[0];return terminal_exit
            if startup_failed:return 75
            terminal_exit=child.returncode;return terminal_exit
    finally:
        for signum,handler in previous.items():
            signal.signal(signum,handler)
        if acquired:
            try:
                admission.release_build()
                if BENCHMARK and terminal_exit is not None:
                    import benchmark_admission
                    benchmark_admission.canary_terminal(role_config,admission.process_identity(os.getpid()),run,terminal_exit)
            except (OSError,ValueError,RuntimeError,SystemExit,subprocess.SubprocessError):
                # A completed container no longer writes the cache. The admission
                # ledger reclaims the dead wrapper's identity on its next refresh.
                print('Builder finished; admission lease release deferred',file=sys.stderr)


def main():
    global ROOT,LOCK_ROOT,CREATED_ROOT,BENCHMARK
    arguments=sys.argv[1:]
    if arguments[:1]==['--benchmark278']:
        if os.geteuid()!=0:raise SystemExit('Root private benchmark launcher required')
        sys.path.insert(0,'/opt/oxidex-benchmark278')
        import host_admission
        config=host_admission.configure_benchmark()
        ROOT=config['root'];LOCK_ROOT=host_admission.LOCK_ROOT
        CREATED_ROOT=Path('/var/lib/oxidex-benchmark278');BENCHMARK=True;arguments=arguments[1:]
        if not arguments or arguments[0]!='run' or '--label=oxidex.remote-build=true' not in arguments:
            raise SystemExit('Private benchmark adapter accepts only canonical build transactions')
    if arguments and arguments[0]=='run' and '--label=oxidex.remote-build=true' in arguments:
        if os.geteuid()!=0:
            raise SystemExit('Remote builder admission requires root')
        if not BENCHMARK:sys.path.insert(0,'/opt/gha-runners')
        import host_admission
        config=configured_build_limits()
        if config.get('role') not in (None,'builder','benchmark-builder'):raise SystemExit('Unknown builder role')
        raise SystemExit(run_build(arguments,host_admission,role_config=config if config.get('role') in ('builder','benchmark-builder') else None))
    os.execv('/usr/bin/docker',['docker',*arguments])


if __name__=='__main__':
    main()
