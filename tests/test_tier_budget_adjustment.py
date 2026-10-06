"""Verify the actual remote budget program against an idle host fixture."""
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from lib.spot_runner_autoscaler import adjust_admission, builder_ready
from lib.runner_fleet import GceVm


class BudgetAdjustmentTests(unittest.TestCase):
    def test_builder_oom_adjusts_only_builder_and_stays_below_observed_envelope(self):
        host=GceVm('vm','z','RUNNING','n2-standard-8',True,8,16,instance_id='1')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);admission=root/'admission.json';builder=root/'builder.json'
            admission.write_text(json.dumps(dict(work_gib=12,runner_gib=2,builder_gib=6,runner_names=['runner'])))
            builder.write_text(json.dumps(dict(memory_gib=6,cpus=4)))
            captured=[]
            def command(argv,**kwargs):
                captured.append(argv)
                outer=next(arg.removeprefix('--command=') for arg in argv if arg.startswith('--command='))
                shell=shlex.split(outer)[-1]
                program=shlex.split(shell.split('python3 -c ',1)[1])[0]
                program=program.replace('/etc/gha-admission.json',str(admission)).replace('/etc/oxidex-remote-build.json',str(builder)).replace('/var/lib/gha-admission/leases.json',str(root/'leases.json'))
                program=program.replace('/run/gha-admission-adjust.lock',str(root/'adjust.lock'))
                import lib.host_admission as module
                import sys
                with patch.dict(sys.modules,{'host_admission':module}),patch.object(module,'observed_envelope',return_value=11.5),patch.object(module,'request',return_value=True):
                    exec(program,{})
                return SimpleNamespace(returncode=0)
            with patch('lib.spot_runner_autoscaler.subprocess.run',side_effect=command):
                self.assertIs(adjust_admission(host,SimpleNamespace(project='p'),'builder'),True)
            config=json.loads(admission.read_text());limits=json.loads(builder.read_text())
            self.assertEqual(config['runner_gib'],2)
            self.assertEqual(config['builder_gib'],10)
            self.assertEqual(limits['memory_gib'],10)
            self.assertEqual(len(captured),1)

    def test_ceiling_is_reported_as_capacity_pressure_not_success(self):
        host=GceVm('vm','z','RUNNING','n2-standard-8',True,8,16,instance_id='1')
        with patch('lib.spot_runner_autoscaler.subprocess.run',return_value=SimpleNamespace(returncode=76)):
            self.assertEqual(adjust_admission(host,SimpleNamespace(project='p'),'runner'),'capacity')

    def test_builder_canary_compiles_and_runs_a_valid_locked_project(self):
        import tomllib
        host=GceVm('vm','z','RUNNING','n2-standard-8',True,8,16,instance_id='1')
        with tempfile.TemporaryDirectory() as folder:
            def command(argv,**kwargs):
                outer=next(arg.removeprefix('--command=') for arg in argv if arg.startswith('--command='))
                shell=shlex.split(outer)[-1]
                self.assertIn('cargo run --locked',shell)
                setup=shlex.split(shell.split('python3 -c ',1)[1])[0]
                exec(setup.replace('/mnt/runner-data/remote-build/sources/admission-canary',folder),{})
                self.assertEqual(tomllib.loads((Path(folder)/'Cargo.toml').read_text())['package']['edition'],'2021')
                self.assertEqual(tomllib.loads((Path(folder)/'Cargo.lock').read_text())['package'][0]['name'],'oxidex-admission-canary')
                self.assertTrue((Path(folder)/'src/main.rs').read_text().endswith('\n'))
                return SimpleNamespace(returncode=0)
            with patch('lib.spot_runner_autoscaler.subprocess.run',side_effect=command):
                self.assertTrue(builder_ready(host,SimpleNamespace(project='p')))
