"""Snapshot a local checkout, transfer it over authenticated SSH, and time builds."""
import argparse
import hashlib
import json
import secrets
import shlex
import subprocess
import tarfile
import time
from pathlib import Path


def retryable_exit(code):
    return code in (75, 137, 143, 255)


def stopped_builder_reconciliation_command():
    """Embed current reconciliation for legacy hosts under the transaction SH lock."""
    from . import host_admission
    source=Path(host_admission.__file__).read_text()
    program=("namespace={'__name__':'oxidex_transaction_reconcile'}\n"
             "exec("+repr(source)+",namespace)\n"
             "try:\n"
             " namespace['reconcile_stopped_builders']()\n"
             "except (OSError,ValueError,KeyError,TypeError,namespace['subprocess'].SubprocessError):\n"
             " raise SystemExit(75)\n")
    return '/usr/bin/python3 -I -c '+shlex.quote(program)


def transaction_script(project,upload,digest,profile,run,instance_id=None):
    """Serialize source replacement, builds, and immutable artifact export."""
    root='/mnt/runner-data/remote-build'
    source=f'{root}/sources/{project}'
    target=f'{root}/targets/{project}'
    export=f'{root}/exports/{run}/oxidex'
    launcher=f'/usr/local/bin/oxidex-remote-build {shlex.quote(project)}'
    stages=[f'{launcher} cargo fetch --locked --target x86_64-unknown-linux-gnu']
    if profile=='release':stages.append(f'{launcher} just cbindgen-check')
    stages.append(f'{launcher} cargo build '+('--release ' if profile=='release' else '')+'--locked --bin oxidex')
    binary=target+'/'+profile+'/oxidex'
    stages.append(f'{launcher} cargo run '+('--release ' if profile=='release' else '')+'--locked --bin oxidex -- --readonly --version')
    body='\n'.join([
        'set -eu',
        *([identity_guard(instance_id)] if instance_id else []),
        'trap '+shlex.quote('rm -f -- '+shlex.quote(upload))+' EXIT',
        'test ! -e /run/oxidex-build-draining || exit 75',
        # A stopped owned orphan must not veto the transaction before the
        # launcher has an opportunity to reconcile it. The root reconciler
        # takes each active-cache lock nonblocking and never removes running
        # or unknown identities; the fresh inventory below still rejects them.
        stopped_builder_reconciliation_command(),
        # Keep ownership across source replacement; a direct launcher does not
        # acquire the outer project lock and must not see a half-extracted tree.
        f'exec 9>/run/oxidex-cache-active-{project}.lock',
        'flock -n -E 75 9',
        'ids=$(/usr/bin/docker ps -aq --filter label=oxidex.remote-build=true) || exit 75',
        'for id in $ids; do',
        "  mounts=$(/usr/bin/docker inspect --format '{{range .Mounts}}{{if eq .Destination \"/target\"}}{{.Source}}{{end}}{{end}}' \"$id\") || exit 75",
        f'  test "$mounts" != {shlex.quote(target)} || exit 75',
        'done',
        f'{launcher} prepare',
        f'echo {shlex.quote(digest+"  "+upload)} | sha256sum -c -',
        # The validated source is replaceable; Cargo and target caches are separate.
        f'find {shlex.quote(source)} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +',
        f'tar -xzf {shlex.quote(upload)} -C {shlex.quote(source)}',
        # The adapter takes this same lock for each build. Close our descriptor
        # before launching it; source replacement is now complete.
        'exec 9>&-',
        *stages,
        # Build output is untrusted executable code; verification runs inside
        # the same unprivileged container, never through the root SSH shell.
        f'test -f {shlex.quote(binary)} && test ! -L {shlex.quote(binary)}',
        f'test "$(readlink -f -- {shlex.quote(binary)})" = {shlex.quote(binary)}',
        f'install -d -m 0755 {shlex.quote(str(Path(export).parent))}',
        f'install -m 0755 {shlex.quote(binary)} {shlex.quote(export)}',
        f'sha256sum {shlex.quote(export)}',
    ])
    return ('sudo flock --shared /run/oxidex-build.lock flock --exclusive '
            f'/run/oxidex-project-{project}.lock sh -c '+shlex.quote(body)),export


def identity_guard(instance_id):
    import re
    if not re.fullmatch('[0-9]+',str(instance_id)):
        raise ValueError('A provider-observed VM identity is required')
    return ('test "$(curl --fail --silent --show-error --max-time 10 '
            '-H Metadata-Flavor:Google '
            'http://metadata.google.internal/computeMetadata/v1/instance/id)" = '
            +shlex.quote(str(instance_id))+' || exit 75')


def cleanup_script(project,upload,run,instance_id):
    """Remove only this run's upload and root-owned canonical export tree."""
    import re
    if (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',project)
            or not re.fullmatch(r'run-[0-9a-f]{32}',run)
            or upload!='oxidex-remote-source-'+run+'.tar.gz'):
        raise ValueError('Invalid remote cleanup identity')
    export='/mnt/runner-data/remote-build/exports/'+run
    code='\n'.join([
        'import os, shutil, stat',
        'from pathlib import Path',
        'export=Path('+repr(export)+')',
        # Every ancestor is checked so replacing a parent cannot redirect root
        # cleanup into a writable tree. The run name was generated locally.
        'for path in (export,*export.parents):',
        '    if path.is_symlink(): raise RuntimeError("Symlinked export path")',
        '    if not path.exists(): continue',
        '    info=path.lstat()',
        '    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:',
        '        raise RuntimeError("Export cleanup requires root-owned private directories")',
        'if export.exists():',
        '    if export.resolve()!=export: raise RuntimeError("Noncanonical export path")',
        '    shutil.rmtree(export)',
    ])
    body='set -eu; '+identity_guard(instance_id)+'; rm -f -- '+shlex.quote(upload)+'; /usr/bin/python3 -I -c '+shlex.quote(code)
    # Serialize with a transaction that may still be finishing after SSH loss.
    return ('sudo flock --shared /run/oxidex-build.lock flock --exclusive '
            f'/run/oxidex-project-{project}.lock sh -c '+shlex.quote(body))


