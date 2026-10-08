"""Bounded synthetic profile/control checks, no VM or application commands."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from lib.qualification_trust import reservation as r
from lib.qualification_trust import host_gate


class ControlTests(unittest.TestCase):
    def claim(self):
        return dict(schema=2,profile='task20-v1',project='qualification-'+'a'*12+'-'+'b'*32,
            attempt='final-r1',expected_vm_id='438',expected_boot='0b99c4d2-ca5f-4471-bf84-ff4ab8dbefd3',
            head='a'*40,tree='c'*40,bundle_sha256='d'*64,attestation_sha256='e'*64,
            attestation_namespace='oxidex-qualification-source-v1',stages_sha256='f'*64)

    def test_fixed_task20_arguments_and_profile_names(self):
        s=self.claim();identity=['--round',s['attempt'],'--expected-head',s['head'],'--run-id',s['project']]
        good=['python3','/target/checkout/tools/remote-build/task20_worker.py',*identity]
        r.command_arguments('task20-execute',good,s)
        for command,argv in [('prepare',good),('task20-execute',good+['--stages','fmt']),
                             ('upload',['upload']),('upload-receive',['upload'])]:
            with self.assertRaises(ValueError):r.command_arguments(command,argv,s)

    def test_task20_packet_is_separate_from_task19_and_round_is_root_bound(self):
        from lib.qualification_trust.task20_receiver import MEMBERS
        s=self.claim()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in MEMBERS:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
            expected=dict(schema=1,round=s['attempt'],head=s['head'],tree=s['tree'],run_id=s['project'],bundle_sha256=s['bundle_sha256'])
            (root/'task20-round.json').write_text(json.dumps(expected))
            self.assertEqual(host_gate.validate_upload_layout(root,profile='task20-v1',session=s),root/'source-attestation')
            with self.assertRaises(ValueError):host_gate.validate_upload_layout(root)
            (root/'task20-round.json').write_text(json.dumps({**expected,'head':'0'*40}))
            with self.assertRaises(ValueError):host_gate.validate_upload_layout(root,profile='task20-v1',session=s)

    def test_host_bound_proof_unknown_profile_does_not_promote(self):
        from lib.qualification_trust.control_observation import sealed_binding
        s=self.claim();document=b'actual document';s['attestation_sha256']=hashlib.sha256(document).hexdigest()
        actual=dict(head=s['head'],bundle_sha256=s['bundle_sha256'],source_identity={'tree':s['tree']})
        with patch('lib.qualification_trust.launcher_contract.load_binding',return_value=actual), \
             patch('lib.qualification_trust.host_gate.regular_bytes',return_value=document):
            with self.assertRaisesRegex(ValueError,'Task20 full-source profile'):
                sealed_binding('/fixture',s)
            actual['profile']='task20-v1'
            self.assertEqual(sealed_binding('/fixture',s)['tree'],s['tree'])

    def test_root_descriptor_binds_all_fixed_arguments_and_archive_members(self):
        from lib.qualification_trust.control_engine import validate_request
        from lib.qualification_trust.task20_receiver import MEMBERS
        import base64
        s=self.claim();identity=['--round',s['attempt'],'--expected-head',s['head'],'--run-id',s['project']]
        argv=[['prepare'],['upload-receive'],
              ['verify-source','--head',s['head'],'--bundle-sha256',s['bundle_sha256'],'--run-id',s['project']],
              ['python3','/target/checkout/tools/remote-build/task20_bootstrap.py'],
              ['python3','/target/checkout/tools/remote-build/task20_worker.py',*identity],
              ['python3','/target/checkout/tools/remote-build/task20_results.py','--pack',*identity],
              ['python3','/target/checkout/tools/remote-build/task20_results.py','--replay',*identity],['cleanup']]
        names=['prepare','upload-receive','verify-source','task20-bootstrap','task20-execute','task20-pack','task20-replay','cleanup']
        stages=[dict(ordinal=i,command=name,argv_sha256=r.digest(argv[i])) for i,name in enumerate(names)]
        s['stages_sha256']=r.digest(stages)
        request=dict(session=s,stages=stages,argv=argv,archive=dict(sha256='1'*64,bytes=100,
                     members={name:'2'*64 for name in MEMBERS}),
                     document=base64.b64encode(b'document').decode(),signature=base64.b64encode(b'signature').decode())
        self.assertEqual(validate_request(request)[0],s)
        argv[4].append('--arbitrary')
        with self.assertRaises(ValueError):validate_request(request)

    def test_actual_pack_capture_preserves_bytes_and_rejects_link(self):
        from lib.qualification_trust import control_engine as engine
        project=self.claim()['project']
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder).resolve();target=base/'targets'/project;target.mkdir(parents=True)
            archive=target/'task20-results.tar.gz';archive.write_bytes(b'actual bounded fixture')
            proof=base/'proof';proof.mkdir()
            with patch.object(engine,'BUILD',base):engine.capture_pack(project,proof)
            actual=json.loads((proof/'pack-observed.json').read_text())
            self.assertEqual(actual['archive_sha256'],hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(actual['archive_bytes'],archive.stat().st_size)
            archive.unlink();archive.symlink_to(proof/'pack-observed.json')
            with patch.object(engine,'BUILD',base),self.assertRaises(OSError):engine.capture_pack(project,proof)
