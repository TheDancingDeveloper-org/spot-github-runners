"""Bounded synthetic profile/control checks, no VM or application commands."""
import hashlib
import io
import json
import os
import stat
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from lib.qualification_trust import reservation as r
from lib.qualification_trust import host_gate


class ControlTests(unittest.TestCase):
    def test_uploaded_names_are_durable_before_success_is_published(self):
        from lib.qualification_trust import control_engine as engine
        project=self.claim()['project']; identity='1'*32
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); dossier=root/'dossier'; dossier.mkdir()
            source=root/'sources'/project; source.mkdir(parents=True)
            incoming=dossier/'receive'/'incoming'; incoming.mkdir(parents=True)
            (incoming/'packet').write_bytes(b'verified packet')
            directories={p.stat().st_ino:p for p in (source,incoming)}
            synced=set(); real_fsync=os.fsync
            def fsync(fd):
                info=os.fstat(fd)
                if stat.S_ISDIR(info.st_mode) and info.st_ino in directories:
                    synced.add(directories[info.st_ino])
                real_fsync(fd)
            def finish(*args,**kwargs):
                self.assertEqual(synced,{source,incoming},'Renamed entries must be durable before terminal success')
                self.assertEqual((source/'packet').read_bytes(),b'verified packet')
                self.assertEqual(list(incoming.iterdir()),[])
                return {'status':'finished'}
            store=Mock(); store.locked.return_value=nullcontext();store.read.return_value={}
            store.finish.side_effect=finish
            request={'argv':[[],['upload-receive']], 'archive':dict(sha256='a'*64,bytes=1,members={})}
            with patch.object(engine,'BUILD',root), \
                 patch.object(engine,'selected',return_value=(dossier,request,{})), \
                 patch.object(engine,'idle',return_value=True), \
                 patch.object(engine,'activity',return_value=nullcontext()), \
                 patch.object(engine,'receive',return_value={'verified':True}), \
                 patch.object(engine.custody,'Store',return_value=store), \
                 patch.object(engine.custody,'current_actor',return_value={'pid':1,'start':2}), \
                 patch.object(engine.custody,'host_identity',return_value=('438','boot')), \
                 patch.object(engine.os,'fsync',side_effect=fsync):
                self.assertEqual(engine.stage_locked(project,identity,1,io.BytesIO()),{'status':'finished'})

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

    def test_export_lost_ack_reuses_only_selected_completed_receipt(self):
        from lib.qualification_trust import control_engine as engine
        project=self.claim()['project'];identity='1'*32;payload=b'actual root-observed pack bytes'
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);proof=base/'proof';proof.mkdir()
            target=base/'targets'/project;target.mkdir(parents=True)
            archive=target/'task20-results.tar.gz';archive.write_bytes(payload)
            observed={'schema':1,'project':project,'archive_sha256':hashlib.sha256(payload).hexdigest(),
                      'archive_bytes':len(payload)}
            (proof/'pack-observed.json').write_text(json.dumps(observed))
            record={'state':'SOURCE_VERIFIED','active':None,'next_ordinal':6,'owner':{'pid':101,'start':1}}
            def fixture_json(path):return json.loads(Path(path).read_text())
            with patch.object(engine,'BUILD',base),patch.object(engine,'selected',return_value=(proof,{},record)) as selected, \
                 patch.object(engine,'idle',return_value=True),patch.object(engine,'activity',return_value=nullcontext()), \
                 patch.object(engine,'root_json',side_effect=fixture_json),patch.object(engine,'write',wraps=engine.write) as write, \
                 patch.object(engine,'hold_export_uncertainty') as hold:
                class ShortStream(io.BytesIO):
                    def write(self, block):return 0
                with self.assertRaisesRegex(ValueError,'Export stream did not accept'):
                    engine.export_locked(project,identity,ShortStream())
                self.assertFalse((proof/'export-result.json').exists())
                class FlushFail(io.BytesIO):
                    def flush(self):raise BrokenPipeError('lost transport')
                with self.assertRaises(BrokenPipeError):
                    engine.export_locked(project,identity,FlushFail())
                self.assertFalse((proof/'export-result.json').exists())
                write.assert_not_called();hold.assert_not_called()
                first=io.BytesIO();self.assertEqual(engine.export_locked(project,identity,first)['bytes'],len(payload))
                self.assertEqual(first.getvalue(),payload)
                self.assertEqual(write.call_count,1)
                hold.assert_not_called()
                # The first stream's transport acknowledgement disappears.
                # A second root-owned caller may resend bytes, but publishes
                # no second receipt or stage/custody effect.
                record['owner']={'pid':202,'start':2};record['next_ordinal']=7
                retry=io.BytesIO();self.assertEqual(engine.export_locked(project,identity,retry),
                                                      json.loads((proof/'export-result.json').read_text()))
                self.assertEqual(retry.getvalue(),payload)
                self.assertEqual(write.call_count,1)
                hold.assert_not_called()
                self.assertEqual(selected.call_args_list[-1].args,(project,identity))
                self.assertEqual(record['state'],'SOURCE_VERIFIED')
                self.assertEqual(record['next_ordinal'],7)
                # A retained receipt for another reservation cannot authorize
                # even the first byte of another export.
                receipt_path=proof/'export-result.json';receipt_path.chmod(0o600)
                foreign=json.loads(receipt_path.read_text());foreign['reservation_id']='2'*32
                receipt_path.write_text(json.dumps(foreign))
                refused=io.BytesIO()
                with self.assertRaisesRegex(ValueError,'Retained export receipt'):
                    engine.export_locked(project,identity,refused)
                self.assertEqual(refused.getvalue(),b'')
                hold.assert_called_once_with(project,identity);hold.reset_mock()
                foreign['reservation_id']=identity;receipt_path.write_text(json.dumps(foreign))
                changed={**observed,'archive_sha256':'0'*64}
                (proof/'pack-observed.json').write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError,'Retained export receipt'):
                    engine.export_locked(project,identity,io.BytesIO())
                hold.assert_called_once_with(project,identity);hold.reset_mock()
                (proof/'pack-observed.json').write_text(json.dumps(observed))
                receipt_path.write_text('{')
                with self.assertRaises(json.JSONDecodeError):engine.export_locked(project,identity,io.BytesIO())
                hold.assert_called_once_with(project,identity);hold.reset_mock()
                receipt_path.write_text(json.dumps(foreign))
                archive.write_bytes(b'changed archive, same number byte')
                with self.assertRaisesRegex(ValueError,'Export differs'):
                    engine.export_locked(project,identity,io.BytesIO())
                hold.assert_called_once_with(project,identity);hold.reset_mock()
                record['state']='HELD'
                with self.assertRaisesRegex(ValueError,'Actual pack terminal'):
                    engine.export_locked(project,identity,io.BytesIO())
                hold.assert_not_called()

    def test_export_receipt_reader_refuses_nonroot_and_symlink(self):
        from lib.qualification_trust import control_engine as engine
        from lib.qualification_trust.control_observation import root_json
        project=self.claim()['project'];identity='1'*32
        observed={'schema':1,'project':project,'archive_sha256':'a'*64,'archive_bytes':1}
        with tempfile.TemporaryDirectory() as folder:
            proof=Path(folder);receipt=proof/'export-result.json'
            receipt.write_text(json.dumps({'schema':1,'reservation_id':identity,'project':project,
                                           'sha256':'a'*64,'bytes':1}))
            with self.assertRaises(ValueError):
                engine.prior_export_receipt(proof,project,identity,observed)
            receipt.unlink();receipt.symlink_to(proof/'other.json')
            with self.assertRaises(OSError):root_json(receipt)

    def test_uncertain_export_holds_only_exact_selected_custody(self):
        from lib.qualification_trust import control_engine as engine
        project=self.claim()['project'];identity='1'*32
        host=('438','0b99c4d2-ca5f-4471-bf84-ff4ab8dbefd3')
        record={'reservation_id':identity,'host':list(host),'session':{'project':project},
                'state':'SOURCE_VERIFIED','active':None,'next_ordinal':6}
        store=Mock();store.locked.return_value=nullcontext();store.read.return_value=record
        store.match.side_effect=r.Store.match
        with patch.object(engine.custody,'Store',return_value=store), \
             patch.object(engine.custody,'host_identity',return_value=host):
            engine.hold_export_uncertainty(project,identity)
            self.assertEqual(record['state'],'HELD')
            store.write.assert_called_once_with(record)
            store.write.reset_mock()
            with self.assertRaisesRegex(ValueError,'Selected export custody changed'):
                engine.hold_export_uncertainty(project,identity)
            store.write.assert_not_called()
            record['state']='SOURCE_VERIFIED'
            with self.assertRaisesRegex(ValueError,'Reservation/host identity differs'):
                engine.hold_export_uncertainty(project,'2'*32)
            store.write.assert_not_called()
