#!/usr/bin/python3
"""Two worktree commands inside one aggregate admitted builder container.

This emits self-observed process/counter evidence. Only an independently bound
root observer can turn genuine concurrent builds into a qualification result.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ALLOWED={'cargo','rustc','rustfmt','clippy-driver','just','uv','python3','cbindgen'}


def validate_worktree_paths(name):
    if not isinstance(name,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}',name):
        raise ValueError('Invalid worktree identity')
    source=Path('/src/worktrees')/name;target=Path('/target')/name
    for path in (source,target):
        if path.resolve()!=path or any(part.is_symlink() for part in (path,*path.parents)):
            raise ValueError('Symlinked worktree/cache boundary')
    if not source.is_dir():raise ValueError('Missing worktree source')


def aggregate_counters():
    rows=Path('/proc/self/cgroup').read_text().splitlines()
    paths=[line[3:] for line in rows if line.startswith('0::')]
    if len(paths)!=1 or not paths[0].startswith('/') or '..' in paths[0].split('/'):
        raise ValueError('Unknown aggregate cgroup')
    directory=Path('/sys/fs/cgroup'+paths[0]);info=directory.stat()
    record={'path':str(directory),'inode':info.st_ino,'device':info.st_dev,'monotonic_ns':time.monotonic_ns()}
    for name in ('memory.current','memory.peak','memory.max','memory.events','cpu.stat','cpu.max'):
        value=(directory/name).read_text()
        if len(value)>16384:raise ValueError('Counter bound exceeded')
        record[name.replace('.','_')]=value.strip()
    return record


def run_worktree_commands(jobs):
    if not isinstance(jobs,list) or len(jobs)!=2:raise ValueError('Exactly two aggregate worktrees required')
    names=[]
    for job in jobs:
        if not isinstance(job,dict) or set(job)!={'worktree_id','argv'}:raise ValueError('Unknown worktree command fields')
        name=job['worktree_id'];argv=job['argv'];validate_worktree_paths(name)
        if (not isinstance(argv,list) or not 1<=len(argv)<=256 or not isinstance(argv[0],str) or argv[0] not in ALLOWED
            or any(not isinstance(arg,str) or len(arg)>16384 or '\0' in arg for arg in argv)):
            raise ValueError('Unsupported/oversized command argv')
        if any(arg=='--target-dir' or arg.startswith('--target-dir=') for arg in argv):
            raise ValueError('Worktree target override forbidden')
        names.append(name)
    if len(set(names))!=2:raise ValueError('Worktrees and targets must be distinct')
    gaps=[]
    def capture(phase):
        try:return aggregate_counters()
        except (OSError,ValueError) as error:gaps.append(phase+':'+type(error).__name__);return None
    before=capture('before');children=[];records=[];spawn_error=None
    try:
        for job in jobs:
            name=job['worktree_id'];target='/target/'+name
            os.makedirs(target,exist_ok=True)
            env=dict(os.environ,CARGO_HOME='/cargo',CARGO_TARGET_DIR=target,ZIG_LOCAL_CACHE_DIR=target+'/.zig-cache')
            child=subprocess.Popen(job['argv'],cwd='/src/worktrees/'+name,env=env)
            children.append(child)
            records.append(dict(worktree_id=name,executable=job['argv'][0],argv_sha256=hashlib.sha256(json.dumps(job['argv'],separators=(',',':'),ensure_ascii=False).encode()).hexdigest(),pid=child.pid,target=target,started_monotonic_ns=time.monotonic_ns()))
    except OSError as error:spawn_error=type(error).__name__
    probe_start=time.monotonic_ns()
    # First-second-first liveness proves an actual common observation point;
    # simple spawn-before-wait ordering alone is not overlap evidence.
    overlap=(len(children)==2 and children[0].poll() is None
             and children[1].poll() is None and children[0].poll() is None)
    probe_end=time.monotonic_ns();exit_codes=[]
    for child,record in zip(children,records):
        exit_codes.append(child.wait());record['reaped_monotonic_ns']=time.monotonic_ns()
    after=capture('after')
    return dict(schema='aggregate-worktree-commands-v1',qualification=False,children=records,
                exit_codes=exit_codes,spawn_error=spawn_error,overlap_observed=overlap,
                overlap_probe_start_ns=probe_start,overlap_probe_end_ns=probe_end,
                aggregate_before=before,aggregate_after=after,complete=not gaps,gaps=gaps)


def main():
    if len(sys.argv)!=2 or len(sys.argv[1].encode())>32768:raise ValueError('Bounded JSON worktree specification required')
    result=run_worktree_commands(json.loads(sys.argv[1]));print(json.dumps(result,sort_keys=True),flush=True)
    if result['spawn_error'] or len(result['exit_codes'])!=2:raise SystemExit(78)
    raise SystemExit(next((code for code in result['exit_codes'] if code),0))


if __name__=='__main__':main()
