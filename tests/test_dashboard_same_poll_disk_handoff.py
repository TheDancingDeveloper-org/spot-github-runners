"""A newly missing VM must release transferred disks during the same denied-listing poll."""

import copy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from lib.dashboard_server import instance_usage, reconcile_instance_records
from lib.fleet_inventory import observe_fleet_items


class SamePollDiskHandoffTests(unittest.TestCase):
    now = 1800000000.0
    zone = "us-south1-b"
    prefix = "projects/p/zones/us-south1-b/disks/"

    def setUp(self):
        pricing = patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={})
        pricing.start()
        self.addCleanup(pricing.stop)

    def snapshot(self, *, private, successor_only=False, listed=True):
        created = datetime.fromtimestamp(self.now - 7200, timezone.utc).isoformat()
        shared = {"source": self.prefix + "builder-shared", "diskSizeGb": "100", "type": "PERSISTENT",
                  "boot": False, "autoDelete": False}
        retained = dict(shared, source=self.prefix + "builder-private", diskSizeGb="40")
        items = []
        for identity in (("2",) if successor_only else ("1", "2")):
            disks = [dict(shared)]
            if identity == "1" and private:
                disks.append(dict(retained))
            items.append({"id": identity, "name": "builder-owner-" + identity, "zone": "zones/" + self.zone,
                          "status": "RUNNING", "machineType": "machineTypes/n4-highcpu-16",
                          "creationTimestamp": created, "lastStartTimestamp": created,
                          "scheduling": {"provisioningModel": "SPOT"}, "disks": disks})
        catalog = {shared["source"]: {"id": "physical-shared", "name": "builder-shared", "type": "pd-ssd",
                                      "sizeGb": "100", "creationTimestamp": created}}
        if private:
            catalog[retained["source"]] = {"id": "physical-private", "name": "builder-private",
                                            "type": "pd-balanced", "sizeGb": "40", "creationTimestamp": created}
        return observe_fleet_items(items, disk_types=catalog if listed else {}, disk_types_known=listed,
                                   shapes={(self.zone, "n4-highcpu-16"): {"vcpu": 16, "ram_gib": 32.0}})

    def poll(self, rows, observations, hours, *, listed):
        return reconcile_instance_records(json.loads(json.dumps(rows)), copy.deepcopy(observations), project="p",
                                           now=self.now + hours * 3600, disks_listed=listed,
                                           tax_overrides={"rust_builder": 0})[0]

    def initial(self, *, private):
        rows = self.poll([], self.snapshot(private=private), 0, listed=True)
        old = next(row for row in rows if row["instance_id"] == "1")
        new = next(row for row in rows if row["instance_id"] == "2")
        self.assertTrue(old["disks_detail"][0]["billing_owner"])
        self.assertFalse(new["disks_detail"][0]["billing_owner"])
        self.assertEqual(new["pricing"]["disk_hourly_usd"], 0)
        self.assertGreater(old["pricing"]["disk_hourly_usd"], 0)
        return rows, old["pricing"]["disk_hourly_usd"]

    @staticmethod
    def storage_cost(rows, now):
        return sum(usage["cost_all_in"] - usage["cost_compute"]
                   for usage in (instance_usage(row, now) for row in rows))

    def test_all_retained_disks_transferred_end_liability_in_first_missing_poll(self):
        initial, rate = self.initial(private=False)
        observed = self.snapshot(private=False, successor_only=True, listed=False)
        for existing in (initial, list(reversed(initial))):
            with self.subTest(first_existing=existing[0]["instance_id"]):
                rows = self.poll(existing, observed, 1, listed=False)
                old = next(row for row in rows if row["instance_id"] == "1")
                new = next(row for row in rows if row["instance_id"] == "2")
                self.assertEqual(old["status"], "DELETED")
                self.assertEqual(new["status"], "RUNNING")
                self.assertFalse(old["disk_liability_pending"])
                self.assertEqual(old["disk_end_epoch"], self.now + 3600)
                self.assertAlmostEqual(self.storage_cost(rows, self.now + 7200), 4 * rate, places=5)

    def test_private_retained_disk_stays_pending_but_shared_disk_bills_only_new_owner(self):
        initial, rate = self.initial(private=True)
        observed = self.snapshot(private=True, successor_only=True, listed=False)
        for existing in (initial, list(reversed(initial))):
            with self.subTest(first_existing=existing[0]["instance_id"]):
                rows = self.poll(existing, observed, 1, listed=False)
                old = next(row for row in rows if row["instance_id"] == "1")
                new = next(row for row in rows if row["instance_id"] == "2")
                self.assertEqual(old["status"], "DELETED")
                self.assertTrue(old["disk_liability_pending"])
                self.assertGreater(old["pricing"]["disk_hourly_usd"], 0)
                self.assertGreater(new["pricing"]["disk_hourly_usd"], 0)
                self.assertEqual(new["disks_detail"][0]["id"], "physical-shared")
                self.assertTrue(new["disks_detail"][0]["billing_owner"])
                self.assertAlmostEqual(old["pricing"]["disk_hourly_usd"] + new["pricing"]["disk_hourly_usd"],
                                       rate, places=6)
                self.assertEqual({disk["id"] for disk in old["disks_detail"]
                                  if disk.get("billing_owner") is not False}, {"physical-private"})
                self.assertAlmostEqual(self.storage_cost(rows, self.now + 7200), 4 * rate, places=5)

    def test_next_denied_poll_does_not_lock_in_one_poll_of_duplicate_history(self):
        initial, rate = self.initial(private=True)
        observed = self.snapshot(private=True, successor_only=True, listed=False)
        first_missing = self.poll(initial, observed, 1, listed=False)
        second_missing = self.poll(first_missing, observed, 2, listed=False)
        self.assertAlmostEqual(self.storage_cost(second_missing, self.now + 7200), 4 * rate, places=5)
        repeated = self.poll(second_missing, observed, 2, listed=False)
        self.assertAlmostEqual(self.storage_cost(repeated, self.now + 7200), 4 * rate, places=5)


if __name__ == "__main__":
    unittest.main()
