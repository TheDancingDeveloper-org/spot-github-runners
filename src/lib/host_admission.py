"""Root-owned aggregate memory admission for runner hooks and remote builds.

Installed standalone on hosts. Clients cannot supply their own budget or
release another container's lease: Unix peer credentials determine ownership.
"""
import fcntl
import json
import math
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

BUILD_ROOT = Path('/mnt/runner-data/remote-build')
LOCK_ROOT = Path('/run')
CONFIG_PATH = Path('/etc/gha-admission.json')
ADMISSION_SOCKET = '/run/gha-admission/socket'
LEDGER_ROOT = Path('/var/lib/gha-admission')
BENCHMARK_CONFIG = None


class OrphanRunnerHook(ValueError):
    """The requesting hook has outlived its GitHub-assigned Worker."""


def lock_file(path):
    return open(path,'a',opener=lambda name,flags:os.open(name,flags|os.O_NOFOLLOW,0o600))


def _benchmark_module():
    try:
        from . import benchmark_admission
    except ImportError:
        import benchmark_admission
    return benchmark_admission


def benchmark_capacity_record(config):return _benchmark_module().benchmark_capacity_record(config)


def validate_benchmark_isolation(config,receipt,snapshot,now):
    return _benchmark_module().validate_benchmark_isolation(config,receipt,snapshot,now)


def configure_benchmark():
    """Fixed private runtime entry; no environment/client path override."""
    global BUILD_ROOT,LOCK_ROOT,CONFIG_PATH,ADMISSION_SOCKET,LEDGER_ROOT,BENCHMARK_CONFIG
    module=_benchmark_module();config,_=module.config_and_receipt()
    BUILD_ROOT=Path(module.ROOT);LOCK_ROOT=Path('/run/oxidex-benchmark278')
    CONFIG_PATH=Path(module.CONFIG);ADMISSION_SOCKET=module.SOCKET
    LEDGER_ROOT=Path(module.LEDGER).parent;BENCHMARK_CONFIG=config
    return config

