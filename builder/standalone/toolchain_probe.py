#!/usr/bin/python3
"""Actual version-only tool evidence; no application compilation or installs."""
import hashlib
import json
import subprocess
import time
from pathlib import Path

COMPILER=Path('/home/runner/.rustup/toolchains/1.99.0-x86_64-unknown-linux-gnu/bin/rustc')
COMMIT='b940084d7eb6a299eb4bfeb8e34901bc051e7ac4'


def main():
    raw=subprocess.check_output([str(COMPILER),'-vV'],text=True,timeout=15)
    if 'commit-hash: '+COMMIT not in raw.splitlines():raise ValueError('Actual compiler differs from pin')
    commands={'cargo':[str(COMPILER.with_name('cargo')),'-V'],
              'rustfmt':[str(COMPILER.with_name('rustfmt')),'--version'],
              'clippy':[str(COMPILER.with_name('clippy-driver')),'--version'],
              'zig':['/opt/zig/zig','version'],
              'cargo_zigbuild':['/home/runner/.cargo/bin/cargo-zigbuild','--version'],
              'cbindgen':['/home/runner/.cargo/bin/cbindgen','--version'],
              'just':['/home/runner/.cargo/bin/just','--version'],
              'rsync':['/usr/bin/rsync','--version']}
    tools={name:subprocess.check_output(argv,text=True,timeout=15).strip() for name,argv in commands.items()}
    expected={'cargo':'cargo 1.99.0 ','rustfmt':'rustfmt 1.10.0','clippy':'clippy 0.1.99 ',
              'zig':'0.15.2','cargo_zigbuild':'cargo-zigbuild 0.23.4'}
    if (any(not tools[name].startswith(prefix) for name,prefix in expected.items())
        or tools['zig']!='0.15.2' or tools['cargo_zigbuild']!='cargo-zigbuild 0.23.4'):
        raise ValueError('Actual development tool versions differ from pin')
    rows=Path('/proc/self/cgroup').read_text().splitlines()
    paths=[line[3:] for line in rows if line.startswith('0::')]
    if len(paths)!=1 or not paths[0].startswith('/') or '..' in paths[0].split('/'):
        raise ValueError('Unknown actual container cgroup')
    directory=Path('/sys/fs/cgroup'+paths[0])
    caps={name:(directory/name).read_text().strip() for name in ('memory.max','memory.swap.max','cpu.max')}
    quota,period=caps['cpu.max'].split()
    if caps['memory.max']!=str(28*1024**3) or caps['memory.swap.max']!='0' or int(quota)!=16*int(period):
        raise ValueError('Actual aggregate container limits differ')
    binary=Path('/home/runner/.cargo/bin/cargo-zigbuild')
    print(json.dumps(dict(schema='builder-tools-actual-v1',compiler_path=str(COMPILER),rustc=raw,tools=tools,
        cargo_zigbuild_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),actual_cgroup=caps,
        observed_at_ns=time.time_ns(),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        container_hostname=Path('/etc/hostname').read_text().strip(),
        cgroup_path=str(directory),cgroup_inode=directory.stat().st_ino,cgroup_device=directory.stat().st_dev),sort_keys=True))


if __name__=='__main__':main()
