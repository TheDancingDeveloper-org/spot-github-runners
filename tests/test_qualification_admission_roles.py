"""Execute shared startup bytes without dedicated-only installed modules."""
import base64
import builtins
import io
import json
import re
import struct
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
from lib.runner_manager import args_parser, select_sizing, render_startup


class QualificationAdmissionRoleTests(unittest.TestCase):
    def setUp(self):
        args=args_parser().parse_args(['--max-cost','.125','--tier','starting',
            '--tier-slots','8','--cpu','8','--ram','16','--instance-type','n2-standard-8',
            '--remote-builder','--runner-prefix','gha','--tier-job-memory-gib','2'])
        select_sizing(args)
        self.startup=render_startup(args,'token','group','deployment')
        self.sources={path:base64.b64decode(encoded).decode()
            for encoded,path in re.findall(r"printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d >(\S+)",self.startup)}
        self.assertFalse(any('qualification_trust' in path for path in self.sources))
        self.admission=self.module('/opt/gha-runners/build/host_admission.py')
        self.launcher=self.module('/usr/local/bin/oxidex-remote-build')
        self.adapter=self.module('/usr/local/bin/docker')
        program=self.startup.split("python3 - <<'ADMISSION_CONFIG'\n",1)[1].split('\nADMISSION_CONFIG',1)[0]
        target=io.StringIO();opener=MagicMock();opener.return_value.__enter__.return_value=target
        with patch('builtins.open',opener),patch('socket.gethostname',return_value='fixture'):
            exec(compile(program,'<emitted-admission-config>','exec'),{})
        self.config=json.loads(target.getvalue())

    def module(self,path):
        module=ModuleType('emitted_'+Path(path).stem)
        exec(compile(self.sources[path],path,'exec'),module.__dict__)
        return module

    @contextmanager
    def unavailable_contract(self):
        original=builtins.__import__;attempts=[]
        def imported(name,*args,**kwargs):
            if name.startswith('qualification_trust'):
                attempts.append(name)
                raise ModuleNotFoundError('Dedicated package is not installed on this host')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=imported):yield attempts

    def record(self,project):
        argv=self.launcher.container_command(project,['python3','-V'],4,4)
        selected,run,name=self.adapter.build_identity(argv[1:])
        self.assertEqual(selected,project)
        mounts=[]
        for index,value in enumerate(argv[:argv.index('oxidex-remote-builder:1.97.1')]):
            if value=='-v':
                source,destination=argv[index+1].split(':')
                mounts.append(dict(Type='bind',Source=source,Destination=destination,RW=True))
        return dict(Id='a'*64,Name='/'+name,Config={'Labels':{
            'oxidex.remote-build':'true','oxidex.cache-project':project,'oxidex.run-id':run}},
            State=dict(Running=False,Paused=False,Restarting=False,Pid=0,Status='created'),Mounts=mounts)

    def docker(self,row,events):
        def execute(argv,**kwargs):
            events.append(argv)
            if argv[1]=='ps':return SimpleNamespace(stdout=row['Id']+'\n')
            if argv[1]=='inspect':return SimpleNamespace(stdout=json.dumps([row]))
            if argv[1]=='rm':
                self.assertEqual(argv,['/usr/bin/docker','rm',row['Id']])
                return SimpleNamespace(stdout='')
            raise AssertionError(argv)
        return execute

    def test_shared_emitted_launcher_names_reconcile_without_dedicated_import(self):
        for project in ('qualification-example','qualification-'+'a'*12+'-'+'b'*32):
            with self.subTest(project=project),tempfile.TemporaryDirectory() as directory:
                config=Path(directory)/'config.json';config.write_text(json.dumps(self.config))
                row=self.record(project);events=[]
                with patch.object(self.admission,'CONFIG_PATH',config),\
                     patch.object(self.admission,'LOCK_ROOT',Path(directory)),\
                     patch.object(self.admission.subprocess,'run',side_effect=self.docker(row,events)),\
                     self.unavailable_contract() as imports:
                    self.assertEqual(self.admission.builder_cache_identity(row),(project,row['Config']['Labels']['oxidex.run-id']))
                    self.assertFalse(self.admission.builders_running())
                self.assertEqual(imports,[])
                self.assertEqual(sum(argv[1]=='rm' for argv in events),1)

    def test_shared_emitted_service_acquire_and_release_survive_prefixed_orphans(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'config.json';config.write_text(json.dumps(self.config))
            row=self.record('qualification-example');events=[];clients=[]
            for action in ('acquire','release'):
                client=MagicMock();client.__enter__.return_value=client
                client.getsockopt.return_value=struct.pack('3i',123,0,0)
                client.recv.return_value=json.dumps(dict(action=action,kind='builder')).encode()
                clients.append(client)
            server=MagicMock();server.__enter__.return_value=server
            server.bind.side_effect=lambda path:Path(path).touch()
            server.accept.side_effect=[(client,None) for client in clients]+[StopIteration]
            with patch.object(self.admission,'CONFIG_PATH',config),\
                 patch.object(self.admission,'LOCK_ROOT',root),\
                 patch.object(self.admission,'LEDGER_ROOT',root/'ledger'),\
                 patch.object(self.admission,'ADMISSION_SOCKET',str(root/'socket-dir/socket')),\
                 patch.object(self.admission,'process_identity',return_value='host:123:1'),\
                 patch.object(self.admission,'observed_envelope',return_value=self.config['work_gib']),\
                 patch.object(self.admission.subprocess,'run',side_effect=self.docker(row,events)),\
                 patch.object(self.admission.socket,'socket',return_value=server),\
                 patch.object(self.admission.socket,'SO_PEERCRED',17,create=True),\
                 patch('os.geteuid',return_value=0),patch('os.chown'),\
                 self.unavailable_contract() as imports:
                with self.assertRaises(StopIteration):self.admission.serve()
            self.assertEqual([json.loads(client.sendall.call_args.args[0]) for client in clients],
                             [dict(accepted=True),dict(accepted=True)])
            self.assertEqual(json.loads((root/'ledger/leases.json').read_text()),{})
            self.assertEqual(sum(argv[1]=='rm' for argv in events),2)
            self.assertEqual(imports,[])

    def test_legacy_benchmark_role_keeps_ordinary_mounts_without_new_package(self):
        # The separately installed benchmark_assets producer installs its own
        # host_admission/benchmark_admission/builder_access, not qualification_trust.
        for project in ('qualification-example','qualification-'+'a'*12+'-'+'b'*32):
            with self.subTest(project=project),tempfile.TemporaryDirectory() as directory:
                config=Path(directory)/'benchmark.json';config.write_text('{"role":"benchmark-builder"}')
                root=Path('/mnt/runner-data/benchmark-builder-278');row=self.record(project)
                for mount in row['Mounts']:
                    mount['Source']=mount['Source'].replace(str(self.admission.BUILD_ROOT),str(root))
                with patch.object(self.admission,'CONFIG_PATH',config),                     patch.object(self.admission,'BUILD_ROOT',root),                     self.unavailable_contract() as imports:
                    self.assertIsNotNone(self.admission.builder_cache_identity(row))
                    self.assertEqual(imports,[])

    def test_dedicated_noncanonical_namespace_refuses_before_import(self):
        with self.unavailable_contract() as imports:
            self.assertIsNone(self.admission.builder_cache_identity(self.record('qualification-example'),host_config={'role':'builder'}))
            self.assertEqual(imports,[])

    def test_dedicated_missing_package_never_falls_back_to_shared_mounts(self):
        project='qualification-'+'a'*12+'-'+'b'*32;row=self.record(project)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'config.json';config.write_text('{"role":"builder"}')
            events=[]
            with patch.object(self.admission,'CONFIG_PATH',config),\
                 patch.object(self.admission,'LOCK_ROOT',root),\
                 patch.object(self.admission.subprocess,'run',side_effect=self.docker(row,events)),\
                 self.unavailable_contract() as imports:
                with self.assertRaisesRegex(ValueError,'Dedicated qualification mount contract unavailable'):
                    self.admission.builder_cache_identity(row)
                self.assertTrue(self.admission.builders_running())
            self.assertEqual(len(imports),2)
            self.assertFalse(any(argv[1]=='rm' for argv in events))

    def test_unknown_or_malformed_root_role_refuses_without_import(self):
        for config in ({'role':'unknown'},[],{'role':[]}):
            with self.subTest(config=config),self.unavailable_contract() as imports:
                with self.assertRaises(ValueError):
                    self.admission.builder_cache_identity(self.record('qualification-example'),host_config=config)
                self.assertEqual(imports,[])


if __name__=='__main__':unittest.main()
