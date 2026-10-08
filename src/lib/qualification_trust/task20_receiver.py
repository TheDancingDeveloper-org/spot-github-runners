"""Bounded root-supervised archive intake; never execute or overwrite uploads."""
import hashlib
import gzip
import json
import os
from pathlib import Path
import re
import tarfile

MEMBERS = frozenset({'reference/approved-linux-perl.json', 'repository.bundle',
    'maintainer.allowed_signers', 'rust-toolchain.toml', 'task20_bootstrap.py',
    'qualification_source.py', 'task20-round.json',
    *('source-attestation/'+name for name in ('document.json', 'document.sig',
      'github-commit.json', 'pgp-proof.txt', 'integration-ref.json'))})
MAX_ARCHIVE = 256*1024*1024
MAX_MEMBER = 256*1024*1024
MAX_TOTAL = 2*1024*1024*1024


def receive(stream, directory, *, archive_sha256, archive_bytes, member_hashes):
    """Caller owns reservation/global lock; directory is exclusively new.

    Expected hash/size/member hashes come from the root-reviewed descriptor,
    never an archive member or uploader-provided environment. Failed raw bytes
    and partial extracted inputs remain owned evidence for explicit recovery.
    """
    if (not isinstance(archive_sha256, str) or not re.fullmatch('[0-9a-f]{64}', archive_sha256)
            or type(archive_bytes) is not int or not 0 < archive_bytes <= MAX_ARCHIVE
            or not isinstance(member_hashes, dict) or set(member_hashes) != MEMBERS
            or any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value)
                   for value in member_hashes.values())):
        raise ValueError('Root-reviewed bounded archive and twelve member hashes required')
    directory = Path(directory)
    if directory != directory.resolve() or any(path.is_symlink() for path in directory.parents):
        raise ValueError('Canonical root-controlled receiver directory required')
    directory.mkdir(mode=0o700, exist_ok=False)
    archive = directory/'source.tar.gz'
    value = hashlib.sha256(); remaining = archive_bytes
    with archive.open('xb') as output:
        while remaining:
            block = stream.read(min(1024*1024, remaining))
            if not isinstance(block, bytes) or not block:
                raise ValueError('Interrupted source archive; partial evidence retained')
            if len(block) > remaining:
                raise ValueError('Archive transport exceeded declared byte count')
            output.write(block); value.update(block); remaining -= len(block)
        output.flush(); os.fsync(output.fileno())
    if stream.read(1) != b'':
        raise ValueError('Archive transport has trailing bytes')
    if value.hexdigest() != archive_sha256:
        raise ValueError('Actual source archive digest differs')
    incoming = directory/'incoming'; incoming.mkdir(mode=0o700)
    seen = {}; total = 0
    # Inspect each raw header before tarfile can allocate extended metadata.
    # Canonical sender uses short USTAR regular names; PAX/GNU is unnecessary.
    with gzip.open(archive, 'rb') as source:
        while True:
            header = source.read(512)
            if len(header) != 512:
                raise ValueError('Truncated raw tar header')
            if header == b'\0'*512:
                if source.read(512) != b'\0'*512:
                    raise ValueError('Two canonical tar end blocks required')
                tail = source.read(10241)
                if len(tail) > 10240 or tail.strip(b'\0'):
                    raise ValueError('Unbounded or nonzero trailing tar data')
                break
            if header[156:157] not in (b'0',b'\0'):
                raise ValueError('Raw PAX/GNU/link/special header forbidden')
            member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
            if member.name not in MEMBERS or member.name in seen or not member.isfile() or member.linkname:
                raise ValueError('Unexpected, duplicate, linked or special archive member')
            if type(member.size) is not int or not 0 <= member.size <= MAX_MEMBER:
                raise ValueError('Archive member byte bound exceeded')
            total += member.size
            if total > MAX_TOTAL:
                raise ValueError('Expanded archive byte bound exceeded')
            # Member names came from the fixed closed set, not arbitrary paths.
            target = incoming/member.name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest = hashlib.sha256(); left = member.size
            with target.open('xb') as output:
                while left:
                    block = source.read(min(1024*1024,left))
                    if not block:
                        raise ValueError('Truncated source member')
                    output.write(block); digest.update(block); left -= len(block)
                output.flush(); os.fsync(output.fileno())
            padding = (-member.size)%512
            if padding and source.read(padding) != b'\0'*padding:
                raise ValueError('Noncanonical or truncated tar member padding')
            if digest.hexdigest() != member_hashes[member.name]:
                raise ValueError('Actual source member differs from reviewed packet')
            target.chmod(0o400)
            seen[member.name] = {'bytes':member.size,'sha256':digest.hexdigest()}
    if set(seen) != MEMBERS:
        raise ValueError('Complete twelve-member Task20 packet required')
    receipt = {'schema':1,'profile':'task20-v1','archive_sha256':value.hexdigest(),
               'archive_bytes':archive_bytes,'expanded_bytes':total,'members':seen,
               'source_verified':False}
    with (directory/'receive-result.json').open('x') as output:
        json.dump(receipt, output, sort_keys=True); output.flush(); os.fsync(output.fileno())
    fd = os.open(directory, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
    return receipt
