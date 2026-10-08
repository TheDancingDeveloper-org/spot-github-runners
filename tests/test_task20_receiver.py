"""Tiny synthetic archives only; no application input, VM or live reservation."""
import hashlib
import io
import gzip
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from lib.qualification_trust.task20_receiver import MEMBERS,receive


class ReceiverTests(unittest.TestCase):
    def packet(self, extra=None):
        data={name:('fixture '+name).encode() for name in MEMBERS}
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w:gz') as archive:
            for name,content in data.items():
                member=tarfile.TarInfo(name);member.size=len(content)
                archive.addfile(member,io.BytesIO(content))
            if extra is not None:archive.addfile(extra,io.BytesIO(b'x'*extra.size))
        value=raw.getvalue()
        return value,{name:hashlib.sha256(content).hexdigest() for name,content in data.items()}

    def call(self, stream, folder, value, hashes, **changes):
        return receive(stream, folder, **{**dict(archive_sha256=hashlib.sha256(value).hexdigest(),
                            archive_bytes=len(value),member_hashes=hashes),**changes})

    def test_complete_packet_records_actual_bytes_without_source_verification_claim(self):
        value,hashes=self.packet()
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder).resolve()/'receive'
            result=self.call(io.BytesIO(value),output,value,hashes)
            self.assertEqual(result['archive_bytes'],len(value))
            self.assertEqual(set(result['members']),MEMBERS)
            self.assertFalse(result['source_verified'])
            with self.assertRaises(FileExistsError):self.call(io.BytesIO(value),output,value,hashes)

    def test_interrupted_and_changed_transport_retain_evidence(self):
        value,hashes=self.packet()
        for actual in (value[:-10],value+b'extra',value[:-1]+b'x'):
            with tempfile.TemporaryDirectory() as folder:
                output=Path(folder).resolve()/'receive'
                with self.assertRaises(ValueError):self.call(io.BytesIO(actual),output,value,hashes)
                self.assertTrue((output/'source.tar.gz').exists())
                self.assertFalse((output/'receive-result.json').exists())

    def test_extra_duplicate_and_link_members_refused(self):
        for name,kind in [('../escape',tarfile.REGTYPE),('repository.bundle',tarfile.REGTYPE),
                          ('unexpected',tarfile.SYMTYPE)]:
            member=tarfile.TarInfo(name);member.type=kind
            if kind==tarfile.SYMTYPE:member.linkname='/etc/passwd'
            value,hashes=self.packet(member)
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(ValueError):
                    self.call(io.BytesIO(value),Path(folder).resolve()/'receive',value,hashes)

    def test_member_hash_and_unbounded_size_are_not_accepted(self):
        value,hashes=self.packet();hashes['repository.bundle']='0'*64
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                self.call(io.BytesIO(value),Path(folder).resolve()/'receive',value,hashes)
            with self.assertRaises(ValueError):
                self.call(io.BytesIO(value),Path(folder).resolve()/'large',value,hashes,archive_bytes=10**100)

    def test_raw_extended_metadata_rejected_before_body_allocation(self):
        _,hashes=self.packet()
        header=tarfile.TarInfo('metadata');header.type=tarfile.XHDTYPE
        header.size=2*1024*1024*1024
        value=gzip.compress(header.tobuf(format=tarfile.USTAR_FORMAT)+b'\0'*1024)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'Raw PAX'):
                self.call(io.BytesIO(value),Path(folder).resolve()/'receive',value,hashes)
