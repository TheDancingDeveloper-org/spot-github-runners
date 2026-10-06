"""Execute Dockerfile version probes against the pinned CLI contract.

Set CARGO_ZIGBUILD_PROBE_BINARY to a checksum-pinned 0.23.4 executable for
acceptance; ordinary offline runs use its verified top-level-version contract.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lib import builder_assets

ROOT = Path(__file__).resolve().parents[1]


class BuilderToolchainProbeTests(unittest.TestCase):
    def probes(self, dockerfile):
        # Execute the real clauses, including the exact-version comparison.
        return [part.strip().rstrip('\\').strip()
                for line in dockerfile.splitlines()
                for part in line.split('&&')
                if 'zigbuild' in part and '--version' in part]

    def execute(self, clause, version='0.23.4', actual=True):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            binary = folder / 'cargo-zigbuild'
            pinned = os.environ.get('CARGO_ZIGBUILD_PROBE_BINARY') if actual else None
            if pinned:
                binary.symlink_to(Path(pinned).resolve(strict=True))
            else:
                # cargo-zigbuild 0.23.4 src/bin/cargo-zigbuild.rs declares
                # `version` on Opt; cargo's injected zigbuild subcommand does
                # not inherit it. Nested --version exits2 in the real binary.
                binary.write_text('#!' + sys.executable + '\nimport sys\n'
                                  'if sys.argv[1:]==["--version"]:\n'
                                  f' print("cargo-zigbuild {version}")\n'
                                  'else:\n'
                                  ' print("unexpected argument --version",file=sys.stderr)\n'
                                  ' raise SystemExit(2)\n')
                binary.chmod(0o755)
            cargo = folder / 'cargo'
            cargo.write_text('#!' + sys.executable + '\nimport os,sys\n'
                             'os.execv(os.path.join(os.path.dirname(__file__),"cargo-zigbuild"),'
                             '["cargo-zigbuild",*sys.argv[1:]])\n')
            cargo.chmod(0o755)
            return subprocess.run(['/bin/sh', '-c', clause],
                                  env=dict(os.environ, PATH=str(folder) + os.pathsep + os.environ['PATH']),
                                  capture_output=True, text=True)

    def assert_probes_execute(self, dockerfile):
        probes = self.probes(dockerfile)
        self.assertEqual(len(probes), 2)
        for clause in probes:
            with self.subTest(clause=clause):
                result = self.execute(clause)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_owned_dockerfile_probes_execute(self):
        self.assert_probes_execute((ROOT / 'builder/Dockerfile').read_text())

    def test_embedded_bootstrap_probes_execute(self):
        self.assert_probes_execute(builder_assets.BUILDER_DOCKERFILE)

    def test_wrong_installed_version_still_rejects_bootstrap(self):
        clause, _ = self.probes((ROOT / 'builder/Dockerfile').read_text())
        self.assertNotEqual(self.execute(clause, version='0.23.3', actual=False).returncode, 0)


if __name__ == '__main__':
    unittest.main()
