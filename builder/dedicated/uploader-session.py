#!/usr/bin/python3
"""Keep every legacy uploader SSH command under the root reservation fence."""
import fcntl
import os
import pwd
import subprocess
import sys

SELF='/usr/local/sbin/oxidex-uploader-session'


def main():
    if os.geteuid()!=0:
        # SSH forced command executes as the enrolled uploader. No shell
        # interpolation: sudo receives original text as one inert argument.
        command=os.environ.get('SSH_ORIGINAL_COMMAND','')
        if not command:raise ValueError('Interactive uploader sessions not admitted')
        os.execv('/usr/bin/sudo',['sudo','-n',SELF,'--request',command])
    sys.path.insert(0,'/opt/oxidex-builder')
    from qualification_trust.control_observation import root_json
    from qualification_trust.reservation import route_guard
    config=root_json('/etc/oxidex-builder-uploader.json')
    account=pwd.getpwnam(config['username'])
    if sys.argv[1:2]!=['--request'] or len(sys.argv)!=3 or len(sys.argv[2].encode())>16384:
        raise ValueError('Bounded uploader command required')
    if os.environ.get('SUDO_UID')!=str(account.pw_uid) or account.pw_uid!=config['uid']:
        raise ValueError('Actual sudo uploader identity differs')
    fd=os.open('/run/oxidex-build.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_SH)
        route_guard(pid=os.getpid())  # denies foreign sessions before any bytes
        # Ordinary uploader retains its existing unprivileged shell authority;
        # never execute its original command with root credentials.
        result=subprocess.run(['/bin/sh','-c',sys.argv[2]],
            user=account.pw_uid,group=account.pw_gid,extra_groups=os.getgrouplist(account.pw_name,account.pw_gid),
            env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':account.pw_dir,'USER':account.pw_name,'LANG':'C'},
            pass_fds=(fd,))
        return result.returncode
    finally:os.close(fd)


if __name__=='__main__':raise SystemExit(main())
