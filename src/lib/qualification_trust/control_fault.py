"""Root-only, exact owned stage observation and one-shot supervisor interruption.

The caller retains the canonical global SH lock. Nothing here releases a lease,
cleans up a stage, or treats a process disappearing as successful completion.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import time

from . import reservation as custody
from . import control_engine as engine
from .control_observation import command

RUNTIME=Path('/var/lib/oxidex-builder')
CONTROL=Path('/usr/local/sbin/oxidex-reservation-control')
ENGINE=Path('/opt/oxidex-builder/qualification_trust/control_engine.py')
FAULT=Path('/opt/oxidex-builder/qualification_trust/control_fault.py')
RESERVATION=Path('/opt/oxidex-builder/qualification_trust/reservation.py')
OBSERVATION=Path('/opt/oxidex-builder/qualification_trust/control_observation.py')
RECEIVER=Path('/opt/oxidex-builder/qualification_trust/task20_receiver.py')
PACKAGE=Path('/opt/oxidex-builder/qualification_trust/__init__.py')
SCOPE_FIELDS={'schema','vm_id','boot','installation_nonce','project','reservation',
              'actor_pid','actor_start_ticks','cgroup','observed_at','deadline',
              'enrollment_sha256'}


def root_bytes(path, maximum=1048576, *, mode=None, with_identity=False, durable=False):
    """Bounded root-owned, nofollow read with stable inode and trusted ancestry."""
    path=Path(path)
    for parent in path.parents:
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('Untrusted root state ancestor')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_nlink!=1
                or before.st_mode&0o022 or not 0<before.st_size<=maximum
                or (mode is not None and stat.S_IMODE(before.st_mode)!=mode)):
            raise ValueError('Untrusted root state file')
        raw=os.read(fd,maximum+1)
        if durable:
            # A prior process may have published complete bytes but died before
            # syncing the file or its directory entry. Re-establish both here.
            os.fsync(fd)
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
        after=os.fstat(fd);current=path.lstat()
        keys=('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
        if (len(raw)!=before.st_size or any(getattr(before,key)!=getattr(after,key)
                or getattr(before,key)!=getattr(current,key) for key in keys)):
            raise ValueError('Root state changed during observation')
        return (raw,tuple(getattr(before,key) for key in keys)) if with_identity else raw
    finally:os.close(fd)


def runtime_binding(nonce, host):
    """Bind caller's nonce to committed root history and installed control bytes."""
    if not isinstance(nonce,str) or not re.fullmatch('[0-9a-f]{32}',nonce):
        raise ValueError('Exact installation nonce required')
    backup=RUNTIME/('runtime-update-'+nonce)
    plan=json.loads(root_bytes(backup/'plan.json'),object_pairs_hook=custody.pairs)
    rows=[json.loads(row,object_pairs_hook=custody.pairs)
          for row in root_bytes(backup/'journal.jsonl').splitlines()]
    verified=[index for index,row in enumerate(rows) if isinstance(row,dict)
              and row.get('stage') in ('FILES_AND_SERVICE_VERIFIED','FINISH_RECOVERED_HELD')]
    commits=[(index,row) for index,row in enumerate(rows) if isinstance(row,dict)
             and row.get('stage')=='FINISH_RELEASE_COMMITTED']
    last=rows[-1] if isinstance(rows[-1],dict) else {}
    if (not isinstance(plan,dict) or type(plan.get('schema')) is not int or plan['schema']!=1
            or plan.get('nonce')!=nonce or plan.get('vm_id')!=host[0]
            or not commits or not verified or verified[-1]>=commits[-1][0]
            or last.get('stage') not in
               ('FINISH_RELEASE_COMMITTED','COMMITTED_REBOOT_RECOVERED')
            or commits[-1][1].get('qualification_ready') is not False
            or (plan.get('boot') if last.get('stage')=='FINISH_RELEASE_COMMITTED'
                else last.get('boot'))!=host[1]):
        raise ValueError('Committed installation custody unavailable')
    assets=plan.get('assets')
    if not isinstance(assets,dict):raise ValueError('Installed asset inventory unavailable')
    committed=commits[-1][1].get('files')
    if not isinstance(committed,dict):raise ValueError('Committed file inventory unavailable')
    for path in (CONTROL,ENGINE,FAULT,RESERVATION,OBSERVATION,RECEIVER,PACKAGE):
        entry=assets.get(str(path))
        if not isinstance(entry,dict) or set(entry)!={'data','sha256'}:
            raise ValueError('Selected control asset absent from installation')
        raw=base64.b64decode(entry['data'],validate=True)
        if (hashlib.sha256(raw).hexdigest()!=entry['sha256']
                or not isinstance(committed.get(str(path)),dict)
                or committed[str(path)].get('sha256')!=entry['sha256']
                or hashlib.sha256(root_bytes(path,262144,mode=0o700 if path==CONTROL else 0o644)).hexdigest()!=entry['sha256']):
            raise ValueError('Installed control asset differs from committed bytes')
    return hashlib.sha256(root_bytes(custody.CONFIG,16384)).hexdigest()


