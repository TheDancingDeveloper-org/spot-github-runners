"""Standalone root sampler; no credentials, job payloads or journal text leave the host."""
import hashlib
import json
import re
import subprocess
import time
import math
from pathlib import Path


def validate_io_pressure(values):
    keys={f'{scope}_{name}' for scope in ('some','full') for name in ('avg10','avg60','avg300','total')}
    if not isinstance(values,dict) or set(values)!=keys:
        raise ValueError('Incomplete IO pressure evidence')
    for key,value in values.items():
        if isinstance(value,bool) or not isinstance(value,(int,float)):
            raise ValueError('Invalid IO pressure number')
        if key.endswith('_total'):
            if not isinstance(value,int) or not 0<=value<2**64:
                raise ValueError('Invalid IO pressure counter')
        elif not 0<=value<=100 or not math.isfinite(value):
            raise ValueError('IO pressure percentage outside bounds')
    return values


def parse_io_pressure(text):
    """Parse the two kernel PSI lines, bounded independently of OOM telemetry."""
    if not isinstance(text,str) or len(text)>1024:
        raise ValueError('IO pressure sample exceeds bounded size')
    values={};scopes=set()
    for line in text.splitlines():
        parts=line.split()
        if len(parts)!=5 or parts[0] not in ('some','full') or parts[0] in scopes:
            raise ValueError('Malformed IO pressure line')
        scope=parts[0];scopes.add(scope)
        for item in parts[1:]:
            key,separator,value=item.partition('=')
            name=scope+'_'+key
            if not separator or name in values:
                raise ValueError('Malformed IO pressure field')
            values[name]=int(value) if key=='total' else float(value)
    return validate_io_pressure(values)


def workload_inventory(records,runner_names):
    """Map only exact inspected Docker IDs to configured runners/builders."""
    if (not isinstance(records,list) or not isinstance(runner_names,list)
        or any(not isinstance(name,str) for name in runner_names) or len(set(runner_names))!=len(runner_names)):
        raise ValueError('Invalid Docker workload inventory')
    mapping={};seen=set()
    for row in records:
        if not isinstance(row,dict) or not isinstance(row.get('Config'),dict):
            raise ValueError('Incomplete Docker workload record')
        identity=row.get('Id','');name=row.get('Name','')
        labels=row.get('Config',{}).get('Labels') or {}
        if (not isinstance(identity,str) or not re.fullmatch(r'[0-9a-f]{64}',identity)
            or identity in seen or not isinstance(name,str) or not isinstance(labels,dict)):
            raise ValueError('Incomplete Docker workload identity')
        seen.add(identity)
        runner=name.startswith('/') and name[1:] in runner_names
        builder=labels.get('oxidex.remote-build')=='true'
        if runner and not builder:mapping[identity]='runner'
        elif builder and not runner:mapping[identity]='builder'
    return mapping


def inspected_workloads(instance_name=None):
    config_path=Path('/etc/gha-admission.json')
    try:config=json.loads(config_path.read_text())
    except FileNotFoundError:
        # A broken existing config/symlink is not a legacy absence proof.
        try:config_path.lstat()
        except FileNotFoundError:pass
        else:raise ValueError('Existing admission configuration is unreadable')
        if (not isinstance(instance_name,str)
            or not re.fullmatch(r'[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?',instance_name)):
            raise ValueError('Legacy inventory requires an exact provider VM name')
        config={'runner_names':[f'gha-{instance_name}-{index}' for index in range(1,101)]}
    if not isinstance(config,dict):raise ValueError('Invalid admission configuration')
    runner_names=config['runner_names']
    workload_inventory([],runner_names)
    result=subprocess.run(['/usr/bin/docker','ps','-aq','--no-trunc'],check=True,
                          capture_output=True,text=True,timeout=10)
    ids=result.stdout.split()
    if len(ids)>500 or len(set(ids))!=len(ids) or any(not re.fullmatch(r'[0-9a-f]{64}',value) for value in ids):
        raise ValueError('Incomplete Docker workload inventory')
    if not ids:return {}
    result=subprocess.run(['/usr/bin/docker','inspect',*ids],check=True,
                          capture_output=True,text=True,timeout=10)
    if len(result.stdout)>4*1024*1024:raise ValueError('Docker workload evidence exceeds bound')
    records=json.loads(result.stdout)
    if (not isinstance(records,list) or len(records)!=len(ids)
        or any(not isinstance(row,dict) for row in records) or {row.get('Id') for row in records}!=set(ids)):
        raise ValueError('Docker workload identities changed')
    return workload_inventory(records,runner_names)


