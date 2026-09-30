import json
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from lib.runner_manager import cleanup_orphan_runner_disks


class RunnerDiskLifecycleTests(unittest.TestCase):
    @patch("lib.runner_manager.datetime")
    @patch("lib.runner_manager.gcloud")
    def test_orphan_cleanup_deletes_only_unattached_owned_disks(self, gcloud, clock):
        clock.now.return_value = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)
        clock.fromisoformat.side_effect = datetime.fromisoformat
        gcloud.return_value = json.dumps([
            {"name": "runner-data-old", "status": "READY", "users": [],
             "labels": {"managed-by": "spot-runner-manager"},
             "creationTimestamp": "2026-09-28T00:00:00Z"},
            {"name": "runner-data-in-use", "status": "READY", "users": ["vm-url"],
             "labels": {"managed-by": "spot-runner-manager"},
             "creationTimestamp": "2026-09-28T00:00:00Z"},
            {"name": "runner-data-new", "status": "READY", "users": [],
             "labels": {"managed-by": "spot-runner-manager"},
             "creationTimestamp": "2026-09-30T05:00:00Z"},
            {"name": "unrelated", "status": "READY", "users": [], "labels": {},
             "creationTimestamp": "2026-09-28T00:00:00Z"},
            {"name": "manager-stopping", "status": "CREATING", "users": [],
             "labels": {"managed-by": "spot-runner-manager"},
             "creationTimestamp": "2026-09-28T00:00:00Z"},
        ])

        removed = cleanup_orphan_runner_disks("us-south1-b")

        self.assertEqual(removed, ["runner-data-old"])
        gcloud.assert_any_call([
            "compute", "disks", "delete", "runner-data-old", "--zone=us-south1-b", "--quiet"
        ])
        self.assertEqual(gcloud.call_count, 2)


if __name__ == "__main__":
    unittest.main()