def unit_identity(project,identity,active):
    ordinal=active['ordinal'];actor=active['actor']
    if type(ordinal) is not int or not 0<=ordinal<8:
        raise ValueError('Unknown active stage ordinal')
    unit='oxidex-reservation-'+identity+'-'+str(ordinal)+'.service'
    names='LoadState,ActiveState,SubState,MainPID,ControlPID,ControlGroup,User,Transient,KillMode,Restart,Type'
    raw=command(['/usr/bin/systemctl','show',unit,'--property='+names,'--no-pager'])
    lines=raw.splitlines()
    if len(lines)!=11 or any(line.count('=')!=1 for line in lines):
        raise ValueError('Incomplete actual stage service observation')
    fields=dict(line.split('=',1) for line in lines)
    if (set(fields)!=set(names.split(',')) or fields['LoadState']!='loaded'
            or fields['ActiveState']!='active' or fields['SubState']!='running'
            or fields['MainPID']!=str(actor['pid']) or fields['ControlPID']!='0'
            or fields['User'] not in ('','root') or fields['Transient']!='yes'
            or fields['KillMode']!='control-group' or fields['Restart']!='no'
            or fields['Type']!='exec'):
        raise ValueError('Actual stage unit differs from selected owner')
    cgroup='/system.slice/'+unit
    if fields['ControlGroup']!=cgroup:
        raise ValueError('Actual stage cgroup differs')
    observed,_=custody.process(actor['pid'])
    if observed!=actor:raise ValueError('Stage PID/start ticks changed')
    proc=Path('/proc')/str(actor['pid'])
    if proc_bytes(proc/'cgroup',4096)!=('0::'+cgroup+'\n').encode():
        raise ValueError('Stage process is outside selected cgroup')
    status=proc_bytes(proc/'status',65536).decode()
    uids=[line.split()[1:] for line in status.splitlines() if line.startswith('Uid:')]
    if uids!=[['0','0','0','0']]:raise ValueError('Selected stage is not root owned')
    argv=proc_bytes(proc/'cmdline',4096).rstrip(b'\0').split(b'\0')
    expected=[b'/usr/local/sbin/oxidex-reservation-control',b'stage',b'--project',
              project.encode(),b'--reservation',identity.encode(),b'--ordinal',str(ordinal).encode()]
    if argv not in (expected,[b'/usr/bin/python3',*expected]):
        raise ValueError('Selected stage command identity differs')
    group=Path('/sys/fs/cgroup')/cgroup.lstrip('/')
    info=group.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
        raise ValueError('Untrusted selected stage cgroup')
    members=(group/'cgroup.procs').read_text().splitlines()
    if str(actor['pid']) not in members or any(not row.isdecimal() for row in members):
        raise ValueError('Selected stage absent from actual cgroup')
    if custody.process(actor['pid'])[0]!=actor:
        raise ValueError('Stage identity changed after service observation')
    return cgroup


def proc_bytes(path,maximum):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        raw=os.read(fd,maximum+1)
        if not raw or len(raw)>maximum:raise ValueError('Incomplete bounded process identity')
        return raw
    finally:os.close(fd)


def snapshot(project,identity,nonce,*,record=None):
    """A complete live join; caller holds global SH, not the store writer lock."""
    if custody.enrollment() is None:
        raise ValueError('Enrolled mounted reservation custody required')
    host=custody.host_identity()
    enrollment_sha256=runtime_binding(nonce,host)
    if record is None:
        store=custody.Store()
        record_path=store.root/'record.json'
        before,identity_before=root_bytes(record_path,custody.LIMIT,mode=0o600,with_identity=True)
        selected=store.read()
        if selected is None or custody.canonical(selected)!=before:
            raise ValueError('Reservation changed during observation')
    else:selected=record
    # A caller retaining the store lock supplies its already validated record.
    # The dossier and descriptor are independently root-owned and nofollow.
    path=engine.dossier(project)
    request=json.loads(root_bytes(path/'descriptor.json',custody.LIMIT),object_pairs_hook=custody.pairs)
    engine.validate_request(request)
    custody.Store.match(selected,identity,host)
    if (selected['session']!=request['session'] or selected['stages']!=request['stages']
            or selected['descriptor_sha256']!=custody.digest(request)):
        raise ValueError('Selected root descriptor changed')
    if (selected['session']['project']!=project or selected['host']!=list(host)
            or selected['state'] not in ('SOURCE_VERIFIED','HELD') or selected['active'] is None):
        raise ValueError('No selected active owned stage')
    active=selected['active'];cgroup=unit_identity(project,identity,active)
    if record is None:
        after,identity_after=root_bytes(record_path,custody.LIMIT,mode=0o600,with_identity=True)
        if after!=before or identity_after!=identity_before:
            raise ValueError('Reservation changed during observation')
    return dict(schema=1,vm_id=host[0],boot=host[1],installation_nonce=nonce,
                project=project,reservation=identity,actor_pid=active['actor']['pid'],
                actor_start_ticks=active['actor']['start'],cgroup=cgroup,
                enrollment_sha256=enrollment_sha256),custody.digest(selected)


