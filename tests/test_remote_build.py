import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RemoteBuildTests(unittest.TestCase):
    def launcher(self):
        spec = importlib.util.spec_from_file_location('remote_build', ROOT / 'builder/host-launcher.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_project_paths_cannot_escape_and_commands_are_restricted(self):
        launcher = self.launcher()
        for name in ('../secret', '/tmp/x', '-v', 'a/b', ''):
            with self.assertRaises(ValueError):
                launcher.validate_project(name)
        self.assertEqual(launcher.validate_project('oxidex-refactor'), 'oxidex-refactor')
        with self.assertRaises(ValueError):
            launcher.container_command('oxidex', ['bash', '-c', 'whoami'], 24, 96)

    def test_container_is_unprivileged_bounded_and_caches_are_persistent(self):
        command = self.launcher().container_command('oxidex', ['cargo', 'build', '--release'], 24, 96)
        self.assertIn('--user=1001:1001', command)
        self.assertIn('--cpus=24', command)
        self.assertIn('--memory=96g', command)
        self.assertIn('--cap-drop=ALL', command)
        self.assertIn('--security-opt=no-new-privileges', command)
        self.assertIn('/mnt/runner-data/remote-build/targets/oxidex:/target', command)
        self.assertIn('/mnt/runner-data/remote-build/cargo:/cargo', command)
        self.assertNotIn('/var/run/docker.sock', ' '.join(command))

    def test_startup_builder_option_is_opt_in_and_assets_are_embedded(self):
        from lib.runner_manager import args_parser, render_startup, select_sizing
        from lib import builder_assets
        args = args_parser().parse_args(['--max-cost', '.5', '--remote-builder','--tier','bulky'])
        select_sizing(args)
        script = render_startup(args, 'test-token', 'test-group', 'test-deployment')
        self.assertIn('REMOTE_BUILDER=1', script)
        self.assertNotIn('{{', script)
        self.assertIn(builder_assets.BUILDER_DOCKERFILE.splitlines()[0], 'FROM oxidex-self-hosted-runner:2.337.0')

    def test_resource_mode_cannot_install_unadmitted_builder(self):
        from lib.runner_manager import args_parser,select_sizing
        args=args_parser().parse_args(['--max-cost','.5','--remote-builder'])
        with self.assertRaisesRegex(RuntimeError,'shared memory admission'):
            select_sizing(args)

    def test_active_builder_blocks_host_draining(self):
        from lib.spot_runner_autoscaler import quiesce_remote_builder
        from lib.runner_fleet import GceVm
        from types import SimpleNamespace
        from unittest.mock import patch
        vm=GceVm('test','zone-a','RUNNING','n4-standard-32',True,instance_id='1')
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=75)) as run:
            self.assertFalse(quiesce_remote_builder(vm, SimpleNamespace(project='p')))
            self.assertIn("builders_running",run.call_args.args[0][-1])
            self.assertNotIn('/opt/gha-runners/host_admission.py reconcile-builders',run.call_args.args[0][-1])


if __name__ == '__main__':
    unittest.main()