def admission_budget(tier, observed_overhead_gib, workload_peak_gib, *, slots=None):
    slots = tier.initial_slots if slots is None else slots
    if not 1 <= slots <= tier.maximum_slots or not 0 <= observed_overhead_gib < tier.ram_gib:
        raise ValueError('Invalid host overhead or runner slots')
    if not math.isfinite(workload_peak_gib) or workload_peak_gib <= 0:
        raise ValueError('Invalid workload memory reservation')
    reserve = max(2.,observed_overhead_gib) + .25 * slots
    work = tier.ram_gib - reserve
    if work < workload_peak_gib:
        raise ValueError('Workload cannot fit after host/listener reserve')
    return dict(reserve_gib=reserve,work_gib=work,
                concurrency=min(slots,int(work//workload_peak_gib)))


def builder_memory_budget(total_gib,available_gib,working_gib):
    """Actual total minus resident non-work overhead (at least2GiB).

    Working bytes must come from a complete identity/cap-matched inventory;
    do not subtract already-resident host usage a second time.
    """
    values=(total_gib,available_gib,working_gib)
    try:
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in values):return 0
        if available_gib>total_gib or working_gib>total_gib-available_gib:return 0
    except OverflowError:return 0
    reserve=max(2.,total_gib-available_gib-working_gib)
    envelope=total_gib-reserve
    return 28. if envelope>=28 else 0.


def builder_capacity_record(config):
    expected=dict(host_cpus=16,builder_cpus=16,work_gib=28,builder_gib=28,runner_gib=0)
    if (config.get('role')!='builder' or config.get('runner_names')!=[]
        or any(type(config.get(k)) not in (int,float) or config[k]!=v for k,v in expected.items())):
        raise ValueError('Unknown dedicated builder host capacity')
    return dict(schema=2,role='builder',host_cpus=16,host_cpu_reserve=0,
                host_memory_reserve_gib=2,builder_cpus=16,builder_gib=28,
                runner_cpus=0,runner_gib=0,work_gib=28)


def execution_limits(capacity):
    if isinstance(capacity,dict) and capacity.get('role')=='benchmark-builder':
        expected=dict(schema=3,role='benchmark-builder',host_cpus=32,host_cpu_reserve=0,
            host_memory_reserve_gib=2,builder_cpus=16,builder_gib=28,runner_cpus=0,runner_gib=0,
            work_gib=28,builder_parallelism=1)
        if capacity!=expected or any(type(capacity[k]) is not type(v) for k,v in expected.items()):
            raise ValueError('Unknown benchmark capacity schema')
        return 16,0,0
    expected=dict(schema=2,role='builder',host_cpus=16,host_cpu_reserve=0,
                  host_memory_reserve_gib=2,builder_cpus=16,builder_gib=28,
                  runner_cpus=0,runner_gib=0,work_gib=28)
    if set(capacity)!=set(expected) or any(type(capacity[k]) is not type(v) or capacity[k]!=v for k,v in expected.items()):
        raise ValueError('Unknown dedicated builder capacity schema')
    # The16CPU container shares host services; there is no extra CPU allowance.
    return 16,0,0


def builder_owner_kind(owner,config):
    benchmark=config.get('role')=='benchmark-builder'
    if benchmark:_benchmark_module().production_config(config)
    else:builder_capacity_record(config)
    if os.cpu_count()!=(32 if benchmark else 16):raise ValueError('Actual host CPU count differs from builder role')
    parts=owner.split(':',2)
    if (len(parts)!=3 or parts[0]!='host' or not re.fullmatch('[0-9]+',parts[1])
        or process_identity(int(parts[1]))!=owner):raise ValueError('Unknown builder process owner')
    if benchmark:
        actual,_=_benchmark_module().config_and_receipt()
        if actual!=config:raise ValueError('Actual benchmark policy changed')
        return 'builder'
    actual=json.loads(Path('/etc/oxidex-remote-build.json').read_text())
    pin=actual.get('approved_image_id')
    if ({k:v for k,v in actual.items() if k!='approved_image_id'}!={'role':'builder','cpus':16,'memory_gib':28,'image':'oxidex-remote-builder:1.99.0'}
        or not isinstance(pin,str) or not re.fullmatch('sha256:[0-9a-f]{64}',pin)):
        raise ValueError('Actual builder launcher policy differs from reservation')
    return 'builder'


def _observed_builder_envelope(config):
    builder_capacity_record(config)
    memory={line.split(':')[0]:int(line.split()[1])/1024**2 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
    result=subprocess.run(['/usr/bin/docker','ps','-aq','--no-trunc'],check=True,capture_output=True,text=True,timeout=10)
    if result.stderr:raise ValueError('Incomplete builder inventory diagnostics')
    inventory=result.stdout.split()
    if len(inventory)>1 or any(not re.fullmatch('[0-9a-f]{64}',cid) for cid in inventory):raise ValueError('Unknown aggregate builder inventory')
    working=0.
    for cid in inventory:
        owner,row=docker_identity(cid);state=row['State'];caps=row['HostConfig']
        if any(not isinstance(v,dict) for v in (state,caps,row['Config'])):raise ValueError('Malformed builder inventory')
        if (row['Id']!=cid or builder_cache_identity(row) is None or row['Config'].get('Image')!='oxidex-remote-builder:1.99.0'
            or row['Config'].get('User')!='1001:1001' or caps.get('NanoCpus')!=16*10**9
            or caps.get('Memory')!=28*1024**3 or caps.get('MemorySwap')!=28*1024**3):raise ValueError('Unmatched aggregate builder caps/identity')
        pid=state['Pid']
        if type(pid) is not int or pid<=0:raise ValueError('Unknown builder PID')
        cgroup=Path(f'/proc/{pid}/cgroup').read_text()
        paths=[line[3:] for line in cgroup.splitlines() if line.startswith('0::')]
        if len(paths)!=1 or not paths[0].startswith('/') or cid not in paths[0] or '..' in paths[0].split('/'):raise ValueError('Unmatched builder cgroup')
        directory=Path('/sys/fs/cgroup'+paths[0])
        if int((directory/'memory.max').read_text())!=28*1024**3:raise ValueError('Actual cgroup cap differs from policy')
        working+=int((directory/'memory.current').read_text())/1024**3
        if docker_identity(cid)[0]!=owner or Path(f'/proc/{pid}/cgroup').read_text()!=cgroup:raise ValueError('Builder changed during memory sample')
    return builder_memory_budget(memory['MemTotal'],memory['MemAvailable'],working)


def observed_builder_envelope(config):
    try:return _observed_builder_envelope(config)
    except (TypeError,IndexError,AttributeError,OverflowError) as error:
        raise ValueError('Malformed aggregate builder evidence') from error


def legacy_owner_for_worker(owner):
    match=re.fullmatch(r'runner:([a-f0-9]{64}):(.+):([0-9]+):([0-9]+)',owner)
    if match is None:
        raise ValueError('Invalid Worker lease identity')
    return f'container:{match[1]}:{match[2]}'


class LeasePool:
    def __init__(self,path,limit_gib,*,alive,capacity=None,owner_kind=None,retain=None,removal_fence=None):
        self.path = Path(path)
        if not math.isfinite(limit_gib) or limit_gib <= 0:
            raise ValueError('Invalid aggregate memory budget')
        self.limit = limit_gib
        self.alive = alive
        self.retain=retain;self.removal_fence=removal_fence
        if removal_fence is not None and not callable(removal_fence):raise ValueError('Invalid removal effect fence')
        if retain is not None and not callable(retain):raise ValueError('Invalid reservation retention policy')
        self.capacity=capacity;self.owner_kind=owner_kind
        if capacity is not None:
            execution_limits(capacity)
            if limit_gib!=28 or not callable(owner_kind):raise ValueError('Unknown builder pool')

    def _fits(self,rows,owner,amount):
        if self.capacity is None:return sum(rows.values())+amount<=self.limit
        requested=dict(rows);requested[owner]=amount
        for actor,reservation in requested.items():
            if self.owner_kind(actor)!='builder' or type(reservation) not in (int,float) or reservation!=28:
                raise ValueError('Unknown/legacy builder reservation')
        return len(requested)<=1 and sum(requested.values())<=self.limit

    def _change(self,owner,amount,legacy_owner=None):
        if legacy_owner is not None:
            if (self.capacity is not None or self.retain is not None
                or legacy_owner!=legacy_owner_for_worker(owner)):
                raise ValueError('Invalid runner lease transition')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # Lock a separate inode: the data file is atomically replaced.
        fd = os.open(str(self.path)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'r+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            rows = json.loads(self.path.read_text()) if self.path.exists() else {}
            original_rows=dict(rows)
            # A real maintenance reservation can outlive its process. Custody
            # is distinct from liveness, and only explicit cleanup unprotects it.
            protected={key for key,value in rows.items() if self.retain is not None and self.retain(key,value)}
            rows = {key:value for key,value in rows.items() if key in protected or self.alive(key)}
            # An upgraded hook names the Worker while an older daemon may
            # have reserved the same persistent container. Transfer/release
            # that exact row under this one ledger lock, with no double count.
            legacy_value=rows.pop(legacy_owner,None) if legacy_owner is not None else None
            accepted = True
            if amount is not None and legacy_value is not None and legacy_value!=amount:
                # A container may still have its old, larger memory cap.
                # Never reduce its aggregate reservation by renaming the row.
                accepted=False
            elif amount is None:
                if owner in protected:raise ValueError('Owned maintenance cleanup proof required before release')
                rows.pop(owner,None)
            elif owner in rows:
                accepted = rows[owner] == amount and (self.capacity is None or self._fits({k:v for k,v in rows.items() if k!=owner},owner,amount))
            elif self._fits(rows,owner,amount):
                rows[owner] = amount
            else:
                accepted = False
            if not accepted and legacy_value is not None:
                rows[legacy_owner]=legacy_value
            temporary = self.path.with_suffix('.tmp')
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as target:
                os.fchmod(target.fileno(),0o600)
                json.dump(rows,target)
                target.flush()
                os.fsync(target.fileno())
            if self.removal_fence is not None:
                # Pure only: all observations and target fsync are finished.
                # Refusal leaves the ORIGINAL actual ledger intact under EX.
                self.removal_fence({key:value for key,value in original_rows.items() if key not in rows})
            temporary.replace(self.path)
            return accepted

    def acquire(self,owner,amount):
        if not math.isfinite(amount) or not 0 < amount <= self.limit:
            raise ValueError('Request exceeds host memory budget')
        return self._change(owner,amount)

    def release(self,owner):
        self._change(owner,None)

    def acquire_runner(self,owner,amount):
        if not math.isfinite(amount) or not 0 < amount <= self.limit:
            raise ValueError('Request exceeds host memory budget')
        return self._change(owner,amount,legacy_owner_for_worker(owner))

    def release_runner(self,owner):
        self._change(owner,None,legacy_owner_for_worker(owner))

    def refresh(self):
        # Reclaim only independently dead owners, never another live lease.
        self._change(None,None)


def process_identity(pid):
    stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    return f'host:{pid}:{stat[19]}'


def process_record(pid):
    root = Path(f'/proc/{pid}')
    stat = (root/'stat').read_text().rsplit(')',1)[1].split()
    return int(stat[1]),stat[19],(root/'comm').read_text().strip(),(root/'cgroup').read_text()


def runner_cgroup(cgroup):
    ids = re.findall(r'(?:docker[-/])([a-f0-9]{64})(?:\.scope)?',cgroup)
    return next(iter(set(ids))) if len(set(ids)) == 1 else None


def worker_owner(pid,container_id,container_started):
    """Bind a hook to its live Worker, not its persistent runner container."""
    seen=set()
    for _ in range(32):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        try:
            parent,start,comm,cgroup=process_record(pid)
        except FileNotFoundError as error:
            raise OrphanRunnerHook('Runner hook lost its Worker ancestry') from error
        if runner_cgroup(cgroup)!=container_id:
            raise OrphanRunnerHook('Runner hook left its container')
        if comm=='Runner.Worker':
            return f'runner:{container_id}:{container_started}:{pid}:{start}'
        pid=parent
    raise OrphanRunnerHook('Runner hook has no live Worker ancestor')


def legacy_runner_idle(record):
    """Reclaim old container leases only on a complete baseline-only process list."""
    container_id=record['Id']
    output=subprocess.run(['/usr/bin/docker','top',container_id,'-eo','pid,comm'],
                          check=True,capture_output=True,text=True,timeout=10).stdout.splitlines()
    if not output or output[0].split()!=['PID','COMMAND']:
        return False
    rows=[line.split() for line in output[1:]]
    if (len(rows)!=3 or any(len(row)!=2 or not row[0].isdigit() for row in rows)
        or {row[1] for row in rows}!={'run.sh','run-helper.sh','Runner.Listener'}):
        return False
    pids={comm:int(pid) for pid,comm in rows}
    if record['State'].get('Pid')!=pids['run.sh']:
        return False
    for comm,parent in (('run-helper.sh','run.sh'),('Runner.Listener','run-helper.sh')):
        actual_parent,_,actual_comm,cgroup=process_record(pids[comm])
        if actual_parent!=pids[parent] or actual_comm!=comm or runner_cgroup(cgroup)!=container_id:
            return False
    return True


def docker_identity(container):
    row = json.loads(subprocess.run(['docker','inspect',container],check=True,capture_output=True,text=True,timeout=10).stdout)[0]
    if not row['State']['Running']:
        raise ValueError('Container is not running')
    return f"container:{row['Id']}:{row['State']['StartedAt']}", row


def owner_alive(owner):
    try:
        if owner.startswith('runner:'):
            match=re.fullmatch(r'runner:([a-f0-9]{64}):(.+):([0-9]+):([0-9]+)',owner)
            if match is None:
                return True
            container_id,container_started,pid,start=match.groups()
            try:
                _,actual_start,comm,cgroup=process_record(int(pid))
            except FileNotFoundError:
                return False
            if comm!='Runner.Worker' or actual_start!=start or runner_cgroup(cgroup)!=container_id:
                return False
            actual,row=docker_identity(container_id)
            return actual==f'container:{container_id}:{container_started}'
        kind,identity,start = owner.split(':',2)
        if kind == 'host':
            if process_identity(int(identity)) == owner:
                return True
            return builders_running()  # PID reuse cannot free a surviving build.
        row=json.loads(subprocess.run(['/usr/bin/docker','inspect',identity],
            check=True,capture_output=True,text=True,timeout=10).stdout)[0]
        if not (row['State']['Running'] and row['Id']==identity
                and row['State']['StartedAt']==start):
            return False
        # Old versions used the lifetime of a persistent runner container as
        # the lease lifetime. Keep any uncertain process; only a complete
        # baseline-only inventory proves its old job has ended.
        return not legacy_runner_idle(row)
    except (OSError,ValueError,KeyError,IndexError,subprocess.SubprocessError):
        if owner.startswith(('host:','runner:')):
            if owner.startswith('runner:'):
                return True  # Unknown Docker/process state is not proof of idleness.
            return builders_running()
        try:
            rows=subprocess.run(['/usr/bin/docker','ps','-a','--no-trunc','--format','{{.ID}}'],
                check=True,capture_output=True,text=True,timeout=10).stdout.splitlines()
            # Only a complete successful ID inventory can prove absence.
            return owner.split(':',2)[1] in rows
        except (OSError,ValueError,subprocess.SubprocessError):
            return True  # Unknown state cannot reclaim a live runner lease.


def builder_cache_identity(record):
    """Recognize only the root adapter's exact name, labels and bind mounts."""
    config=record.get('Config') or {}
    if not isinstance(config,dict):return None
    labels=config.get('Labels') or {}
    if not isinstance(labels,dict):return None
    project=labels.get('oxidex.cache-project','')
    run=labels.get('oxidex.run-id','')
    if (not isinstance(project,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',project)
        or not isinstance(run,str) or not re.fullmatch(r'run-[0-9a-f]{32}',run)
        or labels.get('oxidex.remote-build')!='true'
        or record.get('Name')!='/oxidex-run-'+run):
        return None
    mounts=record.get('Mounts',[])
    if not isinstance(mounts,list) or any(not isinstance(mount,dict) for mount in mounts):
        return None
    for destination,folder in (('/src','sources'),('/target','targets')):
        matching=[mount for mount in mounts if mount.get('Destination')==destination]
        if (len(matching)!=1 or matching[0].get('Type')!='bind'
            or matching[0].get('Source')!=str(BUILD_ROOT/folder/project)):
            return None
    return project,run


def stopped_builder(record):
    state=record.get('State') or {}
    if not isinstance(state,dict):return False
    return (state.get('Running') is False and state.get('Paused') is False
            and state.get('Restarting') is False and state.get('Pid')==0
            and state.get('Status') in ('created','exited','dead'))


def reconcile_stopped_builders():
    """Called under global SH/EX; never force-remove a container or take a wait lock."""
    result=subprocess.run(['/usr/bin/docker','ps','-aq','--no-trunc',
                           '--filter','label=oxidex.remote-build=true'],
                          check=True,capture_output=True,text=True,timeout=10)
    ids=result.stdout.split()
    if len(set(ids))!=len(ids) or any(not re.fullmatch(r'[0-9a-f]{64}',value) for value in ids):
        raise ValueError('Incomplete builder container inventory')
    remaining=False
    for identity in ids:
        def inspect():
            rows=json.loads(subprocess.run(['/usr/bin/docker','inspect',identity],
                check=True,capture_output=True,text=True,timeout=10).stdout)
            if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict) or rows[0].get('Id')!=identity:
                raise ValueError('Builder identity changed')
            return rows[0]
        record=inspect()
        cache=builder_cache_identity(record)
        if cache is None or not stopped_builder(record):
            remaining=True
            continue
        with lock_file(LOCK_ROOT/f'oxidex-cache-active-{cache[0]}.lock') as active:
            try:
                fcntl.flock(active,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                remaining=True
                continue
            # A start can complete between the first inspect and lock acquire.
            # No protocol client survives this exclusive lock, but recheck state
            # and identity before rm, which also refuses a now-running container.
            current=inspect()
            if builder_cache_identity(current)!=cache or not stopped_builder(current):
                remaining=True
                continue
            subprocess.run(['/usr/bin/docker','rm',identity],check=True,
                           capture_output=True,text=True,timeout=10)
    return remaining


def builders_running(*,global_lock_held=False):
    try:
        if global_lock_held:
            # Only the root drain CLI calls this, under its inherited global EX.
            return reconcile_stopped_builders()
        with lock_file(LOCK_ROOT/'oxidex-build.lock') as global_lock:
            fcntl.flock(global_lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
            return reconcile_stopped_builders()
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        return True  # Unknown container state cannot reclaim memory.


def memory_envelope(config,total_gib,used_gib,container_usage):
    working=0.
    for usage in container_usage:
        match=re.fullmatch(r'\s*([0-9.]+)\s*(B|kB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*',usage.split('/')[0])
        if not match:raise ValueError('Unrecognized Docker memory usage')
        scales={'B':1,'kB':1000,'MB':1000**2,'GB':1000**3,'TB':1000**4,
                'KiB':1024,'MiB':1024**2,'GiB':1024**3,'TiB':1024**4}
        working+=float(match[1])*scales[match[2]]/1024**3
    reserve=max(2,used_gib-working)+.25*len(config['runner_names'])
    return max(0,min(config['work_gib'],total_gib-reserve))


def observed_envelope(config):
    if config.get('role')=='benchmark-builder':return _benchmark_module().observed_benchmark_envelope(config)
    if config.get('role')=='builder':return observed_builder_envelope(config)
    memory={line.split(':')[0]:int(line.split()[1])/1024/1024
            for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
    result=subprocess.run(['/usr/bin/docker','stats','--no-stream','--format','{{json .}}'],
                           check=True,capture_output=True,text=True,timeout=15)
    usage=[json.loads(line)['MemUsage'] for line in result.stdout.splitlines() if line.strip()]
    return memory_envelope(config,memory['MemTotal'],memory['MemTotal']-memory['MemAvailable'],usage)


def peer_owner(pid,uid,config):
    if uid == 0:
        return process_identity(pid),'builder'
    if uid != 1001:
        raise ValueError('Unauthorized admission peer')
    cgroup = Path(f'/proc/{pid}/cgroup').read_text()
    identity = runner_cgroup(cgroup)
    if identity is None:
        raise ValueError('Runner must belong to a Docker cgroup')
    _,row = docker_identity(identity)
    if row['Name'].lstrip('/') not in config['runner_names']:
        raise ValueError('Container is outside this runner deployment')
    return worker_owner(pid,identity,row['State']['StartedAt']),'runner'


def request(action,kind='runner'):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(ADMISSION_SOCKET)
        client.sendall((json.dumps(dict(action=action,kind=kind))+'\n').encode())
        client.shutdown(socket.SHUT_WR)
        response = json.loads(client.recv(4096))
    if isinstance(response,dict) and response.get('code')=='orphaned-runner-hook':
        raise OrphanRunnerHook(response.get('error','Runner hook lost its Worker ancestry'))
    if 'error' in response:
        raise RuntimeError(response['error'])
    return response['accepted']


def acquire_build():
    if not request('acquire','builder'):
        raise SystemExit(75)


def release_build():
    request('release','builder')


def serve():
    if os.geteuid() != 0:
        raise RuntimeError('Admission service must run as root')
    directory = Path(ADMISSION_SOCKET).parent
    directory.mkdir(mode=0o755,exist_ok=True)
    ledger = LEDGER_ROOT
    ledger.mkdir(mode=0o700,exist_ok=True)
    path = directory/'socket'
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
        server.bind(str(path))
        os.chown(path,0,1001)
        path.chmod(0o660)
        server.listen(128)
        envelope=0
        measured_at=0
        while True:
            client,_ = server.accept()
            with client:
                try:
                    client.settimeout(5)
                    pid,uid,_ = struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    config = (_benchmark_module().config_and_receipt()[0] if BENCHMARK_CONFIG is not None
                              else json.loads(Path(str(CONFIG_PATH)).read_text()))
                    if BENCHMARK_CONFIG is not None and config!=BENCHMARK_CONFIG:raise ValueError('Private benchmark deployment changed')
                    owner,kind = peer_owner(pid,uid,config)
                    raw = client.recv(4096)
                    message = json.loads(raw)
                    if message.get('kind','runner') != kind:
                        raise ValueError('Admission kind does not match peer identity')
                    limit=config['work_gib']
                    retain=_benchmark_module().ImageMaintenancePolicy(config) if config.get('role')=='benchmark-builder' else None
                    pool = LeasePool(ledger/'leases.json',config['work_gib'],alive=owner_alive,retain=retain,removal_fence=retain.finalize if retain is not None else None)
                    if config.get('role')=='benchmark-builder':pool.refresh()
                    if message['action']=='acquire':
                        if config.get('role') in ('builder','benchmark-builder') or time.time()-measured_at>=60:
                            try:
                                envelope=observed_envelope(config)
                                measured_at=time.time()
                            except (OSError,ValueError,KeyError,subprocess.SubprocessError):
                                envelope=0
                        limit=min(limit,envelope)
                    # A late stopped create may appear after its lease was
                    # pruned on an earlier empty inventory. Always reconcile
                    # refreshes, even when no dead host owner remains to query.
                    builders_present=builders_running()
                    if message['action'] == 'release':
                        if kind!='builder' or not builders_present:
                            if kind=='runner':pool.release_runner(owner)
                            else:pool.release(owner)
                        accepted = True
                    elif message['action'] == 'acquire':
                        if limit<config[kind+'_gib'] or (kind == 'builder' and (LOCK_ROOT/'oxidex-build-draining').exists()):
                            accepted = False
                        else:
                            pool.limit=limit
                            if config.get('role')=='builder':
                                pool.capacity=builder_capacity_record(config)
                                pool.owner_kind=lambda actor:builder_owner_kind(actor,config)
                            elif config.get('role')=='benchmark-builder':
                                pool.capacity=benchmark_capacity_record(config)
                                def benchmark_owner(actor):
                                    role=builder_owner_kind(actor,config)
                                    # This last independent observation occurs
                                    # under leaseEX directly before the grant.
                                    _benchmark_module().observed_benchmark_envelope(config)
                                    retain.canary_grant.observe(actor)
                                    return role
                                pool.owner_kind=benchmark_owner
                            accepted = (pool.acquire_runner(owner,config[kind+'_gib']) if kind=='runner'
                                        else pool.acquire(owner,config[kind+'_gib']))
                            if accepted and kind=='runner' and not owner_alive(owner):
                                # The Worker may have exited between peer
                                # authentication and the ledger write.
                                pool.release_runner(owner)
                                accepted=False
                    else:
                        raise ValueError('Unknown admission action')
                    answer = dict(accepted=accepted)
                except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as exc:
                    answer = dict(error=str(exc)[:200])
                    if isinstance(exc,OrphanRunnerHook):
                        answer['code']='orphaned-runner-hook'
                        # Old container clients decode only the error string.
                        # The upgraded completed hook recognizes this prefix.
                        answer['error']='ORPHAN_RUNNER_HOOK: '+str(exc)[:180]
                try:
                    client.sendall(json.dumps(answer).encode())
                except OSError:
                    # The disconnected caller can retry; its lease stays in
                    # the ledger until refreshed against actual owner state.
                    pass


if __name__ == '__main__':
    action = sys.argv[1]
    if action=='benchmark-serve':
        if sys.argv[2:]:raise SystemExit('Fixed benchmark scope takes no client paths')
        configure_benchmark();action='serve'
    if action == 'serve':
        serve()
    elif action == 'acquire':
        # A GitHub-assigned job is already busy during its pre-job hook. Let it
        # finish admission even after isolation; drain waits for busy to clear.
        while True:
            try:
                if request('acquire'):
                    break
            except OSError:
                # A root-controlled service restart must not fail an assigned job.
                pass
            time.sleep(5)
    elif action == 'release':
        request('release')
    elif action == 'reconcile-builders':
        if os.geteuid()!=0 or sys.argv[2:]!=['--global-lock-held']:
            raise SystemExit('Root drain requires its held global lock')
        raise SystemExit(75 if builders_running(global_lock_held=True) else 0)
    else:
        raise SystemExit('Unknown admission command')
