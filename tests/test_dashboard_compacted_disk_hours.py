"""Folding and retention preserve disk duration independently of rate or cost."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import dashboard_server as server
import test_dashboard_deferred_tax_boundaries as deferred_fixture


class CompactedDiskHoursTests(unittest.TestCase):
    epoch = 1800057600.0  # UTC day boundary: exact hourly and daily window oracles.

    def setUp(self):
        self.assertEqual(self.epoch % 86400, 0)
        catalog = {"disk_monthly_gib": {"pd-ssd": 7.30}}
        for name in ("lib.dashboard_models.fetch_live_spot_pricing",
                     "lib.dashboard_server.fetch_live_spot_pricing"):
            stub = patch(name, return_value=catalog)
            stub.start()
            self.addCleanup(stub.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "record.json"

    def at(self, hours):
        return self.epoch + hours * 3600

    def reload(self, record):
        self.path.write_text(json.dumps(record, allow_nan=False))
        return json.loads(self.path.read_text())

    def observation(self):
        disk = {"source": "z/builder-cache", "id": "disk-1", "type": "pd-ssd", "gb": 100,
                "created_epoch": self.epoch, "auto_delete": False, "boot": False, "billing_owner": True}
        return {"instance_id": "disk-1", "name": "builder-cache", "role": "rust_builder", "kind": "disk",
                "status": "DETACHED", "region": "us-south1", "zone": "us-south1-b", "spot": True,
                "machine_type": "", "vcpu": 0, "ram_gib": 0, "created_epoch": self.epoch,
                "boot_disk_type": "pd-ssd", "boot_disk_gb": 0, "data_disk_type": "pd-ssd",
                "data_disk_gb": 100, "data_disks_attached_gb": 100, "data_disks": {"pd-ssd": 100},
                "disk_types_known": True, "disks_detail": [disk]}

    def disk_record(self, *, now=None):
        return server._live_instance_record(self.observation(), project="test-project",
                                            now=self.at(8) if now is None else now,
                                            tax_overrides={"rust_builder": 0})

    def reconcile(self, record, now):
        return server._live_instance_record(self.observation(), project="test-project", now=now,
                                            previous=self.reload(record), tax_overrides={"rust_builder": 0})

    def assert_usage_equal(self, actual, expected):
        for key in ("compute_hours", "compute_hours_in_window", "disk_hours", "disk_hours_in_window",
                    "cost_all_in", "cost_compute", "window_all_in", "window_compute"):
            with self.subTest(metric=key):
                self.assertAlmostEqual(actual[key], expected[key], places=7)

    def disjoint_record(self):
        record = self.disk_record()
        record.update(disk_segments=[[self.at(0), self.at(2), 1], [self.at(3), self.at(4), 0],
                                     [self.at(5), self.at(6), 2]], disk_segment_start=self.at(6))
        return record

    def test_partial_and_complete_fold_preserve_lifetime_and_window_after_reload(self):
        original = self.disjoint_record()
        before = copy.deepcopy(original)
        for keep in (0, 1):
            with self.subTest(keep=keep):
                folded = self.reload(server._fold_record_detail(original, keep))
                self.assertTrue(folded["folded_cost_buckets"])
                self.assertEqual(original, before)
                for window_hour, expected_hours, expected_cost in ((0, 6, 6), (3, 4, 4), (5, 3, 4), (7, 1, 1)):
                    with self.subTest(window_hour=window_hour):
                        expected = server.instance_usage(original, self.at(8), self.at(window_hour))
                        self.assertEqual(expected["disk_hours"], 6)
                        self.assertEqual(expected["disk_hours_in_window"], expected_hours)
                        self.assertEqual(expected["window_all_in"], expected_cost)
                        self.assert_usage_equal(server.instance_usage(folded, self.at(8), self.at(window_hour)), expected)
                twice = self.reload(server._fold_record_detail(folded, keep))
                self.assert_usage_equal(server.instance_usage(twice, self.at(8)),
                                        server.instance_usage(original, self.at(8)))

    def test_zero_cost_disk_segment_keeps_duration_when_folded(self):
        record = self.disk_record()
        record.update(disk_segments=[[self.at(0), self.at(4), 0]], disk_segment_start=self.at(8))
        expected = server.instance_usage(record, self.at(8), self.at(1))
        self.assertEqual((expected["disk_hours"], expected["disk_hours_in_window"], expected["cost_all_in"]),
                         (4, 3, 0))
        self.assert_usage_equal(server.instance_usage(self.reload(server._fold_record_detail(record, 0)),
                                                      self.at(8), self.at(1)), expected)

    def test_automatic_segment_limit_fold_preserves_exact_usage(self):
        record = self.disjoint_record()
        expected = server.instance_usage(record, self.at(8), self.at(3))
        with patch.object(server, "MAX_DETAILED_SEGMENTS", 1):
            compacted = self.reload(self.reconcile(record, self.at(8)))
        self.assertLessEqual(len(compacted["disk_segments"]), 1)
        self.assertTrue(compacted["folded_cost_buckets"])
        self.assert_usage_equal(server.instance_usage(compacted, self.at(8), self.at(3)), expected)

    def test_daily_coarsening_preserves_hours_and_uniform_partial_window(self):
        record = self.disk_record(now=self.at(48))
        record.update(disk_segments=[[self.at(h), self.at(h + 1), 1] for h in range(48)],
                      disk_segment_start=self.at(48))
        coarse = self.reload(server._coarsen_record_buckets(server._fold_record_detail(record, 0)))
        self.assertEqual(coarse["folded_bucket_seconds"], 86400)
        self.assertEqual(len(coarse["folded_cost_buckets"]), 2)
        for window_hour in (0, 24, 25.5):
            expected = server.instance_usage(record, self.at(48), self.at(window_hour))
            self.assertEqual(expected["disk_hours"], 48)
            self.assertEqual(expected["disk_hours_in_window"], 48 - window_hour)
            self.assert_usage_equal(server.instance_usage(coarse, self.at(48), self.at(window_hour)), expected)

    def retention_record(self):
        now = self.at(8) + server.DELETED_INSTANCE_RETENTION_SECONDS
        record = self.disk_record()
        record.update(disk_segments=[[self.at(0), self.at(2), 1], [self.at(3), self.at(4), 0]],
                      disk_segment_start=now - 3600)
        return record, now

    def test_expired_detailed_segments_preserve_lifetime_not_recent_window(self):
        record, now = self.retention_record()
        expected = server.instance_usage(record, now, now - 7200)
        self.assertEqual((expected["disk_hours"], expected["disk_hours_in_window"]), (4, 1))
        pruned = self.reload(self.reconcile(record, now))
        self.assertEqual(pruned["disk_segments"], [])
        self.assert_usage_equal(server.instance_usage(pruned, now, now - 7200), expected)
        self.assertAlmostEqual(pruned.get("pruned_disk_hours", 0), 3)
        repeated = self.reload(self.reconcile(pruned, now))
        self.assert_usage_equal(server.instance_usage(repeated, now, now - 7200), expected)

    def test_expired_folded_buckets_transfer_disk_hours_to_lifetime_once(self):
        record, now = self.retention_record()
        expected = server.instance_usage(record, now, now - 7200)
        folded = self.reload(server._fold_record_detail(record, 0))
        self.assertTrue(folded["folded_cost_buckets"])
        pruned = self.reload(self.reconcile(folded, now))
        self.assertEqual(pruned["folded_cost_buckets"], {})
        self.assert_usage_equal(server.instance_usage(pruned, now, now - 7200), expected)
        self.assertAlmostEqual(pruned.get("pruned_disk_hours", 0), 3)
        self.assert_usage_equal(server.instance_usage(self.reload(self.reconcile(pruned, now)), now, now - 7200),
                                expected)

    def test_legacy_three_value_bucket_keeps_costs_and_only_new_known_disk_duration(self):
        record = self.disk_record()
        record.update(disk_segments=[[self.at(0), self.at(1), 0]], disk_segment_start=self.at(8),
                      folded_cost_buckets={str(int(self.epoch)): [1., 3., 2.]})
        expected = server.instance_usage(record, self.at(8), self.epoch)
        self.assertEqual((expected["compute_hours"], expected["disk_hours"], expected["cost_all_in"]), (1, 1, 3))
        folded = self.reload(server._fold_record_detail(record, 0))
        self.assert_usage_equal(server.instance_usage(folded, self.at(8), self.epoch), expected)
        # Preserve the old cost reader's public tuple contract for existing callers.
        self.assertEqual(server._bucket_window_costs(folded["folded_cost_buckets"], self.epoch, self.at(8)),
                         (1., 3., 2.))

    def test_deleted_record_compaction_carries_folded_disk_duration_to_role_buckets(self):
        record = self.disjoint_record()
        record.update(status="DELETED", deleted_epoch=self.at(8), disk_end_epoch=self.at(8))
        expected = server.instance_usage(record, self.at(8))
        buckets = {}
        server._compact_deleted_record(self.reload(server._fold_record_detail(record, 0)), buckets, self.at(8))
        buckets = self.reload(buckets)
        prefix = "rust_builder|"
        self.assertTrue(buckets)
        self.assertTrue(all(k.startswith(prefix) for k in buckets))
        self.assertEqual(server._bucket_window_costs(buckets, self.epoch, self.at(8), prefix)[1],
                         expected["cost_all_in"])
        self.assertTrue(all(len(value) == 4 for value in buckets.values()), "Duration missing from bucket schema")
        self.assertAlmostEqual(sum(value[3] for value in buckets.values()), expected["disk_hours"])

    def deferred_recovery(self, *, automatic=False, prune=False):
        fixture = deferred_fixture.DeferredTaxBoundaryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def poll(hour, observation):
            fixture.at(hour)
            fixture.store.fleet_source = lambda: [copy.deepcopy(observation)]
            fixture.store.refresh_fleet_inventory()
            self.assertEqual(fixture.store.fleet_inventory_status()["last_error"], "")
            fixture.reload()

        boot_only = fixture.observation(known=True, mixed_boot=True)
        boot_only.update(disks_detail=boot_only["disks_detail"][:1], data_disk_gb=0,
                         data_disks_attached_gb=0, data_disks={})
        poll(1, boot_only)
        poll(2, fixture.observation(known=False, mixed_boot=True))
        fixture.tax(4, 20)
        if automatic:
            fixture.tax(6, 0)  # Two closed intervals exceed the patched limit of one.
            with patch.object(server, "MAX_DETAILED_SEGMENTS", 1):
                poll(6, fixture.observation(known=False, mixed_boot=True))
        elif prune:
            # Simulate retention pressure before authoritative metadata returns.
            with patch.object(server, "DELETED_INSTANCE_RETENTION_SECONDS", 3600):
                poll(6, fixture.observation(known=False, mixed_boot=True))
        else:
            fixture.store.state["instances"] = [server._fold_record_detail(row, 0)
                                                for row in fixture.store.state["instances"]]
            fixture.store._persist()
            fixture.reload()
        recovered = fixture.observation(known=True, mixed_boot=True)
        recovered["disks_detail"][-1]["created_epoch"] = fixture.epoch + 2 * 3600
        # Repeat recovery after hydration: neither cost nor duration can be replayed.
        for _ in range(2):
            poll(8, recovered)
            row = fixture.store.state["instances"][0]
            usage = server.instance_usage(row, fixture.now, fixture.epoch + 2 * 3600)
            # Boot: .20/hour throughout, data: 1/hour from hour 2. Tax is 20%
            # from hour 4, ending at hour 6 only in the automatic-fold case.
            self.assertAlmostEqual(usage["cost_all_in"], 8.08 if automatic else 8.56)
            self.assertAlmostEqual(usage["window_all_in"], 7.68 if automatic else 8.16)
            self.assertEqual(usage["disk_hours"], 8)
            self.assertEqual(usage["disk_hours_in_window"], 6)
        # Once resolved, compaction is safe and should actually remove disk detail.
        folded = server._fold_record_detail(row, 0)
        self.assertEqual(folded["disk_segments"], [])
        self.assert_usage_equal(server.instance_usage(self.reload(folded), fixture.now),
                                server.instance_usage(row, fixture.now))

    def test_deferred_recovery_after_size_budget_fold_does_not_duplicate_duration(self):
        self.deferred_recovery(automatic=False)

    def test_deferred_recovery_after_segment_limit_does_not_duplicate_duration(self):
        self.deferred_recovery(automatic=True)

    def test_deferred_recovery_after_retention_does_not_duplicate_duration(self):
        self.deferred_recovery(prune=True)

    def test_unpaid_history_survives_pruning_and_direct_segment_limit(self):
        record, now = self.retention_record()
        record["unpaid_disk_details"] = [{"source": "z/cache", "billing_deferred_since": self.epoch}]
        expected = server.instance_usage(record, now)
        with patch.object(server, "MAX_DETAILED_SEGMENTS", 1):
            retained = self.reload(self.reconcile(record, now))
        self.assertEqual(retained["disk_segments"], record["disk_segments"])
        self.assertEqual(retained.get("pruned_disk_hours", 0), 0)
        self.assert_usage_equal(server.instance_usage(retained, now), expected)

    def test_pending_recovery_retains_disk_detail_but_can_fold_compute(self):
        markers = ({"disk_history_deferred_at": 0}, {"deferred_disk_sources": ["z/cache"]},
                   {"unpaid_disk_details": [{"source": "z/cache"}]},
                   {"disks_detail": [{"billing_deferred_since": 0}]})
        for marker in markers:
            with self.subTest(marker=marker):
                record = self.disjoint_record()
                record.update(marker)
                record["prior_runs"] = [[self.at(0), self.at(1), 2, 1]]
                original = copy.deepcopy(record)
                folded = self.reload(server._fold_record_detail(record, 0))
                self.assertEqual(record, original)
                self.assertEqual(folded["disk_segments"], record["disk_segments"])
                self.assertEqual(folded["prior_runs"], [])
                self.assert_usage_equal(server.instance_usage(folded, self.at(8)),
                                        server.instance_usage(record, self.at(8)))

    def test_unpaid_detail_rejects_size_budget_instead_of_losing_coverage(self):
        record = self.disk_record(now=self.at(48))
        record.update(disk_segments=[[self.at(h), self.at(h + 1), 1] for h in range(48)],
                      disk_segment_start=self.at(48), unpaid_disk_details=[{"source": "z/cache"}])
        original = copy.deepcopy(record)
        # Enough room for fully folded/coarsened history, too little for exact detail.
        with patch.object(server, "MAX_INSTANCE_STATE_BYTES", server._serialized_size([[record], [], {}]) - 100):
            with self.assertRaises(server.InventoryTooLargeError):
                server._fit_size_budget([record], [], {}, self.at(48))
        self.assertEqual(record, original)


if __name__ == "__main__":
    unittest.main()
