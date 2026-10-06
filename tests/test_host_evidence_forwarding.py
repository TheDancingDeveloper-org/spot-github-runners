import io
import shlex
import unittest
from dataclasses import replace
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from lib.fleet_evidence import read_host_events
from lib.runner_fleet import GceVm


class SamplerForwardingTests(unittest.TestCase):
    vm=GceVm('exact-vm','asia-northeast2-a','RUNNING','c2d-standard-32',True,
             instance_id='2161856278046720537')

    def test_pending_ids_reach_standalone_sampler_under_exact_vm_guard(self):
        identity='a'*64
        source=b"import json\nif __name__=='__main__':\n print(json.dumps({'forwarded':list(globals().get('OOM_ATTRIBUTION_IDS',())), 'host':globals().get('HOST_INSTANCE_NAME')}))\n"
        for cursor,expected in ((None,[]),([identity],[identity])):
            with self.subTest(cursor=cursor):
                def run(argv,**kwargs):
                    self.assertEqual(argv[:4],['gcloud','compute','ssh',self.vm.name])
                    self.assertIn('--zone='+self.vm.zone,argv)
                    self.assertEqual(kwargs['timeout'],60)
                    command=next(arg[len('--command='):] for arg in argv if arg.startswith('--command='))
                    shell=shlex.split(command)
                    self.assertEqual(shell[:3],['sudo','sh','-c'])
                    guarded=shell[3]
                    self.assertIn('http://metadata.google.internal/computeMetadata/v1/instance/id',guarded)
                    self.assertIn('= '+self.vm.instance_id+' || exit 75; python3 -c ',guarded)
                    program=shlex.split(guarded.split('; python3 -c ',1)[1])[0]
                    output=io.StringIO()
                    with redirect_stdout(output):exec(program,{'__name__':'__main__'})
                    return SimpleNamespace(stdout=output.getvalue())
                with patch('lib.fleet_evidence.Path.read_bytes',return_value=source),\
                     patch('lib.fleet_evidence.subprocess.run',side_effect=run):
                    observed=read_host_events('homelab-424523',self.vm,cursor)
                self.assertEqual(observed,{'forwarded':expected,'host':self.vm.name,'instance_id':self.vm.instance_id})

    def test_invalid_ids_fail_before_any_source_or_provider_access(self):
        for cursor in ('a'*64,[';print("bad")'],['A'*64],[None],['a'*64]*1001):
            with self.subTest(cursor=cursor),\
                 patch('lib.fleet_evidence.Path.read_bytes') as source,\
                 patch('lib.fleet_evidence.subprocess.run') as provider:
                with self.assertRaises(ValueError):read_host_events('project',self.vm,cursor)
                source.assert_not_called();provider.assert_not_called()

    def test_actual_standalone_source_accepts_optional_cursor_without_project_imports(self):
        from pathlib import Path
        source=Path('src/lib/host_evidence.py').read_text()
        # Execute the deployed definitions without main, replace only providers,
        # then execute the actual main statement with the injected cursor.
        definitions,entrypoint=source.rsplit("if __name__ == '__main__':",1)
        namespace={'__name__':'isolated-host'}
        exec(compile(definitions,'host-evidence','exec'),namespace)
        observed=[]
        namespace['sample']=lambda cursor=(),host=None:observed.append((cursor,host)) or {'events':[]}
        for cursor in ((),('b'*64,)):
            namespace['OOM_ATTRIBUTION_IDS']=cursor
            with redirect_stdout(io.StringIO()):exec(compile('if True:'+entrypoint,'host-main','exec'),namespace)
        self.assertEqual(observed,[((),None),(('b'*64,),None)])

    def test_invalid_vm_name_cannot_reach_source_injection_or_provider(self):
        for name in ('bad;name','UPPERCASE','a'*64):
            with patch('lib.fleet_evidence.Path.read_bytes') as source,\
                 patch('lib.fleet_evidence.subprocess.run') as provider:
                with self.assertRaises(ValueError):read_host_events('project',replace(self.vm,name=name))
                source.assert_not_called();provider.assert_not_called()