def observe(project,identity,nonce):
    first,record_digest=snapshot(project,identity,nonce)
    second,after_digest=snapshot(project,identity,nonce)
    if first!=second or record_digest!=after_digest:
        raise ValueError('Active owner changed during observation')
    now=time.time()
    return dict(first,observed_at=now,deadline=now+120)


def validate_scope(scope,now):
    if not isinstance(scope,dict) or set(scope)!=SCOPE_FIELDS or type(scope.get('schema')) is not int or scope['schema']!=1:
        raise ValueError('Exact reviewed fault scope required')
    for key in ('observed_at','deadline'):
        if type(scope[key]) not in (int,float):raise ValueError('Invalid scope time')
    if not 0<=now-scope['observed_at']<=120 or not now<scope['deadline']<=scope['observed_at']+120:
        raise ValueError('Stale fault scope')
    for key in ('actor_pid','actor_start_ticks'):
        if type(scope[key]) is not int or scope[key]<=0:raise ValueError('Invalid actor identity')
    return scope


def interrupt_owned(scope):
    """Fixed SIGKILL to the selected Task20 stage supervisor via Linux pidfd."""
    validate_scope(scope,time.time())
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
        raise ValueError('Linux pidfd exact interruption unavailable')
    project=scope['project'];identity=scope['reservation'];nonce=scope['installation_nonce']
    store=custody.Store()
    with store.locked():
        record=store.read();custody.Store.match(record,identity,custody.host_identity())
        active=record['active']
        if (record['state'] not in ('SOURCE_VERIFIED','HELD') or active is None or active['ordinal']!=4
                or active['command']!='task20-execute'):
            raise ValueError('Only active reviewed execute supervisor may be interrupted')
        current,_=snapshot(project,identity,nonce,record=record)
        if any(scope[key]!=value for key,value in current.items()):
            raise ValueError('Fault scope differs from current owned actor')
        intent=engine.dossier(project)/'interrupt-owned-4.json'
        retained=intent.exists() or intent.is_symlink()
        if retained:
            previous=json.loads(root_bytes(intent,custody.LIMIT,mode=0o400,durable=True),object_pairs_hook=custody.pairs)
            original=previous.get('scope') if isinstance(previous,dict) else None
            if not isinstance(original,dict) or set(original)!=SCOPE_FIELDS:
                raise ValueError('Retained interruption intent differs')
            validate_scope(original,original['observed_at'])
            expected=dict(schema=1,scope=original,scope_sha256=custody.digest(original),
                          actor=active['actor'],host=record['host'],
                          reservation_id=identity,ordinal=4,action='PIDFD_SIGKILL')
            if (custody.canonical(previous)!=custody.canonical(expected)
                    or any(original[key]!=value for key,value in current.items())):
                raise ValueError('Retained interruption intent differs')
        elif record['state']=='HELD':
            raise ValueError('Held stage lacks an authenticated interruption intent')
        pidfd=os.pidfd_open(current['actor_pid'],0)
        try:
            again,_=snapshot(project,identity,nonce,record=record)
            if again!=current or time.time()>=scope['deadline']:
                raise ValueError('Selected actor changed before interruption')
            if not retained:
                engine.write(intent,dict(schema=1,scope=scope,scope_sha256=custody.digest(scope),
                                         actor=active['actor'],host=record['host'],
                                         reservation_id=identity,ordinal=4,action='PIDFD_SIGKILL'))
            # A visible HELD record can likewise precede its directory fsync.
            record['state']='HELD'
            store.write(record)
            # pidfd prevents PID reuse between this last kernel/service join and
            # the signal. Any failure leaves the durable hold and intent intact.
            unit_identity(project,identity,active)
            validate_scope(scope,time.time())
            signal.pidfd_send_signal(pidfd,signal.SIGKILL,None,0)
        finally:os.close(pidfd)
    return dict(status='INTERRUPTION_REQUESTED_HELD',reservation=identity,
                actor_pid=current['actor_pid'],actor_start_ticks=current['actor_start_ticks'])
