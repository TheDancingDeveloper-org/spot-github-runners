import importlib.util
import base64
import json
import signal
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch,mock_open,MagicMock
from types import SimpleNamespace


class BuilderAdmissionTests(unittest.TestCase):
    def admission(self,**kwargs):
        return SimpleNamespace(reconcile_stopped_builders=lambda:False,**kwargs)

    def arguments(self,payload=None):
        args=['run','--label=oxidex.remote-build=true','-v',
              '/mnt/runner-data/remote-build/sources/cache:/src','-v',
              '/mnt/runner-data/remote-build/targets/cache:/target',
              'oxidex-remote-builder:1.97.1','cargo','build']
        if payload is not None:
            args[-2:]=['python3','/src/tools/remote-build/container_job.py',
                       base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()]
        return args

    def module(self):
        spec=importlib.util.spec_from_file_location('docker_admission',Path(__file__).resolve().parents[1]/'builder/docker-admission.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_full_host_returns_retryable_without_starting_docker(self):
        module=self.module()
        admission=self.admission(acquire_build=lambda:(_ for _ in ()).throw(SystemExit(75)),release_build=lambda:None)
        with patch('builtins.open',mock_open()),patch.object(module.fcntl,'flock'),\
             patch.object(module,'container_id',return_value=None),\
             patch.object(module.subprocess,'run',side_effect=AssertionError('started without capacity')):
            self.assertEqual(module.run_build(self.arguments(),admission),75)

    def test_lock_contention_never_acquires_or_releases_memory_lease(self):
        for busy_lock in (1,2):
            with self.subTest(busy_lock=busy_lock):
                module=self.module();admission=MagicMock()
                locks=[None]*(busy_lock-1)+[BlockingIOError('held')]
                with patch('builtins.open',mock_open()),\
                     patch.object(module.fcntl,'flock',side_effect=locks),\
                     patch.object(module.subprocess,'run',side_effect=AssertionError('started contending build')):
                    self.assertEqual(module.run_build(self.arguments(),admission),75)
                admission.acquire_build.assert_not_called()
                admission.release_build.assert_not_called()

    def test_create_and_start_clients_both_inherit_locks_without_create_timeout(self):
        module=self.module();admission=MagicMock();events=[]
        lock_files=[MagicMock(),MagicMock()]
        for index,handle in enumerate(lock_files,10):
            handle.__enter__.return_value=handle;handle.fileno.return_value=index
        admission.acquire_build.side_effect=lambda:events.append('acquire')
        child=MagicMock();child.poll.return_value=0;child.returncode=0
        with patch('builtins.open',side_effect=lock_files),\
             patch.object(module.fcntl,'flock',side_effect=lambda *args:events.append('lock')),\
             patch.object(module,'container_id',return_value=None),\
             patch.object(module.subprocess,'run') as create,\
             patch.object(module.subprocess,'Popen',return_value=child) as start:
            self.assertEqual(module.run_build(self.arguments(),admission),0)
        self.assertEqual(events,['lock','lock','acquire'])
        self.assertEqual(create.call_args.kwargs['pass_fds'],(10,11))
        self.assertNotIn('timeout',create.call_args.kwargs)
        self.assertEqual(start.call_args.kwargs['pass_fds'],(10,11))

    def test_real_lock_descriptors_stay_open_for_clients_and_close_on_every_exit(self):
        for outcome in ('complete','second-open-fails','contended'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as directory,\
                 open(Path(directory)/'global.lock','a') as global_handle,\
                 open(Path(directory)/'active.lock','a') as active_handle:
                module=self.module();admission=MagicMock();clients=[]
                handles=[global_handle] if outcome=='second-open-fails' else [global_handle,active_handle]
                open_results=[*handles,OSError('cache lock unavailable')] if outcome=='second-open-fails' else handles
                child=MagicMock();child.poll.return_value=0;child.returncode=0
                def client(argv,**kwargs):
                    self.assertTrue(all(not handle.closed for handle in handles))
                    self.assertEqual(kwargs['pass_fds'],tuple(handle.fileno() for handle in handles))
                    for descriptor in kwargs['pass_fds']:os.fstat(descriptor)
                    clients.append(argv[1]);return child
                with patch('builtins.open',side_effect=open_results),\
                     patch.object(module.fcntl,'flock',side_effect=[None,BlockingIOError('busy')] if outcome=='contended' else None),\
                     patch.object(module,'container_id',return_value=None),\
                     patch.object(module.subprocess,'run',side_effect=client),\
                     patch.object(module.subprocess,'Popen',side_effect=client):
                    result=module.run_build(self.arguments(),admission)
                self.assertTrue(all(handle.closed for handle in handles))
                self.assertEqual(result,0 if outcome=='complete' else 75)
                if outcome=='complete':self.assertEqual(clients,['create','start'])
                else:admission.acquire_build.assert_not_called()

    def test_builder_result_propagates_and_lease_always_released(self):
        module=self.module();events=[]
        admission=self.admission(acquire_build=lambda:events.append('acquire'),release_build=lambda:events.append('release'))
        child=MagicMock();child.poll.return_value=137;child.returncode=137
        with patch('builtins.open',mock_open()),patch.object(module.fcntl,'flock'),\
             patch.object(module,'container_id',return_value=None),\
             patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=0)),\
             patch.object(module.subprocess,'Popen',return_value=child) as docker:
            self.assertEqual(module.run_build(self.arguments(),admission),137)
        self.assertEqual(events,['acquire','release'])
        self.assertEqual(docker.call_args.args[0][0],'/usr/bin/docker')

    def test_payload_identity_and_mounts_are_validated(self):
        module=self.module();run='run-'+'a'*32
        self.assertEqual(module.build_identity(self.arguments({'run_id':run})),('cache',run,'oxidex-run-'+run))
        for args in (self.arguments({'run_id':'../other'}),
                     [*self.arguments(),'--name=unrelated'],
                     [v.replace('/sources/cache','/sources/other') for v in self.arguments()]):
            with self.assertRaises(ValueError):module.build_identity(args)

    def test_unknown_inventory_keeps_lease_and_locks_until_absent(self):
        module=self.module();events=[]
        admission=self.admission(acquire_build=lambda:events.append('acquire'),release_build=lambda:events.append('release'))
        child=MagicMock();child.poll.return_value=0;child.returncode=0
        def inventory(name):
            events.append('inventory')
            if events.count('inventory')==2:
                raise RuntimeError('daemon inaccessible')
            self.assertNotIn('release',events)
            return None
        with patch('builtins.open',mock_open()),patch.object(module.fcntl,'flock'),\
             patch.object(module,'container_id',side_effect=inventory),\
             patch.object(module.time,'sleep',return_value=None),\
             patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=0)),\
             patch.object(module.subprocess,'Popen',return_value=child):
            self.assertEqual(module.run_build(self.arguments(),admission),0)
        self.assertEqual(events,['inventory','acquire','inventory','inventory','release'])

    def test_signal_stops_exact_container_and_reaps_cli_before_release(self):
        module=self.module();events=[];handlers={}
        child=MagicMock();child.poll.side_effect=[None,None,143];child.returncode=143
        def wait(timeout):
            if not events:
                events.append('signal');handlers[signal.SIGTERM](signal.SIGTERM,None)
                raise module.subprocess.TimeoutExpired('docker',1)
            events.append('reaped');return 143
        child.wait.side_effect=wait
        admission=self.admission(acquire_build=lambda:None,release_build=lambda:events.append('release'))
        def register(signum,handler):handlers[signum]=handler;return signal.SIG_DFL
        def stop(*args):events.append('stop');return True
        with patch('builtins.open',mock_open()),patch.object(module.fcntl,'flock'),\
             patch.object(module.signal,'signal',side_effect=register),\
             patch.object(module,'container_id',return_value=None),\
             patch.object(module,'stop_owned',side_effect=stop),\
             patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=0)),\
             patch.object(module.subprocess,'Popen',return_value=child):
            self.assertEqual(module.run_build(self.arguments(),admission),143)
        self.assertEqual(events,['signal','stop','reaped','release'])
        child.terminate.assert_called_once()

    def test_cancel_during_create_never_starts_container(self):
        module=self.module();handlers={}
        admission=self.admission(acquire_build=lambda:None,release_build=lambda:None)
        def register(signum,handler):handlers[signum]=handler;return signal.SIG_DFL
        def create(*args,**kwargs):
            self.assertEqual(args[0][1],'create')
            handlers[signal.SIGTERM](signal.SIGTERM,None)
        with patch('builtins.open',mock_open()),patch.object(module.fcntl,'flock'),\
             patch.object(module.signal,'signal',side_effect=register),\
             patch.object(module,'container_id',side_effect=[None,'a'*12,None]),\
             patch.object(module,'stop_owned',return_value=True) as stop,\
             patch.object(module.subprocess,'run',side_effect=create),\
             patch.object(module.subprocess,'Popen',side_effect=AssertionError('started cancelled container')):
            self.assertEqual(module.run_build(self.arguments(),admission),143)
        stop.assert_called_once()

    def test_startup_failures_clean_created_container_before_releasing(self):
        for stage in ('create', 'start'):
            with self.subTest(stage=stage):
                module=self.module();events=[]
                admission=self.admission(acquire_build=lambda:None,
                    release_build=lambda:events.append('release'))
                def stop(*args):events.append('stop');return True
                error=module.subprocess.TimeoutExpired('docker create',60)
                with patch('builtins.open',mock_open()),patch.object(module.fcntl,'flock'), \
                     patch.object(module,'container_id',side_effect=[None,'a'*12,None]), \
                     patch.object(module,'stop_owned',side_effect=stop), \
                     patch.object(module.subprocess,'run',side_effect=error if stage=='create' else None), \
                     patch.object(module.subprocess,'Popen',side_effect=OSError('unable to start client')):
                    self.assertEqual(module.run_build(self.arguments(),admission),75)
                self.assertEqual(events,['stop','release'])

    def test_release_failure_preserves_completed_build_exit(self):
        module=self.module()
        admission=self.admission(acquire_build=lambda:None,release_build=lambda:(_ for _ in ()).throw(OSError('socket restarted')))
        child=MagicMock();child.poll.return_value=0;child.returncode=0
        with patch('builtins.open',mock_open()),patch.object(module.fcntl,'flock'),\
             patch.object(module,'container_id',return_value=None),\
             patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=0)),\
             patch.object(module.subprocess,'Popen',return_value=child):
            self.assertEqual(module.run_build(self.arguments(),admission),0)

    def test_stop_refuses_other_container_identity(self):
        module=self.module()
        with patch.object(module,'container_id',return_value='a'*12),\
             patch.object(module.subprocess,'check_output',return_value=json.dumps([{'Name':'/other'}])),\
             patch.object(module.subprocess,'run',side_effect=AssertionError('stopped other build')):
            with self.assertRaises(RuntimeError):module.stop_owned('oxidex-run-run-'+'a'*32,'cache','run-'+'a'*32)
