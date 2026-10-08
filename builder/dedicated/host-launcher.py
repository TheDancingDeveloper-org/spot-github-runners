#!/usr/bin/env python3
"""Root-owned remote builder launcher: fixed mounts, image, UID and limits."""
import json
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path('/mnt/runner-data/remote-build')
ALLOWED = {'cargo', 'rustc', 'rustfmt', 'clippy-driver', 'just', 'uv', 'python3', 'cbindgen'}


def lock_file(path):
    return open(path,'a',opener=lambda name,flags:os.open(name,flags|os.O_NOFOLLOW,0o600))


def validate_project(project):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', project):
        raise ValueError('Project must be a simple identifier, never a path')
    return project


def container_command(project, command, cpus, memory_gib, image="oxidex-remote-builder:1.97.1", *, root=None):
    validate_project(project)
    if image not in ('oxidex-remote-builder:1.97.1','oxidex-remote-builder:1.99.0'):
        raise ValueError('Unreviewed builder image')
    if not command or command[0] not in ALLOWED:
        raise ValueError('Unsupported build command')
    if cpus < 1 or memory_gib < 1:
        raise ValueError('Resource limits must be positive')
    root=ROOT if root is None else Path(root)
    if root not in (ROOT,Path('/mnt/runner-data/benchmark-builder-278')):
        raise ValueError('Unknown root-owned builder namespace')
    return ['docker', 'run', '--rm', '--user=1001:1001', f'--cpus={cpus}',
            '--label=oxidex.remote-build=true',
            f'--memory={memory_gib}g', f'--memory-swap={memory_gib}g', '--pids-limit=4096',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--entrypoint=/usr/bin/time',
            '-v', f'{root}/sources/{project}:/src',
            '-v', f'{root}/targets/{project}:/target', '-v', f'{root}/cargo:/cargo',
            image, '-v', '/usr/bin/python3',
            '/usr/local/bin/oxidex-build-entrypoint', *command]


def checked_path(path):
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlinked builder paths are forbidden')


