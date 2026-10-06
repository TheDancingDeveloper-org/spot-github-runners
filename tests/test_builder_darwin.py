"""Verify the SDK-free Darwin backend without compiling any source."""
import importlib.util
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = {
    '_CFRelease', '_CFTimeZoneCopySystem', '_CFTimeZoneGetName',
    '_CFTimeZoneResetSystem', '_CFStringGetCStringPtr', '_CFStringGetLength',
    '_CFStringGetBytes',
}


class BuilderDarwinTests(unittest.TestCase):
    def test_image_stub_generation_produces_exact_exports_and_full_mit_notice(self):
        dockerfile = (ROOT / 'builder/Dockerfile').read_text()
        block = dockerfile.split('RUN install -d -m 0755 /opt/opensourceDarwinStubs', 1)[1]
        block = 'install -d -m 0755 /opt/opensourceDarwinStubs' + block.split('\nCOPY entrypoint.py', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / 'frameworks'
            subprocess.run(['/bin/sh', '-eu', '-c', block.replace(
                '/opt/opensourceDarwinStubs', shlex.quote(str(generated)))],
                check=True, capture_output=True, text=True)
            stub = generated / 'CoreFoundation.framework/CoreFoundation.tbd'
            text = stub.read_text()
            self.assertEqual(set(re.findall(r'_CF\w+', text)), SYMBOLS)
            self.assertEqual(text.splitlines()[0], '--- !tapi-tbd')
            self.assertEqual(text.splitlines()[-1], '...')
            self.assertEqual(stub.stat().st_mode & 0o777, 0o444)
            self.assertEqual(hashlib.sha256(stub.read_bytes()).hexdigest(),
                             'e555b2500bcfd7471f5251deea43cf18c69ec8d0ff6b8e8de282279d91b971ca')
            notice = (generated / 'NOTICE').read_text()
            license_text = (ROOT / 'builder/DARWIN-STUB-NOTICE.md').read_text().split('## MIT license\n', 1)[1]
            self.assertIn(' '.join(license_text.split()), ' '.join(notice.split()))

    def test_image_contains_two_darwin_std_targets_and_attributed_stub(self):
        dockerfile = (ROOT / 'builder/Dockerfile').read_text()
        self.assertIn('aarch64-apple-darwin x86_64-apple-darwin', dockerfile)
        self.assertIn('/opt/opensourceDarwinStubs/CoreFoundation.framework', dockerfile)
        self.assertIn('targets: [ arm64-macos, x86_64-macos ]', dockerfile)
        self.assertIn('/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation', dockerfile)
        for symbol in SYMBOLS:
            self.assertIn(symbol, dockerfile)
        self.assertIn('Copyright (c) 2012-2013 Mozilla Foundation', dockerfile)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', dockerfile)
        self.assertIn('core-foundation-sys 0.8.7', dockerfile)
        self.assertNotIn('MacOSX.sdk', dockerfile)

    def test_darwin_job_preserves_caller_flags_and_exposes_framework_path(self):
        spec = importlib.util.spec_from_file_location('darwin_entry', ROOT / 'builder/entrypoint.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        command = ['cargo', 'zigbuild', '--release', '--locked', '--target', 'aarch64-apple-darwin',
                   '--config', 'target.aarch64-apple-darwin.rustflags=["-Lframework=/opt/opensourceDarwinStubs"]']
        supplied = {
            'PATH': '/usr/bin', 'SDKROOT': '/unavailable/proprietary-sdk',
            'RUSTFLAGS': '-C debuginfo=1', 'CARGO_ENCODED_RUSTFLAGS': '-C\x1fstrip=none',
            'MACOSX_DEPLOYMENT_TARGET': '11.0',
            'RUSTC_WRAPPER': '/src/tools/remote-build/rustc_guard.py',
            'RUSTC_WORKSPACE_WRAPPER': '/src/tools/remote-build/rustc_guard.py',
        }

        def tool_output(args, **kwargs):
            if args[:2] == ['rustup', 'which']:
                return '/opt/toolchain/bin/' + args[-1] + '\n'
            return 'version fixture\n'

        with patch.dict(os.environ, supplied, clear=True), \
                patch.object(module.Path, 'read_text', return_value='channel = "1.97.1"'), \
                patch.object(module.sys, 'argv', ['entrypoint', *command]), \
                patch.object(module.subprocess, 'check_output', side_effect=tool_output), \
                patch.object(module.os, 'execvpe') as execute, patch('builtins.print'):
            module.main()
        program, argv, env = execute.call_args.args
        self.assertEqual(program, '/opt/toolchain/bin/cargo')
        self.assertEqual(argv, ['/opt/toolchain/bin/cargo', *command[1:]])
        self.assertEqual(env.get('OXIDEX_DARWIN_FRAMEWORK_DIR'), '/opt/opensourceDarwinStubs')
        self.assertNotIn('SDKROOT', env)
        for key in supplied.keys() - {'PATH', 'SDKROOT'}:
            self.assertEqual(env[key], supplied[key])
        self.assertEqual(env['CARGO_ZIGBUILD_ZIG_PATH'], '/opt/zig/zig')
        self.assertEqual(env['ZIG_GLOBAL_CACHE_DIR'], '/cargo/zig-global-cache')

    def test_notice_identifies_pinned_bindings_and_runtime_only_framework(self):
        notice = (ROOT / 'builder/DARWIN-STUB-NOTICE.md').read_text()
        self.assertIn('652bab0a62d87df8a974ffad2ad9f7be218a8b56', notice)
        self.assertIn('Copyright (c) 2012-2013 Mozilla Foundation', notice)
        self.assertIn('MIT', notice)
        self.assertIn('no implementation', notice)
        self.assertIn('magika', notice)
        for symbol in SYMBOLS:
            self.assertIn(symbol, notice)


if __name__ == '__main__':
    unittest.main()
