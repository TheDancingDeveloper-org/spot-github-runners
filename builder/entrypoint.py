#!/usr/bin/env python3
"""Prove the checkout's compiler pin before executing an unprivileged build."""
import os
import re
import subprocess
import sys
from pathlib import Path


def main():
    text = Path('/src/rust-toolchain.toml').read_text()
    match = re.search(r'^channel\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', text, re.M)
    if not match:
        raise RuntimeError('A numeric Rust toolchain pin is required')
    pin = match[1]
    compiler = subprocess.check_output(['rustup', 'which', '--toolchain', pin, 'rustc'], text=True).strip()
    cargo = subprocess.check_output(['rustup', 'which', '--toolchain', pin, 'cargo'], text=True).strip()
    env = dict(os.environ, RUSTC=compiler)
    # Pin PATH as well, for just recipes, subprocesses and rustdoc.
    env['PATH'] = str(Path(compiler).parent) + ':' + env['PATH']
    env['RUSTDOC'] = str(Path(compiler).with_name('rustdoc'))
    env['RUSTUP_TOOLCHAIN'] = pin
    # Apply to Python/Just as well: the cached router launches a Python job
    # that invokes Cargo later. Keep Zig caches inside the persistent mounts.
    env['CARGO_ZIGBUILD_ZIG_PATH'] = '/opt/zig/zig'
    env['CARGO_ZIGBUILD_CACHE_DIR'] = '/cargo/zigbuild-cache'
    env['ZIG_GLOBAL_CACHE_DIR'] = '/cargo/zig-global-cache'
    # Hidden so portable artifact export does not include the compiler cache.
    env['ZIG_LOCAL_CACHE_DIR'] = '/target/.zig-cache'
    # Zig supplies Darwin libc; only the open-source CF interface stub is added.
    # Callers append this as a framework search path to their existing flags.
    env['OXIDEX_DARWIN_FRAMEWORK_DIR'] = '/opt/opensourceDarwinStubs'
    env.pop('SDKROOT', None)
    print(subprocess.check_output([compiler, '-vV'], text=True), flush=True)
    print(subprocess.check_output([cargo, '-V'], text=True), flush=True)
    command = sys.argv[1:] or ['cargo', 'build', '--release', '--locked', '--bin', 'oxidex']
    if command[0] == 'cargo':
        command[0] = cargo
    os.execvpe(command[0], command, env)


if __name__ == '__main__':
    main()
