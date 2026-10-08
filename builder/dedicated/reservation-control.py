#!/usr/bin/python3
"""Root-authenticated reservation control with enabled fixed-scope mutations."""
import argparse
import fcntl
import json
import os
import stat
import sys
import signal
import subprocess
import hashlib


RECONCILE_LOG=b'Actual owned quiescence observed; no sources/targets/evidence removed.\n'


def reconcile_log(path):
    """Create or authenticate the one deterministic root cleanup observation."""
    flags=os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK
    try:
        fd=os.open(path,flags)
    except FileNotFoundError:
        fd=os.open(path,flags|os.O_CREAT|os.O_EXCL,0o600)
        try:
            if os.write(fd,RECONCILE_LOG)!=len(RECONCILE_LOG):
                raise ValueError('Reconciliation log creation incomplete')
        except BaseException:
            os.close(fd)
            raise
    try:
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_nlink!=1
                or before.st_mode&0o022 or before.st_size>len(RECONCILE_LOG)):
            raise ValueError('Retained reconciliation log differs')
        os.lseek(fd,0,os.SEEK_SET)
        existing=os.read(fd,len(RECONCILE_LOG)+1)
        if len(existing)!=before.st_size or not RECONCILE_LOG.startswith(existing):
            raise ValueError('Retained reconciliation log differs')
        after=os.fstat(fd)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ValueError('Reconciliation log changed during observation')
        if len(existing)<len(RECONCILE_LOG):
            remainder=RECONCILE_LOG[len(existing):]
            if os.write(fd,remainder)!=len(remainder):
                raise ValueError('Reconciliation log completion failed')
        os.lseek(fd,0,os.SEEK_SET)
        if (os.fstat(fd).st_size!=len(RECONCILE_LOG)
                or os.read(fd,len(RECONCILE_LOG)+1)!=RECONCILE_LOG):
            raise ValueError('Reconciliation log completion differs')
        # Complete bytes alone do not prove a prior failed file or directory
        # sync was durable. Every accepted retry syncs both again.
        stable=os.fstat(fd)
        os.fsync(fd)
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
        current=os.fstat(fd)
        named=os.lstat(path)
        identity=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        if identity(stable)!=identity(current) or identity(current)!=identity(named):
            raise ValueError('Reconciliation log changed after durability proof')
        return hashlib.sha256(RECONCILE_LOG).hexdigest()
    finally:os.close(fd)


