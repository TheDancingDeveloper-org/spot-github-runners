"""Fresh builder-only startup renderer. No GitHub credentials or registration."""
import base64
import gzip
import hashlib
import json
import re
from pathlib import Path
from .builder_only_assets import ASSETS, STARTUP
from .builder_access import canonical_sha,approved_mapping

IMAGE='oxidex-remote-builder:1.99.0'
COMMIT='b940084d7eb6a299eb4bfeb8e34901bc051e7ac4'
MARKER='/var/lib/oxidex-builder-bootstrap.complete'
READINESS='/var/lib/oxidex-builder-readiness.json'


def builder_guest_sources():
    trust={f'/opt/oxidex-builder/qualification_trust/{path.name}':path.read_text()
           for path in Path(__file__).with_name('qualification_trust').glob('*.py')}
    return {**ASSETS,**trust,'/opt/gha-runners/host_admission.py':Path(__file__).with_name('host_admission.py').read_text(),
            '/opt/gha-runners/builder_access.py':Path(__file__).with_name('builder_access.py').read_text()}


def render_builder_startup(args,deployment_id):
    uploader=approved_mapping(getattr(args,'authorized_uploader',None))
    uploader_sha=canonical_sha(uploader)
    if getattr(args,'authorized_uploader_sha256',None)!=uploader_sha:
        raise ValueError('Provider startup requires root-bound uploader mapping digest')
    expected=dict(role='builder',cpu=16,ram=32,runner_count=0,builder_cpus=16,builder_memory_gib=28,
                  disk_size_gb=100,disk_type='hyperdisk-balanced')
    for key,value in expected.items():
        actual=getattr(args,key,None)
        if (type(actual) not in (int,float) if type(value) is int else type(actual) is not str) or actual!=value:raise ValueError('Builder role/shape mismatch: '+key)
    performance=(getattr(args,'data_disk_iops',None),getattr(args,'data_disk_throughput',None))
    if any(type(v)is not int for v in performance) or performance not in ((3000,140),(4000,250)):
        raise ValueError('Builder cache performance must match reviewed baseline or fast pair')
    for key,value in (('boot_disk_size_gb',40),('boot_disk_iops',3000),('boot_disk_throughput',140)):
        actual=getattr(args,key,value)
        if type(actual) is not int or actual!=value:raise ValueError('Builder boot disk mismatch')
    replacements={}
    for key in ('instance_name','project','zone','disk_device_name'):
        actual=getattr(args,key,None)
        if not isinstance(actual,str) or not re.fullmatch('[a-z][a-z0-9-]{0,62}',actual):raise ValueError('Unsafe builder identity: '+key)
        replacements[key.upper()]=actual
    if not args.instance_name.startswith('builder-'):raise ValueError('Dedicated builder names require builder- prefix')
    if not isinstance(deployment_id,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,127}',deployment_id):raise ValueError('Unsafe deployment identity')
    replacements['FRESH_GUARD_SOURCE']=Path(__file__).with_name('builder_fresh_guard.py').read_text()
    replacements['ACCESS_GUARD_SOURCE']=Path(__file__).with_name('builder_access.py').read_text().split("if __name__=='__main__':",1)[0]
    replacements['DEPLOYMENT_ID']=deployment_id
    replacements['UPLOADER_CONFIG']=json.dumps(uploader,separators=(',',':'),sort_keys=True)
    replacements['UPLOADER_SHA']=uploader_sha
    replacements['UPLOADER_USERNAME']=uploader['username']
    config=dict(role='builder',cpus=16,memory_gib=28,image=IMAGE,approved_image_id=None,
                reservation_enrollment=dict(schema=2,enabled=False,uuid=None,by_id=None,serial=None))
    admission=dict(role='builder',host_cpus=16,builder_cpus=16,work_gib=28,runner_gib=0,builder_gib=28,runner_names=[])
    replacements['BUILD_CONFIG']=json.dumps(config,separators=(',',':'),sort_keys=True)
    replacements['ADMISSION_CONFIG']=json.dumps(admission,separators=(',',':'),sort_keys=True)
    sources=builder_guest_sources()
    # The complete current admission + qualification sources exceed GCP's
    # 256 KiB metadata limit when only base64 encoded. Compress deterministic
    # transport bytes; installed bytes and readiness hashes remain canonical.
    decoder = "python3 -c 'import gzip,sys;sys.stdout.buffer.write(gzip.decompress(sys.stdin.buffer.read()))'"
    replacements['ASSET_WRITES']='\n'.join("install -d -o root -g root -m 0755 "+str(Path(path).parent)+"\nprintf '%s' '"+base64.b64encode(gzip.compress(source.encode(),mtime=0)).decode()+"' | base64 -d | "+decoder+" >"+path for path,source in sources.items())
    hashes={path:hashlib.sha256(source.encode()).hexdigest() for path,source in sources.items()}
    replacements['READINESS_SOURCE']=readiness_source(deployment_id,hashes,args.disk_device_name)
    result=STARTUP
    for key,value in replacements.items():result=result.replace('{{'+key+'}}',value)
    # All renderer tokens must be consumed before issuing startup metadata.
    if re.search(r'{{[A-Z_]+}}',result):raise ValueError('Unrendered builder startup token')
    return result


