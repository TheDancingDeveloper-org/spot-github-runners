import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];HOME=ROOT/'tests/fixtures/dedicated-builder';sys.path.insert(0,str(ROOT/'src'))
from lib import builder_fresh_guard as guard
class TemplateTests(unittest.TestCase):
 def command(self,argv):
  self.calls.append(argv)
  if argv[1]=='list-unit-files':return 'safe@.service disabled -\nbase.service enabled -\nalias.service alias -\n'
  if argv[1]=='cat':
   self.assertIn('safe@.service',argv);return '# /usr/lib/systemd/system/safe@.service\n[Service]\nExecStart='+self.template_exec+'\n'
  if argv[1]=='list-units':return self.loaded
  if argv[1]=='show':
   self.assertIn('--all',argv);names=[v for v in argv[2:]if not v.startswith('--')];self.assertFalse(any(n.endswith('@.service')for n in names));rows=[]
   for name in names:
    identity='base.service'if name=='alias.service'else name
    names_field='base.service alias.service'if identity=='base.service'else identity
    record={'Id':identity,'Names':names_field,'Description':'normal service'}
    if self.actual_record is not None and name==self.actual_record['Id'].replace(chr(92)*2,chr(92)):record=dict(self.actual_record)
    if self.exec_present:record['ExecStart']='/usr/bin/sleep infinity'
    if self.missing is not None:record.pop(self.missing,None)
    if self.unknown:record['Unexpected']='untrusted'
    rows.append('\n'.join(k+'='+v for k,v in record.items()))
   return '\n\n'.join(rows)+'\n'
  raise AssertionError(argv)
 def setUp(self):self.actual_record=None;self.exec_present=False;self.missing=None;self.unknown=False;self.calls=[];self.loaded='base.service loaded active running Base\nsafe@7.service loaded active running Instance\n';self.template_exec='/usr/bin/sleep infinity';self.temp=tempfile.TemporaryDirectory()
 def tearDown(self):self.temp.cleanup()
 def run_guard(self):
  with patch.object(guard,'command',side_effect=self.command):guard.fresh_runtime_guard(Path(self.temp.name))
 def test_actual_template_files_and_loaded_instances_alias_coverage(self):
  self.run_guard();self.assertTrue(any(a[1]=='cat'for a in self.calls));self.assertTrue(any('safe@7.service'in a for a in self.calls if a[1]=='show'))
 def test_template_exec_runner_refuses(self):
  self.template_exec='/opt/actions/Runner.Listener'
  with self.assertRaisesRegex(ValueError,'runner/admission'):self.run_guard()
 def test_loaded_runner_instance_refuses(self):
  self.loaded+='gha-runner@9.service loaded active running Runner\n'
  with self.assertRaisesRegex(ValueError,'runner/admission'):self.run_guard()
 def test_actual_device_hex_escape_accepts_and_malformed_refuses(self):
  actual='systemd-fsck@dev-disk-by'+chr(92)+'x2dpartuuid-3a46eb76'+chr(92)+'x2d31ed.service'
  self.loaded=actual+' loaded active exited Device\n'
  self.actual_record={'Id':actual,'Names':'\"'+actual.replace(chr(92),chr(92)*2)+'\"','Description':'Device'};self.run_guard();self.actual_record=None
  for bad in ('systemd-fsck@dev'+chr(92)+'x2.service','systemd-fsck@dev'+chr(92)+'xGG.service','systemd-fsck@dev'+chr(92)+'q.service'):
   self.loaded=bad+' loaded active exited Device\n'
   with self.assertRaises(ValueError):self.run_guard()
 def test_actual_systemctl_quoted_names_roundtrip(self):
  import json
  self.actual_record=json.loads((HOME/'actual-systemctl-escaped-record.json').read_bytes())
  actual=self.actual_record['Id'];self.loaded=actual+' loaded active exited Device\n';self.run_guard()
  self.actual_record['Id']=actual.replace(chr(92),chr(92)*2);self.run_guard()
  self.actual_record['Names']='"'+actual.replace(chr(92),chr(92)*4)+'"';self.run_guard()
 def test_actual_sparse_exec_fields_accept_identity_missing_or_unknown_refuses(self):
  self.run_guard();self.exec_present=True;self.run_guard()
  for field in ('Id','Names','Description'):
   self.missing=field
   with self.assertRaisesRegex(ValueError,'Incomplete service properties'):self.run_guard()
  self.missing=None;self.unknown=True
  with self.assertRaisesRegex(ValueError,'Incomplete service properties'):self.run_guard()
 def test_unknown_or_incomplete_loaded_inventory_refuses(self):
  for bad in ('', 'safe@.service loaded active running Bad\n', 'nonsense\n'):
   self.loaded=bad
   with self.assertRaises(ValueError):self.run_guard()
if __name__=='__main__':unittest.main()