def make_snapshot(source: Path, archive: Path) -> dict:
    names = subprocess.check_output(['git','-C',str(source),'ls-files','-z']).decode().split('\0')
    files=[]
    with tarfile.open(archive,'w:gz',compresslevel=3) as tar:
        for name in names:
            parts=Path(name).parts
            if not name or any(p in {'.git','.codex','.claude','.agents','target','node_modules','.venv'} for p in parts):
                continue
            path=source/name
            if path.name.startswith('.env') or path.suffix in {'.pem','.key'} or not path.is_file() or path.is_symlink():
                continue
            files.append({'path':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size})
            tar.add(path,arcname=name,recursive=False)
    return {'files':files,'file_count':len(files),'archive_bytes':archive.stat().st_size,
            'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--instance',required=True)
    parser.add_argument('--zone',required=True)
    parser.add_argument('--project',default='homelab-424523')
    parser.add_argument('--worktree-id',required=True)
    parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--profile',choices=['debug','release'],default='release')
    parser.add_argument('--artifact-dir',type=Path)
    args=parser.parse_args(argv)
    import re
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',args.worktree_id):
        raise ValueError('Invalid worktree identifier')
    source=args.source.expanduser().resolve();evidence=args.evidence_dir.expanduser().resolve()
    evidence.mkdir(parents=True,exist_ok=True)
    receipt={'profile':args.profile,'source':str(source),'instance':args.instance,'zone':args.zone,'project':args.project}
    def save():
        (evidence/'remote-build.json').write_text(json.dumps(receipt,indent=2))
    def ssh(command):
        return ['gcloud','compute','ssh',args.instance,'--zone='+args.zone,'--project='+args.project,
                '--quiet','--command='+command]
    upload=None;instance_id=None;run=None
    try:
        instance_id=subprocess.check_output(['gcloud','compute','instances','describe',args.instance,
            '--zone='+args.zone,'--project='+args.project,'--format=value(id)'],text=True).strip()
        identity_guard(instance_id);receipt['instance_id']=instance_id
        start=time.monotonic();archive=evidence/'remote-source.tar.gz'
        receipt['snapshot']=make_snapshot(source,archive)
        receipt['packaging_seconds']=time.monotonic()-start
        receipt['source_commit']=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
        receipt['source_status']=subprocess.check_output(['git','-C',str(source),'status','--porcelain'],text=True)
        save()
        start=time.monotonic()
        run='run-'+secrets.token_hex(16)
        upload='oxidex-remote-source-'+run+'.tar.gz'
        subprocess.run(['gcloud','compute','scp',str(archive),args.instance+':~/'+upload,
                        '--zone='+args.zone,'--project='+args.project,'--quiet'],check=True)
        digest=receipt['snapshot']['archive_sha256']
        receipt['upload_seconds']=time.monotonic()-start;receipt['run_id']=run;save()
        command,binary=transaction_script(args.worktree_id,upload,digest,args.profile,run,instance_id)
        start=time.monotonic();log_path=evidence/'transaction.log'
        with log_path.open('w') as log:
            result=subprocess.run(ssh(command),stdout=log,stderr=subprocess.STDOUT)
        receipt['transaction_seconds']=time.monotonic()-start
        receipt['transaction_exit_code']=result.returncode
        receipt['retryable']=retryable_exit(result.returncode);save()
        if result.returncode:
            raise RuntimeError(f'Remote transaction failed; see {log_path}')
        with log_path.open() as log:
            last_line=''
            for line in log:last_line=line.strip()
        digest=last_line.split()[0] if last_line else ''
        if not re.fullmatch('[0-9a-f]{64}',digest):
            raise RuntimeError('Remote artifact checksum was missing')
        receipt['binary_sha256']=digest
        if args.artifact_dir:
            output=args.artifact_dir.expanduser().resolve();output.mkdir(parents=True,exist_ok=True)
            artifact=output/'oxidex'
            subprocess.run(['gcloud','compute','scp',args.instance+':'+binary,str(artifact),
                            '--zone='+args.zone,'--project='+args.project,'--quiet','--scp-flag=-C'],check=True)
            if hashlib.sha256(artifact.read_bytes()).hexdigest()!=digest:
                raise RuntimeError('Downloaded binary checksum mismatch')
            receipt['artifact']=str(artifact)

        receipt['verified']=True;save()
    except Exception as exc:
        receipt['error']=str(exc)
        if isinstance(exc,subprocess.CalledProcessError):
            receipt['retryable']=retryable_exit(exc.returncode)
        save();raise
    finally:
        if upload and instance_id:
            # Covers partial SCP and SSH failures too, including receipt-only
            # builds. A reused VM name never authorizes upload/export deletion.
            try:
                cleanup=cleanup_script(args.worktree_id,upload,run,instance_id)
                result=subprocess.run(ssh(cleanup),capture_output=True,text=True,timeout=45)
                receipt['upload_cleanup_confirmed']=result.returncode==0
                receipt['export_cleanup_confirmed']=result.returncode==0
            except (OSError,subprocess.SubprocessError):
                receipt['upload_cleanup_confirmed']=False
                receipt['export_cleanup_confirmed']=False
            save()
    print(json.dumps({k:v for k,v in receipt.items() if k!='snapshot'},indent=2))
    return 0
