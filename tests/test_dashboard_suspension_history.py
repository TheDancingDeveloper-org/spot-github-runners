"""Preserve observed suspension boundaries and missing earlier compute evidence."""
import json
import unittest

from lib.dashboard_server import _retain_unpriced_history, _unpriced_history_in_window


class SuspensionTests(unittest.TestCase):
    def test_first_seen_resumed_vm_has_prior_compute_uncertainty(self):
        record = {"status": "RUNNING", "started_epoch": 900}
        obs = {"created_epoch": 100, "last_suspended_epoch": 700}
        _retain_unpriced_history(record, obs, None, 1000)
        record = json.loads(json.dumps(record))
        self.assertTrue(_unpriced_history_in_window(record, 100, 700, include_storage=False))
        self.assertFalse(_unpriced_history_in_window(record, 900, 1000, include_storage=False))

    def test_later_restart_does_not_extend_previous_suspension(self):
        previous = {"status": "RUNNING", "started_epoch": 500,
                    "unpriced_history_intervals": {"suspended_memory": [[400, 500]]}}
        record = {"status": "RUNNING", "started_epoch": 900}
        _retain_unpriced_history(record, {"last_suspended_epoch": 400}, previous, 1000)
        self.assertFalse(_unpriced_history_in_window(record, 600, 800, include_storage=True))

    def test_new_suspension_has_its_own_resume_boundary(self):
        previous = {"status": "RUNNING", "started_epoch": 500,
                    "unpriced_history_intervals": {"suspended_memory": [[400, 500]]}}
        record = {"status": "RUNNING", "started_epoch": 900}
        _retain_unpriced_history(record, {"last_suspended_epoch": 800}, previous, 1000)
        self.assertEqual(record["unpriced_history_intervals"]["suspended_memory"], [[400, 500], [800, 900]])
        self.assertFalse(_unpriced_history_in_window(record, 600, 700, include_storage=True))
        self.assertTrue(_unpriced_history_in_window(record, 800, 900, include_storage=True))

    def test_staging_does_not_confirm_a_resume_boundary(self):
        suspended = {"status": "SUSPENDED", "started_epoch": 300, "stopped_epoch": 400,
                     "unpriced_history_intervals": {"suspended_memory": [[400, None]]}}
        staging = {"status": "STAGING", "started_epoch": 300}
        _retain_unpriced_history(staging, {"last_suspended_epoch": 400}, suspended, 500)
        running = {"status": "RUNNING", "started_epoch": 700}
        _retain_unpriced_history(running, {"last_suspended_epoch": 400}, staging, 800)
        self.assertTrue(_unpriced_history_in_window(running, 550, 650, include_storage=True))
        self.assertFalse(_unpriced_history_in_window(running, 700, 800, include_storage=True))

    def test_compacted_suspension_does_not_reopen_on_a_later_restart(self):
        day = 86400
        previous = {"status": "RUNNING", "started_epoch": 2 * day}
        _retain_unpriced_history(previous, {"created_epoch": day // 2, "last_suspended_epoch": day},
                                 None, 2 * day)
        compacted = {"status": "RUNNING", "started_epoch": 2 * day}
        _retain_unpriced_history(compacted, {"last_suspended_epoch": day}, previous, 35 * day)
        self.assertNotIn("suspended_memory", compacted["unpriced_history_intervals"])
        restarted = {"status": "RUNNING", "started_epoch": 36 * day}
        _retain_unpriced_history(restarted, {"last_suspended_epoch": day}, compacted, 36 * day)
        self.assertFalse(_unpriced_history_in_window(restarted, 35 * day, 36 * day, include_storage=True))

    def test_missing_timestamp_carries_pending_suspension_through_resume(self):
        suspended = {"status": "SUSPENDED", "started_epoch": 300, "stopped_epoch": 400}
        _retain_unpriced_history(suspended, {"last_suspended_epoch": 400}, None, 450)
        staging = {"status": "STAGING", "started_epoch": 300}
        _retain_unpriced_history(staging, {"last_suspended_epoch": 400}, suspended, 500)
        running = {"status": "RUNNING", "started_epoch": 700}
        _retain_unpriced_history(running, {}, staging, 800)
        restored = {"status": "RUNNING", "started_epoch": 700}
        _retain_unpriced_history(restored, {"last_suspended_epoch": 400}, running, 900)
        self.assertTrue(_unpriced_history_in_window(restored, 550, 650, include_storage=True))
        self.assertFalse(_unpriced_history_in_window(restored, 700, 800, include_storage=True))

    def test_staging_with_old_start_keeps_suspension_uncertainty_open(self):
        previous = {"status": "SUSPENDED", "started_epoch": 300, "stopped_epoch": 400,
                    "unpriced_history_intervals": {"suspended_memory": [[400, None]]}}
        staging = {"status": "STAGING", "started_epoch": 300}
        _retain_unpriced_history(staging, {"last_suspended_epoch": 400}, previous, 600)
        later = {"status": "STAGING", "started_epoch": 300}
        _retain_unpriced_history(later, {}, staging, 700)
        self.assertTrue(_unpriced_history_in_window(later, 650, 700, include_storage=True))

    def test_legacy_suspended_record_seeds_pending_when_timestamp_is_missing(self):
        previous = {"status": "SUSPENDED", "started_epoch": 300, "stopped_epoch": 400,
                    "unpriced_history_intervals": {"suspended_memory": [[400, None]]}}
        staging = {"status": "STAGING", "started_epoch": 300}
        _retain_unpriced_history(staging, {}, previous, 500)
        running = {"status": "RUNNING", "started_epoch": 700}
        _retain_unpriced_history(running, {}, staging, 800)
        restored = {"status": "RUNNING", "started_epoch": 700}
        _retain_unpriced_history(restored, {"last_suspended_epoch": 400}, running, 900)
        self.assertTrue(_unpriced_history_in_window(restored, 550, 650, include_storage=True))

    def test_legacy_open_interval_seeds_pending_without_stop_timestamp(self):
        previous = {"status": "SUSPENDED", "started_epoch": 300,
                    "unpriced_history_intervals": {"suspended_memory": [[400, None]]}}
        staging = {"status": "STAGING", "started_epoch": 300}
        _retain_unpriced_history(staging, {}, previous, 500)
        later = {"status": "STAGING", "started_epoch": 300}
        _retain_unpriced_history(later, {}, staging, 700)
        self.assertTrue(_unpriced_history_in_window(later, 650, 700, include_storage=True))
