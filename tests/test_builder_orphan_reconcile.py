"""Exercise stopped-builder reconciliation with real per-cache flock exclusion."""
import copy
import fcntl
import json
import importlib.util
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,MagicMock
from lib import host_admission


class BuilderOrphanReconcileTests(unittest.TestCase):
    def record(self):
        run='run-'+'b'*32
        return {'Id':'a'*64,'Name':'/oxidex-run-'+run,
                'Config':{'Labels':{'oxidex.remote-build':'true',
                                   'oxidex.cache-project':'cache','oxidex.run-id':run}},
                'State':{'Running':False,'Paused':False,'Restarting':False,'Pid':0,'Status':'created'},
                'Mounts':[{'Type':'bind','Destination':'/src',
                           'Source':'/mnt/runner-data/remote-build/sources/cache'},
                          {'Type':'bind','Destination':'/target',
                           'Source':'/mnt/runner-data/remote-build/targets/cache'}]}

    def docker(self,inventory,record,events):
        def execute(command,**kwargs):
            events.append(command)
            if command[1:3]==['ps','-aq']:
                return SimpleNamespace(stdout=next(inventory),returncode=0)
            if command[1]=='inspect':
                return SimpleNamespace(stdout=json.dumps([record]),returncode=0)
            if command[1]=='rm':
                self.assertNotIn('--force',command)
                self.assertEqual(command[-1],record['Id'])
                return SimpleNamespace(stdout='',returncode=0)
            raise AssertionError(command)
        return execute

    def test_late_stopped_create_is_reconciled_after_first_empty_inventory(self):
        events=[];row=self.record()
        with tempfile.TemporaryDirectory() as directory,\
             patch.object(host_admission,'LOCK_ROOT',Path(directory),create=True),\
             patch.object(host_admission.subprocess,'run',side_effect=self.docker(iter(['',row['Id']+'\n']),row,events)):
            self.assertFalse(host_admission.builders_running())
            self.assertFalse(host_admission.builders_running())
        self.assertEqual(sum(command[1]=='rm' for command in events),1)

    def test_surviving_wrapper_or_start_cli_cache_lock_protects_stopped_container(self):
        row=self.record();events=[]
        with tempfile.TemporaryDirectory() as directory:
            active=Path(directory)/'oxidex-cache-active-cache.lock'
            with active.open('a') as inherited_cli_lock:
                fcntl.flock(inherited_cli_lock,fcntl.LOCK_EX)
                with patch.object(host_admission,'LOCK_ROOT',Path(directory),create=True),\
                     patch.object(host_admission.subprocess,'run',side_effect=self.docker(iter([row['Id']+'\n']),row,events)):
                    self.assertTrue(host_admission.builders_running())
        self.assertFalse(any(command[1]=='rm' for command in events))

    def test_new_wrapper_reconciles_same_cache_orphan_before_lock_and_admission(self):
        spec=importlib.util.spec_from_file_location('orphan_adapter',
            Path(__file__).resolve().parents[1]/'builder/docker-admission.py')
        adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
        row=self.record();events=[];old_container_present=True
        def docker(command,**kwargs):
            nonlocal old_container_present
            if command[1]=='ps':return SimpleNamespace(stdout=row['Id']+'\n' if old_container_present else '')
            if command[1]=='inspect':return SimpleNamespace(stdout=json.dumps([row]))
            if command[1]=='rm':
                old_container_present=False;events.append('reconciled')
            elif command[1]=='create':events.append('created')
            else:raise AssertionError(command)
            return SimpleNamespace(returncode=0)
        def acquire():
            self.assertFalse(old_container_present)
            events.append('acquire')
        admission=SimpleNamespace(reconcile_stopped_builders=host_admission.reconcile_stopped_builders,
                                  acquire_build=acquire,release_build=lambda:events.append('release'))
        child=MagicMock();child.poll.return_value=0;child.returncode=0
        args=['run','--label=oxidex.remote-build=true','-v',
              '/mnt/runner-data/remote-build/sources/cache:/src','-v',
              '/mnt/runner-data/remote-build/targets/cache:/target','image','cargo','build']
        original_open=open
        with tempfile.TemporaryDirectory() as directory:
            def lock_open(path,*args,**kwargs):
                return original_open(Path(directory)/Path(path).name,*args,**kwargs)
            with patch.object(host_admission,'LOCK_ROOT',Path(directory)),\
                 patch('builtins.open',side_effect=lock_open),\
                 patch.object(adapter,'container_id',return_value=None),\
                 patch.object(adapter.subprocess,'run',side_effect=docker),\
                 patch.object(adapter.subprocess,'Popen',return_value=child):
                self.assertEqual(adapter.run_build(args,admission),0)
        self.assertEqual(events,['reconciled','acquire','created','release'])

    def test_running_and_foreign_or_malformed_identity_are_never_removed(self):
        cases=[]
        for change in ('running','name','run-label','cache-label','remote-label','source','target','duplicate-source'):
            row=self.record()
            if change=='running':row['State'].update(Running=True,Status='running',Pid=123)
            elif change=='name':row['Name']='/oxidex-run-run-invalid'
            elif change=='run-label':row['Config']['Labels']['oxidex.run-id']='run-'+'c'*32
            elif change=='cache-label':row['Config']['Labels']['oxidex.cache-project']='other'
            elif change=='remote-label':row['Config']['Labels']['oxidex.remote-build']='false'
            elif change=='source':row['Mounts'][0]['Source']='/foreign/source'
            elif change=='target':row['Mounts'][1]['Source']='/foreign/target'
            else:row['Mounts'].append(copy.deepcopy(row['Mounts'][0]))
            cases.append((change,row))
        for change,row in cases:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                events=[]
                with patch.object(host_admission,'LOCK_ROOT',Path(directory),create=True),\
                     patch.object(host_admission.subprocess,'run',side_effect=self.docker(iter([row['Id']+'\n']),row,events)):
                    self.assertTrue(host_admission.builders_running())
                self.assertFalse(any(command[1]=='rm' for command in events))

    def test_drain_global_lock_is_nonblocking_and_held_lock_mode_does_not_reacquire(self):
        with tempfile.TemporaryDirectory() as directory:
            global_path=Path(directory)/'oxidex-build.lock'
            with global_path.open('a') as drain:
                fcntl.flock(drain,fcntl.LOCK_EX)
                with patch.object(host_admission,'LOCK_ROOT',Path(directory),create=True),\
                     patch.object(host_admission.subprocess,'run',return_value=SimpleNamespace(stdout='')) as docker:
                    self.assertTrue(host_admission.builders_running())
                    docker.assert_not_called()
                    self.assertFalse(host_admission.builders_running(global_lock_held=True))
                    docker.assert_called_once()

    def test_state_is_rechecked_under_cache_lock_before_nonforced_removal(self):
        stopped=self.record();running=copy.deepcopy(stopped)
        running['State'].update(Running=True,Status='running',Pid=123)
        rows=iter([stopped,running]);events=[]
        def execute(command,**kwargs):
            events.append(command)
            if command[1]=='ps':return SimpleNamespace(stdout=stopped['Id']+'\n')
            if command[1]=='inspect':return SimpleNamespace(stdout=json.dumps([next(rows)]))
            raise AssertionError('Removed a container after it started')
        with tempfile.TemporaryDirectory() as directory,\
             patch.object(host_admission,'LOCK_ROOT',Path(directory),create=True),\
             patch.object(host_admission.subprocess,'run',side_effect=execute):
            self.assertTrue(host_admission.builders_running())
        self.assertEqual(sum(command[1]=='inspect' for command in events),2)

    def test_standalone_deployed_drain_cli_uses_held_global_lock(self):
        script=Path(host_admission.__file__)
        with patch.object(sys,'argv',[str(script),'reconcile-builders','--global-lock-held']),\
             patch('os.geteuid',return_value=0),\
             patch('subprocess.run',return_value=SimpleNamespace(stdout='')) as docker,\
             patch('fcntl.flock',side_effect=AssertionError('reacquired drain lock')):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(script),run_name='__main__')
        self.assertEqual(result.exception.code,0)
        docker.assert_called_once()

    def test_quiesce_preserves_vm_guard_and_runs_reconcile_under_global_exclusive_lock(self):
        from lib.spot_runner_autoscaler import quiesce_remote_builder
        vm=SimpleNamespace(instance_id='12345',name='vm',zone='zone')
        with patch('lib.spot_runner_autoscaler.subprocess.run',return_value=SimpleNamespace(returncode=0)) as ssh:
            self.assertTrue(quiesce_remote_builder(vm,SimpleNamespace(project='project')))
        command=next(value for value in ssh.call_args.args[0] if value.startswith('--command='))
        self.assertIn('sudo flock -n -E 75 /run/oxidex-build.lock',command)
        self.assertIn('metadata.google.internal/computeMetadata/v1/instance/id',command)
        self.assertIn('= 12345 || exit 75;',command)
        self.assertIn('/usr/bin/python3 -I -c',command)
        self.assertNotIn('/opt/gha-runners/host_admission.py reconcile-builders',command)
        # Extract and execute the embedded program without an installed CLI.
        import shlex
        outer=shlex.split(command.removeprefix('--command='))
        shell=outer[-1]
        tokens=shlex.split(shell)
        index=tokens.index('-c')
        program=tokens[index+1]
        with patch('subprocess.run',return_value=SimpleNamespace(stdout='')) as docker:
            with self.assertRaises(SystemExit) as result:
                exec(compile(program,'<embedded-drain>','exec'),{})
        self.assertEqual(result.exception.code,0)
        docker.assert_called_once()
        self.assertNotIn('docker rm',command)


if __name__=='__main__':unittest.main()
