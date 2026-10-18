"""Pure deferred-history boundaries, independent of store or provider pricing."""

import copy
import json
import unittest

from lib.dashboard_deferred_history import append_deferred_context, deferred_segments


class DeferredHistoryHelpersTests(unittest.TestCase):
    def context(self, tax=0, rate=1):
        return {"tax_rate_percent": tax, "rates": {"pd-ssd": rate}}

    @staticmethod
    def price(disk, context):
        return disk["gb"] * context["rates"][disk["type"]] * (1 + context["tax_rate_percent"] / 100)

    def test_seed_prefers_deferred_then_creation_then_observation_including_zero(self):
        for metadata, expected in (({"billing_deferred_since": 0, "created_epoch": -5}, 0),
                                   ({"created_epoch": 0}, 0), ({}, 10),
                                   ({"billing_deferred_since": None, "created_epoch": 2}, 2)):
            with self.subTest(metadata=metadata):
                disk = dict(metadata)
                self.assertTrue(append_deferred_context(disk, 10, self.context()))
                self.assertEqual(disk["billing_deferred_pricing"], [[expected, self.context()]])

    def test_empty_history_seeds_and_duplicate_snapshot_is_noop(self):
        disk = {"billing_deferred_since": 1, "billing_deferred_pricing": []}
        self.assertTrue(append_deferred_context(disk, 2, self.context()))
        before = copy.deepcopy(disk)
        self.assertFalse(append_deferred_context(disk, 3, self.context()))
        self.assertEqual(disk, before)

    def test_changed_tax_price_and_trust_contexts_append(self):
        disk = {"billing_deferred_since": 1}
        for at, context in ((1, self.context()), (4, self.context(20)), (5, self.context(20, 2)),
                            (6, dict(self.context(20, 2), tax_rate_overridden=True)),
                            (7, self.context(0, 2))):
            self.assertTrue(append_deferred_context(disk, at, context))
        self.assertEqual([at for at, _ in disk["billing_deferred_pricing"]], [1, 4, 5, 6, 7])
        self.assertEqual(disk["billing_deferred_pricing"][-1][1]["tax_rate_percent"], 0)

    def test_append_copies_nested_context_and_does_not_mutate_shared_timeline(self):
        old = [[1, self.context()]]
        disk = {"billing_deferred_pricing": old}
        context = self.context(20)
        self.assertTrue(append_deferred_context(disk, 4, context))
        context["rates"]["pd-ssd"] = 99
        disk["billing_deferred_pricing"][0][1]["rates"]["pd-ssd"] = 42
        self.assertEqual(old, [[1, self.context()]])
        self.assertEqual(disk["billing_deferred_pricing"][1], [4, self.context(20)])

    def test_union_of_size_tax_and_price_boundaries_with_creation_backfill(self):
        disk = {"type": "pd-ssd", "gb": 300, "created_epoch": 0,
                "recovered_sizes": [[1, 100], [6, 200]],
                "billing_deferred_pricing": [[1, self.context()], [4, self.context(20)],
                                             [7, self.context(20, 2)]]}
        before = copy.deepcopy(disk)
        actual = deferred_segments(disk, 0, 8, self.context(99, 99), self.price)
        self.assertEqual(actual, [(0, 1, 100), (1, 4, 100), (4, 6, 120),
                                  (6, 7, 240), (7, 8, 480)])
        self.assertEqual(disk, before)
        self.assertEqual(deferred_segments(json.loads(json.dumps(disk)), 0, 8,
                                           self.context(99, 99), self.price), actual)

    def test_pending_sizes_and_fallback_context_when_no_pricing_history(self):
        disk = {"type": "pd-ssd", "gb": 999, "billing_deferred_sizes": [[2, 10], [5, 20]]}
        self.assertEqual(deferred_segments(disk, 0, 8, self.context(20), self.price),
                         [(0, 2, 12), (2, 5, 12), (5, 8, 24)])

    def test_recovered_sizes_take_precedence_over_pending_sizes(self):
        disk = {"type": "pd-ssd", "gb": 999, "billing_deferred_sizes": [[0, 40]],
                "recovered_sizes": [[0, 10]]}
        self.assertEqual(deferred_segments(disk, 0, 8, self.context(), self.price), [(0, 8, 10)])

    def test_no_sizes_uses_current_size_and_no_context_uses_fallback(self):
        disk = {"type": "pd-ssd", "gb": 10}
        self.assertEqual(deferred_segments(disk, 0, 8, self.context(), self.price), [(0, 8, 10)])

    def test_clipped_window_uses_latest_snapshot_and_excludes_end_boundary(self):
        disk = {"type": "pd-ssd", "gb": 10,
                "billing_deferred_pricing": [[1, self.context()], [4, self.context(20)],
                                             [8, self.context(50)]]}
        self.assertEqual(deferred_segments(disk, 5, 8, self.context(), self.price), [(5, 8, 12)])

    def test_duplicate_timestamp_uses_last_context_with_no_zero_length_interval(self):
        disk = {"type": "pd-ssd", "gb": 10, "billing_deferred_since": 1}
        append_deferred_context(disk, 1, self.context())
        append_deferred_context(disk, 4, self.context(20))
        append_deferred_context(disk, 4, self.context(0))
        self.assertEqual(deferred_segments(disk, 0, 8, self.context(), self.price),
                         [(0, 1, 10), (1, 4, 10), (4, 8, 10)])

    def test_zero_tax_and_zero_rate_are_not_replaced_by_fallback(self):
        disk = {"type": "pd-ssd", "gb": 10,
                "billing_deferred_pricing": [[1, self.context(0, 0)], [4, self.context(0, 1)]]}
        self.assertEqual(deferred_segments(disk, 0, 8, self.context(20, 99), self.price),
                         [(0, 1, 0), (1, 4, 0), (4, 8, 10)])

    def test_callback_cannot_mutate_disk_context_or_next_interval(self):
        context = self.context()
        disk = {"type": "pd-ssd", "gb": 10, "nested": {"value": 1},
                "billing_deferred_sizes": [[1, 10], [4, 20]],
                "billing_deferred_pricing": [[1, context]]}
        before = copy.deepcopy(disk)

        def mutating_price(candidate, snapshot):
            result = self.price(candidate, snapshot)
            candidate["nested"]["value"] = 99
            candidate["billing_deferred_pricing"][0][1]["rates"]["pd-ssd"] = 88
            snapshot["rates"]["pd-ssd"] = 77
            return result

        self.assertEqual(deferred_segments(disk, 0, 8, context, mutating_price),
                         [(0, 1, 10), (1, 4, 10), (4, 8, 20)])
        self.assertEqual(disk, before)
        self.assertEqual(context, self.context())
        bare = {"type": "pd-ssd", "gb": 10}

        def mutate_fallback(candidate, snapshot):
            snapshot["rates"]["pd-ssd"] = 42
            return 0

        deferred_segments(bare, 0, 1, context, mutate_fallback)
        self.assertEqual(context, self.context())

    def test_empty_or_reversed_window_does_not_call_pricer(self):
        def unexpected(*_args):
            self.fail("Empty window called price_disk")
        for start, end in ((1, 1), (2, 1)):
            self.assertEqual(deferred_segments({}, start, end, {}, unexpected), [])


if __name__ == "__main__":
    unittest.main()
