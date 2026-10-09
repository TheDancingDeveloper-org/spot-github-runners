"""Root-owned Task20 endpoint implementation, with durable stage custody."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
from . import reservation as custody
from .control_observation import root_json,sealed_binding,quiescent
from .task20_receiver import receive,MEMBERS,MAX_ARCHIVE

BUILD=Path('/mnt/runner-data/remote-build')
LOG_LIMIT=128*1024*1024
CAPTURE_POLL_SECONDS=.05
POST_EOF_EXIT_OBSERVATION_SECONDS=5
POST_EXIT_PIPE_DRAIN_SECONDS=5


def write(path,value,*,mode=0o400):
    raw=custody.canonical(value)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    os.fchmod(fd,mode)  # exact RO reader policy, independent of root's 077 umask
    with os.fdopen(fd,'wb') as output:output.write(raw);output.flush();os.fsync(output.fileno())
    directory=os.open(Path(path).parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(directory)
    finally:os.close(directory)


def file_digest(path):
    value=hashlib.sha256();total=0
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            total+=len(block)
            if total>LOG_LIMIT:raise ValueError('Bounded control log exceeded; preserve bytes and hold')
            value.update(block)
    return value.hexdigest()


def own_unit():
    for row in Path('/proc/self/cgroup').read_text().splitlines():
        path=row.split(':',2)[-1]
        name=path.rsplit('/',1)[-1]
        if name.startswith('oxidex-reservation-') and name.endswith('.service'):
            from .control_observation import command
            pid=command(['/usr/bin/systemctl','show',name,'--property=MainPID','--value']).strip()
            if pid!=str(os.getpid()):raise ValueError('Actual stage unit owner differs')
            return name
    return None


def idle(project):return quiescent(project,own_unit=own_unit())


def stage_unit_processes():
    """Read every process in the actual root-owned stage service cgroup."""
    unit=own_unit()
    rows=Path('/proc/self/cgroup').read_text().splitlines()
    if unit is None or len(rows)!=1 or not rows[0].startswith('0::') or rows[0][3:].rsplit('/',1)[-1]!=unit:
        raise ValueError('Actual stage scope unavailable; retain custody')
    root=Path('/sys/fs/cgroup')/rows[0][3:].lstrip('/')
    pending=[root];members=set()
    while pending:
        current=pending.pop();info=current.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('Untrusted stage cgroup; retain custody')
        for row in (current/'cgroup.procs').read_text().splitlines():
            if not row.isdecimal():raise ValueError('Unknown stage cgroup process')
            members.add(int(row))
        with os.scandir(current) as entries:
            pending.extend(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))
    if os.getpid() not in members:raise ValueError('Stage owner absent from actual cgroup')
    return members


def stop_stage_children(process):
    """On excess output, stop the process group and every remaining unit child."""
    try:os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError:pass  # The process exited between scope observation and kill.
    deadline=time.monotonic()+5
    while True:
        others=stage_unit_processes()-{os.getpid()}
        if not others:break
        for pid in others:
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass  # A listed child may exit before signaling.
        if time.monotonic()>=deadline:
            raise ValueError('Stage descendants remain after output limit; retain custody')
        time.sleep(.02)
    process.wait(timeout=5)


def capture_stage(argv,output,*,limit=LOG_LIMIT,scope_processes=stage_unit_processes):
    """Observe output and launcher independently while the caller retains all locks.

    A live launcher may run without output. Once its pipe reaches EOF, allow a
    bounded status observation, then stop and hold if it remains alive. Time
    alone never establishes successful completion or releases custody. After
    launcher exit, wait for actual pipe EOF within the bounded drain period.
    """
    if type(limit) is not int or not 0<limit<=LOG_LIMIT:
        raise ValueError('Invalid stage log bound')
    process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,close_fds=True,start_new_session=True,
        env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C'})
    count=0;pipe_eof=False;exit_deadline=None;drain_deadline=None
    try:
        with process.stdout, selectors.DefaultSelector() as selector:
            fd=process.stdout.fileno()
            os.set_blocking(fd,False)
            selector.register(fd,selectors.EVENT_READ)
            while True:
                ready=selector.select(CAPTURE_POLL_SECONDS) if not pipe_eof else ()
                if ready:
                    try:block=os.read(fd,65536)
                    except BlockingIOError:block=None
                    if block==b'':
                        selector.unregister(fd);pipe_eof=True
                    elif block:
                        remaining=limit-count
                        if remaining:
                            kept=block[:remaining]
                            output.write(kept);count+=len(kept)
                        if len(block)>remaining:
                            raise ValueError('Bounded control log exceeded during capture; custody held')
                status=process.poll()
                if status is not None:
                    if scope_processes()!={os.getpid()}:
                        raise ValueError('Actual stage descendants remain; custody held')
                    if pipe_eof:return status
                    if drain_deadline is None:
                        drain_deadline=time.monotonic()+POST_EXIT_PIPE_DRAIN_SECONDS
                    if time.monotonic()>=drain_deadline:
                        raise ValueError('Stage pipe remains open after launcher exit; custody held')
                    # A timeout's empty ready set predates this status check;
                    # the pipe may become readable or reach EOF just after it.
                    continue
                if pipe_eof:
                    if exit_deadline is None:
                        exit_deadline=time.monotonic()+POST_EOF_EXIT_OBSERVATION_SECONDS
                    if time.monotonic()>=exit_deadline:
                        raise ValueError('Launcher remains alive after stage pipe EOF; custody held')
                    time.sleep(CAPTURE_POLL_SECONDS)
    except BaseException:
        try:stop_stage_children(process)
        except Exception:pass  # Unknown descendants are retained as HELD.
        raise


def dossier(project):
    if not custody.re.fullmatch('qualification-[0-9a-f]{12}-[0-9a-f]{32}',project):raise ValueError('Fixed project required')
    return custody.ROOT/project


def validate_request(request):
    if not isinstance(request,dict) or set(request)!={'session','stages','argv','archive','document','signature'}:
        raise ValueError('Exact root-reviewed control dossier required')
    session=request['session'];stages=request['stages'];args=request['argv'];archive=request['archive']
    custody.validate_session(session,stages)
    if session.get('profile')!='task20-v1' or not isinstance(args,list) or len(args)!=8:
        raise ValueError('Exact Task20 profile required')
    for stage,argv in zip(stages,args):
        custody.command_arguments(stage['command'],argv,session)
        if custody.digest(argv)!=stage['argv_sha256']:raise ValueError('Reviewed descriptor argv differs')
    if not isinstance(archive,dict) or set(archive)!={'sha256','bytes','members'} or type(archive['bytes']) is not int or not 0<archive['bytes']<=MAX_ARCHIVE or set(archive['members'])!=MEMBERS:
        raise ValueError('Root-approved complete source archive binding required')
    for value in [archive['sha256'],*archive['members'].values()]:
        if not isinstance(value,str) or not custody.re.fullmatch('[0-9a-f]{64}',value):raise ValueError('Malformed packet hash')
    document=base64.b64decode(request['document'],validate=True);signature=base64.b64decode(request['signature'],validate=True)
    if len(document)>custody.LIMIT or len(signature)>16384:raise ValueError('Control claim byte bound')
    return session,document,signature


def reserve(request):
    session,document,signature=validate_request(request)
    # Caller owns global EX and the root-authenticated descriptor channel.
    path=dossier(session['project'])
    actual=custody.authenticate_claim(document,signature,session)
    store=custody.Store()
    with store.locked():previous=store.read()
    if previous is not None and previous['state']!='RELEASED':
        store.match(previous,previous['reservation_id'],custody.host_identity())
        if previous['session']!=session or previous['descriptor_sha256']!=custody.digest(request):
            raise ValueError('Foreign retained custody; descriptor recovery refused')
        # Lost publication acknowledgement: restore only the exact root-bound
        # dossier; never acquire a second reservation or replay a stage.
        path.mkdir(mode=0o700,exist_ok=True)
        if not (path/'descriptor.json').exists():write(path/'descriptor.json',request)
        elif root_json(path/'descriptor.json')!=request:raise ValueError('Retained descriptor differs')
        return previous
    if path.exists() or path.is_symlink():raise ValueError('Prior dossier retained; inspect only')
    record=custody.Store().acquire(session,request['stages'],host=custody.host_identity(),
        owner=custody.current_actor(),authenticate=lambda claim:actual,
        quiescent=lambda:idle(session['project']),descriptor_sha256=custody.digest(request))
    path.mkdir(mode=0o700)
    parent=os.open(custody.ROOT,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(parent)
    finally:os.close(parent)
    write(path/'descriptor.json',request)
    return record


def selected(project,identity):
    path=dossier(project)
    request=root_json(path/'descriptor.json')
    validate_request(request)
    with custody.Store().locked():record=custody.Store().read()
    custody.Store.match(record,identity,custody.host_identity())
    if record['session']!=request['session'] or record['stages']!=request['stages'] or record['descriptor_sha256']!=custody.digest(request):
        raise ValueError('Root descriptor changed since reservation')
    return path,request,record


def stage(project,identity,ordinal,stream,*,protective_fds=()):
    with activity(project,'operation'):
        return stage_locked(project,identity,ordinal,stream,protective_fds=protective_fds)


def stage_locked(project,identity,ordinal,stream,*,protective_fds=()):
    path,request,record=selected(project,identity)
    if type(ordinal) is not int or not 0<=ordinal<8:raise ValueError('Fixed ordinal required')
    argv=request['argv'][ordinal];actor=custody.current_actor();host=custody.host_identity();store=custody.Store()
    idle(project)
    store.begin(identity,ordinal,host=host,actor=actor,argv=argv)
    log=path/('stage-'+str(ordinal)+'.log')
    # Refuse prior output before any stage effect. Logs are durable owned proof.
    fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
    status=0;binding=None
    try:
        with os.fdopen(fd,'wb') as output:
            if ordinal==0:
                source=BUILD/'sources'/project;target=BUILD/'targets'/project
                source.mkdir(mode=0o700,exist_ok=False);target.mkdir(mode=0o700,exist_ok=False)
                os.chown(target,1001,1001)
                output.write(b'Owned source/target prepared; no uploader write access.\n')
            elif ordinal==1:
                with activity(project,'upload'):
                    result=receive(stream,path/'receive',archive_sha256=request['archive']['sha256'],
                        archive_bytes=request['archive']['bytes'],member_hashes=request['archive']['members'])
                    # Existing source is root-owned and empty; move only verified intake.
                    source=BUILD/'sources'/project
                    if any(source.iterdir()):raise ValueError('Prepared source unexpectedly populated')
                    incoming=path/'receive/incoming'
                    for member in incoming.iterdir():member.rename(source/member.name)
                    # Receive synced the file bytes. Persist both changed name
                    # spaces before Store.finish can advance past this upload.
                    for parent in (source,incoming):
                        directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                        try:os.fsync(directory)
                        finally:os.close(directory)
                    output.write(custody.canonical(result)+b'\n')
            elif ordinal==2:
                from .launcher_contract import create_binding
                from .host_gate import PROTECTIVE_FDS
                token=PROTECTIVE_FDS.set(protective_fds)
                try:
                    with source_locks(project):
                        create_binding(BUILD,project,record['session']['head'],record['session']['bundle_sha256'],
                                       profile='task20-v1',session=record['session'])
                finally:PROTECTIVE_FDS.reset(token)
                binding=sealed_binding(BUILD,record['session'])
                output.write(custody.canonical(binding)+b'\n')
            elif ordinal==7:
                idle(project)
                output.write(b'Actual custody cleanup only; source/cache/evidence retained.\n')
            else:
                # Fixed root runtime and descriptor-derived argv only.
                proof={'schema':1,'reservation_id':identity,'session':record['session'],
                       'stage':ordinal,'host':list(host),'actor':actor,
                       'descriptor_sha256':custody.digest(request),'container':None,
                       'command':record['stages'][ordinal]['command'],
                       'argv_sha256':record['stages'][ordinal]['argv_sha256'],
                       'custody_state':'SOURCE_VERIFIED','active_ordinal':ordinal,
                       'purpose':'execute' if ordinal==4 else 'bootstrap' if ordinal==3 else 'diagnostic',
                       'seal_sha256':file_digest(BUILD/'qualification-sealed'/project/'execution-binding.json')}
                artifact=path/('host-proof-'+str(ordinal)+'.json')
                write(artifact,proof)
                # Prior stage proof remains immutable. No container survives this
                # boundary, so the canonical next bind may be replaced atomically.
                temporary=path/('.proof-'+str(ordinal));write(temporary,proof,mode=0o444)
                os.replace(temporary,path/'host-proof.json')
                status=capture_stage(['/usr/local/bin/oxidex-remote-build',project,*argv],output)
                if ordinal==5 and status==0:
                    capture_pack(project,path)
            output.flush();os.fsync(output.fileno())
        # The terminal record must never outlive the new stage-log name.
        directory=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
        sha=file_digest(log)
        return store.finish(identity,ordinal,host=host,actor=actor,exit_code=status,
                            log_sha256=sha,quiescent=lambda:idle(project),binding=binding)
    except Exception:
        # Preserve active identity and retained bytes. Explicit reconciliation,
        # never a retry or inferred terminal success, resolves partial effects.
        with store.locked():
            held=store.read();store.match(held,identity,host);held['state']='HELD';store.write(held)
        if log.exists():
            retained=os.open(log,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                if os.fstat(retained).st_size>LOG_LIMIT:
                    raise ValueError('Retained stage log exceeded fixed production bound')
                os.fsync(retained)
            finally:os.close(retained)
        raise


def reconcile_old_boot_leases(project,record,host):
    """Refresh only a proven dead dedicated host owner under caller's global EX."""
    import importlib
    admission=importlib.import_module('lib.host_admission' if __package__.startswith('lib.') else 'host_admission')
    if record['host'][0]!=host[0] or record['host'][1]==host[1]:
        raise ValueError('Same-VM distinct reboot proof required before lease refresh')
    active=record['active']
    if (active is None or active['command'] not in
            ('task20-bootstrap','task20-execute','task20-pack','task20-replay')):
        raise ValueError('No selected old container stage owns admission; custody held')
    rows=root_json('/var/lib/gha-admission/leases.json')
    if not isinstance(rows,dict) or len(rows)!=1:
        raise ValueError('Unknown or foreign admission owner; custody held')
    owner,amount=next(iter(rows.items()))
    match=custody.re.fullmatch(r'host:([1-9][0-9]*):([1-9][0-9]*)',owner) if isinstance(owner,str) else None
    if match is None or type(amount) not in (int,float) or amount!=28:
        raise ValueError('Foreign admission owner; custody held')
    config=root_json('/etc/gha-admission.json')
    admission.builder_capacity_record(config)
    # Same-VM new boot was validated by the caller. A current PID, even with
    # different start ticks, may be a new foreign process and must hold.
    pid=int(match[1])
    def dead(selected):
        if selected!=owner:raise ValueError('Admission ledger changed; custody held')
        try:actual=admission.process_identity(pid)
        except FileNotFoundError:return False
        except (OSError,ValueError,IndexError) as error:
            raise ValueError('Admission PID/start observation uncertain; custody held') from error
        raise ValueError('Admission PID remains or was reused; custody held: '+actual)
    # Observe all other fences before the canonical pool can alter its ledger.
    # No unit/container/cgroup/transfer uncertainty is accepted as dead work.
    if quiescent(project,own_unit=own_unit(),admission_leases=False) is not True:
        raise ValueError('Complete quiescence unavailable; custody held')
    unit='oxidex-reservation-'+record['reservation_id']+'-'+str(active['ordinal'])+'.service'
    from .control_observation import command
    state=command(['/usr/bin/systemctl','show',unit,
                   '--property=ActiveState,MainPID,ControlGroup','--no-pager'])
    fields=dict(row.split('=',1) for row in state.splitlines() if '=' in row)
    if fields!={'ActiveState':'inactive','MainPID':'0','ControlGroup':''}:
        raise ValueError('Old stage unit/cgroup presence uncertain; custody held')
    # Recheck actual absence immediately before LeasePool's locked refresh.
    dead(owner)
    pool=admission.LeasePool('/var/lib/gha-admission/leases.json',28,alive=dead)
    pool.refresh()
    if root_json('/var/lib/gha-admission/leases.json')!={}:
        raise ValueError('Admission refresh left a retained lease; custody held')


