"""Server-arrival completion timestamps cannot round into the future."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import DashboardStore


class BuildFinishPrecisionTests(unittest.TestCase):
    base = 1800000000.0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DashboardStore(state_path=Path(self.temp.name) / "state.json")
        self.store.start()
        self.addCleanup(self.store.close)
        self.store.state["rust_builds"] = []

    def stamp(self, epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")

    def test_status_only_terminal_finish_does_not_cross_fractional_second_boundary(self):
        requested = self.stamp(self.base - 7200)
        for fraction in (0.994, 0.996, 0.999):
            for status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                with self.subTest(fraction=fraction, status=status):
                    self.store.state["rust_builds"] = []
                    now = self.base + fraction
                    with patch("lib.dashboard_server.time.time", return_value=now):
                        self.store.record_rust_build({"build_id": "precision", "status": "RUNNING",
                                                      "requested_at": requested})
                        result = self.store.record_rust_build({"build_id": "precision", "status": status})["build"]
                        finished = datetime.fromisoformat(result["finished_at"].replace("Z", "+00:00")).timestamp()
                        self.assertLessEqual(finished, now)
                        self.assertEqual(self.store.state["scaling_telemetry"]["builds_ran_past_hour"], 1)
                        self.store._sync_rust_build_telemetry(now)
                        self.assertEqual(self.store.state["scaling_telemetry"]["builds_ran_past_hour"], 1)
                        self.assertIsNone(result["delivered_at"])

    def test_explicit_reported_finish_and_duration_remain_authoritative(self):
        now = self.base + 0.996
        requested = self.stamp(self.base - 7200)
        finished = self.stamp(self.base + 30.25)
        with patch("lib.dashboard_server.time.time", return_value=now):
            result = self.store.record_rust_build({"build_id": "reported", "status": "FAILED",
                "requested_at": requested, "finished_at": finished, "total_duration_seconds": 7230.25,
                "compile_duration_seconds": 7230.25, "delivery_duration_seconds": 0})["build"]
        self.assertEqual(result["finished_at"], finished)
        self.assertEqual(result["total_duration_seconds"], 7230.25)
        self.assertEqual(result["compile_duration_seconds"], 7230.25)

    def test_explicit_duration_keeps_existing_timestamp_derivation(self):
        now = self.base + 0.996
        with patch("lib.dashboard_server.time.time", return_value=now):
            result = self.store.record_rust_build({"build_id": "duration", "status": "FAILED",
                "requested_at": self.stamp(self.base - 7200), "total_duration_seconds": 7201})["build"]
        finished = datetime.fromisoformat(result["finished_at"].replace("Z", "+00:00")).timestamp()
        self.assertEqual(finished, self.base + 1)
        self.assertEqual(result["total_duration_seconds"], 7201)
