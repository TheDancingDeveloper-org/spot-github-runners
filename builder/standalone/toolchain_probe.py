#!/usr/bin/python3
"""Actual tool and browser dependency evidence; no application builds or installs."""
import hashlib
import json
import subprocess
import time
from pathlib import Path

COMPILER=Path('/home/runner/.rustup/toolchains/1.99.0-x86_64-unknown-linux-gnu/bin/rustc')
COMMIT='b940084d7eb6a299eb4bfeb8e34901bc051e7ac4'


def validate_website_tools(row):
    if (not isinstance(row, dict) or row.get('node') != 'v24.21.0'
            or row.get('playwright') != '1.63.0'
            or row.get('chromium_revision') != '1243'
            or row.get('chromium_version') != '153.0.8010.12'
            or row.get('browser_path') != '/opt/playwright-browsers'
            or type(row.get('uid')) is not int or row.get('uid') != 1001
            or row.get('executable_readable') is not True
            or row.get('libraries_available') is not True):
        raise ValueError('Actual website tools differ from pins or libraries are unavailable')
    return row


def website_tools():
    script = r"""
const fs = require('fs');
const cp = require('child_process');
const {chromium} = require('/opt/playwright/node_modules/playwright');
const executable = chromium.executablePath();
fs.accessSync(executable, fs.constants.R_OK | fs.constants.X_OK);
const libraries = cp.execFileSync('/usr/bin/ldd', [executable], {timeout: 5000, encoding: 'utf8'});
if (libraries.includes('not found')) throw new Error('Missing Chromium shared library');
const version = cp.execFileSync(executable, ['--version'], {timeout: 5000, encoding: 'utf8'}).trim();
const manifest = JSON.parse(fs.readFileSync('/opt/playwright/node_modules/playwright-core/browsers.json'));
const revision = manifest.browsers.find(row => row.name === 'chromium').revision;
console.log(JSON.stringify({node: process.version,
  playwright: require('/opt/playwright/node_modules/playwright/package.json').version,
  chromium_revision: revision, chromium_version: version.split(' ').at(-1),
  browser_path: process.env.PLAYWRIGHT_BROWSERS_PATH, uid: process.getuid(),
  executable_readable: true, libraries_available: true}));
"""
    output = subprocess.check_output(['/opt/node/bin/node', '-e', script],
                                     text=True, timeout=15)
    return validate_website_tools(json.loads(output))


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
    tools['node'] = subprocess.check_output(['/opt/node/bin/node', '--version'], text=True, timeout=15).strip()
    tools['npm'] = subprocess.check_output(['/opt/node/bin/npm', '--version'], text=True, timeout=15).strip()
    website = website_tools()
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
    print(json.dumps(dict(schema='builder-tools-actual-v1',compiler_path=str(COMPILER),rustc=raw,tools=tools,website_tools=website,
        cargo_zigbuild_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),actual_cgroup=caps,
        observed_at_ns=time.time_ns(),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        container_hostname=Path('/etc/hostname').read_text().strip(),
        cgroup_path=str(directory),cgroup_inode=directory.stat().st_ino,cgroup_device=directory.stat().st_dev),sort_keys=True))


if __name__=='__main__':main()
