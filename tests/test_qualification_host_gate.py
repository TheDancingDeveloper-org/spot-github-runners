import tempfile
import unittest
from pathlib import Path
from lib.qualification_trust.host_gate import validate_identity,copy_sealed,regular_bytes

class HostGateTests(unittest.TestCase):
    def test_wrong_run_or_short_identity_refuses(self):
        head='a'*40
        validate_identity(head,'b'*64,'qualification-'+head[:12]+'-'+'c'*32)
        for run in ('other','qualification-'+'d'*12+'-'+'c'*32):
            with self.assertRaises(ValueError):validate_identity(head,'b'*64,run)
        with self.assertRaises(ValueError):validate_identity('a'*12,'b'*64,'other')
    def test_symlink_upload_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();(root/'source').mkdir();(root/'source/link').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):copy_sealed(root/'source',root/'sealed')
    def test_copy_is_separate_readonly_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();(root/'source').mkdir();(root/'source/x').write_bytes(b'original')
            copy_sealed(root/'source',root/'sealed');(root/'source/x').write_bytes(b'changed')
            self.assertEqual(regular_bytes(root/'sealed/x'),b'original')
            self.assertEqual((root/'sealed/x').stat().st_mode&0o777,0o444)

    def test_unbound_and_alternate_python_execution_refuses(self):
        from lib.qualification_trust.host_gate import execution_arguments
        head='a'*40;run='qualification-'+head[:12]+'-'+'c'*32
        binding=dict(head=head,run_id=run,bundle_sha256='b'*64,uploaded_signers_sha256='d'*64)
        for argv in (['python3','-c','print(1)'],['cargo','build'],['python3','/src/qualification_source.py'],['python3','/src/qualification_bootstrap.py','--head','e'*40]):
            with self.assertRaises(ValueError):execution_arguments(argv,binding)
        allowed=['python3','/target/checkout/tools/remote-build/qualification.py','--remote-run','--output','/target/ops/evidence/remote-qualification/'+run,'--expected-head',head]
        self.assertEqual(execution_arguments(allowed,binding),allowed)
