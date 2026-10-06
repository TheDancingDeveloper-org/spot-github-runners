"""Small offline regressions for host admission; no Docker or infrastructure."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from lib.qualification_trust import host_gate as gate


class IntakeTests(unittest.TestCase):
    def test_copy_permissions_ignore_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'; source.mkdir()
            (source / 'file').write_bytes(b'input')
            mask = os.umask(0o077)
            try:
                gate.copy_sealed(source, root / 'sealed')
            finally:
                os.umask(mask)
            self.assertEqual(stat.S_IMODE((root / 'sealed').stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((root / 'sealed/file').stat().st_mode), 0o444)

    def test_regular_read_opens_nonblocking_before_fstat(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'data'; path.write_bytes(b'data')
            original = os.open
            def checked(name, flags, *args, **kwargs):
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_NOFOLLOW)
                return original(name, flags, *args, **kwargs)
            with patch.object(gate.os, 'open', side_effect=checked):
                self.assertEqual(gate.regular_bytes(path), b'data')

    def test_intake_rejects_uploader_authority_and_import_carriers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('repository.bundle', 'maintainer.allowed_signers', 'rust-toolchain.toml',
                         'qualification_bootstrap.py', 'qualification_source.py'):
                (root / name).write_text('data')
            (root / 'reference').mkdir()
            for extra in ('execution-binding.json', 'verified.git', 'argparse.py', 'hashlib.pyc'):
                path = root / extra; path.write_text('injected')
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    gate.validate_upload_layout(root)
                path.unlink()


class GitBoundaryTests(unittest.TestCase):
    def test_tree_rejects_symlinks_submodules_escaping_paths_and_bytecode(self):
        from lib.qualification_trust.git_sandbox import safe_tree_entries
        for mode, kind, name in [('120000', 'blob', 'link'), ('160000', 'commit', 'module'),
                                 ('100644', 'blob', '../escape'), ('100644', 'blob', '.git/config'),
                                 ('100644', 'blob', 'pkg/__pycache__/mod.pyc')]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_tree_entries(f'{mode} {kind} {"a"*40}\t{name}\0'.encode())
        self.assertEqual(safe_tree_entries(f'100644 blob {"a"*40}\tfile.py\0'.encode()),
                         [('file.py', 'a'*40, 0o644)])

    def test_import_limits_and_guardian_handoff(self):
        from lib.qualification_trust import git_sandbox
        from types import SimpleNamespace
        import resource
        with patch.object(git_sandbox.resource, 'setrlimit') as limits:
            git_sandbox.limits()
        limits.assert_any_call(resource.RLIMIT_AS, (1024**3, 1024**3))
        limits.assert_any_call(resource.RLIMIT_FSIZE, (512*1024**2, 512*1024**2))
        limits.assert_any_call(resource.RLIMIT_NPROC, (32, 32))
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            token = gate.PROTECTIVE_FDS.set((41, 42))
            def inspect(command, locks, diagnostics):
                self.assertEqual(locks, (41, 42, 43))
                argv = command(44, locks)
                self.assertTrue(argv[3].endswith('/import_supervisor.py'))
                self.assertEqual(argv[4:8], ['44', '41,42,43', '65534', '65534'])
                (stage/'importer/identity.json').write_text('{}')
            try:
                with patch.object(gate.os, 'geteuid', return_value=0), \
                     patch.object(gate.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=65534, pw_gid=65534)), \
                     patch.object(gate.os, 'chown'), \
                     patch.object(gate, 'import_slot', return_value=__import__('contextlib').nullcontext(43)), \
                     patch.object(gate, 'wait_guardian', side_effect=inspect):
                    self.assertEqual(gate.sandbox_import(stage, 'a'*40, 'b'*64, 'run', None), {})
            finally:
                gate.PROTECTIVE_FDS.reset(token)

    def test_real_tiny_signed_bundle_import_ignores_uploader_git_config(self):
        import hashlib
        import subprocess
        from lib.qualification_trust import git_sandbox, qualification_source as verifier
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); repo = root/'repo'; repo.mkdir()
            incoming = root/'incoming'; incoming.mkdir(); work = root/'work'; work.mkdir()
            key = root/'key'
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)], check=True)
            public = key.with_suffix('.pub').read_text().split()
            signer = root/'signers'; signer.write_text(verifier.PRINCIPAL+' '+' '.join(public)+'\n')
            fingerprint = subprocess.check_output(['ssh-keygen','-lf',str(signer)],text=True).split()[1]
            env = dict(os.environ,GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_NOSYSTEM='1',
                       GIT_AUTHOR_NAME='swackhamer',GIT_AUTHOR_EMAIL=verifier.PRINCIPAL,
                       GIT_COMMITTER_NAME='swackhamer',GIT_COMMITTER_EMAIL=verifier.PRINCIPAL)
            def git(*args):
                return subprocess.check_output(['git','-C',str(repo),*args],env=env,stderr=subprocess.DEVNULL).decode().strip()
            git('init','-q')
            (repo/'.exiftool-version').write_text('13.59\n')
            (repo/'rust-toolchain.toml').write_text('[toolchain]\nchannel = "1.99.0"\n')
            (repo/'hello.py').write_text('safe = True\n')
            git('add','.')
            git('-c','core.hooksPath=/dev/null','-c','gpg.format=ssh','-c','user.signingkey='+str(key),
                '-c','commit.gpgsign=true','commit','-qm','tiny source')
            head = git('rev-parse','HEAD'); bundle = incoming/'repository.bundle'
            git('bundle','create',str(bundle),'HEAD')
            injected = incoming/'verified.git'; injected.mkdir()
            (injected/'config').write_text('[core]\nsshCommand = touch '+str(root/'executed')+'\n')
            with patch.dict(os.environ, dict(os.environ)), \
                 patch.object(verifier,'KEY',' '.join(public[:2])), patch.object(verifier,'FINGERPRINT',fingerprint):
                git_sandbox.import_source(incoming,work,head,hashlib.sha256(bundle.read_bytes()).hexdigest(),
                    'qualification-'+head[:12]+'-'+'a'*32,signer,verifier_module=verifier)
            self.assertFalse((root/'executed').exists())
            self.assertEqual((work/'checkout/hello.py').read_text(),'safe = True\n')
            self.assertEqual(json.loads((work/'identity.json').read_text())['head'],head)
            self.assertNotIn('sshCommand',(work/'repository.git/config').read_text())
            import shutil
            checkout=work/'checkout';shutil.copytree(work/'repository.git',checkout/'.git')
            (checkout/'.git/config').write_text('[core]\n\tbare = false\n')
            subprocess.run(['git','-C',str(checkout),'config','--local','user.name','Measurement'],env=env,check=True)
            self.assertEqual(subprocess.check_output(['git','-C',str(checkout),'status','--porcelain'],env=env),b'')
            owned=work/'measurement'
            subprocess.run(['git','-C',str(checkout),'worktree','add','--detach','--no-checkout',str(owned),'HEAD'],
                           env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            self.assertTrue((owned/'.git').is_file())



class LifecycleTests(unittest.TestCase):
    def test_failed_admission_leaves_no_binding_and_same_namespace_revokes_old(self):
        from lib.qualification_trust import launcher_contract as launch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); trust = root/'qualification-sealed'; trust.mkdir()
            project = 'qualification-'+'a'*12+'-'+'b'*32
            def fake_copy(source, target):
                target.mkdir(); (target/'execution-binding.json').write_text('untrusted')
            with patch.object(launch,'trusted_directory'), patch.object(launch,'copy_sealed',side_effect=fake_copy), \
                 patch.object(launch,'verify_sealed',side_effect=ValueError('bad signature')):
                with self.assertRaisesRegex(ValueError,'bad signature'):
                    launch.create_binding(root,project,'a'*40,'b'*64)
            self.assertEqual(list(trust.iterdir()),[])
            sealed = trust/project; sealed.mkdir()
            (sealed/'execution-binding.json').write_text('previous successful binding')
            with patch.object(launch,'trusted_directory'):
                with self.assertRaisesRegex(ValueError,'already attempted'):
                    launch.create_binding(root,project,'a'*40,'b'*64)
            self.assertFalse((sealed/'execution-binding.json').exists())

    def test_cleanup_refuses_any_active_sealed_submount_and_preserves_targets(self):
        from lib.qualification_trust import launcher_contract as launch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); project = 'qualification-'+'a'*12+'-'+'b'*32
            sealed = root/'qualification-sealed'/project; sealed.mkdir(parents=True)
            target = root/'targets'/project; target.mkdir(parents=True)
            (target/'evidence').write_text('retain')
            inventory = [{'Mounts':[{'Source':str(sealed/'checkout')}]}]
            with patch.object(launch,'trusted_directory'):
                with self.assertRaisesRegex(ValueError,'still mounted'):
                    launch.cleanup(root,project,inventory)
                self.assertTrue(sealed.exists())
                launch.cleanup(root,project,[])
            self.assertFalse(sealed.exists())
            self.assertEqual((target/'evidence').read_text(),'retain')


class MountContractTests(unittest.TestCase):
    def test_actual_adapter_policy_identity_and_reconciliation_agree(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/lib'))
        from lib.builder_only_assets import ASSETS
        from lib.qualification_trust.mounts import volume_arguments, expected_mounts
        from lib import host_admission
        from types import ModuleType
        launcher=ModuleType('launcher');exec(compile(ASSETS['/usr/local/bin/oxidex-remote-build'],'launcher','exec'),launcher.__dict__)
        adapter=ModuleType('adapter');exec(compile(ASSETS['/usr/local/bin/docker'],'adapter','exec'),adapter.__dict__)
        root=Path('/mnt/runner-data/remote-build');project='qualification-'+'a'*12+'-'+'b'*32
        image='oxidex-remote-builder:1.99.0'
        argv=launcher.container_command(project,['python3','-I','-B','/qualification-runtime.py'],16,28,image)
        index=argv.index(image);prefix=[];i=0
        while i<index:
            if argv[i]=='-v':i+=2
            else:prefix.append(argv[i]);i+=1
        argv=prefix+volume_arguments(root,project)+argv[index:]
        adapter.validate_build_policy(argv[1:],dict(cpus=16,memory_gib=28))
        selected,run,name=adapter.build_identity(argv[1:]);self.assertEqual(selected,project)
        mounts=[dict(Type='bind',Destination=dst,Source=src,RW=rw)
                for dst,(src,rw) in expected_mounts(root,project).items()]
        adapter.inspected_mounts(mounts,project)
        row=dict(Name='/'+name,Config={'Labels':{'oxidex.remote-build':'true','oxidex.cache-project':project,'oxidex.run-id':run}},Mounts=mounts)
        self.assertEqual(host_admission.builder_cache_identity(row,host_config={'role':'builder'}),(project,run))
        mounts[2]['RW']=True
        with self.assertRaises(ValueError):adapter.inspected_mounts(mounts,project)
        self.assertIsNone(host_admission.builder_cache_identity(row,host_config={'role':'builder'}))

    def test_fresh_startup_creates_parents_before_asset_and_sets_anchor_after(self):
        from lib.builder_startup import render_builder_startup
        from test_dedicated_builder_startup import inputs
        from types import SimpleNamespace
        text=render_builder_startup(SimpleNamespace(**inputs()),'test')
        path='/etc/oxidex-remote-build/qualification-maintainer.allowed_signers'
        self.assertLess(text.index('install -d -o root -g root -m 0755 /etc/oxidex-remote-build'),text.index('>'+path))
        self.assertLess(text.index('>'+path),text.index('chmod 0444 '+path))
        self.assertIn('gnupg git openssh-client',text)
        self.assertNotIn('/var/lib/oxidex-builder/qualification-sealed',text)


class ExecutionBoundaryTests(unittest.TestCase):
    def test_strict_bootstrap_admits_once_and_uses_only_host_isolated_wrapper(self):
        from lib.qualification_trust import launcher_contract as launch
        from lib.builder_only_assets import ASSETS
        from types import ModuleType
        import sys
        module=ModuleType('launcher');exec(compile(ASSETS['/usr/local/bin/oxidex-remote-build'],'launcher','exec'),module.__dict__)
        head='a'*40; project='qualification-'+head[:12]+'-'+'b'*32
        root=Path('/mnt/runner-data/remote-build')
        binding=dict(schema=2,head=head,run_id=project,bundle_sha256='c'*64,uploaded_signers_sha256='d'*64)
        args=['python3','/src/qualification_bootstrap.py','--head',head,'--output',
              '/target/ops/evidence/remote-qualification/'+project,'--bundle-sha256','c'*64,'--signers-sha256','d'*64]
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory)/'lock';lock.touch()
            opener=launch.os.open
            def open_lock(path,*a,**kw):return opener(lock if str(path).startswith('/run/') else path,*a,**kw)
            with patch.object(launch,'trusted_directory'),patch.object(launch.os,'open',side_effect=open_lock), \
                 patch.object(launch,'create_binding',return_value=binding) as create, \
                 patch.object(launch,'load_binding',return_value=binding), \
                 patch.object(Path,'exists',return_value=False), \
                 patch.object(launch,'execution_arguments',wraps=launch.execution_arguments), \
                 patch.object(launch,'volume_arguments',wraps=launch.volume_arguments):
                calls=[]
                result=launch.dispatch(root,project,args,module.container_command,
                    dict(cpus=16,memory_gib=28,image='oxidex-remote-builder:1.99.0'),lambda argv:calls.append(argv) or 0)
            self.assertEqual(result,0);create.assert_called_once_with(root,project,head,'c'*64)
            self.assertEqual(calls[0][-7:],['python3','-I','-B','/qualification-runtime.py','bootstrap',head,project])
            self.assertNotIn('/src/qualification_bootstrap.py',calls[0])
            self.assertIn(str(root/'qualification-sealed'/project/'checkout')+':/target/checkout:ro',calls[0])

    def test_attested_bootstrap_cannot_skip_explicit_gate(self):
        from lib.qualification_trust import launcher_contract as launch
        head='a'*40; project='qualification-'+head[:12]+'-'+'b'*32
        args=['python3','/src/qualification_bootstrap.py','--head',head,'--output','other',
              '--bundle-sha256','c'*64,'--signers-sha256','d'*64]
        with tempfile.TemporaryDirectory() as directory:
            lock=Path(directory)/'lock';lock.touch();opener=launch.os.open
            with patch.object(launch,'trusted_directory'),patch.object(launch.os,'open',side_effect=lambda p,*a,**kw:opener(lock,*a,**kw)), \
                 patch.object(Path,'exists',side_effect=lambda:False),patch.object(launch,'create_binding') as create:
                with self.assertRaises(ValueError):
                    launch.dispatch(Path('/mnt/runner-data/remote-build'),project,args,None,{},lambda argv:None)
                create.assert_not_called()

    def test_runtime_imports_only_authenticated_tree_and_installed_libraries(self):
        from lib.qualification_trust import runtime
        import sys
        head='a'*40; project='qualification-'+head[:12]+'-'+'b'*32
        old_path=sys.path[:];old_argv=sys.argv[:];old_bytecode=sys.dont_write_bytecode
        try:
            with patch.dict(os.environ,{'PYTHONPATH':'/src:/target','GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'evil'}), \
                 patch.object(runtime.os,'chdir'),patch.object(runtime.runpy,'run_path') as execute:
                sys.path[:]=['/src','/target','', '/usr/lib/python3.11']
                runtime.main(['bootstrap',head,project])
                self.assertEqual(sys.path,['/target/checkout/tools/remote-build','/usr/lib/python3.11'])
                self.assertNotIn('PYTHONPATH',os.environ)
                self.assertEqual(execute.call_args.args[0],'/target/checkout/tools/remote-build/qualification.py')
                self.assertIn('--prepare-inputs',sys.argv)
                self.assertNotIn('GIT_CONFIG_NOSYSTEM',os.environ)
        finally:
            sys.path[:]=old_path;sys.argv[:]=old_argv;sys.dont_write_bytecode=old_bytecode


class DetachedAdmissionTests(unittest.TestCase):
    def test_real_detached_signature_authenticates_bundle_before_git(self):
        import subprocess
        from lib.qualification_trust import qualification_source as verifier
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);key=root/'key';anchor=root/'anchor'
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True)
            public=key.with_suffix('.pub').read_text().split()
            anchor.write_text(verifier.PRINCIPAL+' '+' '.join(public)+'\n')
            fingerprint=subprocess.check_output(['ssh-keygen','-lf',str(anchor)],text=True).split()[1]
            head='a'*40;bundle='b'*64;run='qualification-'+head[:12]+'-'+'c'*32
            document=root/'document.json'
            document.write_text(json.dumps(dict(head=head,bundle_sha256=bundle,run_id=run,
                                               namespace=verifier.NAMESPACE,mode=verifier.ATTESTED)))
            subprocess.run(['ssh-keygen','-Y','sign','-f',str(key),'-n',verifier.NAMESPACE,str(document)],check=True,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            document.with_suffix('.json.sig').rename(root/'document.sig')
            with patch.object(gate,'ANCHOR',anchor),patch.object(verifier,'KEY',' '.join(public[:2])),patch.object(verifier,'FINGERPRINT',fingerprint):
                gate.preauthenticate(root,head,bundle,run)
                with self.assertRaises(ValueError):gate.preauthenticate(root,head,'d'*64,run)
                (root/'document.sig').write_text('forged')
                with self.assertRaises(subprocess.CalledProcessError):gate.preauthenticate(root,head,bundle,run)

    def test_rejected_detached_proof_never_reaches_importer(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);incoming=root/'incoming';incoming.mkdir()
            (incoming/'repository.bundle').write_bytes(b'tiny')
            anchor=root/'anchor';anchor.write_text('anchor')
            original=Path.lstat
            def owned(path,*args,**kwargs):
                if path==anchor:return SimpleNamespace(st_mode=stat.S_IFREG|0o444,st_uid=0)
                return original(path,*args,**kwargs)
            head='a'*40;run='qualification-'+head[:12]+'-'+'b'*32
            with patch.object(gate,'ANCHOR',anchor),patch.object(Path,'lstat',owned), \
                 patch.object(gate,'validate_upload_layout',return_value=incoming/'source-attestation'), \
                 patch.object(gate,'preauthenticate',side_effect=ValueError('unauthenticated')), \
                 patch.object(gate,'sandbox_import') as imported:
                with self.assertRaisesRegex(ValueError,'unauthenticated'):
                    gate.verify_sealed(root,head,gate.digest(b'tiny'),run)
                imported.assert_not_called()


if __name__ == '__main__':
    unittest.main()
