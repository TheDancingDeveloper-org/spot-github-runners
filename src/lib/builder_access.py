"""Startup enrollment from an explicit root-approved public SSH mapping.

The principal mapping is an authenticated deployment input, never inferred
from getent, client environment or the key comment. No private keys are read.
"""
import base64
import hashlib
import json
import os
import pwd
import grp
import re
import stat
import subprocess
import sys
from pathlib import Path


def canonical_sha(record):
    return hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def approved_mapping(record):
    fields={'schema','principal','username','uid','identity_receipt_sha256','public_key','public_key_sha256'}
    if not isinstance(record,dict) or set(record)!=fields or record['schema']!='root-approved-ssh-mapping-v1':
        raise ValueError('Explicit root-approved SSH mapping required')
    if (not isinstance(record['principal'],str) or not re.fullmatch(r'[A-Za-z0-9_.+@-]{3,254}',record['principal'])
        or not isinstance(record['username'],str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',record['username'])
        or type(record['uid']) is not int or not 1000<=record['uid']<=2147483647 or record['uid']==1001
        or not isinstance(record['identity_receipt_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',record['identity_receipt_sha256'])):
        raise ValueError('Unsafe or unauthenticated uploader identity')
    key=record['public_key']
    if not isinstance(key,str) or len(key)>4096 or '\n' in key or '\r' in key:
        raise ValueError('One bounded public SSH key required')
    parts=key.split()
    if len(parts) not in (2,3) or parts[0]!='ssh-ed25519':raise ValueError('Explicit Ed25519 public key required')
    if len(parts)==3 and not re.fullmatch('[A-Za-z0-9_.+@-]{1,128}',parts[2]):raise ValueError('Unsafe public key comment')
    try:binary=base64.b64decode(parts[1],validate=True)
    except ValueError as error:raise ValueError('Malformed public SSH key') from error
    if (len(binary)!=51 or binary[:19]!=b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'
        or record['public_key_sha256']!=hashlib.sha256(key.encode()).hexdigest()):
        raise ValueError('Public SSH key bytes differ from approved mapping')
    return record


def uploader_policy_bytes(record):
    """Fixed host policy derived only from the root-approved mapping."""
    username=approved_mapping(record)['username']
    return {
        '/etc/ssh/sshd_config.d/40-oxidex-uploader-fence.conf':
            ('Match User '+username+'\n    ForceCommand /usr/local/sbin/oxidex-uploader-session\n'
             '    DisableForwarding yes\n    PermitTTY no\nMatch all\n').encode(),
        '/etc/sudoers.d/oxidex-uploader-session':
            (username+' ALL=(root) NOPASSWD: /usr/local/sbin/oxidex-uploader-session\n').encode(),
    }


def uploader_enrollment_plan(record,account,group):
    approved_mapping(record)
    if group!={'name':'oxidex-build','gid':1001}:raise ValueError('Restricted build group collision')
    if account is not None:
        if (account.get('principal')!=record['principal'] or account.get('username')!=record['username']
            or account.get('uid')!=record['uid'] or not isinstance(account.get('groups'),list)
            or any(name in account['groups'] for name in ('docker','sudo','wheel','admin'))):
            raise ValueError('Uploader account/mapping/privileges differ')
    return dict(username=record['username'],uid=record['uid'],supplementary_groups=['oxidex-build'],
                sudo_commands=['/usr/local/bin/oxidex-remote-build'],create_account=account is None)


def restricted_group_guard(record,accounts=None,groups=None):
    """Only the explicitly approved uploader may hold host GID1001 access."""
    approved_mapping(record)
    accounts=pwd.getpwall() if accounts is None else accounts
    groups=grp.getgrall() if groups is None else groups
    matching=[g for g in groups if g.gr_gid==1001 or g.gr_name=='oxidex-build']
    if len(matching)!=1 or matching[0].gr_gid!=1001 or matching[0].gr_name!='oxidex-build':
        raise ValueError('Restricted build group collision')
    group=matching[0];members=set(group.gr_mem)
    names=set();uids=set()
    for account in accounts:
        if account.pw_name in names or account.pw_uid in uids:raise ValueError('Ambiguous local account inventory')
        names.add(account.pw_name);uids.add(account.pw_uid)
        if account.pw_uid==1001:raise ValueError('Host account collides with container build UID')
        if account.pw_gid==1001:members.add(account.pw_name)
        if account.pw_name==record['username'] and account.pw_uid!=record['uid']:
            raise ValueError('Approved uploader UID mismatch')
        if account.pw_uid==record['uid'] and account.pw_name!=record['username']:
            raise ValueError('Approved uploader UID collision')
    if members-set([record['username']]) or not members<=names:
        raise ValueError('Unapproved restricted build group members')
    if record['username'] in members and not any(a.pw_name==record['username'] and a.pw_uid==record['uid'] for a in accounts):
        raise ValueError('Restricted uploader membership mismatch')
    return {'group':'oxidex-build','gid':1001,'approved_member':record['username'],'members':sorted(members)}


def nofollow_root_json(path,expected_sha=None):
    """Open every absolute ancestor by descriptor; require root/nonwritable."""
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('Absolute deployment path required')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child;info=os.fstat(fd)
            if info.st_uid!=0 or info.st_mode&0o022:raise ValueError('Untrusted deployment ancestor')
        child=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            info=os.fstat(child)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or info.st_size>262144:
                raise ValueError('Untrusted bounded deployment record')
            with os.fdopen(os.dup(child),'rb') as stream:raw=stream.read(262145)
            if len(raw)>262144 or (expected_sha is not None and hashlib.sha256(raw).hexdigest()!=expected_sha):
                raise ValueError('Deployment record hash/size differs')
            return json.loads(raw)
        finally:os.close(child)
    finally:os.close(fd)


def effective_sudo_guard(username, *, require_uploader_session=False):
    """Read effective policy, including per-user/google-sudoers grants.

    Sudo's verbose C-locale format may grant only the fixed launcher and
    uploader supervisor.
    Unknown output/options/RunAs identities fail closed; no command is executed.
    """
    result=subprocess.run(['/usr/bin/sudo','-n','-ll','-U',username],capture_output=True,text=True,
        timeout=10,env={**os.environ,'LC_ALL':'C','LANG':'C'})
    if result.stderr or result.returncode not in (0,1):raise ValueError('Unavailable effective sudo policy')
    if result.returncode==1:
        if not re.fullmatch(r'User '+re.escape(username)+r' is not allowed to run sudo on [A-Za-z0-9_.-]+\.\s*',result.stdout):
            raise ValueError('Unknown sudo denial output')
        if require_uploader_session:raise ValueError('Uploader sudo grant is absent')
        return
    entries=result.stdout.split('Sudoers entry:')[1:]
    if not entries:raise ValueError('Unknown effective sudo policy format')
    observed=set()
    for entry in entries:
        if entry.count('Commands:')!=1:raise ValueError('Unknown sudo command scope')
        metadata,commands=entry.split('Commands:')
        lines=[line.strip() for line in metadata.splitlines() if line.strip()]
        # Debian13 sudo prefixes the entry with its exact source filename.
        if lines and lines[0] in ('/etc/sudoers.d/oxidex-build',
                                  '/etc/sudoers.d/oxidex-uploader-session'):lines.pop(0)
        pairs=[line.split(':',1) for line in lines]
        if any(len(pair)!=2 for pair in pairs) or len({pair[0] for pair in pairs})!=len(pairs):
            raise ValueError('Unknown or duplicate effective sudo fields')
        fields=dict(pairs)
        commands_seen=[line.strip() for line in commands.splitlines() if line.strip()]
        if (set(fields)-{'RunAsUsers','RunAsGroups','Options'} or fields.get('RunAsUsers','').strip()!='root'
            or fields.get('RunAsGroups','root').strip()!='root'
            or fields.get('Options','!authenticate').strip() not in ('!authenticate','authenticate')
            or commands_seen not in
               (['/usr/local/bin/oxidex-remote-build'],
                ['/usr/local/sbin/oxidex-uploader-session'])):
            raise ValueError('Uploader has broader effective sudo privileges')
        if commands_seen[0] in observed:raise ValueError('Duplicate effective sudo grant')
        observed.add(commands_seen[0])
    if require_uploader_session and '/usr/local/sbin/oxidex-uploader-session' not in observed:
        raise ValueError('Uploader sudo grant is absent')


def enroll(record,expected_sha):
    if os.geteuid()!=0:raise ValueError('Root deployment enrollment required')
    approved_mapping(record)
    if canonical_sha(record)!=expected_sha:raise ValueError('Startup mapping differs from root-approved input')
    try:group=grp.getgrgid(1001)
    except KeyError:raise ValueError('Restricted startup group absent')
    restricted_group_guard(record)
    group_row=dict(name=group.gr_name,gid=group.gr_gid)
    try:account=pwd.getpwnam(record['username'])
    except KeyError:account=None
    try:by_uid=pwd.getpwuid(record['uid'])
    except KeyError:by_uid=None
    if (by_uid is not None and (account is None or by_uid.pw_name!=account.pw_name)):
        raise ValueError('Approved uploader UID collision')
    row=None if account is None else dict(principal=record['principal'],username=account.pw_name,uid=account.pw_uid,
        groups=[grp.getgrgid(gid).gr_name for gid in os.getgrouplist(account.pw_name,account.pw_gid)])
    plan=uploader_enrollment_plan(record,row,group_row)
    if account is not None:effective_sudo_guard(record['username'])
    if account is None:
        subprocess.run(['/usr/sbin/useradd','--create-home','--uid',str(record['uid']),
            '--shell','/bin/bash','--groups','oxidex-build',record['username']],check=True,timeout=30)
        account=pwd.getpwnam(record['username'])
    if account.pw_uid!=record['uid'] or account.pw_dir!='/home/'+record['username']:
        raise ValueError('Provisioned account differs from approved mapping')
    parent=os.open('/home',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        parent_info=os.fstat(parent)
        if parent_info.st_uid!=0 or parent_info.st_mode&0o022:raise ValueError('Unsafe home ancestor')
        home=os.open(record['username'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
    finally:os.close(parent)
    try:
        info=os.fstat(home)
        if info.st_uid!=record['uid'] or info.st_mode&0o022:raise ValueError('Unsafe uploader home')
        try:os.mkdir('.ssh',0o700,dir_fd=home)
        except FileExistsError:
            # An existing directory is accepted only after the nofollow ownership/mode checks below.
            pass
        directory=os.open('.ssh',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=home)
        try:
            info=os.fstat(directory)
            if info.st_uid not in (0,record['uid']) or info.st_mode&0o077:raise ValueError('Unsafe SSH key directory')
            raw=(record['public_key']+'\n').encode()
            # Never follow swapped ancestors or overwrite existing authorized keys.
            try:fd=os.open('authorized_keys',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            except FileExistsError:
                fd=os.open('authorized_keys',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
                try:
                    info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid!=record['uid'] or info.st_mode&0o077 or os.read(fd,8192)!=raw:
                        raise ValueError('Existing key mapping differs')
                finally:os.close(fd)
            else:
                with os.fdopen(fd,'wb') as output:
                    os.fchown(output.fileno(),record['uid'],account.pw_gid);output.write(raw);output.flush();os.fsync(output.fileno())
            os.fchown(directory,record['uid'],account.pw_gid)
            os.fsync(directory)
        finally:os.close(directory)
    finally:os.close(home)
    if not plan['create_account']:
        subprocess.run(['/usr/sbin/usermod','--append','--groups','oxidex-build',record['username']],check=True,timeout=30)
    restricted_group_guard(record)
    effective_sudo_guard(record['username'])
    return dict(schema='builder-uploader-observed-v1',principal=record['principal'],username=record['username'],
        uid=record['uid'],mapping_sha256=expected_sha,public_key_sha256=record['public_key_sha256'],
        identity_receipt_sha256=record['identity_receipt_sha256'],groups=plan['supplementary_groups'])


if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit('Root mapping file and startup-bound canonical digest required')
    print(json.dumps(enroll(nofollow_root_json(sys.argv[1]),sys.argv[2]),sort_keys=True))
