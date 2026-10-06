"""Real small processes and locks; Linux additionally exercises adopted descendants.

macOS uses group cleanup, not a mock Linux subreaper. Root/drop and installed
Linux integration remain remote controls. No builds, network or large inputs.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = str(Path(__file__).resolve().parents[1] / 'src')


def wait_for(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Bounded process control did not complete')


def running(pid):
    value = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, timeout=2)
    return value.returncode == 0 and bool(value.stdout.strip()) and not value.stdout.strip().startswith('Z')


class SupervisionTests(unittest.TestCase):
    def lifecycle(self, termination):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            # The helper and grandchild must never receive any protective lock FD.
            child = """import os,sys,json,time,fcntl
from pathlib import Path
root=Path(sys.argv[1])
for fd in json.loads(sys.argv[2]):
    try: os.fstat(fd)
    except OSError: pass
    else: raise SystemExit('protective descriptor leaked')
pid=os.fork()
if pid==0:
    (root/'grandchild').write_text(str(os.getpid()))
    time.sleep(30)
else:
    (root/'child').write_text(str(os.getpid()))
    time.sleep(30)
"""
            # Locks belong to the launcher initially; the guardian inherits them.
            driver = f"""import sys,os,fcntl,tempfile
sys.path.insert(0,{SOURCE!r})
from lib.qualification_trust.host_gate import wait_guardian
root={str(root)!r}
locks=[]
for name in ('build','namespace','import'):
    fd=os.open(root+'/'+name,os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_EX);locks.append(fd)
def command(reader,fds):
    code='import sys;sys.path.insert(0,'+repr({SOURCE!r})+');from lib.qualification_trust.import_supervisor import supervise;raise SystemExit(supervise('+repr([{sys.executable!r},'-c',{child!r},root,__import__('json').dumps(fds)])+','+str(reader)+','+repr(fds)+',timeout='+str({.4 if termination is None else 10})+',subreaper='+str(sys.platform=='linux')+'))'
    return [sys.executable,'-I','-B','-c',code]
with open(root+'/diagnostics','w+b') as output:wait_guardian(command,tuple(locks),output)
"""
            owner = subprocess.Popen([sys.executable, '-I', '-B', '-c', driver], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pids = []
            try:
                wait_for(lambda: (root/'child').exists() and (root/'grandchild').exists())
                pids = [int((root/name).read_text()) for name in ('child', 'grandchild')]
                self.assertTrue(all(running(pid) for pid in pids))
                with (open(root/'build', 'r+b') as build_lock,
                      open(root/'namespace', 'r+b') as namespace_lock,
                      open(root/'import', 'r+b') as import_lock):
                    fds = [build_lock.fileno(), namespace_lock.fileno(), import_lock.fileno()]
                    for fd in fds:
                        with self.assertRaises(BlockingIOError):fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if termination is not None:os.kill(owner.pid, termination)
                    def released():
                        try:
                            for fd in fds:fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:return False
                        return True
                    wait_for(released)
                    self.assertFalse(any(running(pid) for pid in pids), (root/'diagnostics').read_text())
                    owner.wait(timeout=3)
            finally:
                if owner.poll() is None:owner.kill();owner.wait(timeout=3)
                for pid in pids:
                    if running(pid):
                        try:os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass  # The observed cleanup target already exited.

    def test_term_retains_locks_until_child_and_grandchild_stop(self):self.lifecycle(signal.SIGTERM)
    def test_hup_retains_locks_until_child_and_grandchild_stop(self):self.lifecycle(signal.SIGHUP)
    def test_owner_sigkill_still_has_guardian_and_locks(self):self.lifecycle(signal.SIGKILL)
    def test_deadline_terminates_descendants(self):self.lifecycle(None)

    def test_production_guardian_requires_linux_subreaper(self):
        from lib.qualification_trust import import_supervisor
        with patch.object(import_supervisor.sys, 'platform', 'darwin'):
            with self.assertRaises(RuntimeError):import_supervisor.enable_subreaper()


class OracleTransportTests(unittest.TestCase):
    def test_exact_clone_uses_fresh_config_and_only_https_exception(self):
        from lib.qualification_trust import runtime_git as wrapper, git_sandbox
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();(root/'tools/release').mkdir(parents=True)
            (root/'.exiftool-version').write_text('13.59\n')
            (root/'tools/release/oracle-lock.json').write_text(json.dumps({'exiftool':dict(version='13.59',repo=wrapper.ORIGIN,tag_object='a'*40)}))
            destination=Path('/target/ops/cache/exiftool/13.59/.exiftool.staging-123-abcdef')
            with patch.object(wrapper,'CHECKOUT',root),patch.object(Path,'resolve',lambda p:p), \
                 patch.object(Path,'is_dir',return_value=True),patch.object(Path,'iterdir',return_value=iter(())):
                command,env=wrapper.oracle_clone(['clone','--no-checkout',wrapper.ORIGIN,str(destination)])
            self.assertIn('protocol.https.allow=always',command)
            self.assertIn('--template=',command)
            self.assertEqual(env['GIT_CONFIG_NOSYSTEM'],'1')
            self.assertNotIn('GIT_CONFIG_COUNT',env)
            # Ask real Git to resolve the policy; no network operation is performed.
            query=command[:command.index('clone')]+['config','--get','protocol.https.allow']
            self.assertEqual(subprocess.check_output(query,env=env,cwd='/',timeout=3).strip(),b'always')
            imported=git_sandbox.clean_environment()
            self.assertNotIn('protocol.https.allow',[v for k,v in imported.items() if k.startswith('GIT_CONFIG_KEY_')])
            for args in (['clone','--no-checkout','https://evil.example/x',str(destination)],
                         ['clone','--no-checkout',wrapper.ORIGIN,'/target/other']):
                with patch.object(wrapper,'CHECKOUT',root),self.assertRaises(ValueError):wrapper.oracle_clone(args)
            # Mutable destination repository config cannot affect this exception.
            with patch.object(wrapper,'CHECKOUT',root),patch.object(Path,'resolve',lambda p:p), \
                 patch.object(Path,'is_dir',return_value=True),patch.object(Path,'iterdir',return_value=iter([Path('config')])):
                with self.assertRaises(ValueError):wrapper.oracle_clone(['clone','--no-checkout',wrapper.ORIGIN,str(destination)])

    def test_runtime_and_scrubbed_system_config_both_deny_general_https(self):
        from lib.builder_only_assets import ASSETS
        import configparser
        value=configparser.ConfigParser();value.read_string(ASSETS['/etc/oxidex-remote-build/qualification.gitconfig'])
        self.assertEqual(value['protocol "https"']['allow'],'never')
        text=(Path(SOURCE)/'lib/qualification_trust/runtime.py').read_text()
        self.assertIn("'protocol.https.allow': 'never'",text)
        from lib.qualification_trust.mounts import expected_mounts
        self.assertEqual(expected_mounts('/mnt/runner-data/remote-build','qualification-'+'a'*12+'-'+'b'*32)['/qualification-bin/git'],('/opt/oxidex-builder/qualification_trust/runtime_git.py',False))


if __name__=='__main__':unittest.main()