def inspection_fence():
    """Read custody only on an idle global fence; never race a routed build."""
    # Same atomic canonical lock creation as the existing launcher after reboot.
    fd=os.open('/run/oxidex-build.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o022:
            raise ValueError('Untrusted canonical build fence')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:
        os.close(fd);raise
    return fd


def active_fence():
    """Join an active stage's SH fence without creating or replacing it."""
    fd=os.open('/run/oxidex-build.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o022:
            raise ValueError('Untrusted canonical build fence')
        fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
    except BaseException:
        os.close(fd);raise
    return fd


def main():
    if os.geteuid()!=0:raise RuntimeError('Root-authenticated control transport required')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['inspect','reserve','stage','submit-stage','export','reconcile','reconcile-reboot','release','observe-owned','interrupt-owned'])
    parser.add_argument('--project')
    parser.add_argument('--reservation')
    parser.add_argument('--ordinal',type=int)
    parser.add_argument('--expected-old-boot')
    parser.add_argument('--installation-nonce')
    parser.add_argument('--scope-sha256')
    args=parser.parse_args()
    sys.path.insert(0,'/opt/oxidex-builder')
    from qualification_trust.reservation import ROOT,Store,enrollment,host_identity
    from qualification_trust import control_engine as engine
    from qualification_trust.reservation import pairs,current_actor
    if args.operation in ('observe-owned','interrupt-owned'):
        from qualification_trust import control_fault
        if args.project is None or args.reservation is None or args.installation_nonce is None:
            parser.error('Exact project, reservation and installation nonce required')
        fd=active_fence()
        try:
            if args.operation=='observe-owned':
                if args.scope_sha256 is not None:parser.error('Scope digest belongs to interruption only')
                result=control_fault.observe(args.project,args.reservation,args.installation_nonce)
            else:
                if args.scope_sha256 is None:parser.error('Exact reviewed scope digest required')
                raw=sys.stdin.buffer.read(65537)
                if len(raw)>65536:raise ValueError('Bounded reviewed scope required')
                if hashlib.sha256(raw).hexdigest()!=args.scope_sha256:
                    raise ValueError('Reviewed scope bytes differ')
                scope=json.loads(raw,object_pairs_hook=pairs)
                if (scope.get('project')!=args.project or scope.get('reservation')!=args.reservation
                        or scope.get('installation_nonce')!=args.installation_nonce):
                    raise ValueError('Selected scope arguments differ')
                result=control_fault.interrupt_owned(scope)
            print(json.dumps(result,sort_keys=True));return 0
        finally:os.close(fd)
    if args.operation=='submit-stage':
        if args.project is None or args.reservation is None or args.ordinal is None:parser.error('Stage identity required')
        engine.selected(args.project,args.reservation)
        if not 0<=args.ordinal<8:raise ValueError('Fixed ordinal required')
        unit='oxidex-reservation-'+args.reservation+'-'+str(args.ordinal)
        return subprocess.run(['/usr/bin/systemd-run','--unit='+unit,'--pipe','--wait',
            '--property=Type=exec','--property=Restart=no','--property=KillMode=control-group',
            '/usr/local/sbin/oxidex-reservation-control','stage','--project',args.project,
            '--reservation',args.reservation,'--ordinal',str(args.ordinal)],check=False).returncode
    if args.operation in ('stage','export'):
        if args.project is None or args.reservation is None or (args.operation=='stage' and args.ordinal is None):parser.error('Stage identity required')
        # Host-owned service clients should invoke this inside their systemd unit.
        # A disconnected SSH transport must not terminate an already-owned child.
        signal.signal(signal.SIGHUP,signal.SIG_IGN)
        fd=os.open('/run/oxidex-build.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
            # Reobserve the mounted cache under the active SH fence.
            if enrollment() is None:raise ValueError('Active reservation requires enrolled storage')
            if args.operation=='export':
                engine.export(args.project,args.reservation,sys.stdout.buffer)
            else:
                if engine.own_unit() is None:raise RuntimeError('Actual host-owned stage unit required; use submit-stage')
                result=engine.stage(args.project,args.reservation,args.ordinal,sys.stdin.buffer,protective_fds=(fd,))
                print(json.dumps(result,sort_keys=True))
        finally:os.close(fd)
        return 0
    fd=inspection_fence()
    try:
        policy=enrollment()
        if args.operation=='reserve':
            raw=sys.stdin.buffer.read(65537)
            if len(raw)>65536:raise ValueError('Bounded root control request required')
            result=engine.reserve(json.loads(raw,object_pairs_hook=pairs))
            print(json.dumps(result,sort_keys=True));return 0
        if args.operation=='reconcile-reboot':
            if args.project is None or args.reservation is None or args.expected_old_boot is None:parser.error('Exact original reboot identity required')
            result=engine.reconcile_reboot(args.project,args.reservation,args.expected_old_boot)
            print(json.dumps(result,sort_keys=True));return 0
        if args.operation in ('reconcile','release'):
            if args.project is None or args.reservation is None:parser.error('Owned reservation identity required')
            if args.operation=='release':
                result=Store().release(args.reservation,host=host_identity(),project=args.project,
                    quiescent=lambda:engine.idle(args.project))
            else:
                path,request,record=engine.selected(args.project,args.reservation)
                result=Store().cleanup_after_failure(args.reservation,host=host_identity(),
                    actor=current_actor(),argv=request['argv'][7],quiescent=lambda:engine.idle(args.project))
                # Custody cleanup observes state only; no deletion or child restart.
                log=path/'reconcile-cleanup.log'
                log_sha256=reconcile_log(log)
                result=Store().finish(args.reservation,7,host=host_identity(),actor=current_actor(),
                    exit_code=0,log_sha256=log_sha256,quiescent=lambda:engine.idle(args.project))
            print(json.dumps(result,sort_keys=True));return 0
        record=None
        if ROOT.exists() or ROOT.is_symlink():
            with Store().locked():record=Store().read()
        elif policy is not None:
            raise RuntimeError('Enrolled custody storage unavailable; retain hold')
        print(json.dumps({'schema':1,'host':host_identity(),'enrollment':policy,
                          'record':record,'mutation_enabled':True},sort_keys=True))
    finally:os.close(fd)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
