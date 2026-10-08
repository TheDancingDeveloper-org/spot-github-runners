"""Root-owned Task20 endpoint implementation, with durable stage custody."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from . import reservation as custody
from .control_observation import root_json,sealed_binding,quiescent
from .task20_receiver import receive,MEMBERS,MAX_ARCHIVE

BUILD=Path('/mnt/runner-data/remote-build')


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
            if total>128*1024*1024:raise ValueError('Bounded control log exceeded; preserve bytes and hold')
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
                    for member in (path/'receive/incoming').iterdir():member.rename(source/member.name)
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
                result=subprocess.run(['/usr/local/bin/oxidex-remote-build',project,*argv],
                                      stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
                                      env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C'})
                status=result.returncode
                if ordinal==5 and status==0:
                    capture_pack(project,path)
            output.flush();os.fsync(output.fileno())
        sha=file_digest(log)
        return store.finish(identity,ordinal,host=host,actor=actor,exit_code=status,
                            log_sha256=sha,quiescent=lambda:idle(project),binding=binding)
    except Exception:
        # Preserve active identity and retained bytes. Explicit reconciliation,
        # never a retry or inferred terminal success, resolves partial effects.
        with store.locked():
            held=store.read();store.match(held,identity,host);held['state']='HELD';store.write(held)
        raise


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


def export_locked(project,identity,output):
    path,request,record=selected(project,identity)
    if record['state']!='SOURCE_VERIFIED' or record['active'] is not None or record['next_ordinal'] not in (6,7):
        raise ValueError('Actual pack terminal required before result export')
    idle(project)
    source=BUILD/'targets'/project/'task20-results.tar.gz'
    expected=root_json(path/'pack-observed.json')
    with activity(project,'export'):
        fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>2*1024**3:
                raise ValueError('Bounded regular result archive required')
            digest=hashlib.sha256();count=0
            while True:
                block=os.read(fd,1024*1024)
                if not block:break
                output.write(block);digest.update(block);count+=len(block)
            after=os.fstat(fd)
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or count!=before.st_size:
                raise ValueError('Export bytes changed; never replay automatically')
            if expected['archive_sha256']!=digest.hexdigest() or expected['archive_bytes']!=count:
                raise ValueError('Export differs from actual retained pack bytes')
            receipt={'schema':1,'reservation_id':identity,'project':project,'sha256':digest.hexdigest(),'bytes':count}
            write(path/'export-result.json',receipt)
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
    idle(project)
    with store.locked():
        record=store.read()
        if record is None or record['reservation_id']!=identity or record['session']['project']!=project:
            raise ValueError('Exact retained reboot custody required')
        if record['host'][0]!=host[0] or record['host'][1]!=expected_old_boot or host[1]==expected_old_boot:
            raise ValueError('Same provider VM and distinct actual reboot required')
        if record['state']=='RELEASED':return record
        path=dossier(project);actor=custody.current_actor()
        evidence={'schema':1,'reservation_id':identity,'historical_host':record['host'],
                  'observed_host':list(host),'record_sha256':custody.digest(record),
                  'actor':actor,'quiescent':True,'outcome':'INTERRUPTED_REBOOT_CUSTODY_ONLY'}
        write(path/'reboot-reconciliation.json',evidence)
        log=path/'reboot-cleanup.log'
        with log.open('xb') as output:
            output.write(custody.canonical(evidence)+b'\n');output.flush();os.fsync(output.fileno())
        if record['active'] is not None:
            record['events'].append({**record['active'],'exit_code':None,'log_sha256':None,
                                     'disposition':'INTERRUPTED'})
        ordinal=len(record['stages'])-1
        record['events'].append({'ordinal':ordinal,'command':'cleanup','actor':actor,
                                 'exit_code':0,'log_sha256':file_digest(log)})
        record.update(state='RELEASED',active=None,next_ordinal=len(record['stages']))
        # Preserve original host and signed claim; separate evidence records the
        # new boot. No old stage is replayed or promoted to a successful build.
        store.write(record)
        return record
