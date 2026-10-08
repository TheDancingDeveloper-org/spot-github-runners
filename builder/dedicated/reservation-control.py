#!/usr/bin/python3
"""Root-only observation endpoint; Task20 mutation remains deliberately disabled."""
import argparse
import fcntl
import json
import os
import sys


def inspection_fence():
    """Read custody only on an idle global fence; never race a routed build."""
    fd=os.open('/run/oxidex-build.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:
        os.close(fd);raise
    return fd


def main():
    if os.geteuid()!=0:raise RuntimeError('Root-authenticated control transport required')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['inspect','reserve','stage','reconcile','release'])
    args=parser.parse_args()
    sys.path.insert(0,'/opt/oxidex-builder')
    from qualification_trust.reservation import ROOT,Store,enrollment,host_identity
    if args.operation!='inspect':
        raise RuntimeError('BLOCKED_LOCK: Task20 full profile, receiver enrollment and terminal producers not complete')
    fd=inspection_fence()
    try:
        policy=enrollment()
        record=None
        if ROOT.exists() or ROOT.is_symlink():
            with Store().locked():record=Store().read()
        elif policy is not None:
            raise RuntimeError('Enrolled custody storage unavailable; retain hold')
        print(json.dumps({'schema':1,'host':host_identity(),'enrollment':policy,
                          'record':record,'mutation_enabled':False},sort_keys=True))
    finally:os.close(fd)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