def cleanup_source(source):
    """Descriptor-relative source-only cleanup; never follow links or mounts."""
    fd=os.open(source,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        device=os.fstat(fd).st_dev;count=[0]
        def empty(directory,depth):
            if depth>64:raise ValueError('Source cleanup depth bound')
            for name in os.listdir(directory):
                count[0]+=1
                if count[0]>100000:raise ValueError('Source cleanup entry bound')
                info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                if info.st_dev!=device:raise ValueError('Foreign filesystem inside source')
                import stat
                if stat.S_ISDIR(info.st_mode):
                    child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                    try:empty(child,depth+1)
                    finally:os.close(child)
                    os.rmdir(name,dir_fd=directory)
                else:os.unlink(name,dir_fd=directory)
        empty(fd,0);os.fsync(fd)
    finally:os.close(fd)


def source_not_mounted(source,target):
    inventory=subprocess.run(['/usr/bin/docker','ps','-aq','--no-trunc'],check=True,capture_output=True,text=True,timeout=10)
    if inventory.stderr:raise ValueError('Unknown source/cache inventory')
    ids=inventory.stdout.split()
    if len(set(ids))!=len(ids) or any(not re.fullmatch('[0-9a-f]{64}',cid) for cid in ids):raise ValueError('Malformed source/cache inventory')
    if ids:
        result=subprocess.run(['/usr/bin/docker','inspect',*ids],check=True,capture_output=True,text=True,timeout=10)
        rows=json.loads(result.stdout)
        if result.stderr or not isinstance(rows,list) or sorted(row['Id'] for row in rows)!=sorted(ids):raise ValueError('Incomplete source/cache inspection')
        if any(mount.get('Source') in (str(source),str(target)) for row in rows for mount in row['Mounts']):
            raise ValueError('Source/cache still belongs to an actual Docker transaction')


def project_action(root,project,arguments,config,uploader,benchmark,*,protective_fds=()):
    if config.get('role') == 'builder':
        sys.path.insert(0, '/opt/oxidex-builder')
        from qualification_trust.reservation import route_guard
        route_guard(project, argv=arguments)
    def request_gate():
        if benchmark:
            import benchmark_admission
            operation='prepare'if arguments==['prepare']else'cleanup'if arguments==['cleanup']else'execute'
            benchmark_admission.canary_operation(config,project,arguments,operation=operation)
    request_gate()
    if arguments not in (['prepare'],['cleanup']) and config.get('role') in ('builder','benchmark-builder'):
        expected=config.get('approved_image_id')
        if not isinstance(expected,str) or not re.fullmatch('sha256:[0-9a-f]{64}',expected):
            raise ValueError('Ordinary execution requires an approved immutable image')
        result=subprocess.run(['/usr/bin/docker','image','inspect',config['image']],check=True,capture_output=True,text=True,timeout=10)
        rows=json.loads(result.stdout)
        if result.stderr or not isinstance(rows,list) or len(rows)!=1 or rows[0]['Id']!=expected:
            raise ValueError('Current builder tag differs from approved immutable image')
    for path in (root, root/'sources', root/'targets', root/'cargo',root/'sources'/project,root/'targets'/project):
        checked_path(path);request_gate();path.mkdir(parents=True,exist_ok=True)
    if config.get('role')=='builder':
        for path in (root.parent,root,root/'sources'):
            checked_path(path);os.chown(path,0,1001);path.chmod(0o710)
    if arguments==['prepare']:
        source=root/'sources'/project
        request_gate()
        os.chown(source,uploader,1001);source.chmod(0o2770)
        for path in (root/'targets'/project,root/'cargo'):request_gate();os.chown(path,1001,1001)
        print(source);return 0
    if arguments==['cleanup']:
        lock_root=Path('/run/oxidex-benchmark278') if benchmark else Path('/run')
        with lock_file(lock_root/f'oxidex-cache-active-{project}.lock') as active:
            fcntl.flock(active,fcntl.LOCK_EX|fcntl.LOCK_NB)
            source_not_mounted(root/'sources'/project,root/'targets'/project)
            if project.startswith('qualification-'):
                if benchmark:raise ValueError('Qualification requires dedicated builder')
                sys.path.insert(0,'/opt/oxidex-builder')
                from qualification_trust.launcher_contract import cleanup
                from qualification_trust.mounts import qualification_project
                if not qualification_project(project):raise ValueError('Malformed qualification namespace')
                with lock_file(Path('/run')/('oxidex-qualification-'+project+'.lock')) as source_lock:
                    fcntl.flock(source_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    ids=subprocess.check_output(['/usr/bin/docker','ps','-aq','--no-trunc'],text=True,timeout=10).split()
                    if len(set(ids))!=len(ids) or any(not re.fullmatch('[0-9a-f]{64}',x) for x in ids):raise ValueError('Unknown cleanup inventory')
                    rows=json.loads(subprocess.check_output(['/usr/bin/docker','inspect',*ids],text=True,timeout=10)) if ids else []
                    if sorted(row['Id'] for row in rows)!=sorted(ids):raise ValueError('Incomplete cleanup inventory')
                    cleanup(root,project,rows)
            cleanup_source(root/'sources'/project)
        return 0
    if config.get('role')=='builder' and not Path('/var/lib/oxidex-builder-bootstrap.complete').is_file():
        if project!='bootstrap-tools' or arguments!=['python3','/usr/local/bin/oxidex-toolchain-probe']:return 75
    if project.startswith('qualification-'):
        if benchmark:raise ValueError('Qualification gate requires dedicated builder')
        sys.path.insert(0,'/opt/oxidex-builder')
        from qualification_trust.launcher_contract import dispatch
        result=dispatch(root,project,arguments,container_command,config,
                        lambda invocation:subprocess.run(invocation).returncode, protective_fds=protective_fds)
        if isinstance(result,dict):print(json.dumps(result,sort_keys=True));return 0
        return result
    command=arguments or ['cargo','build','--release','--locked','--bin','oxidex']
    invocation=container_command(project,command,config['cpus'],config['memory_gib'],config.get('image','oxidex-remote-builder:1.97.1'),root=root)
    if benchmark:invocation=['/usr/bin/python3','/opt/oxidex-benchmark278/docker-admission.py','--benchmark278',*invocation[1:]]
    request_gate()
    return subprocess.run(invocation).returncode


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Invoke this root-owned launcher with sudo')
    benchmark=sys.argv[1:2]==['--benchmark278']
    arguments=sys.argv[2:] if benchmark else sys.argv[1:]
    if not arguments:raise ValueError('Project required')
    project = validate_project(arguments[0])
    if benchmark:
        sys.path.insert(0,'/opt/oxidex-benchmark278')
        import benchmark_admission
        config,_=benchmark_admission.config_and_receipt()
        root=Path(benchmark_admission.ROOT)
        # Installer creates private root/ancestors. Never repair occupied-cache
        # ancestors or infer uploader identity from the client environment.
        uploader=config['authorized_uploader_uid']
        if type(uploader) is not int or uploader<1000 or uploader==1001:raise ValueError('Root-approved uploader UID required')
        for path in (root,root/'sources',root/'targets'):
            checked_path(path)
            if not path.is_dir() or path.stat().st_uid!=0 or path.stat().st_mode&0o022:
                raise ValueError('Private namespace not provisioned securely')
        cargo=root/'cargo';checked_path(cargo)
        if not cargo.is_dir() or cargo.stat().st_uid not in (0,1001) or cargo.stat().st_mode&0o007:
            raise ValueError('Private Cargo cache not provisioned securely')
        config={**config,'cpus':16,'memory_gib':28}
    else:
        config = json.loads(Path('/etc/oxidex-remote-build.json').read_text());root=ROOT
        uploader=int(os.environ.get('SUDO_UID','1001'))
    lock_root=Path('/run/oxidex-benchmark278') if benchmark else Path('/run')
    with lock_file(lock_root/'oxidex-build.lock') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        if arguments[1:]!=['cleanup'] and (lock_root/'oxidex-build-draining').exists():
            raise SystemExit(75)
        raise SystemExit(project_action(root,project,arguments[1:],config,uploader,benchmark,protective_fds=(lock.fileno(),)))


if __name__ == '__main__':
    main()
