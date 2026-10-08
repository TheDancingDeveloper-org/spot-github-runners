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
replacement=Path(sys.argv[3])
initial_mode=int(sys.argv[4],8)
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
uploaded=b'raise RuntimeError("uploaded Python executed")\n'
assets={path:{'data':base64.b64encode(policy.get(path,uploaded)).decode(),
              'sha256':hashlib.sha256(policy.get(path,uploaded)).hexdigest()}
        for path in globals_['TARGETS']}
helper_raw=helper.read_bytes()
selected_helper=[helper_raw]
selected_state={'mode':initial_mode,'uid':0,'gid':0}
legacy=hashlib.sha256(helper_raw).hexdigest()==('463b7887505c9593d400ed8534b7a792038129690b7a4ad08bc5649533022e36')
if helper.name=='builder_access_15asset.py':
    assert legacy, 'Retained installed helper bytes changed'
if legacy:
    assert b'def uploader_policy_bytes' not in helper_raw
assert len(globals_['TARGETS'])==25
assert globals_['TARGETS'][globals_['QUALIFICATION']+'control_fault.py']==0o644
assert hashlib.sha256(helper_raw).hexdigest() in globals_['TRUSTED_BUILDER_ACCESS_SHA256']
def root_read(path):
    if str(path)=='/opt/gha-runners/builder_access.py':
        raw=selected_helper[0]
        return ({'sha256':hashlib.sha256(raw).hexdigest(),
                 **selected_state,'device':1,'inode':1},raw)
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
            group.gr_mem=['foreign']
            try:
                globals_['validate_policy_assets'](assets)
            except ValueError as error:
                assert 'Unapproved restricted build group members' in str(error)
            else:
                raise AssertionError('Foreign group member accepted')
            group.gr_mem=[]
            wrong=types.SimpleNamespace(pw_name=account.pw_name,pw_uid=2001,pw_gid=1001)
            with patch.object(pwd,'getpwnam',return_value=wrong):
                try:globals_['validate_policy_assets'](assets)
                except ValueError as error:assert 'Enrolled uploader account changed' in str(error)
                else:raise AssertionError('Wrong NSS UID accepted')
            target=next(iter(policy))
            original=assets[target]
            broadened=b'PermitTTY yes\n'
            assets[target]={'data':base64.b64encode(broadened).decode(),
                            'sha256':hashlib.sha256(broadened).hexdigest()}
            try:globals_['validate_policy_assets'](assets)
            except ValueError as error:assert 'Uploaded policy differs' in str(error)
            else:raise AssertionError('Broadened policy accepted')
            assets[target]=original
            assert globals_['observed_work_processes']()==[]
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
                          'live_snapshot':lambda plan,**kw:{'drain_token':None},
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
            # The held install replaces the helper only after these pre-update
            # decisions. Post-install daemon validation then uses the candidate.
            replacement_raw=replacement.read_bytes()
            replacement_sha=hashlib.sha256(replacement_raw).hexdigest()
            assert replacement_sha not in globals_['TRUSTED_BUILDER_ACCESS_SHA256']
            helper_target='/opt/gha-runners/builder_access.py'
            assets[helper_target]={'data':base64.b64encode(replacement_raw).decode(),
                                   'sha256':replacement_sha}
            selected_helper[0]=replacement_raw
            selected_state['mode']=0o644
            globals_['validate_installed_policy'](assets,mapping)
            assert globals_['validate_policy_assets'](assets,installed=True)['uid']==2000
            assets[helper_target]['sha256']='0'*64
            try:globals_['validate_installed_policy'](assets,mapping)
            except ValueError as error:assert 'Runtime asset differs from reviewed bytes' in str(error)
            else:raise AssertionError('Wrong selected asset hash accepted')
            assets[helper_target]['sha256']=replacement_sha
    finally:sys.path.remove(poison)
# Every accepted pre-update mode is exact; post-install state stays 0644.
files={path:{'sha256':row['sha256'],'mode':globals_['TARGETS'][path]}
       for path,row in assets.items()}
files[globals_['LAUNCHER_TARGET']]['mode']=0o644
pin=assets[globals_['LAUNCHER_TARGET']]['sha256']
globals_['verify_installed_runtime'](assets,files,client_launcher_sha256=pin)
files['/opt/gha-runners/builder_access.py']['mode']=0o600
try:globals_['verify_installed_runtime'](assets,files,client_launcher_sha256=pin)
except ValueError as error:assert 'Installed runtime bytes or held mode differ' in str(error)
else:raise AssertionError('Post-install 0600 helper accepted')

def refused(label):
    try:globals_['trusted_builder_access'](selected_sha256=replacement_sha)
    except ValueError as error:
        assert 'Trusted installed builder access asset unavailable' in str(error),label
    else:raise AssertionError('Untrusted helper accepted: '+label)
for field,value in (('uid',1001),('gid',1001),('mode',0o666),('mode',0o640),
                    ('mode',0o700),('mode',0o777),('mode',0o600)):
    previous=selected_state[field];selected_state[field]=value
    try:refused(field+'='+oct(value))
    finally:selected_state[field]=previous
selected_helper[0]=b'raise RuntimeError("unknown root code executed")\n'
try:refused('unknown bytes')
finally:selected_helper[0]=replacement_raw
try:globals_['trusted_builder_access'](selected_sha256='0'*64)
except ValueError as error:assert 'Trusted installed builder access asset unavailable' in str(error)
else:raise AssertionError('Wrong selected digest accepted')
try:globals_['trusted_builder_access']()
except ValueError as error:assert 'Trusted installed builder access asset unavailable' in str(error)
else:raise AssertionError('Unknown replacement accepted before selected phase')
@contextmanager
def red_ancestor(path,*,authenticate=False):
    assert authenticate
    raise ValueError('Untrusted selected ancestor')
    yield
with patch.dict(globals_,{'retained_ancestor_selection':red_ancestor}):
    try:globals_['trusted_builder_access'](selected_sha256=replacement_sha)
    except ValueError as error:assert 'Untrusted selected ancestor' in str(error)
    else:raise AssertionError('Writable or symlinked parent accepted')
@contextmanager
def red_file(path,state,read_state):
    raise ValueError('Selected file custody changed')
    yield
with patch.dict(globals_,{'retained_file_selection':red_file}):
    try:globals_['trusted_builder_access'](selected_sha256=replacement_sha)
    except ValueError as error:assert 'Selected file custody changed' in str(error)
    else:raise AssertionError('Changed selected file accepted')
print('isolated standalone policy capture/update/recovery, process and installed checks: PASS')