def readiness_source(deployment_id,hashes,disk_device_name='builder-cache'):
    return '''import hashlib,json,os,re,stat,subprocess,time
from pathlib import Path
import runpy
module=runpy.run_path('/opt/gha-runners/host_admission.py',run_name='builder_readiness')
for executable in ('/usr/bin/git','/usr/bin/ssh-keygen','/usr/bin/python3',
                   '/usr/bin/systemd-run','/usr/bin/sudo','/usr/sbin/sshd','/usr/sbin/visudo'):
    if not os.access(executable,os.X_OK):raise SystemExit('Host verifier dependency unavailable')
subprocess.run(['/usr/bin/git','--version'],check=True,timeout=10,stdout=subprocess.DEVNULL)
expected_hashes='''+repr(hashes)+'''
for path,expected in expected_hashes.items():
    if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected:raise SystemExit('Installed builder source differs')
config=json.loads(Path('/etc/gha-admission.json').read_text())
module['builder_capacity_record'](config)
if os.cpu_count()!=16 or module['observed_builder_envelope'](config)!=28:raise SystemExit('Actual builder memory/CPU cannot fit')
if subprocess.check_output(['/usr/bin/docker','ps','-aq','--no-trunc'],text=True,timeout=10).strip():raise SystemExit('Builder probe cleanup incomplete')
if json.loads(Path('/var/lib/gha-admission/leases.json').read_text())!={}:raise SystemExit('Builder probe lease cleanup incomplete')
image=json.loads(subprocess.check_output(['/usr/bin/docker','image','inspect','oxidex-remote-builder:1.99.0'],text=True,timeout=10))[0]['Id']
if image!=Path('/var/lib/oxidex-builder/image-id').read_text().strip():raise SystemExit('Built image differs')
created=json.loads(Path('/var/lib/oxidex-builder/last-created-container.json').read_text())
if created['image_id']!=image or created['labels']['oxidex.cache-project']!='bootstrap-tools':raise SystemExit('Probe image/identity differs')
lines=Path('/var/lib/oxidex-builder/tool-probe-output').read_text().splitlines()
tools=json.loads(lines[-1])
hostname=tools['container_hostname']
if not re.fullmatch('[0-9a-f]{12}',hostname) or not created['container_id'].startswith(hostname):raise SystemExit('Actual probe container differs from created receipt')
boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
if tools['boot_id']!=boot_id or not created['observed_at_ns']<=tools['observed_at_ns']<=time.time_ns():raise SystemExit('Probe boot/chronology differs')
if tools['schema']!='builder-tools-actual-v1' or 'commit-hash: '''+COMMIT+'''' not in tools['rustc'].splitlines():raise SystemExit('Actual compiler differs')
expected_caps={'memory.max':str(28*1024**3),'memory.swap.max':'0'}
if any(tools['actual_cgroup'].get(k)!=v for k,v in expected_caps.items()):raise SystemExit('Actual memory/swap differs')
quota,period=tools['actual_cgroup']['cpu.max'].split()
if int(quota)!=16*int(period):raise SystemExit('Actual CPU cap differs')
memory={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
device=Path('/dev/disk/by-id/google-''' + disk_device_name + '''')
actual_device=device.resolve(strict=True);info=actual_device.stat()
if not stat.S_ISBLK(info.st_mode):raise SystemExit('Cache device is not block storage')
uuid=subprocess.check_output(['/usr/sbin/blkid','-s','UUID','-o','value',str(device)],text=True,timeout=10).strip()
mounted_uuid=subprocess.check_output(['/usr/bin/findmnt','-n','-o','UUID','/mnt/runner-data'],text=True,timeout=10).strip()
serial=subprocess.check_output(['/usr/bin/lsblk','-dn','-o','SERIAL',str(device)],text=True,timeout=10).strip()
if not uuid or uuid!=mounted_uuid or not serial or len(serial)>256:raise SystemExit('Actual persistent cache device differs')
storage=dict(device_name=''' + repr(disk_device_name) + ''',realpath=str(actual_device),major=os.major(info.st_rdev),minor=os.minor(info.st_rdev),
    uuid=uuid,serial=serial,mount_fs_device=Path('/mnt/runner-data').stat().st_dev)
record=dict(schema='builder-bootstrap-observed-v1',deployment_id='''+repr(deployment_id)+''',observed_at_ns=time.time_ns(),
    boot_id=boot_id,image_id=image,tool_evidence=tools,
    created_container_evidence=created,installed_source_sha256=expected_hashes,admission_config=config,
    actual_memory_bytes=memory,actual_storage=storage,workload_qualified=False,cpu_host_services_share=True)
path=Path('/var/lib/oxidex-builder-readiness.json')
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'w') as target:
    json.dump(record,target,sort_keys=True);target.flush();os.fsync(target.fileno())
'''
