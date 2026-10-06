"""Exercise the completed hook with the real admission request decoder."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock,patch

from lib import host_admission
from lib.host_admission import LeasePool

ROOT=Path(__file__).resolve().parents[1]


class RunnerReleaseRetryTests(unittest.TestCase):
    def program(self):
        hook=(ROOT/'runner/job-completed.sh').read_text()
        self.assertLess(hook.index("<<'RELEASE_CLIENT'"),hook.index('gha-runner-clean-target.py'))
        return hook.split("<<'RELEASE_CLIENT'\n",1)[1].split('\nRELEASE_CLIENT',1)[0]

    def test_socket_restart_eof_malformed_and_rejected_replies_retry_until_ack(self):
        responses=[OSError('daemon unavailable'),b'',b'{',b'{}',b'[]',
                   b'{"accepted":false}',b'{"error":"daemon restoring"}',b'{"accepted":true}']
        clients=[];delays=[]
        def socket(*args):
            reply=responses.pop(0)
            if isinstance(reply,Exception):raise reply
            client=MagicMock();client.__enter__.return_value=client;client.recv.return_value=reply
            clients.append(client);return client
        with patch('runpy.run_path',return_value={'request':host_admission.request}),\
             patch.object(host_admission.socket,'socket',side_effect=socket),\
             patch('time.sleep',side_effect=delays.append),redirect_stderr(io.StringIO()):
            exec(self.program(),{})
        self.assertEqual(responses,[])
        self.assertEqual(delays,[5]*7)
        for client in clients:
            client.settimeout.assert_called_once_with(30)
            self.assertEqual(json.loads(client.sendall.call_args.args[0]),{'action':'release','kind':'runner'})
            client.__exit__.assert_called_once()

    def test_lost_ack_releases_persistent_owner_idempotently_before_hook_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            pool=LeasePool(Path(folder)/'leases.json',15,alive=lambda _:True)
            old='container:'+'a'*64+':start';new='container:'+'b'*64+':start'
            self.assertTrue(pool.acquire(old,15))
            self.assertFalse(pool.acquire(new,15))
            replies=[b'',b'{"accepted":true}'];requests=[];waiting=[]
            def socket(*args):
                client=MagicMock();client.__enter__.return_value=client
                def send(raw):
                    requests.append(json.loads(raw));pool.release(old)
                client.sendall.side_effect=send;client.recv.return_value=replies.pop(0)
                return client
            def retry(seconds):
                waiting.append(seconds)
                self.assertEqual(len(requests),1)
                self.assertEqual(json.loads(pool.path.read_text()),{})
            with patch('runpy.run_path',return_value={'request':host_admission.request}),\
                 patch.object(host_admission.socket,'socket',side_effect=socket),\
                 patch('time.sleep',side_effect=retry),redirect_stderr(io.StringIO()):
                exec(self.program(),{})
            self.assertEqual(waiting,[5]);self.assertEqual(len(requests),2)
            self.assertTrue(pool.acquire(new,15),'next persistent runner must fit after acknowledged release')

    def test_long_daemon_outage_does_not_drop_the_release_obligation(self):
        request=MagicMock(side_effect=[OSError('restoring')]*25+[True])
        output=io.StringIO()
        with patch('runpy.run_path',return_value={'request':request}),patch('time.sleep') as sleep,redirect_stderr(output):
            exec(self.program(),{})
        self.assertEqual(request.call_count,26);self.assertEqual(sleep.call_count,25)
        self.assertEqual(len(output.getvalue().splitlines()),3,'retry diagnostics must remain rate limited')

    def test_complete_shell_hook_runs_cleanup_only_after_ack_and_preserves_opt_out(self):
        for required,expected in [('1',['release','retry','release','cleanup']),('0',['cleanup'])]:
            with self.subTest(required=required),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);events=root/'events.json';events.write_text('[]')
                driver=root/'python3'
                driver.write_text('#!'+sys.executable+'\n'+'''import json,os,runpy,sys,time
from pathlib import Path
p=Path(os.environ['TEST_RELEASE_EVENTS'])
def event(value):
    values=json.loads(p.read_text());values.append(value);p.write_text(json.dumps(values));return values
def request(action):
    if event(action).count('release')==1:raise OSError('daemon restarting')
    return True
if sys.argv[1].endswith('gha-runner-clean-target.py'):
    event('cleanup')
else:
    runpy.run_path=lambda *args,**kwargs:{'request':request}
    time.sleep=lambda seconds:event('retry')
    exec(sys.stdin.read(),{})
''')
                driver.chmod(0o755)
                result=subprocess.run(['bash',str(ROOT/'runner/job-completed.sh')],
                    env={**os.environ,'PATH':str(root)+os.pathsep+os.environ.get('PATH',''),
                         'TEST_RELEASE_EVENTS':str(events),'GHA_ADMISSION_REQUIRED':required},
                    capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(events.read_text()),expected)
