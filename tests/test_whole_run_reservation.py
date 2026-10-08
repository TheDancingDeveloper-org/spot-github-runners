"""Small synthetic custody controls; never create live reservations or jobs."""
import importlib.util
from pathlib import Path
import os
import fcntl
import sys
from contextlib import ExitStack
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1]/'src/lib/qualification_trust/reservation.py'
spec = importlib.util.spec_from_file_location('whole_run_reservation', MODULE)
reservation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reservation)


class ReservationTests(unittest.TestCase):
    def setUp(self):
        # Avoid world-writable /tmp; production nofollow ancestor rules apply.
        self.folder = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.store = reservation.Store(self.folder.name, owner_uid=os.getuid())
        self.head = 'a'*40
        self.project = 'qualification-'+self.head[:12]+'-'+'b'*32
        self.host = ('438', '0b99c4d2-ca5f-4471-bf84-ff4ab8dbefd3')
        self.argv = [['prepare'], ['upload'], ['verify-source'], ['bootstrap'], ['execute'], ['pack'], ['replay'], ['cleanup']]
        self.stages = [dict(ordinal=i, command=argv[0], argv_sha256=reservation.digest(argv)) for i, argv in enumerate(self.argv)]
        self.session = dict(schema=1, project=self.project, attempt='final-r1',
                            expected_vm_id=self.host[0], expected_boot=self.host[1],
                            head=self.head, tree='c'*40, bundle_sha256='d'*64,
                            attestation_sha256='e'*64,
                            attestation_namespace='oxidex-qualification-source-v1',
                            stages_sha256=reservation.digest(self.stages))
        self.actor = {'pid': 100, 'start': 200}

    def tearDown(self):
        self.folder.cleanup()

    def claim(self, **changes):
        return self.store.acquire(self.session, self.stages, **{
            **dict(host=self.host, owner=self.actor,
                   authenticate=lambda session: {**session, 'run_id': self.project},
                   quiescent=lambda: True), **changes})

    def step(self, record, ordinal, **changes):
        identity = record['reservation_id']
        self.store.begin(identity, ordinal, host=self.host, actor=self.actor, argv=self.argv[ordinal])
        return self.store.finish(identity, **{**dict(host=self.host, actor=self.actor,
                            exit_code=0, log_sha256='f'*64, quiescent=lambda: True,
                            binding=self.session if ordinal == 2 else None), **changes})

    def test_foreign_route_and_between_stage_gap_are_blocked(self):
        record = self.claim()
        with self.assertRaises(ValueError):
            self.store.route('foreign', host=self.host, actor_matches=lambda actor: True)
        self.step(record, 0)
        with self.assertRaises(ValueError):
            self.store.route(self.project, host=self.host, actor_matches=lambda actor: True)

    def test_wrong_host_and_false_claim_never_acquire(self):
        for changes in [dict(host=('439', self.host[1])), dict(authenticate=lambda session: True),
                        dict(quiescent=lambda: False)]:
            with self.assertRaises(ValueError):
                self.claim(**changes)
            self.assertIsNone(self.store.read())

    def test_duplicate_acquire_and_stage_replay_refuse(self):
        record = self.claim()
        with self.assertRaises(ValueError):
            self.claim()
        self.step(record, 0)
        with self.assertRaises(ValueError):
            self.store.begin(record['reservation_id'], 0, host=self.host, actor=self.actor, argv=self.argv[0])

    def test_unknown_child_retains_actual_active_stage(self):
        record = self.claim()
        self.store.begin(record['reservation_id'], 0, host=self.host, actor=self.actor, argv=self.argv[0])
        with self.assertRaises(ValueError):
            self.store.finish(record['reservation_id'], host=self.host, actor=self.actor,
                              exit_code=0, log_sha256='f'*64, quiescent=lambda: None)
        self.assertIsNotNone(self.store.read()['active'])

    def test_actual_binding_mismatch_blocks_execution(self):
        record = self.claim()
        self.step(record, 0); self.step(record, 1)
        with self.assertRaises(ValueError):
            self.step(record, 2, binding={**self.session, 'tree': '0'*40})
        self.assertEqual(self.store.read()['state'], 'HELD')
        self.assertIsNotNone(self.store.read()['active'])

    def test_release_preserves_evidence_and_returns_tombstone(self):
        evidence = Path(self.folder.name)/'evidence'
        evidence.write_text('retain')
        record = self.claim()
        for ordinal in range(len(self.argv)):
            self.step(record, ordinal)
        result = self.store.release(record['reservation_id'], host=self.host, quiescent=lambda: True)
        self.assertEqual(result['state'], 'RELEASED')
        self.assertEqual(evidence.read_text(), 'retain')
        self.assertEqual(self.store.release(record['reservation_id'], host=self.host,
                                           quiescent=lambda: None), result)

    def test_failure_is_held_and_cannot_be_replayed(self):
        record = self.claim()
        self.step(record, 0, exit_code=1)
        self.assertEqual(self.store.read()['state'], 'HELD')
        with self.assertRaises(ValueError):
            self.store.begin(record['reservation_id'], 1, host=self.host, actor=self.actor, argv=self.argv[1])

    def test_symlink_state_is_refused(self):
        outside = Path(self.folder.name)/'outside'
        outside.write_text('{}')
        (Path(self.folder.name)/'record.json').symlink_to(outside)
        with self.assertRaises(OSError):
            self.store.read()

    def test_foreign_stage_actor_and_changed_boot_refuse(self):
        record = self.claim()
        self.store.begin(record['reservation_id'], 0, host=self.host, actor=self.actor, argv=self.argv[0])
        for host, match in [(self.host, lambda actor: False), (('438', 'newboot'), lambda actor: True)]:
            with self.assertRaises(ValueError):
                self.store.route(self.project, host=host, actor_matches=match)

    def test_argv_mismatch_and_skipped_source_stage_refuse(self):
        record = self.claim()
        for ordinal, argv in [(0, ['different']), (3, self.argv[3])]:
            with self.assertRaises(ValueError):
                self.store.begin(record['reservation_id'], ordinal, host=self.host, actor=self.actor, argv=argv)

    def test_lost_stage_ack_recovery_preserves_interruption(self):
        record = self.claim()
        identity = record['reservation_id']
        self.store.begin(identity, 0, host=self.host, actor=self.actor, argv=self.argv[0])
        self.store.cleanup_after_failure(identity, host=self.host, actor=self.actor,
                                         argv=self.argv[-1], quiescent=lambda: True)
        self.store.finish(identity, host=self.host, actor=self.actor, exit_code=0,
                          log_sha256='f'*64, quiescent=lambda: True)
        result = self.store.release(identity, host=self.host, quiescent=lambda: True)
        self.assertIsNone(result['events'][0]['exit_code'])
        self.assertEqual(result['events'][0]['disposition'], 'INTERRUPTED')

    def test_released_run_cannot_reacquire_but_new_run_can(self):
        record = self.claim()
        for ordinal in range(len(self.argv)):
            self.step(record, ordinal)
        self.store.release(record['reservation_id'], host=self.host, quiescent=lambda: True)
        with self.assertRaises(ValueError):
            self.claim()
        old = dict(self.session)
        self.project = 'qualification-'+self.head[:12]+'-'+'c'*32
        self.session = {**self.session, 'project': self.project}
        new = self.claim()
        self.assertNotEqual(new['reservation_id'], record['reservation_id'])
        self.assertEqual(self.store.read('released-'+old['project']+'.json')['reservation_id'], record['reservation_id'])
        retry = self.store.release(record['reservation_id'], host=self.host,
                                   project=old['project'], quiescent=lambda: None)
        self.assertEqual(retry['state'], 'RELEASED')
        self.assertEqual(self.store.read()['reservation_id'], new['reservation_id'])

    def test_failed_cleanup_cannot_release(self):
        record = self.claim()
        self.store.cleanup_after_failure(record['reservation_id'], host=self.host,
                                         actor=self.actor, argv=self.argv[-1], quiescent=lambda: True)
        self.store.finish(record['reservation_id'], host=self.host, actor=self.actor,
                          exit_code=1, log_sha256='f'*64, quiescent=lambda: True)
        with self.assertRaises(ValueError):
            self.store.release(record['reservation_id'], host=self.host, quiescent=lambda: True)

    def test_production_route_guard_uses_actual_host_ancestry_and_argv(self):
        record = self.claim()
        self.store.begin(record['reservation_id'], 0, host=self.host, actor=self.actor, argv=self.argv[0])
        with patch.object(reservation, 'ROOT', Path(self.folder.name)), \
             patch.object(reservation, 'Store', return_value=self.store), \
             patch.object(reservation, 'host_identity', return_value=self.host), \
             patch.object(reservation, 'ancestor_matches', return_value=True):
            reservation.route_guard(self.project, argv=self.argv[0])
            with self.assertRaises(ValueError):
                reservation.route_guard('foreign', argv=self.argv[0])
            with self.assertRaises(ValueError):
                reservation.route_guard(self.project, argv=['different'])
        with patch.object(reservation, 'ROOT', Path(self.folder.name)), \
             patch.object(reservation, 'Store', return_value=self.store), \
             patch.object(reservation, 'host_identity', return_value=self.host), \
             patch.object(reservation, 'ancestor_matches', return_value=False):
            with self.assertRaises(ValueError):
                reservation.route_guard(self.project, argv=self.argv[0])

    def test_retained_record_never_calls_process_reclamation(self):
        self.claim()
        with patch.object(reservation, 'ROOT', Path(self.folder.name)), \
             patch.object(reservation, 'Store', return_value=self.store), \
             patch.object(reservation, 'host_identity', return_value=self.host), \
             patch.object(reservation, 'ancestor_matches') as ancestors:
            with self.assertRaises(ValueError):
                reservation.route_guard(self.project)
            ancestors.assert_not_called()

    def test_corrupted_release_cannot_bypass_route_fence(self):
        record = self.claim()
        record['state'] = 'RELEASED'
        self.store.write(record)
        with self.assertRaises(ValueError):
            self.store.read()

    def test_boolean_cleanup_and_missing_log_tombstones_fail_closed(self):
        record = self.claim()
        record.update(state='RELEASED', next_ordinal=len(self.argv), active=None)
        for event in [dict(ordinal=7, command='cleanup', actor=self.actor, exit_code=False, log_sha256='f'*64),
                      dict(ordinal=7, command='cleanup', actor=self.actor, exit_code=0, log_sha256=None)]:
            record['events'] = [event]
            self.store.write(record)
            with self.assertRaises(ValueError):
                self.store.read()

    def test_mismatched_finish_durably_closes_actor_access(self):
        record = self.claim()
        self.step(record, 0); self.step(record, 1)
        with self.assertRaises(ValueError):
            self.step(record, 2, binding={**self.session, 'tree': '0'*40})
        with patch.object(reservation, 'ROOT', Path(self.folder.name)), \
             patch.object(reservation, 'Store', return_value=self.store), \
             patch.object(reservation, 'host_identity', return_value=self.host), \
             patch.object(reservation, 'ancestor_matches', return_value=True):
            with self.assertRaises(ValueError):
                reservation.route_guard(self.project, argv=self.argv[2])

    def test_admission_guard_retains_real_shared_lock_through_grant(self):
        module_path = MODULE.parents[1]/'host_admission.py'
        admission_spec = importlib.util.spec_from_file_location('reservation_admission_fixture', module_path)
        admission = importlib.util.module_from_spec(admission_spec)
        admission_spec.loader.exec_module(admission)
        lock = Path(self.folder.name)/'oxidex-build.lock'
        with patch.object(admission, 'LOCK_ROOT', Path(self.folder.name)), \
             patch.dict(sys.modules, {'qualification_trust': SimpleNamespace(),
                                      'qualification_trust.reservation': SimpleNamespace(route_guard=lambda **kwargs: None)}):
            with ExitStack() as custody:
                admission.hold_builder_reservation(custody, {'role': 'builder'}, os.getpid())
                with lock.open('a') as competing:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with lock.open('a') as after_grant:
                fcntl.flock(after_grant, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_runtime_update_enrolls_new_required_dependency(self):
        sys.path.insert(0, str(MODULE.parents[2]))
        from lib.builder_runtime_update import TARGETS
        self.assertEqual(TARGETS['/opt/oxidex-builder/qualification_trust/reservation.py'], 0o644)
