"""Principal and tax uncertainty select independently, including legacy history."""

import copy
import json
import unittest

from lib.dashboard_history import (compact_unpriced_intervals, history_components, history_in_window,
                                   normalize_unpriced_intervals, split_legacy_tax_intervals,
                                   tax_history_components)


class TaxHistoryComponentTests(unittest.TestCase):
    def test_explicit_component_selection_matrix_for_pair_and_nested_formats(self):
        # Columns: compute principal; all principal; compute plus tax; all plus tax.
        matrix = {"fallback_compute": (True, True, True, True),
                  "prior_compute": (True, True, True, True),
                  "fallback_storage": (False, True, False, True),
                  "suspended_memory": (False, True, False, True),
                  "fallback_compute_tax": (False, False, True, True),
                  "fallback_storage_tax": (False, False, False, True),
                  "fallback_tax": (False, False, True, True),
                  "unknown_future_principal": (True, True, True, True)}
        modes = ((False, False), (True, False), (False, True), (True, True))
        for reason, expected in matrix.items():
            for ranges in ([1, 3], [[1, 3]]):
                for (storage, tax), value in zip(modes, expected):
                    with self.subTest(reason=reason, ranges=ranges, storage=storage, tax=tax):
                        self.assertEqual(history_in_window({reason: ranges}, 1, 3, storage,
                                                           include_tax=tax), value)

    def test_omitted_tax_selection_defaults_to_storage_mode(self):
        for reason in ("fallback_compute_tax", "fallback_storage_tax", "fallback_tax"):
            for storage in (False, True):
                with self.subTest(reason=reason, storage=storage):
                    raw = {reason: [1, 3]}
                    self.assertEqual(history_in_window(raw, 1, 3, storage), storage)
                    self.assertEqual(history_in_window(raw, 1, 3, storage, include_tax=None), storage)

    def test_principal_and_tax_lifetime_flags_are_separate(self):
        matrix = {"fallback_compute": ((True, False), (False, False)),
                  "fallback_storage": ((False, True), (False, False)),
                  "fallback_compute_tax": ((False, False), (True, False)),
                  "fallback_storage_tax": ((False, False), (False, True)),
                  "fallback_tax": ((False, False), (True, True)),
                  "unknown_future_principal": ((True, True), (False, False))}
        for reason, (principal, tax) in matrix.items():
            for ranges in ([1, 3], [[1, 3]], [[2, 2]]):
                with self.subTest(reason=reason, ranges=ranges):
                    self.assertEqual(history_components({reason: ranges}), principal)
                    self.assertEqual(tax_history_components({reason: ranges}), tax)

    def test_mixed_reasons_do_not_hide_disjoint_clean_windows(self):
        raw = {"fallback_compute": [[0, 1]], "fallback_storage": [[2, 3]],
               "fallback_compute_tax": [[4, 5], [8, 9]], "fallback_storage_tax": [[6, 7]]}
        self.assertEqual(history_components(raw), (True, True))
        self.assertEqual(tax_history_components(raw), (True, True))
        for start, end in ((1, 2), (3, 4), (5, 6), (7, 8), (9, 10)):
            for storage, tax in ((False, False), (True, False), (False, True), (True, True)):
                self.assertFalse(history_in_window(raw, start, end, storage, include_tax=tax))
        self.assertFalse(history_in_window(raw, 6, 7, False, include_tax=True))
        self.assertTrue(history_in_window(raw, 6, 7, True, include_tax=True))

    def test_tax_only_compaction_never_sets_principal_flags(self):
        for reason in ("fallback_compute_tax", "fallback_storage_tax", "fallback_tax"):
            with self.subTest(reason=reason):
                raw = {reason: [[0, 4], [5, 6], [8, None]]}
                self.assertEqual(compact_unpriced_intervals(raw, 10, 5),
                                 ({reason: [[5., 6.], [8., None]]}, False, False))
                self.assertEqual(compact_unpriced_intervals({reason: [0, 5]}, 10, 5), ({}, False, False))
                self.assertNotEqual(tax_history_components(raw), (False, False))

    def test_mixed_compaction_flags_principal_before_pruning(self):
        raw = {"fallback_compute": [0, 2], "fallback_storage": [1, 5],
               "fallback_compute_tax": [2, 4], "fallback_storage_tax": [[4, 8], [9, None]]}
        self.assertEqual(compact_unpriced_intervals(raw, 10, 5),
                         ({"fallback_storage_tax": [[4., 8.], [9., None]]}, True, True))
        self.assertEqual(tax_history_components(raw), (True, True))

    def test_legacy_unknown_component_ranges_remain_conservative(self):
        for bounds in ([1, 3], [[1, 3], [5, None]]):
            with self.subTest(bounds=bounds):
                normalized = normalize_unpriced_intervals({"x": bounds})["x"]
                self.assertEqual(split_legacy_tax_intervals({"fallback_tax": bounds}),
                                 {"fallback_compute_tax": normalized, "fallback_storage_tax": normalized})

    def test_legacy_proven_empty_ranges_drop_only_that_component(self):
        raw = {"fallback_tax": [[0, 2], [4, None]]}
        self.assertEqual(split_legacy_tax_intervals(raw, [], []), {})
        self.assertEqual(split_legacy_tax_intervals(raw, [], None),
                         {"fallback_storage_tax": [[0., 2.], [4., None]]})
        self.assertEqual(split_legacy_tax_intervals(raw, None, []),
                         {"fallback_compute_tax": [[0., 2.], [4., None]]})

    def test_split_intersects_actual_disjoint_component_ranges(self):
        raw = {"fallback_tax": [[0, 5], [7, 12]]}
        self.assertEqual(split_legacy_tax_intervals(raw, [[1, 3], [4, 8], [10, 11]], [[3, 10]]),
                         {"fallback_compute_tax": [[1., 3.], [4., 5.], [7., 8.], [10., 11.]],
                          "fallback_storage_tax": [[3., 5.], [7., 10.]]})

    def test_split_accepts_legacy_pairs_as_actual_ranges(self):
        self.assertEqual(split_legacy_tax_intervals({"fallback_tax": [0, 10]}, [2, 4], [6, 8]),
                         {"fallback_compute_tax": [[2., 4.]], "fallback_storage_tax": [[6., 8.]]})

    def test_split_handles_open_legacy_and_component_ends(self):
        self.assertEqual(split_legacy_tax_intervals({"fallback_tax": [[2, None]]},
                                                    [[0, 3], [5, None]], [[9, 12]]),
                         {"fallback_compute_tax": [[2., 3.], [5., None]],
                          "fallback_storage_tax": [[9., 12.]]})
        self.assertEqual(split_legacy_tax_intervals({"fallback_tax": [2, 8]}, [5, None], []),
                         {"fallback_compute_tax": [[5., 8.]]})

    def test_touching_ranges_do_not_invent_zero_length_intersections(self):
        self.assertEqual(split_legacy_tax_intervals({"fallback_tax": [2, 4]}, [0, 2], [4, 8]), {})

    def test_explicit_zero_evidence_at_a_neighbor_endpoint_survives_clipping(self):
        for ranges in ([[0, 3], [3, 3]], [[3, 3], [0, 3]], [[0, 3], [3, 3], [6, 8]]):
            with self.subTest(ranges=ranges):
                self.assertEqual(split_legacy_tax_intervals({"fallback_tax": ranges}, [3, 5], []),
                                 {"fallback_compute_tax": [[3., 3.]]})

    def test_zero_length_legacy_evidence_is_retained_only_with_matching_charge_context(self):
        raw = {"fallback_tax": [3, 3]}
        self.assertEqual(split_legacy_tax_intervals(raw, None, []), {"fallback_compute_tax": [[3., 3.]]})
        self.assertEqual(split_legacy_tax_intervals(raw, [3, 5], [1, 3]),
                         {"fallback_compute_tax": [[3., 3.]]})
        self.assertEqual(split_legacy_tax_intervals(raw, [3, 3], []), {})
        self.assertFalse(history_in_window(split_legacy_tax_intervals(raw), 0, 9, include_tax=True))

    def test_existing_component_reasons_are_preserved_and_merged(self):
        raw = {"fallback_tax": [2, 4], "fallback_compute_tax": [[0, 2], [8, 9]],
               "fallback_storage_tax": [10, None], "fallback_storage": [7, 8], "custom": [0, 1]}
        self.assertEqual(split_legacy_tax_intervals(raw, None, []),
                         {"fallback_compute_tax": [[0., 4.], [8., 9.]],
                          "fallback_storage_tax": [[10., None]], "fallback_storage": [[7., 8.]],
                          "custom": [[0., 1.]]})

    def test_split_is_pure_idempotent_and_json_stable(self):
        raw = {"fallback_tax": [[0, 5], [8, None]], "prior_compute": [2, 3]}
        compute, storage = [[1, 4], [9, None]], [[3, 10]]
        before = copy.deepcopy((raw, compute, storage))
        result = split_legacy_tax_intervals(raw, compute, storage)
        self.assertEqual((raw, compute, storage), before)
        self.assertEqual(split_legacy_tax_intervals(result, [], []), result)
        self.assertEqual(json.loads(json.dumps(result)), result)
        result["prior_compute"][0][0] = 999
        self.assertEqual((raw, compute, storage), before)

    def test_invalid_intervals_are_ignored_but_valid_siblings_survive(self):
        invalid = [[False, 2], [1, True], [float("nan"), 3], [0, float("inf")], [4, 2], ["0", 1], [1]]
        raw = {"fallback_tax": invalid + [[-2, 0], [1, 2]]}
        self.assertEqual(split_legacy_tax_intervals(raw, invalid + [[-1, 2]], []),
                         {"fallback_compute_tax": [[-1., 0.], [1., 2.]]})
        self.assertEqual(history_components({"fallback_tax": invalid}), (False, False))
        self.assertEqual(tax_history_components({"fallback_tax": invalid}), (False, False))

    def test_no_legacy_history_only_normalizes_existing_reasons(self):
        raw = {"fallback_compute_tax": [[3, 4], [1, 3]], "fallback_storage": [0, 1]}
        self.assertEqual(split_legacy_tax_intervals(raw, [], []), normalize_unpriced_intervals(raw))
        for empty in (None, [], {}, "bad"):
            self.assertEqual(split_legacy_tax_intervals(empty), {})
            self.assertEqual(tax_history_components(empty), (False, False))

    def test_window_and_compaction_validation(self):
        for value in (True, float("nan"), float("inf"), "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    history_in_window({}, value, 5)
                with self.assertRaises(ValueError):
                    compact_unpriced_intervals({}, 5, value)
        for start, end in ((2, 2), (3, 2)):
            self.assertFalse(history_in_window({"fallback_compute_tax": [0, None]}, start, end,
                                               include_tax=True))


if __name__ == "__main__":
    unittest.main()
