"""Portable -I -B standalone updater control; invoked in a child interpreter."""
import base64
from contextlib import contextmanager
import grp
import hashlib
import io
import json
from pathlib import Path
import pwd
import runpy
import subprocess
import sys
import tempfile
import types
from contextlib import redirect_stdout
from unittest.mock import patch

updater=Path(sys.argv[1])
helper=Path(sys.argv[2])
scope=runpy.run_path(str(updater),run_name='isolated_standalone_updater')
globals_=scope['validate_policy_assets'].__globals__
assert globals_['__package__'] in (None,'')
assert sys.flags.isolated and sys.dont_write_bytecode
mapping={'schema':'root-approved-ssh-mapping-v1','principal':'test@example.invalid',
         'username':'oxidex-uploader','uid':2000,'identity_receipt_sha256':'1'*64,
         'public_key':'ssh-ed25519 '+base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\x00 '+b'x'*32).decode()}
mapping['public_key_sha256']=hashlib.sha256(mapping['public_key'].encode()).hexdigest()
policy={
    '/etc/ssh/sshd_config.d/40-oxidex-uploader-fence.conf':
        b'Match User oxidex-uploader\n    ForceCommand /usr/local/sbin/oxidex-uploader-session\n'
        b'    DisableForwarding yes\n    PermitTTY no\nMatch all\n',
    '/etc/sudoers.d/oxidex-uploader-session':
        b'oxidex-uploader ALL=(root) NOPASSWD: /usr/local/sbin/oxidex-uploader-session\n'}
assets={path:{'data':base64.b64encode(policy.get(path,b'fixture')).decode(),
              'sha256':hashlib.sha256(policy.get(path,b'fixture')).hexdigest()}
        for path in globals_['TARGETS']}
helper_raw=helper.read_bytes()
def root_read(path):
    if str(path)=='/opt/gha-runners/builder_access.py':
        return ({'sha256':hashlib.sha256(helper_raw).hexdigest(),'mode':0o644,
                 'uid':0,'gid':0,'device':1,'inode':1},helper_raw)
    if str(path)=='/etc/oxidex-builder-uploader.json':
        raw=json.dumps(mapping).encode()
        return {'mode':0o600,'sha256':hashlib.sha256(raw).hexdigest()},raw
    raise AssertionError('Unexpected trusted read: '+str(path))
globals_['root_file_read']=root_read
globals_['root_file_state']=lambda path:{'sha256':assets[path]['sha256'],
                                          'mode':globals_['POLICY_TARGETS'][path]}
@contextmanager
def selected_ancestor(path,*,authenticate=False):
    assert str(path)=='/opt/gha-runners/builder_access.py' and authenticate
    yield lambda:None
@contextmanager
def selected_file(path,state,read_state):
    assert str(path)=='/opt/gha-runners/builder_access.py'
    yield lambda:None
globals_['retained_ancestor_selection']=selected_ancestor
globals_['retained_file_selection']=selected_file
globals_['checked_command']=lambda argv,**kw: (
    'forcecommand /usr/local/sbin/oxidex-uploader-session\n'
    'disableforwarding yes\npermittty no\n') if '-T' in argv else ''
account=types.SimpleNamespace(pw_name='oxidex-uploader',pw_uid=2000,pw_gid=1001)
group=types.SimpleNamespace(gr_name='oxidex-build',gr_gid=1001,gr_mem=[])
sudo=types.SimpleNamespace(stderr='',returncode=0,stdout=(
    'Sudoers entry:\nRunAsUsers: root\nRunAsGroups: root\nOptions: !authenticate\n'
    'Commands: /usr/local/sbin/oxidex-uploader-session\n'))
with tempfile.TemporaryDirectory() as poison:
    (Path(poison)/'builder_access.py').write_text('raise RuntimeError("poisoned import")\n')
    sys.path.insert(0,poison)
    try:
        with patch.object(pwd,'getpwnam',return_value=account),\
     patch.object(pwd,'getpwall',return_value=[account]),\
     patch.object(grp,'getgrall',return_value=[group]),\
     patch.object(Path,'iterdir',return_value=[]),\
     patch.object(subprocess,'run',return_value=sudo):
            # The capture, apply, and policy recovery paths all call this validator.
            for path in ('capture','update','recovery'):
                assert globals_['validate_policy_assets'](assets)['uid']==2000,path
            assert globals_['observed_work_processes']()==[]
            globals_['validate_installed_policy'](assets,mapping)
            asset_raw=json.dumps(assets).encode()
            plan_raw=b'{"selected":"plan"}'
            asset_sha=hashlib.sha256(asset_raw).hexdigest()
            plan_sha=hashlib.sha256(plan_raw).hexdigest()
            def cli_read(path):
                if str(path)=='/synthetic/assets.json':
                    return {'sha256':asset_sha},asset_raw
                if str(path)=='/synthetic/plan.json':
                    return {'sha256':plan_sha},plan_raw
                return root_read(path)
            def selected_action(plan):
                assert plan=={'selected':'plan'}
                assert globals_['validate_policy_assets'](assets)['uid']==2000
                return {'status':'SELECTED'}
            replacements={'root_file_read':cli_read,'validate_plan':lambda *args:None,
                          'live_snapshot':lambda plan:{'drain_token':None},
                          'plan_from_snapshot':lambda *args,**kw:{'capture':True},
                          'check_snapshot':lambda *args,**kw:None,
                          'apply_update':selected_action,
                          'recover_policy_update':selected_action}
            cases=[(['--capture-assets','/synthetic/assets.json','--assets-sha256',asset_sha,
                     '--source-main','a'*40],{'capture':True}),
                   (['--plan','/synthetic/plan.json','--plan-sha256',plan_sha,'--apply'],
                    {'status':'SELECTED'}),
                   (['--plan','/synthetic/plan.json','--plan-sha256',plan_sha,'--recover-policy'],
                    {'status':'SELECTED'})]
            with patch.dict(globals_,replacements),patch.object(sys,'platform','linux'),\
                 patch.object(globals_['os'],'geteuid',return_value=0):
                for arguments,expected in cases:
                    with patch.object(sys,'argv',[str(updater),*arguments]),redirect_stdout(io.StringIO()) as output:
                        globals_['main']()
                    assert json.loads(output.getvalue())==expected
    finally:sys.path.remove(poison)
# A root-owned fixed-path identity is mandatory before any helper bytes run.
saved=root_read
globals_['root_file_read']=lambda path:({**saved(path)[0],'mode':0o666},saved(path)[1])
try:
    globals_['trusted_builder_access']()
except ValueError as error:
    assert 'Trusted installed builder access asset unavailable' in str(error)
else:
    raise AssertionError('Untrusted helper mode accepted')
print('isolated standalone policy capture/update/recovery, process and installed checks: PASS')
