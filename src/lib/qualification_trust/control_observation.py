"""Actual read-only binding and quiescence producers; unknown state holds."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from contextlib import ExitStack


def command(argv):
    result=subprocess.run(argv,check=True,capture_output=True,text=True,timeout=10)
    if result.stderr:
        raise ValueError('Control observation has diagnostics')
    return result.stdout


def root_json(path, maximum=65536):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o022 or info.st_size>maximum:
            raise ValueError('Untrusted host control state')
        from .reservation import pairs
        raw=os.read(fd,maximum+1)
        if len(raw)!=info.st_size:raise ValueError('Host control state changed')
        return json.loads(raw,object_pairs_hook=pairs)
    finally:os.close(fd)


def sealed_binding(root, session):
    """Join actual immutable host seal to the originally signed source claim."""
    from .launcher_contract import load_binding
    from .host_gate import regular_bytes
    import hashlib
    project=session['project']
    if not re.fullmatch('qualification-[0-9a-f]{12}-[0-9a-f]{32}',project):
        raise ValueError('Malformed reserved namespace')
    sealed=Path(root)/'qualification-sealed'/project
    binding=load_binding(sealed,project)
    facts=binding.get('source_identity')
    if not isinstance(facts,dict):raise ValueError('Actual source facts unavailable')
    document=regular_bytes(sealed/'incoming/source-attestation/document.json',65536)
    actual={'head':binding.get('head'),'tree':facts.get('tree'),
            'bundle_sha256':binding.get('bundle_sha256'),
            'attestation_sha256':hashlib.sha256(document).hexdigest()}
    if any(actual[key]!=session[key] for key in actual):
        raise ValueError('Actual sealed source differs from reserved claim')
    # Current source gate is Task19 only. Never mislabel it Task20 admission.
    if session.get('profile')=='task20-v1' and binding.get('profile')!='task20-v1':
        raise ValueError('Task20 full-source profile not installed')
    return actual


def quiescent(project, *, own_unit=None):
    """Observe complete host state under caller's global EX/SH fence.

    own_unit must be independently derived from the actual control process,
    not a request field. This producer neither clears leases nor stops work.
    """
    if not re.fullmatch('qualification-[0-9a-f]{12}-[0-9a-f]{32}',project):
        raise ValueError('Malformed owned namespace')
    config=root_json('/etc/oxidex-remote-build.json')
    if config.get('role')!='builder':raise ValueError('Dedicated zero-runner host required')
    ids=command(['/usr/bin/docker','ps','-aq','--no-trunc']).split()
    if ids:raise ValueError('Actual container transaction remains; custody retained')
    if root_json('/var/lib/gha-admission/leases.json')!={}:
        raise ValueError('Actual admission leases remain; never clear them')
    units=command(['/usr/bin/systemctl','list-units','--type=service',
                   '--state=active,activating,deactivating','--no-legend','--plain','--no-pager'])
    for row in units.splitlines():
        parts=row.split()
        if not parts:continue
        if parts[0].startswith(('oxidex-build-','oxidex-reservation-')) and parts[0]!=own_unit:
            raise ValueError('Actual owned or foreign build unit remains')
    # Root record itself cannot attest that a transfer or cache owner finished.
    # Acquire only existing canonical locks, never create/reset a lock or state.
    with ExitStack() as locks:
        for name in ('oxidex-cache-active-'+project+'.lock','oxidex-qualification-'+project+'.lock'):
            try:fd=os.open('/run/'+name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            except FileNotFoundError:continue
            locks.callback(os.close,fd)
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o022:
                raise ValueError('Untrusted canonical activity lock')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Production fixed receiver/export enrollment is not complete yet.
        # Do not return True from incomplete observations or infer no uploads.
        raise ValueError('Upload/export process and lock enrollment incomplete')