def classify_events(journal, docker, boot_id, timestamp, *, window_start=None, workloads=None):
    events = []
    workloads=workloads or {}
    window_start=timestamp-1800 if window_start is None else window_start
    # PID plus task must identify one decision in the queried boot/window.
    # Never correlate by timestamp alone or guess when PIDs were reused.
    decisions={}
    for row in journal:
        message=row.get('MESSAGE','')
        if not isinstance(message,str):continue
        decision=re.search(r'\boom-kill:\s*constraint=(CONSTRAINT_[A-Z_]+)\b',message,re.I)
        peer=re.search(r'\btask=([^,]+),pid=(\d+),',message)
        if not decision or not peer:continue
        try:at=int(row['__REALTIME_TIMESTAMP'])/1e6
        except (KeyError,ValueError,TypeError):continue
        if not window_start<=at<=timestamp:continue
        identity=None
        constrained=re.search(r'\boom_memcg=([^,]+)',message)
        task=re.search(r'\btask_memcg=([^,]+)',message)
        if (decision[1].upper()=='CONSTRAINT_MEMCG' and constrained and task
            and row.get('_BOOT_ID')==boot_id.replace('-','')):
            ids=[re.findall(r'(?:^|/)docker[-/]([0-9a-f]{64})(?:\.scope)?(?:/|$)',match[1]) for match in (constrained,task)]
            if len(ids[0])==len(ids[1])==1 and ids[0]==ids[1]:identity=ids[0][0]
        decisions.setdefault((peer[2],peer[1]),[]).append((at,decision[1].upper(),identity))
    for row in journal:
        message = row.get('MESSAGE','')
        if not isinstance(message,str):
            continue
        # "invoked oom-killer" starts the diagnostic dump for either a global
        # or a cgroup OOM. Only the actual decision/kill identifies its scope.
        decision=re.search(r'\boom-kill:\s*constraint=(CONSTRAINT_[A-Z_]+)\b',message,re.I)
        if decision:
            kind={'CONSTRAINT_NONE':'host','CONSTRAINT_MEMCG':'container'}.get(decision[1].upper(),'unknown')
        elif re.search(r'\bMemory cgroup out of memory:\s*Killed process\b',message,re.I):
            kind='container'
        elif re.search(r'\bOut of memory:\s*Killed process\b',message,re.I):
            kind='host'
        else:
            continue
        try:
            observed = int(row['__REALTIME_TIMESTAMP']) / 1e6
        except (KeyError,ValueError,TypeError):
            continue
        if not window_start <= observed <= timestamp:
            continue
        event_id = hashlib.sha256(f'{boot_id}:{observed}:{message}'.encode()).hexdigest()
        workload='unknown'
        peer=re.search(r'\btask=([^,]+),pid=(\d+),',message) if decision else None
        kill=re.search(r'\bKilled process (\d+) \(([^)]+)\)',message)
        key=(peer[2],peer[1]) if peer else (kill[1],kill[2]) if kill else None
        candidates=decisions.get(key,[])
        if decision:candidates=[candidate for candidate in candidates if candidate[0]==observed]
        if kind=='container' and len(candidates)==1 and row.get('_BOOT_ID')==boot_id.replace('-',''):
            at,constraint,identity=candidates[0]
            if constraint=='CONSTRAINT_MEMCG' and at<=observed:
                workload=workloads.get(identity,'unknown')
                if workload not in ('runner','builder'):workload='unknown'
        events.append(dict(id=event_id,kind=kind,workload=workload,at=observed))
    for row in docker:
        if row.get('Action') != 'oom':
            continue
        try:
            observed = int(row['timeNano']) / 1e9
        except (KeyError,ValueError,TypeError):
            continue
        if window_start <= observed <= timestamp:
            event_id = hashlib.sha256(f"{boot_id}:{observed}:{row.get('id')}:docker-oom".encode()).hexdigest()
            # Docker's current event schema puts the full ID in Actor.ID.
            # Keep the legacy event hash above stable, but prove role from
            # the same inspected inventory as the kernel cgroup attribution.
            actor=row.get('Actor',{})
            identity=actor.get('ID') if isinstance(actor,dict) else None
            workload=workloads.get(identity or row.get('id'),'unknown')
            if workload not in ('runner','builder'):workload='unknown'
            events.append(dict(id=event_id,kind='container',workload=workload,at=observed))
    return sorted(events,key=lambda row:row['at'])


