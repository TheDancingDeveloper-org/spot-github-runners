import tempfile
import unittest
import subprocess
import tarfile
import hashlib
import shlex
import json
import shutil
from unittest.mock import patch
from pathlib import Path
from lib.remote_build import make_snapshot


class ClientTests(unittest.TestCase):
    def test_snapshot_uses_working_files_without_caches_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'src';root.mkdir()
            subprocess.run(['git','init','-q',str(root)],check=True)
            for name, data in [('Cargo.toml','source'),('.env','secret'),('target/build','cache'),('.codex/config.toml','private')]:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(data)
            subprocess.run(['git','-C',str(root),'add','.'],check=True)
            (root/'Cargo.toml').write_text('working change')
            archive=Path(directory)/'source.tar.gz'
            receipt=make_snapshot(root,archive)
            with tarfile.open(archive) as tar:
                self.assertEqual(tar.getnames(), ['Cargo.toml'])
                self.assertEqual(tar.extractfile('Cargo.toml').read(), b'working change')
            self.assertEqual(receipt['file_count'],1)

    def execute_transaction(self,profile,header_failure=False,artifact_symlink=False,stopped_orphan=False,probe_source_lock=False):
        from lib.remote_build import transaction_script
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'remote/sources/checkout';source.mkdir(parents=True)
            target=root/'remote/targets/checkout'/profile;target.mkdir(parents=True)
            (source/'deleted.rs').write_text('stale source')
            (target/'retained.cache').write_text('retained')
            new=root/'new.rs';new.write_text('new snapshot')
            archive=root/'upload.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:tar.add(new,arcname='new.rs')
            digest=hashlib.sha256(archive.read_bytes()).hexdigest()
            command,export=transaction_script('checkout',str(archive),digest,profile,'run-test')
            body=shlex.split(command)[-1].replace('/mnt/runner-data/remote-build',str(root/'remote'))
            body=body.replace('/run/oxidex-',str(root/'lock-'))
            docker=root/'docker';docker.write_text('#!/bin/sh\nexit 0\n');docker.chmod(0o755)
            if stopped_orphan:
                identity='a'*64;run='run-'+'b'*32;present=root/'orphan';present.touch()
                record=dict(Id=identity,Name='/oxidex-run-'+run,
                    Config=dict(Labels={'oxidex.remote-build':'true','oxidex.cache-project':'checkout','oxidex.run-id':run}),
                    State=dict(Running=False,Paused=False,Restarting=False,Pid=0,Status='created'),
                    Mounts=[dict(Type='bind',Destination='/src',Source=str(source)),
                            dict(Type='bind',Destination='/target',Source=str(target.parent))])
                docker.write_text('#!'+__import__('sys').executable+'\nimport json,sys\nfrom pathlib import Path\n'
                    'present=Path('+repr(str(present))+')\n'
                    'record='+repr(record)+'\n'
                    'if sys.argv[1]=="ps":print(record["Id"] if present.exists() else "")\n'
                    'elif sys.argv[1]=="inspect":\n'
                    ' if "--format" in sys.argv:print(record["Mounts"][1]["Source"])\n'
                    ' else:print(json.dumps([record]))\n'
                    'elif sys.argv[1]=="rm":\n'
                    ' assert sys.argv[2:]==[record["Id"]]\n'
                    ' present.unlink()\n'
                    'else:raise SystemExit(1)\n')
                # Redirect only the generated embedded reconciler's cache locks
                # into this fixture. Exercise the complete shell transaction,
                # including the inventory gate before source replacement.
                tokens=shlex.split(body)
                programs=[tokens[i+1] for i,x in enumerate(tokens[:-1]) if x=='-c']
                if programs:
                    program=programs[0]
                    adjusted=program.replace("namespace['reconcile_stopped_builders']()",
                        "namespace['LOCK_ROOT']=namespace['Path']("+repr(str(root))+")\n namespace['reconcile_stopped_builders']()")
                    body=body.replace(shlex.quote(program),shlex.quote(adjusted))
            body=body.replace('/usr/bin/docker',str(docker))
            launcher=root/'launcher'
            launcher.write_text('#!/bin/sh\n'
                'if [ "$2" = just ]; then exit '+('1' if header_failure else '0')+'; fi\n'
                'if [ "$2 $3" = "cargo build" ]; then\n'
                ' touch '+shlex.quote(str(root/'compiled'))+'\n'
                ' printf "#!/bin/sh\\ntouch '+str(root/'root-executed')+'\\n" >'+shlex.quote(str(target/'oxidex'))+'\n'
                ' chmod +x '+shlex.quote(str(target/'oxidex'))+'\n'+
                (' ln -sf '+shlex.quote(str(new))+' '+shlex.quote(str(target/'oxidex'))+'\n' if artifact_symlink else '')+
                'fi\n'
                'if [ "$2 $3" = "cargo run" ]; then touch '+shlex.quote(str(root/'container-verified'))+'; fi\n')
            launcher.chmod(0o755);body=body.replace('/usr/local/bin/oxidex-remote-build',str(launcher))
            # Use real OS locks for the generated shell's inherited descriptor.
            flock=root/'flock'
            flock.write_text('#!'+__import__('sys').executable+'\nimport fcntl,sys\n'
                'if sys.argv[-1]=="true":\n'
                ' with open(sys.argv[-2],"a") as handle:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
                ' raise SystemExit(0)\n'
                'try:fcntl.flock(int(sys.argv[-1]),fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
                'except BlockingIOError:raise SystemExit(75)\n')
            flock.chmod(0o755)
            if probe_source_lock:
                probe=root/'probe'
                probe.write_text('#!'+__import__('sys').executable+'\nimport fcntl,sys\n'
                    'from pathlib import Path\n'
                    'with open('+repr(str(root/'lock-cache-active-checkout.lock'))+',"a") as handle:\n'
                    ' try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);status="unlocked"\n'
                    ' except BlockingIOError:status="locked"\n'
                    'Path(sys.argv[1]).write_text(status)\n')
                probe.chmod(0o755)
                body=body.replace('tar -xzf ',str(probe)+' '+str(root/'during-extraction')+'; tar -xzf ')
                body=body.replace(str(launcher)+' checkout cargo fetch',str(probe)+' '+str(root/'before-build')+'; '+str(launcher)+' checkout cargo fetch')
            body=body.replace('flock ',str(flock)+' ')
            result=subprocess.run(['sh','-c',body],capture_output=True,text=True)
            if probe_source_lock:
                self.assertEqual((root/'during-extraction').read_text(),'locked')
                self.assertEqual((root/'before-build').read_text(),'unlocked')
            if stopped_orphan:self.assertFalse(present.exists(),result.stderr)
            self.assertFalse((source/'deleted.rs').exists())
            self.assertEqual((source/'new.rs').read_text(),'new snapshot')
            self.assertEqual((target/'retained.cache').read_text(),'retained')
            self.assertFalse(archive.exists())
            self.assertFalse((root/'root-executed').exists())
            if header_failure or artifact_symlink:
                self.assertNotEqual(result.returncode,0)
                if header_failure:self.assertFalse((root/'compiled').exists())
            else:
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertTrue((root/'container-verified').exists())
                exported=root/'remote/exports/run-test/oxidex'
                self.assertEqual(exported.read_bytes(),(target/'oxidex').read_bytes())

    def test_failed_remote_header_validation_prevents_release_compilation(self):
        self.execute_transaction('release',header_failure=True)

    def test_deleted_snapshot_source_does_not_survive_cache_reuse(self):
        self.execute_transaction('debug')

    def test_source_extraction_holds_cache_lock_until_before_build(self):
        self.execute_transaction('debug',probe_source_lock=True)

    def test_stopped_orphan_is_reconciled_before_transaction_inventory_gate(self):
        self.execute_transaction('debug',stopped_orphan=True)

    def test_symlinked_artifact_is_not_exported(self):
        self.execute_transaction('debug',artifact_symlink=True)

    def test_worker_interruption_is_retryable_but_compiler_errors_are_not(self):
        from lib.remote_build import retryable_exit
        self.assertTrue(retryable_exit(255))
        self.assertTrue(retryable_exit(137))
        self.assertTrue(retryable_exit(143))
        self.assertFalse(retryable_exit(101))
        self.assertFalse(retryable_exit(1))

    def execute_client_cleanup(self,outcome,wrong_identity=False,symlink=False):
        from lib import remote_build
        actual_run=subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();remote=root/'remote';remote.mkdir()
            source=root/'source';source.mkdir();evidence=root/'evidence'
            run='run-'+'a'*32;upload='oxidex-remote-source-'+run+'.tar.gz'
            export=remote/'exports'/run;export.mkdir(parents=True)
            (export/'oxidex').write_bytes(b'binary')
            retained=remote/'targets'/'cache';retained.mkdir(parents=True)
            (retained/'keep').write_text('retained')
            if symlink:
                outside=root/'outside';outside.mkdir();(outside/'keep').write_text('safe')
                shutil.rmtree(export);export.symlink_to(outside,target_is_directory=True)
            digest=hashlib.sha256(b'binary').hexdigest()
            def snapshot(path,archive):
                archive.write_bytes(b'snapshot');return dict(archive_sha256='b'*64)
            def invoke(command,**kwargs):
                if command[2]=='scp':
                    if ':~/' in command[4]:
                        (root/upload).write_bytes(b'snapshot')
                        if outcome=='upload_failure':raise subprocess.CalledProcessError(255,command)
                    else:
                        Path(command[4]).write_bytes(b'binary')
                        if outcome=='download_failure':raise subprocess.CalledProcessError(255,command)
                    return subprocess.CompletedProcess(command,0)
                shell=next(value[len('--command='):] for value in command if value.startswith('--command='))
                if 'export=Path(' not in shell and not shell.startswith('test "$(curl'):
                    kwargs['stdout'].write(digest+'  artifact\n');return subprocess.CompletedProcess(command,0)
                # Execute the actual cleanup body against local fixture paths.
                body=(shlex.split(shell)[-1] if 'python3 -I -c' in shell else shell)
                body=body.replace('/mnt/runner-data/remote-build',str(remote))
                body=body.replace(remote_build.identity_guard('123'),'test '+('0' if wrong_identity else '123')+' = 123 || exit 75')
                body=body.replace('st_uid != 0','st_uid != os.getuid()')
                # The fixture's remote directory is its ownership boundary;
                # Darwin temporary-directory ancestors are user/sticky owned.
                body=body.replace('(export,*export.parents)','(export,export.parent,export.parent.parent)')
                return actual_run(['sh','-c',body],cwd=root,capture_output=True,text=True)
            args=['--source',str(source),'--instance','worker','--zone','zone','--worktree-id','cache',
                  '--evidence-dir',str(evidence)]
            if outcome!='no_artifact':args+=['--artifact-dir',str(root/'artifacts')]
            with patch.object(remote_build,'make_snapshot',side_effect=snapshot),\
                 patch.object(remote_build.secrets,'token_hex',return_value='a'*32),\
                 patch.object(remote_build.subprocess,'check_output',side_effect=['123\n','commit\n','']),\
                 patch.object(remote_build.subprocess,'run',side_effect=invoke):
                if outcome.endswith('_failure'):
                    with self.assertRaises(subprocess.CalledProcessError):remote_build.main(args)
                else:self.assertEqual(remote_build.main(args),0)
            receipt=json.loads((evidence/'remote-build.json').read_text())
            self.assertEqual((root/upload).exists(),wrong_identity)
            self.assertEqual(export.exists(),wrong_identity or symlink)
            self.assertEqual(receipt['export_cleanup_confirmed'],not (wrong_identity or symlink))
            self.assertEqual((retained/'keep').read_text(),'retained')
            if outcome!='upload_failure':self.assertEqual(receipt['binary_sha256'],digest)
            if symlink:self.assertEqual((outside/'keep').read_text(),'safe')

    def test_client_reclaims_exports_after_download_or_receipt_only_verification(self):
        for outcome in ('success','no_artifact','upload_failure','download_failure'):
            with self.subTest(outcome=outcome):self.execute_client_cleanup(outcome)

    def test_cleanup_holds_on_vm_identity_mismatch(self):
        self.execute_client_cleanup('success',wrong_identity=True)

    def test_cleanup_refuses_symlinked_export_directory(self):
        self.execute_client_cleanup('no_artifact',symlink=True)