class activity:
    def __init__(self,project,kind):
        if kind not in ('upload','export','operation'):raise ValueError('Fixed activity lock kind')
        self.path='/run/oxidex-'+kind+'-'+project+'.lock'
    def __enter__(self):
        self.fd=os.open(self.path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        info=os.fstat(self.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or info.st_nlink!=1:
            os.close(self.fd);raise ValueError('Untrusted transfer/export lock')
        fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return self
    def __exit__(self,*args):os.close(self.fd)


class source_locks:
    def __init__(self,project):self.project=project;self.fds=[]
    def __enter__(self):
        try:
            for name in ('oxidex-cache-active-'+self.project+'.lock','oxidex-qualification-'+self.project+'.lock'):
                fd=os.open('/run/'+name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
                self.fds.append(fd)
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or info.st_nlink!=1:
                    raise ValueError('Untrusted source import fence')
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return self
        except BaseException:
            self.__exit__();raise
    def __exit__(self,*args):
        for fd in reversed(self.fds):os.close(fd)
        self.fds=[]


def publish_container(project,created):
    """Bind root-observed created container before Docker starts any code."""
    if not custody.ROOT.exists() and not custody.ROOT.is_symlink():
        if custody.enrollment() is not None:raise ValueError('Enrolled custody storage unavailable')
        return
    store=custody.Store()
    with store.locked():record=store.read()
    if record is None or record['session'].get('profile')!='task20-v1':return
    if record['state']=='RELEASED':return
    if record['active'] is None or record['session']['project']!=project or not custody.ancestor_matches(record['active']['actor'],os.getpid()):
        raise ValueError('Created container has no actual Task20 owner')
    path=dossier(project)/'host-proof.json'
    proof=root_json(path)
    if proof['reservation_id']!=record['reservation_id'] or proof['stage']!=record['active']['ordinal']:
        raise ValueError('Created proof differs from actual stage')
    if record['state']!='SOURCE_VERIFIED' or proof['command']!=record['active']['command'] or proof['argv_sha256']!=record['stages'][proof['stage']]['argv_sha256']:
        raise ValueError('Actual custody/command projection differs')
    proof['container']={key:created[key] for key in ('container_id','image_id','created','caps','labels','state')}
    # Docker captured this inode before start. Keep the inode, publish only while
    # the container is still CREATED; the unprivileged reader has not started.
    fd=os.open(path,os.O_WRONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o222:
            raise ValueError('Untrusted bound host proof')
        raw=custody.canonical(proof)
        os.ftruncate(fd,0)
        offset=0
        while offset<len(raw):
            count=os.write(fd,raw[offset:])
            if count<=0:raise ValueError('Host proof write incomplete')
            offset+=count
        os.fsync(fd)
    finally:os.close(fd)
    write(dossier(project)/('container-stage-'+str(proof['stage'])+'.json'),proof)


def export(project,identity,output):
    with activity(project,'operation'):
        return export_locked(project,identity,output)


def export_pack_observation(path,project):
    observed=root_json(path/'pack-observed.json')
    if (not isinstance(observed,dict) or set(observed)!={'schema','project','archive_sha256','archive_bytes'}
            or type(observed['schema']) is not int or observed['schema']!=1
            or observed['project']!=project or type(observed['archive_bytes']) is not int
            or not 0<observed['archive_bytes']<=2*1024**3
            or not isinstance(observed['archive_sha256'],str)
            or not custody.re.fullmatch('[0-9a-f]{64}',observed['archive_sha256'])):
        raise ValueError('Actual pack observation differs from selected result')
    return observed


def prior_export_receipt(path,project,identity,observed):
    """Reuse only a complete root-owned receipt for this selected archive."""
    receipt_path=path/'export-result.json'
    try:receipt=root_json(receipt_path)
    except FileNotFoundError:return None
    expected={'schema':1,'reservation_id':identity,'project':project,
              'sha256':observed['archive_sha256'],'bytes':observed['archive_bytes']}
    if (not isinstance(receipt,dict) or set(receipt)!=set(expected)
            or type(receipt['schema']) is not int or type(receipt['bytes']) is not int
            or receipt!=expected):
        raise ValueError('Retained export receipt differs from selected pack and custody')
    return receipt


def hold_export_uncertainty(project,identity):
    """Keep ambiguous export evidence from advancing replay or cleanup."""
    store=custody.Store();host=custody.host_identity()
    with store.locked():
        record=store.read();store.match(record,identity,host)
        if (record['session']['project']!=project or record['state']!='SOURCE_VERIFIED'
                or record['active'] is not None or record['next_ordinal'] not in (6,7)):
            raise ValueError('Selected export custody changed; retain unknown state')
        record['state']='HELD';store.write(record)


def export_locked(project,identity,output):
    path,request,record=selected(project,identity)
    if record['state']!='SOURCE_VERIFIED' or record['active'] is not None or record['next_ordinal'] not in (6,7):
        raise ValueError('Actual pack terminal required before result export')
    idle(project)
    source=BUILD/'targets'/project/'task20-results.tar.gz'
    try:expected=export_pack_observation(path,project)
    except Exception:
        hold_export_uncertainty(project,identity)
        raise
    with activity(project,'export'):
        try:previous=prior_export_receipt(path,project,identity,expected)
        except Exception:
            hold_export_uncertainty(project,identity)
            raise
        try:fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        except Exception:
            hold_export_uncertainty(project,identity)
            raise
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>2*1024**3:
                hold_export_uncertainty(project,identity)
                raise ValueError('Bounded regular result archive required')
            digest=hashlib.sha256();count=0
            while True:
                block=os.read(fd,1024*1024)
                if not block:break
                if output.write(block)!=len(block):
                    raise ValueError('Export stream did not accept complete archive bytes')
                digest.update(block);count+=len(block)
            output.flush()
            after=os.fstat(fd)
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or count!=before.st_size:
                hold_export_uncertainty(project,identity)
                raise ValueError('Export bytes changed; never replay automatically')
            if expected['archive_sha256']!=digest.hexdigest() or expected['archive_bytes']!=count:
                hold_export_uncertainty(project,identity)
                raise ValueError('Export differs from actual retained pack bytes')
            receipt={'schema':1,'reservation_id':identity,'project':project,'sha256':digest.hexdigest(),'bytes':count}
            if previous is None:
                # A file appearing under our exclusive export lock is unknown
                # state, not a reason to replace a prior publication.
                try:write(path/'export-result.json',receipt)
                except Exception:
                    hold_export_uncertainty(project,identity)
                    raise
            elif previous!=receipt:
                hold_export_uncertainty(project,identity)
                raise ValueError('Retained export receipt differs from streamed archive')
            return receipt
        finally:os.close(fd)


def capture_pack(project,path):
    """Retain actual archive identity independently of writable result metadata."""
    source=BUILD/'targets'/project/'task20-results.tar.gz'
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or not 0<before.st_size<=2*1024**3:
            raise ValueError('Actual bounded pack archive required')
        value=hashlib.sha256();count=0
        while True:
            block=os.read(fd,1024*1024)
            if not block:break
            value.update(block);count+=len(block)
        after=os.fstat(fd)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or count!=before.st_size:
            raise ValueError('Pack changed during root observation')
        write(path/'pack-observed.json',{'schema':1,'project':project,
                                       'archive_sha256':value.hexdigest(),'archive_bytes':count})
    finally:os.close(fd)


def reconcile_reboot(project,identity,expected_old_boot):
    """Explicit same-VM reboot cleanup; never continue old stage tokens.

    Spot replacement is intentionally not inferred from guest metadata: a new
    VM requires separately authenticated provider-absence/disk-ownership proof.
    """
    store=custody.Store();host=custody.host_identity();custody.enrollment()
    with store.locked():
        record=store.read()
        if record is None or record['reservation_id']!=identity or record['session']['project']!=project:
            raise ValueError('Exact retained reboot custody required')
        if record['host'][0]!=host[0] or record['host'][1]!=expected_old_boot or host[1]==expected_old_boot:
            raise ValueError('Same provider VM and distinct actual reboot required')
        try:idle(project)
        except ValueError as error:
            if str(error)!='Actual admission leases remain; never clear them':raise
            reconcile_old_boot_leases(project,record,host)
            idle(project)
        path=dossier(project);actor=custody.current_actor()
        custody.validate_actor(actor)
        evidence_path=path/'reboot-reconciliation.json';log=path/'reboot-cleanup.log'
        released=record['state']=='RELEASED'
        if evidence_path.exists() or evidence_path.is_symlink():
            evidence=root_json(evidence_path)
            if (set(evidence)!={'schema','reservation_id','historical_host','observed_host',
                                'record_sha256','actor','quiescent','outcome'}
                    or evidence['schema']!=1 or evidence['reservation_id']!=identity
                    or evidence['historical_host']!=record['host'] or evidence['observed_host']!=list(host)
                    or (not released and evidence['record_sha256']!=custody.digest(record))
                    or not isinstance(evidence['record_sha256'],str)
                    or not custody.re.fullmatch('[0-9a-f]{64}',evidence['record_sha256'])
                    or evidence['quiescent'] is not True
                    or evidence['outcome']!='INTERRUPTED_REBOOT_CUSTODY_ONLY'):
                raise ValueError('Published reboot evidence differs from retained custody')
            custody.validate_actor(evidence['actor'])
        else:
            if released:raise ValueError('Released reboot custody lacks published evidence')
            if log.exists() or log.is_symlink():
                raise ValueError('Unbound reboot cleanup log retained')
            evidence={'schema':1,'reservation_id':identity,'historical_host':record['host'],
                      'observed_host':list(host),'record_sha256':custody.digest(record),
                      'actor':actor,'quiescent':True,'outcome':'INTERRUPTED_REBOOT_CUSTODY_ONLY'}
            write(evidence_path,evidence)
        raw=custody.canonical(evidence)+b'\n'
        if released and not log.exists():
            raise ValueError('Released reboot custody lacks published cleanup log')
        if log.exists() or log.is_symlink():
            fd=os.open(log,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                info=os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1
                        or info.st_mode&0o022 or info.st_size!=len(raw) or os.read(fd,len(raw)+1)!=raw):
                    raise ValueError('Published reboot cleanup log differs')
            finally:os.close(fd)
        else:
            fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
            with os.fdopen(fd,'wb') as output:
                output.write(raw);output.flush();os.fsync(output.fileno())
            directory=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(directory)
            finally:os.close(directory)
        ordinal=len(record['stages'])-1
        completed=(not released and record['active'] is None and record['next_ordinal']==len(record['stages'])
                   and record['events'] and record['events'][-1].get('ordinal')==ordinal
                   and record['events'][-1].get('command')=='cleanup'
                   and record['events'][-1].get('exit_code')==0
                   and record['events'][-1].get('disposition') is None)
        terminal=(record['events'][-1] if completed else
                  {'ordinal':ordinal,'command':'cleanup','actor':evidence['actor'],
                   'exit_code':0,'log_sha256':file_digest(log)})
        if completed:
            # The ordinal-7 log and actor belong to the already completed
            # stage. Reboot evidence has its own log and publication actor.
            stage_log=dossier(project)/('stage-'+str(ordinal)+'.log')
            if (set(terminal)!={'ordinal','command','actor','exit_code','log_sha256'}
                    or terminal.get('log_sha256')!=file_digest(stage_log)):
                raise ValueError('Completed cleanup differs from retained stage log')
        if released:
            events=record['events']
            stage_terminal=events[-1] if events else None
            if stage_terminal!=terminal:
                stage_log=path/('stage-'+str(ordinal)+'.log')
                if (not stage_terminal or stage_terminal.get('ordinal')!=ordinal
                        or stage_terminal.get('command')!='cleanup' or stage_terminal.get('exit_code')!=0
                        or stage_terminal.get('disposition') is not None
                        or set(stage_terminal)!={'ordinal','command','actor','exit_code','log_sha256'}
                        or not stage_log.exists()
                        or stage_terminal.get('log_sha256')!=file_digest(stage_log)
                        or not any(custody.digest({**record,'state':state})==evidence['record_sha256']
                                   for state in ('SOURCE_VERIFIED','RECOVERING'))):
                    raise ValueError('Released cleanup differs from retained stage log')
                terminal=stage_terminal
            # Earlier interrupted/refused cleanup attempts are durable history.
            # Only the final, unique successful event reconciles this evidence.
            if (not events or events[-1]!=terminal
                    or any(event.get('command')=='cleanup' and event.get('ordinal')==ordinal
                           and event.get('disposition') not in ('INTERRUPTED','FINISH_REFUSED')
                           for event in events[:-1])):
                raise ValueError('Released reboot cleanup differs from published evidence')
            return record
        if record['active'] is not None:
            record['events'].append({**record['active'],'exit_code':None,'log_sha256':None,
                                     'disposition':'INTERRUPTED'})
        # The evidence and log may have been published by an earlier caller.
        # Keep that authenticated publication identity in the terminal receipt;
        # the current caller was observed independently above.
        if not completed:record['events'].append(terminal)
        record.update(state='RELEASED',active=None,next_ordinal=len(record['stages']))
        # Preserve original host and signed claim; separate evidence records the
        # new boot. No old stage is replayed or promoted to a successful build.
        store.write(record)
        return record
