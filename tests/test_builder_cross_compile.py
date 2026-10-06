"""Exercise the builder entrypoint without compiling or contacting a provider."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BuilderCrossCompileTests(unittest.TestCase):
    def execute(self, command):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            compiler = folder / 'rustc'
            compiler.write_text('#!/bin/sh\nprintf "rustc 1.97.1\\n"\n')
            compiler.chmod(0o755)
            cargo = folder / 'cargo'
            cargo.write_text(
                '#!' + sys.executable + '\nimport json,os,sys\n'
                'if sys.argv[1:]==["-V"]: print("cargo 1.97.1 (fixture)")\n'
                'else: print(json.dumps({"argv":sys.argv[1:],"env":dict(os.environ)}))\n')
            cargo.chmod(0o755)
            python = folder / 'python3'
            python.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                              'print(json.dumps({"argv":sys.argv[1:],"env":dict(os.environ)}))\n')
            python.chmod(0o755)
            rustup = folder / 'rustup'
            rustup.write_text('#!/bin/sh\nprintf "%s/%s\\n" "' + str(folder) + '" "$4"\n')
            rustup.chmod(0o755)
            program = (
                'import importlib.util,sys\nfrom unittest.mock import patch\n'
                'spec=importlib.util.spec_from_file_location("entry",sys.argv.pop(1))\n'
                'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
                'with patch.object(module.Path,"read_text",return_value=\'channel = "1.97.1"\'):\n'
                ' module.main()\n')
            env = dict(os.environ, PATH=str(folder) + os.pathsep + os.environ['PATH'],
                       CARGO_HOME='/cargo', CARGO_TARGET_DIR='/target',
                       RUSTC_WRAPPER='/src/tools/remote-build/rustc_guard.py',
                       RUSTC_WORKSPACE_WRAPPER='/src/tools/remote-build/rustc_guard.py',
                       RUSTFLAGS='-C debuginfo=1', CARGO_PROFILE_RELEASE_PANIC='unwind',
                       ZIG_GLOBAL_CACHE_DIR='/tmp/throwaway')
            result = subprocess.run([sys.executable, '-c', program,
                                     str(ROOT / 'builder/entrypoint.py'), *command],
                                    env=env, capture_output=True, text=True, check=True)
            return json.loads(result.stdout.splitlines()[-1])

    def test_cross_build_preserves_argv_and_compiler_guards(self):
        result = self.execute(['cargo', 'zigbuild', '--release', '--locked', '--bin',
                               'oxidex', '--target', 'aarch64-unknown-linux-musl',
                               '--features', 'two separate features'])
        self.assertEqual(result['argv'], ['zigbuild', '--release', '--locked', '--bin',
                                        'oxidex', '--target', 'aarch64-unknown-linux-musl',
                                        '--features', 'two separate features'])
        env = result['env']
        self.assertEqual(env['RUSTC_WRAPPER'], '/src/tools/remote-build/rustc_guard.py')
        self.assertEqual(env['RUSTC_WORKSPACE_WRAPPER'], '/src/tools/remote-build/rustc_guard.py')
        self.assertEqual(env['RUSTFLAGS'], '-C debuginfo=1')
        self.assertEqual(env['CARGO_PROFILE_RELEASE_PANIC'], 'unwind')
        self.assertEqual(env['RUSTUP_TOOLCHAIN'], '1.97.1')

    def test_cross_tools_use_persistent_cache_mounts(self):
        env = self.execute(['cargo', 'zigbuild', '--target=x86_64-unknown-linux-musl'])['env']
        self.assertEqual(env.get('CARGO_ZIGBUILD_ZIG_PATH'), '/opt/zig/zig')
        self.assertEqual(env.get('CARGO_ZIGBUILD_CACHE_DIR'), '/cargo/zigbuild-cache')
        self.assertEqual(env.get('ZIG_GLOBAL_CACHE_DIR'), '/cargo/zig-global-cache')
        self.assertEqual(env.get('ZIG_LOCAL_CACHE_DIR'), '/target/.zig-cache')

    def test_python_job_inherits_cross_tools_for_nested_cargo(self):
        # The general OxiDex router invokes Python, which then invokes Cargo.
        # Its inherited environment must have the same cache and linker setup.
        result = self.execute(['python3', '/src/container_job.py', 'payload'])
        self.assertEqual(result['argv'], ['/src/container_job.py', 'payload'])
        self.assertEqual(result['env'].get('CARGO_ZIGBUILD_ZIG_PATH'), '/opt/zig/zig')
        self.assertEqual(result['env'].get('ZIG_GLOBAL_CACHE_DIR'), '/cargo/zig-global-cache')

    def test_embedded_assets_match_owned_builder_files(self):
        from lib import builder_assets
        self.assertEqual(builder_assets.BUILDER_DOCKERFILE, (ROOT / 'builder/Dockerfile').read_text())
        self.assertEqual(builder_assets.BUILDER_ENTRYPOINT, (ROOT / 'builder/entrypoint.py').read_text())
        self.assertEqual(builder_assets.HOST_LAUNCHER, (ROOT / 'builder/host-launcher.py').read_text())


if __name__ == '__main__':
    unittest.main()
