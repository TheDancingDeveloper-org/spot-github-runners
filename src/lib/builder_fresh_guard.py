"""Complete native occupancy and destination checks for fresh bootstrap only.

No services or processes are changed. An unavailable observation holds startup.
This source is embedded in startup before any filesystem/service mutation.
"""
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

RUNNER=re.compile(r'Runner\.(?:Listener|Worker)|actions\.runner\.|gha-(?:runner|admission)|github[-_]runner',re.I)


def validate_runtime_inventory(processes,units):
    if not isinstance(processes,list) or not isinstance(units,list):raise ValueError('Incomplete native inventory')
    seen=set()
    for row in processes:
        if (not isinstance(row,dict) or type(row.get('pid')) is not int or row['pid']<=0 or row['pid'] in seen
            or not isinstance(row.get('exe'),str) or not isinstance(row.get('argv'),list)
            or any(not isinstance(arg,str) for arg in row['argv'])):raise ValueError('Unknown native process identity')
        seen.add(row['pid'])
        if any(RUNNER.search(word) for word in [row['exe'],*row['argv']]):raise ValueError('Native runner occupancy: hold')
    for unit in units:
        if not isinstance(unit,str) or not unit.startswith('Id='):raise ValueError('Unknown service inventory')
        if RUNNER.search(unit):raise ValueError('Existing runner/admission service: hold')


def command(argv):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=20)
    if result.returncode!=0 or result.stderr or len(result.stdout)>8*1024**2:raise ValueError('Incomplete native service observation')
    return result.stdout


def fresh_runtime_guard(proc_root=Path('/proc')):
    processes=[]
    entries=list(Path(proc_root).iterdir())
    for directory in entries:
        if not directory.name.isdigit():continue
        raw=(directory/'cmdline').read_bytes()
        if len(raw)>65536:raise ValueError('Truncated native argv')
        # Kernel threads have no argv or executable. A vanished userspace row
        # or inaccessible descriptor is uncertainty, never empty occupancy.
        if not raw:
            try:os.readlink(directory/'exe')
            except FileNotFoundError:
                if not directory.is_dir():raise ValueError('Native process disappeared during inventory')
                continue
            raise ValueError('Unavailable userspace argv: hold')
        if not raw.endswith(b'\0'):raise ValueError('Incomplete native argv')
        executable=os.readlink(directory/'exe')
        processes.append(dict(pid=int(directory.name),exe=executable,argv=[arg.decode('utf-8','strict') for arg in raw[:-1].split(b'\0')]))
    listing=command(['/usr/bin/systemctl','list-unit-files','--type=service','--no-legend','--no-pager'])
    names=[]
    for line in listing.splitlines():
        fields=line.split()
        if not 2<=len(fields)<=3 or not re.fullmatch(r'(?:[A-Za-z0-9_.@:-]|\\x[0-9A-Fa-f]{2})+\.service',fields[0]):raise ValueError('Incomplete service names')
        names.append(fields[0])
    if not names or len(names)!=len(set(names)) or len(names)>4096:raise ValueError('Incomplete service inventory')
    # Debian13 refuses `show` for uninstantiated template names. Inspect their
    # actual files/drop-ins, then inspect all concrete files AND loaded instances.
    templates=[name for name in names if name.endswith('@.service')]
    units=[]
    for offset in range(0,len(templates),32):
        selected=templates[offset:offset+32]
        text=command(['/usr/bin/systemctl','cat',*selected,'--no-pager'])
        if not text.strip():raise ValueError('Incomplete template service files')
        units.append('Id='+','.join(selected)+'\n'+text)
    loaded=command(['/usr/bin/systemctl','list-units','--all','--type=service','--plain','--no-legend','--no-pager'])
    instances=[]
    for line in loaded.splitlines():
        fields=line.split()
        if (len(fields)<4 or not re.fullmatch(r'(?:[A-Za-z0-9_.@:-]|\\x[0-9A-Fa-f]{2})+\.service',fields[0])
            or fields[0].endswith('@.service')):raise ValueError('Incomplete loaded service instances')
        instances.append(fields[0])
    if not instances or len(instances)!=len(set(instances)) or len(instances)>4096:
        raise ValueError('Incomplete loaded service inventory')
    names=sorted(set(name for name in names if not name.endswith('@.service'))|set(instances))
    for offset in range(0,len(names),32):
        selected=names[offset:offset+32]
        text=command(['/usr/bin/systemctl','show','--all',*selected,'--property=Id,Names,ExecStart,ExecStartPre,ExecStartPost,Description'])
        records=[part.strip() for part in text.strip().split('\n\n') if part.strip()]
        canonical={};covered={name:set() for name in selected}
        for record in records:
            values=dict(line.split('=',1) for line in record.splitlines())
            required={'Id','Names','Description'};allowed=required|{'ExecStart','ExecStartPre','ExecStartPost'}
            if not required<=set(values)<=allowed:raise ValueError('Incomplete service properties')
            # systemd258 omits unset exec arrays even when --all is requested.
            for field in ('ExecStart','ExecStartPre','ExecStartPost'):values.setdefault(field,'')
            # systemctl quotes its Names array and C-escapes unit-name backslashes.
            # Decode only doubled backslashes before an exact systemd hex escape.
            def unit_name(value):
                value=re.sub(r'\\\\(?=x[0-9A-Fa-f]{2})',lambda match:chr(92),value)
                if not re.fullmatch(r'(?:[A-Za-z0-9_.@:-]|\\x[0-9A-Fa-f]{2})+\.service',value):
                    raise ValueError('Incomplete canonical service names')
                return value
            values['Id']=unit_name(values['Id'])
            aliases=[unit_name(name) for name in shlex.split(values['Names'])]
            values['Names']=' '.join(aliases)
            if (values['Id'] not in aliases or len(aliases)!=len(set(aliases))
                or any(not re.fullmatch(r'(?:[A-Za-z0-9_.@:-]|\\x[0-9A-Fa-f]{2})+\.service',name) for name in aliases)):
                raise ValueError('Incomplete canonical service names')
            prior=canonical.get(values['Id'])
            if prior is not None and prior!=values:raise ValueError('Contradictory canonical service properties')
            canonical[values['Id']]=values
            for name in set(selected)&set(aliases):covered[name].add(values['Id'])
            units.append('Id='+values['Id']+'\n'+record)
        if any(len(matches)!=1 for matches in covered.values()):raise ValueError('Incomplete/ambiguous service alias coverage')
    validate_runtime_inventory(processes,units)


def runtime_destination_guard(root):
    root=Path(root)
    if root.is_symlink():raise ValueError('Symlinked runtime destination')
    for name in ('docker','containerd'):
        path=root/name
        try:info=path.lstat()
        except FileNotFoundError:continue
        if not stat.S_ISDIR(info.st_mode) or any(path.iterdir()):raise ValueError('Occupied destination runtime metadata: hold')


def classic_storage_guard(info,expected_root=None):
    if (not isinstance(info,dict) or info.get('Driver')!='overlay2'
        or not isinstance(info.get('DockerRootDir'),str) or not info['DockerRootDir'].startswith('/')
        or (expected_root is not None and info['DockerRootDir']!=expected_root)
        or not isinstance(info.get('DriverStatus'),list) or not info['DriverStatus']
        or any(not isinstance(row,list) or len(row)!=2 or any(not isinstance(value,str) for value in row) for row in info['DriverStatus'])
        or any('containerd.snapshotter' in value for row in info['DriverStatus'] for value in row)):
        raise ValueError('Actual Docker store is not classic-build compatible')