def command_json_lines(command, *, empty_grep=False):
    result=subprocess.run(command,check=not empty_grep,capture_output=True,text=True,timeout=30)
    if empty_grep:
        if result.returncode==1 and not result.stdout and not result.stderr:
            return []  # journalctl --grep uses 1 for a confirmed empty match.
        if result.returncode!=0:
            raise subprocess.CalledProcessError(result.returncode,command,output=result.stdout,stderr=result.stderr)
        if result.stderr:
            raise RuntimeError('Boot OOM journal query emitted diagnostics')
    raw=result.stdout
    if len(raw) > 4*1024*1024:
        raise RuntimeError('Host evidence exceeds bounded window')
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def sample(attribution_ids=(),instance_name=None):
    if (not isinstance(attribution_ids,(list,tuple)) or len(attribution_ids)>1000
        or any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value) for value in attribution_ids)):
        raise ValueError('Invalid pending OOM attribution IDs')
    timestamp = time.time()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    journal = command_json_lines(['journalctl','-k','-b','0','--since',f'@{int(timestamp-1800)}',
                                  '--no-pager','-o','json','-n','2000'])
    docker = command_json_lines(['docker','events','--since',str(int(timestamp-1800)),
                                 '--until',str(int(timestamp)), '--filter','event=oom','--format','{{json .}}'])
    # Legacy controller state stores only a pending kind. A complete current-
    # boot decision history distinguishes false context from older real globals.
    globals_journal=command_json_lines(['journalctl','-k','-b','0','--no-pager','--quiet','-o','json','-n','2000',
        '--grep=^(oom-kill:constraint=CONSTRAINT_NONE|Out of memory: Killed process)'],empty_grep=True)
    global_events=classify_events(globals_journal,[],boot,timestamp,window_start=0)
    # Missing/changed inventory preserves unattributed events as vetoes.
    try:workloads=inspected_workloads(instance_name)
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):workloads={}
    historical=[];historical_complete=False
    if attribution_ids:
        history=command_json_lines(['journalctl','-k','-b','0','--no-pager','--quiet','-o','json','-n','2000',
            '--grep=^(oom-kill:|Memory cgroup out of memory: Killed process|Out of memory: Killed process)'],empty_grep=True)
        classified=classify_events(history,[],boot,timestamp,window_start=0,workloads=workloads)
        historical_complete=(len(history)<2000 and len(classified)==len(history)
            and all(row.get('_BOOT_ID')==boot.replace('-','') for row in history))
        if historical_complete:
            historical=[event for event in classified if event['id'] in attribution_ids
                        and event['kind']=='container' and event['workload'] in ('runner','builder')]
    memory = {line.split(':')[0]:int(line.split()[1])/1024/1024
              for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
    try:
        io_pressure=parse_io_pressure(Path('/proc/pressure/io').read_text())
    except (OSError,ValueError):
        io_pressure=None  # Kernels without PSI still provide valid OOM evidence.
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=boot:
        raise RuntimeError('Host boot changed during evidence collection')
    return dict(observed_at=timestamp,boot_id=boot,ram_gib=memory['MemTotal'],
                used_ram_gib=memory['MemTotal']-memory['MemAvailable'],
                io_pressure=io_pressure,io_complete=io_pressure is not None,
                events=classify_events(journal,docker,boot,timestamp,workloads=workloads),complete=len(journal)<2000,
                reclassified_events=historical,reclassification_complete=historical_complete,
                boot_oom_complete=(len(globals_journal)<2000 and len(global_events)==len(globals_journal)
                                   and all(event['kind']=='host' for event in global_events)),
                boot_host_oom_ids=sorted(event['id'] for event in global_events if event['kind']=='host'))


if __name__ == '__main__':
    print(json.dumps(sample(globals().get('OOM_ATTRIBUTION_IDS',()),globals().get('HOST_INSTANCE_NAME'))))
