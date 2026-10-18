"""A fleet source keeps VM snapshots current through disk permission loss and recovery."""

import unittest
from unittest.mock import AsyncMock, patch

from lib import compute_api
from lib.fleet_inventory import FleetListing, gce_fleet_source


class DiskPermissionTransitionTests(unittest.TestCase):
    def setUp(self):
        self.items = [self.vm()]
        self.catalog = self.disk_catalog("old-cache", "disk-old", 100)

        async def instances(*args, **kwargs):
            return self.items, []

        async def capacity(api, project, zone, machine_type):
            return {"n4-highcpu-16": {"vcpu": 16, "ram_gib": 32.0},
                    "n4-standard-8": {"vcpu": 8, "ram_gib": 32.0},
                    "n4-standard-32": {"vcpu": 32, "ram_gib": 128.0}}[machine_type]

        self.disks = AsyncMock(return_value=self.catalog)
        for target, replacement in (("get_auth", lambda project: compute_api.Auth("t", "p")),
                                    ("list_instances", instances), ("list_disk_types", self.disks),
                                    ("machine_type_capacity", capacity)):
            mocked = patch.object(compute_api, target, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        # Exactly one factory instance per test: state survives every call in the transition.
        self.source = gce_fleet_source("p")

    @staticmethod
    def disk_source(name):
        return "projects/p/zones/us-south1-a/disks/" + name

    def vm(self, identity="1", *, status="RUNNING", machine="n4-highcpu-16", disk="old-cache", size=100):
        return {"id": identity, "name": "oxidex-runners-" + identity, "zone": "zones/us-south1-a",
                "status": status, "machineType": "machineTypes/" + machine,
                "creationTimestamp": "2026-10-18T00:00:00Z", "lastStartTimestamp": "2026-10-18T01:00:00Z",
                "disks": [{"source": self.disk_source(disk), "diskSizeGb": str(size),
                           "type": "PERSISTENT", "boot": False, "autoDelete": False}]}

    def disk_catalog(self, name, identity, size):
        return {self.disk_source(name): {"name": name, "id": identity, "type": "pd-ssd", "sizeGb": str(size),
                                       "zone": "zones/us-south1-a"}}

    def assert_degraded_snapshot(self, listing, expected_ids):
        self.assertIsInstance(listing, FleetListing)
        self.assertIs(listing.disks_listed, False)
        self.assertIsNone(listing.disk_inventory)
        self.assertEqual([row["instance_id"] for row in listing], expected_ids)
        for row in listing:
            self.assertTrue(row["specs_known"])
            self.assertIs(row["disk_types_known"], False)
            self.assertTrue(row["disks_detail"])
            self.assertTrue(all(d["disk_types_known"] is False for d in row["disks_detail"]))

    def test_success_then_permission_loss_repeated_denial_and_recovery_keep_current_vms(self):
        recovered_catalog = self.disk_catalog("new-cache", "disk-new", 250)
        recovered_catalog.update(self.disk_catalog("runner-data-second", "disk-second", 75))
        self.disks.side_effect = [self.catalog, RuntimeError("HTTP 403 compute.disks.list"),
                                 RuntimeError("Google API HTTP 403: permission denied"), recovered_catalog]
        first = self.source()
        self.assertIs(first.disks_listed, True)
        self.assertEqual(first.disk_inventory, self.catalog)
        self.assertEqual(first[0]["data_disks"], {"pd-ssd": 100})
        self.assertEqual((first[0]["vcpu"], first[0]["ram_gib"]), (16, 32.0))

        self.items = [self.vm(status="TERMINATED", machine="n4-standard-8", disk="new-cache", size=250),
                      self.vm("2", disk="runner-data-second", size=75)]
        denied = self.source()
        self.assert_degraded_snapshot(denied, ["1", "2"])
        self.assertEqual((denied[0]["status"], denied[0]["vcpu"], denied[0]["ram_gib"]),
                         ("TERMINATED", 8, 32.0))
        self.assertEqual(denied[0]["data_disk_gb"], 250)
        self.assertEqual(denied[0]["disks_detail"][0]["source"], self.disk_source("new-cache"))
        self.assertEqual(denied[0]["disks_detail"][0]["id"], "")
        self.assertEqual(denied[1]["data_disk_gb"], 75)

        self.items = [self.vm(status="RUNNING", machine="n4-standard-32", disk="new-cache", size=250)]
        repeated = self.source()
        self.assert_degraded_snapshot(repeated, ["1"])
        self.assertEqual((repeated[0]["status"], repeated[0]["vcpu"], repeated[0]["ram_gib"]),
                         ("RUNNING", 32, 128.0))

        self.items.append(self.vm("2", disk="runner-data-second", size=75))
        recovered = self.source()
        self.assertIs(recovered.disks_listed, True)
        self.assertEqual(recovered.disk_inventory, recovered_catalog)
        self.assertEqual([row["instance_id"] for row in recovered], ["1", "2"])
        self.assertTrue(all(row["disk_types_known"] for row in recovered))
        self.assertTrue(all(d["disk_types_known"] for row in recovered for d in row["disks_detail"]))
        self.assertEqual(recovered[0]["disks_detail"][0]["id"], "disk-new")
        self.assertEqual(recovered[0]["data_disks"], {"pd-ssd": 250})
        self.assertEqual(recovered[1]["data_disks"], {"pd-ssd": 75})

    def test_nonpermission_failure_after_success_rejects_whole_refresh(self):
        self.disks.side_effect = [self.catalog, RuntimeError("HTTP 503 backend unavailable")]
        self.assertIs(self.source().disks_listed, True)
        self.items = [self.vm(status="TERMINATED", disk="new-cache", size=250)]
        with self.assertRaisesRegex(RuntimeError, "disk listing failed:.*HTTP 503"):
            self.source()

    def test_initial_nonpermission_failure_rejects_whole_refresh(self):
        self.disks.side_effect = RuntimeError("connection reset")
        with self.assertRaisesRegex(RuntimeError, "disk listing failed:.*connection reset"):
            self.source()

    def test_initial_permission_denial_returns_degraded_snapshot(self):
        self.disks.side_effect = RuntimeError("HTTP 403 compute.disks.list")
        self.assert_degraded_snapshot(self.source(), ["1"])


if __name__ == "__main__":
    unittest.main()
