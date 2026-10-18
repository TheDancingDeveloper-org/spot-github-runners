"""Disjoint missing-price history preserves known gaps and component lifetimes."""

import copy
import unittest

from lib.dashboard_history import (
    add_unpriced_interval,
    compact_unpriced_intervals,
    history_components,
    history_in_window,
    normalize_unpriced_intervals,
)


class DashboardHistoryIntervalsTests(unittest.TestCase):
    def test_legacy_pair_decodes_without_losing_negative_bounds(self):
        raw = {"prior_compute": [-100, -20], "suspended_memory": (0, None)}
        self.assertEqual(normalize_unpriced_intervals(raw),
                         {"prior_compute": [[-100.0, -20.0]], "suspended_memory": [[0.0, None]]})
        self.assertEqual(raw["prior_compute"], [-100, -20])

    def test_two_suspensions_leave_known_gap_clean(self):
        raw = {"suspended_memory": [[10, 20], [40, 50]]}
        self.assertFalse(history_in_window(raw, 20, 40))
        self.assertTrue(history_in_window(raw, 19, 21))
        self.assertTrue(history_in_window(raw, 39, 41))
        self.assertFalse(history_in_window(raw, 0, 10))
        self.assertFalse(history_in_window(raw, 50, 60))

    def test_only_overlapping_or_touching_ranges_merge(self):
        raw = {"prior_compute": [[40, 50], [10, 20], [19, 25], [25, 30], [12, 15], [40, 50]],
               "fallback_storage": [[30, 40]]}
        self.assertEqual(normalize_unpriced_intervals(raw),
                         {"prior_compute": [[10.0, 30.0], [40.0, 50.0]],
                          "fallback_storage": [[30.0, 40.0]]})

    def test_open_ranges_absorb_later_ranges_but_keep_earlier_gap(self):
        raw = {"prior_compute": [[60, 70], [30, None], [10, 20], [40, None], [30, 35]]}
        self.assertEqual(normalize_unpriced_intervals(raw),
                         {"prior_compute": [[10.0, 20.0], [30.0, None]]})
        self.assertFalse(history_in_window(raw, 20, 30))
        self.assertTrue(history_in_window(raw, 1000, 1001))

    def test_caller_closes_open_range_before_next_suspension(self):
        intervals = normalize_unpriced_intervals({"suspended_memory": [10, None]})
        intervals["suspended_memory"][-1][1] = 20
        intervals = normalize_unpriced_intervals(intervals)
        add_unpriced_interval(intervals, "suspended_memory", 40, None)
        intervals["suspended_memory"][-1][1] = 50
        self.assertEqual(normalize_unpriced_intervals(intervals),
                         {"suspended_memory": [[10.0, 20.0], [40.0, 50.0]]})
        self.assertFalse(history_in_window(intervals, 20, 40))

    def test_add_is_idempotent_and_normalizes_only_named_reason(self):
        untouched = [80, 90]
        intervals = {"prior_compute": [10, 20], "fallback_storage": untouched}
        for _ in range(3):
            add_unpriced_interval(intervals, "prior_compute", 40, 50)
        self.assertEqual(intervals["prior_compute"], [[10.0, 20.0], [40.0, 50.0]])
        self.assertIs(intervals["fallback_storage"], untouched)
        self.assertEqual(untouched, [80, 90])
        add_unpriced_interval(intervals, "prior_compute", 20, 40)
        self.assertEqual(intervals["prior_compute"], [[10.0, 50.0]])

    def test_invalid_bounds_do_not_erase_valid_evidence(self):
        raw = {"prior_compute": [[-20, -10], [0, float("nan")], [float("inf"), None],
                                 [10, 9], [None, 10], [True, 10], ["1", 2], [1], [0, 0]],
               "fallback_storage": "bad", "suspended_memory": []}
        self.assertEqual(normalize_unpriced_intervals(raw), {"prior_compute": [[-20.0, -10.0], [0.0, 0.0]]})
        add_unpriced_interval(raw, "prior_compute", 100, float("inf"))
        self.assertEqual(raw["prior_compute"], [[-20.0, -10.0], [0.0, 0.0]])
        for value in (None, [], 1, "invalid"):
            self.assertEqual(normalize_unpriced_intervals(value), {})

    def test_components_respect_reason_and_unknown_causes(self):
        for reason, expected in (("suspended_memory", (False, True)), ("fallback_storage", (False, True)),
                                 ("prior_compute", (True, False)), ("fallback_compute", (True, False)),
                                 ("future_reason", (True, True))):
            with self.subTest(reason=reason):
                self.assertEqual(history_components({reason: [-10, 0]}), expected)
        self.assertEqual(history_components({"prior_compute": []}), (False, False))
        self.assertEqual(history_components({"fallback_compute": [0, 1], "fallback_storage": [3, 4]}),
                         (True, True))

    def test_compute_only_window_ignores_storage_but_not_unknown_reasons(self):
        raw = {"suspended_memory": [[10, 20], [40, 50]], "prior_compute": [60, 70],
               "fallback_storage": [80, 90], "new_cause": [100, 110]}
        for start, end, expected in ((10, 20, False), (40, 50, False), (60, 70, True),
                                     (80, 90, False), (100, 110, True)):
            self.assertEqual(history_in_window(raw, start, end, include_storage=False), expected)
            self.assertTrue(history_in_window(raw, start, end))

    def test_empty_windows_and_zero_length_intervals_do_not_overlap(self):
        self.assertFalse(history_in_window({"prior_compute": [10, None]}, 20, 20))
        self.assertFalse(history_in_window({"prior_compute": [10, None]}, 30, 20))
        self.assertFalse(history_in_window({"prior_compute": [20, 20]}, 10, 30))

    def test_compaction_flags_include_pruned_intervals_and_keep_open_ones(self):
        raw = {"prior_compute": [[-200, -100], [0, 50]],
               "suspended_memory": [[20, 40], [55, 60], [80, 90]], "fallback_storage": [-300, None]}
        recent, compute_ever, storage_ever = compact_unpriced_intervals(raw, now=100, retention=50)
        self.assertEqual(recent, {"suspended_memory": [[55.0, 60.0], [80.0, 90.0]],
                                  "fallback_storage": [[-300.0, None]]})
        self.assertEqual((compute_ever, storage_ever), (True, True))
        self.assertFalse(history_in_window(recent, 60, 80, include_storage=False))
        self.assertEqual(compact_unpriced_intervals({"prior_compute": [-20, -10]}, 100, 50),
                         ({}, True, False))
        self.assertEqual(compact_unpriced_intervals({"new_cause": [-20, -10]}, 100, 50),
                         ({}, True, True))

    def test_compaction_preserves_straddling_bounds_and_known_gaps(self):
        raw = {"prior_compute": [[10, 60], [80, 90]]}
        self.assertEqual(compact_unpriced_intervals(raw, 100, 50),
                         ({"prior_compute": [[10.0, 60.0], [80.0, 90.0]]}, True, False))
        self.assertFalse(history_in_window(compact_unpriced_intervals(raw, 100, 50)[0], 60, 80))

    def test_pure_helpers_do_not_mutate_or_alias_input(self):
        raw = {"prior_compute": [[40, 50], [10, 20]]}
        saved = copy.deepcopy(raw)
        normalized = normalize_unpriced_intervals(raw)
        recent, _, _ = compact_unpriced_intervals(raw, 100, 100)
        history_components(raw)
        history_in_window(raw, 20, 40)
        self.assertEqual(raw, saved)
        normalized["prior_compute"][0][1] = 999
        recent["prior_compute"][0][1] = 888
        self.assertEqual(raw, saved)


if __name__ == "__main__":
    unittest.main()
