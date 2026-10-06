import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib.fleet_evidence import read_host_events


class HostSampleIdentityTests(unittest.TestCase):
    def test_reused_vm_name_cannot_supply_resource_or_oom_evidence(self):
        vm=SimpleNamespace(name='worker',zone='zone',instance_id='123')
        real_run=subprocess.run
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);curl=root/'curl'
            curl.write_text('#!/bin/sh\nprintf 456\n');curl.chmod(0o755)
            def ssh(argv,**kwargs):
                remote=next(x.removeprefix('--command=') for x in argv if x.startswith('--command='))
                tokens=shlex.split(remote)
                self.assertEqual(tokens[:3],['sudo','sh','-c'])
                result=real_run(['sh','-c',tokens[-1]],env={**os.environ,'PATH':str(root)+':'+os.environ['PATH']},
                    capture_output=True,text=True)
                self.assertEqual(result.returncode,75)
                self.assertEqual(result.stdout,'')
                raise subprocess.CalledProcessError(result.returncode,argv)
            with patch('lib.fleet_evidence.subprocess.run',side_effect=ssh):
                with self.assertRaises(subprocess.CalledProcessError):read_host_events('project',vm)
