"""Actual standalone policy recovery against synthetic root observations only."""
import base64
from contextlib import contextmanager, ExitStack
import grp
import hashlib
import json
from pathlib import Path
import pwd
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = '/opt/gha-runners/builder_access.py'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class PolicyRecoveryTests(unittest.TestCase):
    def fixture(self, phase='selected'):
        scope = runpy.run_path(str(ROOT/'src/lib/builder_runtime_update.py'))
        api = scope['recover_policy_update'].__globals__
        self.assertFalse(api['__package__'])  # Exercise the real standalone trust gate.
        old = (ROOT/'src/lib/builder_access.py').read_bytes()
        new = old + b'\n# reviewed replacement outside the historical trust set\n'
        self.assertNotIn(sha(new), api['TRUSTED_BUILDER_ACCESS_SHA256'])
        mapping = dict(schema='root-approved-ssh-mapping-v1', principal='test@example.invalid',
            username='oxidex-uploader', uid=2000, identity_receipt_sha256='1'*64,
            public_key='ssh-ed25519 '+base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\x00 '+b'x'*32).decode())
        mapping['public_key_sha256'] = sha(mapping['public_key'].encode())
        policy = {
            '/etc/ssh/sshd_config.d/40-oxidex-uploader-fence.conf':
                b'Match User oxidex-uploader\n    ForceCommand /usr/local/sbin/oxidex-uploader-session\n'
                b'    DisableForwarding yes\n    PermitTTY no\nMatch all\n',
            '/etc/sudoers.d/oxidex-uploader-session':
                b'oxidex-uploader ALL=(root) NOPASSWD: /usr/local/sbin/oxidex-uploader-session\n'}
        assets = {name:dict(data=base64.b64encode(policy.get(name, new if name == HELPER else b'fixture')).decode(),
            sha256=sha(policy.get(name, new if name == HELPER else b'fixture'))) for name in api['TARGETS']}
        files = {}; events = []; inode = [0]
        def put(name, raw, mode=0o600):
            inode[0] += 1
            files[str(name)] = (dict(uid=0, gid=0, mode=mode, sha256=sha(raw),
                                    device=1, inode=inode[0]), raw)
        put(HELPER, old, 0o644)
        before = {name:None for name in api['TARGETS']}
        before[HELPER] = files[HELPER][0].copy()
        for name, mode in api['POLICY_TARGETS'].items():
            put(name, b'# original policy\n', mode); before[name] = files[name][0].copy()
        plan = dict(schema=1, nonce='a'*32, source_main='b'*40, vm_id='123',
            boot='0b99c4d2-ca5f-4471-bf84-ff4ab8dbefd3', image_id='sha256:'+'c'*64,
            config_sha256='d'*64, admission_sha256='e'*64, service_sha256='f'*64,
            service_pid=12, service_startticks=34, observed_at=1000., assets=assets, before=before)
        backup = '/var/lib/oxidex-builder/runtime-update-'+plan['nonce']
        put(backup+'/plan.json', json.dumps(plan).encode())
        stages = ['BACKUP_START', 'BACKUPS_COMPLETE', 'MUTATING_HELD',
                  'PERSISTENT_BOOT_HOLD_INSTALLED', 'HOLD_EXPLICIT_RECOVERY']
        put(backup+'/journal.jsonl', b''.join(json.dumps(dict(stage=s)).encode()+b'\n' for s in stages))
        for name in policy:
            put(backup+'/'+str(sorted(api['TARGETS']).index(name))+'.before', b'# original policy\n')
            put(name, policy[name], api['POLICY_TARGETS'][name])
        if phase != 'old':
            put(HELPER, new if phase == 'selected' else b'raise AssertionError("foreign code executed")\n', 0o644)
        put('/etc/oxidex-builder-uploader.json', json.dumps(mapping).encode())
        put(api['PENDING_UPDATE'], json.dumps(dict(nonce=plan['nonce'], boot=plan['boot'], source_main=plan['source_main'])).encode())
        put('/run/oxidex-build-draining', plan['nonce'].encode())
        put('/var/lib/gha-admission/leases.json', b'{}')
        put('/etc/oxidex-remote-build.json', b'config')
        files['/etc/oxidex-remote-build.json'][0]['sha256'] = plan['config_sha256']
        def read(path):
            name = str(path); events.append(('read', name))
            state, raw = files.get(name, (None, b''))
            return (dict(state) if state else None), raw
        @contextmanager
        def retained(*args, **kwargs):
            yield lambda:None
        def write(path, raw, mode, *, before_replace=None, **kwargs):
            if before_replace:before_replace()
            events.append(('write', str(path))); put(path, raw, mode)
        def append(path, row, *, before_publish, selected_history):
            before_publish(); self.assertEqual(read(path), selected_history)
            put(path, read(path)[1]+json.dumps(row).encode()+b'\n'); return read(path)
        def finish(lock, drain, token, check, action, *, release, lock_bound, root_custody):
            self.assertFalse(release); self.assertTrue(root_custody)
            def guard():events.append(('lock', 'held'))
            lock_bound(guard); check(); result=action(); check(); return result
        def command(argv):
            events.append(('command', tuple(argv)))
            if argv[0] == '/usr/bin/curl':return '123'
            self.assertIn(argv[0], ('/usr/bin/docker', '/usr/sbin/sshd', '/usr/sbin/visudo', '/usr/bin/systemctl'))
            return ''
        api.update(root_file_read=read, root_file_state=lambda path:read(path)[0],
            retained_ancestor_selection=retained, retained_file_selection=retained,
            atomic_write=write, append_journal_record=append, finish_owned_drain=finish,
            verify_boot_guard=lambda:None, checked_command=command)
        return api, plan, files, events, backup

    def recover(self, api, plan):
        account=SimpleNamespace(pw_name='oxidex-uploader', pw_uid=2000, pw_gid=1001)
        group=SimpleNamespace(gr_name='oxidex-build', gr_gid=1001, gr_mem=[])
        real_iterdir=Path.iterdir
        def iterdir(path):
            return iter(()) if str(path) == '/proc' else real_iterdir(path)
        with ExitStack() as stack:
            stack.enter_context(patch.object(pwd, 'getpwnam', return_value=account))
            stack.enter_context(patch.object(pwd, 'getpwall', return_value=[account]))
            stack.enter_context(patch.object(grp, 'getgrall', return_value=[group]))
            stack.enter_context(patch.object(Path, 'iterdir', iterdir))
            return api['recover_policy_update'](plan)

    def test_selected_helper_after_partial_apply_restores_policies_and_keeps_drain(self):
        api, plan, files, events, backup=self.fixture()
        result=self.recover(api, plan)
        self.assertEqual(result['status'], 'POLICY_ROLLED_BACK_HELD')
        for path in api['POLICY_TARGETS']:self.assertEqual(files[path][1], b'# original policy\n')
        self.assertEqual(files['/run/oxidex-build-draining'][1], plan['nonce'].encode())
        self.assertEqual(json.loads(files[backup+'/journal.jsonl'][1].splitlines()[-1])['stage'], 'POLICY_ROLLED_BACK_HELD')
        first_helper=events.index(('read', HELPER))
        for path in (backup+'/plan.json', backup+'/journal.jsonl', str(api['PENDING_UPDATE']), '/run/oxidex-build-draining'):
            self.assertLess(events.index(('read', path)), first_helper)
        self.assertIn(('lock', 'held'), events[:first_helper])

    def test_still_installed_historical_helper_restores_policies(self):
        api, plan, files, events, backup=self.fixture('old')
        self.assertEqual(self.recover(api, plan)['status'], 'POLICY_ROLLED_BACK_HELD')
        for path in api['POLICY_TARGETS']:self.assertEqual(files[path][1], b'# original policy\n')

    def test_same_candidate_bytes_still_in_historical_mode_are_recoverable(self):
        api, plan, files, events, backup=self.fixture('old')
        raw=files[HELPER][1]
        files[HELPER][0]['mode']=0o600
        plan['before'][HELPER]['mode']=0o600
        plan['assets'][HELPER]=dict(data=base64.b64encode(raw).decode(), sha256=sha(raw))
        saved=json.dumps(plan).encode()
        files[backup+'/plan.json']=(dict(files[backup+'/plan.json'][0], sha256=sha(saved)), saved)
        self.assertEqual(self.recover(api, plan)['status'], 'POLICY_ROLLED_BACK_HELD')

    def test_foreign_drain_refused_before_loading_selected_helper(self):
        api, plan, files, events, backup=self.fixture()
        files['/run/oxidex-build-draining']=(files['/run/oxidex-build-draining'][0], b'foreign')
        with self.assertRaisesRegex(ValueError, 'Owned policy rollback drain changed'):
            self.recover(api, plan)
        self.assertNotIn(('read', HELPER), events)
        self.assertFalse(any(kind == 'write' for kind, _ in events))

    def test_foreign_helper_is_not_executed_or_restored(self):
        api, plan, files, events, backup=self.fixture('foreign')
        with self.assertRaises(ValueError):self.recover(api, plan)
        self.assertFalse(any(kind == 'write' for kind, _ in events))

    def test_changed_saved_plan_refused_before_selected_helper_execution(self):
        api, plan, files, events, backup=self.fixture()
        files[backup+'/plan.json']=(files[backup+'/plan.json'][0], b'{}')
        with self.assertRaisesRegex(ValueError, 'Recovery plan differs'):self.recover(api, plan)
        self.assertNotIn(('read', HELPER), events)


if __name__ == '__main__':unittest.main()
