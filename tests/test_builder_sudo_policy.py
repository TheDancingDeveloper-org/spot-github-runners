import sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];HOME=ROOT/'tests/fixtures/dedicated-builder';sys.path.insert(0,str(ROOT/'src'))
from lib import builder_access as access
ACTUAL=(HOME/'actual-sudo-verbose-original.stdout').read_text()
class SudoSourceHeaderTests(unittest.TestCase):
 def assess(self,text):
  with patch.object(access.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=text,stderr='')):access.effective_sudo_guard('oxidex-uploader')
 def test_actual_debian_source_header_and_original_format(self):
  self.assess(ACTUAL);self.assess(ACTUAL.replace('Sudoers entry: /etc/sudoers.d/oxidex-build','Sudoers entry:'))
 def test_unknown_source_header_unknown_line_and_duplicate_fields_refuse(self):
  for text in (ACTUAL.replace('/etc/sudoers.d/oxidex-build','/etc/sudoers.d/google-sudoers'),ACTUAL.replace('    RunAsUsers:', '    Unexpected metadata\n    RunAsUsers:'),ACTUAL.replace('    RunAsUsers: root','    RunAsUsers: root\n    RunAsUsers: root')):
   with self.assertRaises(ValueError):self.assess(text)
 def test_wider_commands_options_or_runas_refuse(self):
  for text in (ACTUAL.replace('/usr/local/bin/oxidex-remote-build','ALL'),ACTUAL.replace('RunAsUsers: root','RunAsUsers: ALL'),ACTUAL.replace('Options: !authenticate','Options: !authenticate, !use_pty')):
   with self.assertRaises(ValueError):self.assess(text)
if __name__=='__main__':unittest.main()
