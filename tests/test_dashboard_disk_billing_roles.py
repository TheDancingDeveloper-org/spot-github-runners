"""Shared disk role attribution must not depend on cross-role instance ordering."""

import itertools
import unittest
from unittest.mock import AsyncMock, patch

from lib import compute_api
from lib.fleet_inventory import gce_fleet_source, observe_detached_disks, observe_fleet_items


class SharedDiskBillingRoleTests(unittest.TestCase):
    def _items(self, disk_name, *, ids=("1", "2"), names=("builder-a", "oxidex-runners-a"), boot=False):
        source = "projects/p/zones/us-south1-a/disks/" + disk_name
        return [{"id": identity, "name": name, "zone": "zones/us-south1-a", "status": "RUNNING",
                 "machineType": "machineTypes/n4-highcpu-16",
                 "disks": [{"source": source, "type": "PERSISTENT", "diskSizeGb": "100",
                            "boot": boot, "autoDelete": False, "deviceName": "runner-data"}]}
                for identity, name in zip(ids, names)]

    def _catalog(self, items, *, named=True):
        source = items[0]["disks"][0]["source"]
        detail = {"id": "500", "type": "pd-ssd", "sizeGb": "100",
                  "creationTimestamp": "2026-10-18T00:00:00Z"}
        if named:
            detail["name"] = source.rsplit("/", 1)[-1]
        return {source: detail}

    def _assert_single_charge(self, observations, *, boot=False):
        self.assertEqual(sum(d["billing_owner"] for o in observations for d in o["disks_detail"]), 1)
        self.assertEqual(sum(o["boot_disk_billed_gb"] + sum(o["data_disks"].values())
                             for o in observations), 100)
        for obs in observations:
            self.assertEqual(obs["boot_disk_gb"] if boot else obs["data_disk_gb"], 100)
            self.assertEqual(obs["shared_attachments"], obs["disks_detail"])
            self.assertEqual(obs["disks_detail"][0]["gb"], 100)
            self.assertFalse(obs["disks_detail"][0]["auto_delete"])

    def test_explicit_role_survives_swapped_ids_and_listing_order(self):
        cases = [("runner-data", "cicd_runner"), ("runner-data-cache", "cicd_runner"),
                 ("oxidex-runners-cache", "cicd_runner"), ("newznab-spot-runners-cache", "cicd_runner"),
                 ("spot-runners-cache", "cicd_runner"), ("oxidex-spot-runners-cache", "cicd_runner"),
                 ("builder-cache", "rust_builder")]
        for (name, role), ids, named, boot in itertools.product(
                cases, [("1", "2"), ("2", "1")], [True, False], [True, False]):
            items = self._items(name, ids=ids, boot=boot)
            for ordered in (items, list(reversed(items))):
                with self.subTest(name=name, ids=ids, named=named, boot=boot, first=ordered[0]["id"]):
                    obs = observe_fleet_items(ordered, disk_types=self._catalog(items, named=named))
                    self._assert_single_charge(obs, boot=boot)
                    owner = next(o for o in obs if o["disks_detail"][0]["billing_owner"])
                    self.assertEqual(owner["role"], role)
                    self.assertTrue(all(o.get("storage_billing_ambiguous") is False for o in obs))

    def test_custom_cross_role_disk_flags_every_participant(self):
        # deviceName is an attachment alias, not canonical disk-role evidence.
        for disk_name in ("custom-cache", "runner-database"):
            items = self._items(disk_name, ids=("3", "1", "2"),
                                names=("builder-a", "oxidex-runners-a", "builder-b"))
            for ordered in itertools.permutations(items):
                obs = observe_fleet_items(list(ordered), disk_types=self._catalog(items))
                self._assert_single_charge(obs)
                self.assertTrue(all(o.get("storage_billing_ambiguous") is True for o in obs))
                self.assertTrue(all(o.get("storage_billing_ambiguous_roles") == ["cicd_runner", "rust_builder"]
                                    for o in obs))

    def test_explicit_role_selects_stable_owner_within_that_role(self):
        items = self._items("runner-data", ids=("1", "3", "2"),
                            names=("builder-a", "oxidex-runners-b", "oxidex-runners-a"))
        for ordered in itertools.permutations(items):
            obs = observe_fleet_items(list(ordered), disk_types=self._catalog(items))
            self._assert_single_charge(obs)
            owner = next(o for o in obs if o["disks_detail"][0]["billing_owner"])
            self.assertEqual(owner["instance_id"], "2")

    def test_same_role_keeps_existing_id_order_ownership(self):
        for names in (("builder-a", "builder-b"), ("oxidex-runners-a", "oxidex-runners-b")):
            items = self._items("custom-cache", ids=("2", "1"), names=names)
            for ordered in (items, list(reversed(items))):
                obs = observe_fleet_items(ordered, disk_types=self._catalog(items))
                self._assert_single_charge(obs)
                self.assertEqual(obs[0]["instance_id"], "1")
                self.assertTrue(obs[0]["disks_detail"][0]["billing_owner"])
                self.assertTrue(all(o.get("storage_billing_ambiguous") is False for o in obs))

    def test_ambiguity_does_not_spread_to_unrelated_vm(self):
        items = self._items("custom-cache")
        unrelated = self._items("builder-private", ids=("3",), names=("builder-private",))[0]
        obs = observe_fleet_items(items + [unrelated], disk_types=self._catalog(items))
        self.assertEqual([o.get("storage_billing_ambiguous") for o in obs], [True, True, False])

    def test_denied_catalog_uses_source_role_and_flags_custom_disk(self):
        for name, ambiguous in (("runner-data", False), ("custom-cache", True)):
            obs = observe_fleet_items(self._items(name), disk_types_known=False)
            self._assert_single_charge(obs)
            self.assertEqual([o.get("storage_billing_ambiguous") for o in obs], [ambiguous, ambiguous])
            if not ambiguous:
                self.assertEqual(next(o["role"] for o in obs if o["disks_detail"][0]["billing_owner"]),
                                 "cicd_runner")

    def test_fleet_source_propagates_ambiguity_and_attachment_sources(self):
        items = self._items("custom-cache")
        catalog = self._catalog(items)
        with patch.object(compute_api, "get_auth", return_value=compute_api.Auth("t", "p")), \
                patch.object(compute_api, "list_instances", AsyncMock(return_value=(items, []))), \
                patch.object(compute_api, "list_disk_types", AsyncMock(return_value=catalog)), \
                patch.object(compute_api, "machine_type_capacity",
                             AsyncMock(return_value={"vcpu": 16, "ram_gib": 32.0})):
            obs = gce_fleet_source("p")()
        self.assertEqual(len(obs), 2)
        self._assert_single_charge(obs)
        self.assertTrue(all(o.get("storage_billing_ambiguous") is True for o in obs))
        self.assertTrue(all(o["specs_known"] for o in obs))
        for o in obs:
            self.assertEqual(o["disks_detail"][0]["source"], items[0]["disks"][0]["source"])

    def _later_catalog(self, items, users, *, named=True):
        catalog = self._catalog(items, named=named)
        next(iter(catalog.values()))["users"] = [
            "projects/p/zones/us-south1-a/instances/" + name for name in users]
        return catalog

    def _assert_single_vm_carrier(self, obs, items):
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["instance_id"], items[0]["id"])
        self.assertEqual(obs[0]["data_disk_gb"], 100)
        self.assertEqual(obs[0]["data_disks_attached_gb"], 100)
        self.assertEqual(obs[0]["data_disks"], {"pd-ssd": 100})
        self.assertEqual(len(obs[0]["disks_detail"]), 1)
        detail = obs[0]["disks_detail"][0]
        self.assertEqual(detail["source"], items[0]["disks"][0]["source"])
        self.assertEqual(detail["gb"], 100)
        self.assertIs(detail["billing_owner"], True)
        self.assertIs(detail["auto_delete"], False)
        self.assertIs(detail["boot"], False)

    def test_later_mixed_users_flag_single_attached_carrier_in_either_order(self):
        for carrier in ("oxidex-runners-a", "builder-a"):
            items = self._items("custom-cache", ids=("1",), names=(carrier,))
            for users in itertools.permutations(("oxidex-runners-a", "builder-a")):
                with self.subTest(carrier=carrier, users=users):
                    catalog = self._later_catalog(items, users)
                    obs = observe_fleet_items(items, disk_types=catalog)
                    self._assert_single_vm_carrier(obs, items)
                    self.assertIs(obs[0]["storage_billing_ambiguous"], True)
                    self.assertEqual(obs[0].get("storage_billing_ambiguous_roles"),
                                     ["cicd_runner", "rust_builder"])
                    self.assertEqual(observe_detached_disks(catalog, frozenset(catalog)), [])

    def test_later_mixed_users_preserve_explicit_role_when_owner_is_present(self):
        cases = (("runner-data", "oxidex-runners-a"), ("runner-data-cache", "oxidex-runners-a"),
                 ("oxidex-runners-cache", "oxidex-runners-a"), ("builder-cache", "builder-a"))
        for (disk_name, carrier), named, users in itertools.product(
                cases, (True, False), itertools.permutations(("oxidex-runners-a", "builder-a"))):
            with self.subTest(disk_name=disk_name, named=named, users=users):
                items = self._items(disk_name, ids=("1",), names=(carrier,))
                obs = observe_fleet_items(items, disk_types=self._later_catalog(items, users, named=named))
                self._assert_single_vm_carrier(obs, items)
                self.assertIs(obs[0]["storage_billing_ambiguous"], False)
                self.assertEqual(obs[0].get("storage_billing_ambiguous_roles"), [])

    def test_named_owner_missing_from_snapshot_keeps_charge_but_flags_role_attribution(self):
        items = self._items("builder-cache", ids=("1",), names=("oxidex-runners-a",))
        obs = observe_fleet_items(items, disk_types=self._later_catalog(items, ("oxidex-runners-a", "builder-a")))
        self._assert_single_vm_carrier(obs, items)
        self.assertIs(obs[0]["storage_billing_ambiguous"], True)
        self.assertEqual(obs[0].get("storage_billing_ambiguous_roles"), ["cicd_runner", "rust_builder"])

    def test_later_single_role_or_external_users_do_not_create_cross_role_ambiguity(self):
        for carrier, users in (("oxidex-runners-a", ("oxidex-runners-a", "spot-runners-b")),
                               ("builder-a", ("builder-a", "builder-b")),
                               ("oxidex-runners-a", ("oxidex-runners-a", "external-vm"))):
            items = self._items("custom-cache", ids=("1",), names=(carrier,))
            obs = observe_fleet_items(items, disk_types=self._later_catalog(items, users))
            self._assert_single_vm_carrier(obs, items)
            self.assertIs(obs[0]["storage_billing_ambiguous"], False)
            self.assertEqual(obs[0].get("storage_billing_ambiguous_roles"), [])

    def test_source_retains_later_role_evidence_without_emitting_second_charge(self):
        items = self._items("custom-cache", ids=("1",), names=("oxidex-runners-a",))
        catalog = self._later_catalog(items, ("oxidex-runners-a", "builder-a"))
        with patch.object(compute_api, "get_auth", return_value=compute_api.Auth("t", "p")), \
                patch.object(compute_api, "list_instances", AsyncMock(return_value=(items, []))), \
                patch.object(compute_api, "list_disk_types", AsyncMock(return_value=catalog)), \
                patch.object(compute_api, "machine_type_capacity",
                             AsyncMock(return_value={"vcpu": 16, "ram_gib": 32.0})):
            obs = gce_fleet_source("p")()
        self._assert_single_vm_carrier(obs, items)
        self.assertIs(obs[0]["storage_billing_ambiguous"], True)
        self.assertEqual(obs[0].get("storage_billing_ambiguous_roles"), ["cicd_runner", "rust_builder"])

    def test_private_disk_does_not_inherit_shared_disk_ambiguity(self):
        items = self._items("custom-cache")
        catalog = self._catalog(items)
        for index, item in enumerate(items):
            source = "z/disks/" + item["name"] + "-private"
            item["disks"].append({"source": source, "diskSizeGb": "40", "boot": True, "autoDelete": True})
            catalog[source] = {"id": str(600 + index), "type": "pd-balanced"}
        obs = observe_fleet_items(items, disk_types=catalog)
        for row in obs:
            shared, private = row["disks_detail"]
            self.assertEqual(shared.get("storage_billing_ambiguous_roles"), ["cicd_runner", "rust_builder"])
            self.assertEqual(private.get("storage_billing_ambiguous_roles"), [])
            self.assertEqual(row["storage_billing_ambiguous_roles"], ["cicd_runner", "rust_builder"])
            self.assertTrue(row["storage_billing_ambiguous"])
            self.assertEqual((row["boot_disk_gb"], row["data_disk_gb"]), (40, 100))
        self.assertEqual(sum(row["boot_disk_billed_gb"] + sum(row["data_disks"].values()) for row in obs), 180)

    def test_explicit_builder_extreme_disk_only_affects_owner_billing_trust(self):
        for ids, boot in itertools.product((("1", "2"), ("2", "1")), (False, True)):
            with self.subTest(ids=ids, boot=boot):
                items = self._items("builder-cache", ids=ids, boot=boot)
                catalog = self._catalog(items)
                next(iter(catalog.values())).update(type="pd-extreme", provisionedIops=12000,
                                                    provisionedThroughput=300)
                obs = observe_fleet_items(items, disk_types=catalog)
                self._assert_single_charge(obs, boot=boot)
                for row in obs:
                    self.assertEqual(row["unpriced_disk_performance"], row["role"] == "rust_builder")
                    detail = row["disks_detail"][0]
                    self.assertIs(detail.get("unpriced_disk_performance"), True)
                    self.assertEqual(detail.get("provisionedIops"), 12000)
                    self.assertEqual(detail.get("provisionedThroughput"), 300)
                    self.assertIs(detail.get("disk_types_known"), True)
                    self.assertIs(detail.get("regional"), False)
                    self.assertEqual(detail.get("storage_billing_ambiguous_roles"), [])

    def test_ambiguous_extreme_disk_affects_both_roles_but_bills_once(self):
        items = self._items("custom-cache")
        catalog = self._catalog(items)
        next(iter(catalog.values()))["type"] = "pd-extreme"
        obs = observe_fleet_items(items, disk_types=catalog)
        self._assert_single_charge(obs)
        for row in obs:
            self.assertTrue(row["unpriced_disk_performance"])
            self.assertEqual(row["storage_billing_ambiguous_roles"], ["cicd_runner", "rust_builder"])
            detail = row["disks_detail"][0]
            self.assertIs(detail.get("unpriced_disk_performance"), True)
            self.assertEqual(detail.get("storage_billing_ambiguous_roles"), ["cicd_runner", "rust_builder"])

    def test_regional_storage_trust_uses_billing_owner_or_ambiguous_disk(self):
        for name in ("builder-cache", "custom-cache"):
            items = self._items(name)
            catalog = self._catalog(items)
            next(iter(catalog.values()))["regional"] = True
            obs = observe_fleet_items(items, disk_types=catalog)
            self._assert_single_charge(obs)
            for row in obs:
                self.assertEqual(row["regional_disks"], name == "custom-cache" or row["role"] == "rust_builder")
                self.assertIs(row["disks_detail"][0].get("regional"), True)

    def test_unresolved_disk_type_only_affects_relevant_billing_trust(self):
        for name, listed in itertools.product(("builder-cache", "custom-cache"), (True, False)):
            with self.subTest(name=name, listed=listed):
                items = self._items(name)
                catalog = self._catalog(items)
                next(iter(catalog.values())).pop("type")
                obs = observe_fleet_items(items, disk_types=catalog if listed else {}, disk_types_known=listed)
                self._assert_single_charge(obs)
                for row in obs:
                    relevant = name == "custom-cache" or row["role"] == "rust_builder"
                    self.assertEqual(row["disk_types_known"], not relevant)
                    self.assertIs(row["disks_detail"][0].get("disk_types_known"), False)


class SplitSnapshotDiskBillingRoleTests(unittest.TestCase):
    def catalog(self, name, users, *, include_name=True):
        source = "projects/p/zones/us-south1-a/disks/" + name
        detail = {"id": "501", "type": "pd-ssd", "sizeGb": "100", "zone": "zones/us-south1-a",
                  "users": ["projects/p/zones/us-south1-a/instances/" + user for user in users]}
        if include_name:
            detail["name"] = name
        return {source: detail}

    def assert_one_disk_charge(self, observations):
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["instance_id"], "disk-501")
        self.assertEqual(observations[0]["kind"], "disk")
        self.assertEqual(sum(sum(o["data_disks"].values()) for o in observations), 100)
        self.assertEqual(sum(d["gb"] for o in observations for d in o["disks_detail"]), 100)

    def test_unnamed_mixed_users_flag_ambiguity_independent_of_order(self):
        users = ("builder-a", "oxidex-runners-a", "builder-b")
        for ordered, include_name in itertools.product(itertools.permutations(users), (True, False)):
            with self.subTest(users=ordered, include_name=include_name):
                catalog = self.catalog("custom-cache", ordered, include_name=include_name)
                obs = observe_detached_disks(catalog)
                self.assert_one_disk_charge(obs)
                self.assertTrue(obs[0].get("storage_billing_ambiguous"))
                self.assertEqual(obs[0].get("storage_billing_ambiguous_roles"), ["cicd_runner", "rust_builder"])
                self.assertEqual(obs[0]["role"], "cicd_runner")  # Stable numerical carrier, not role evidence.

    def test_explicit_name_or_source_role_wins_for_mixed_users(self):
        cases = (("runner-data", "cicd_runner"), ("runner-data-cache", "cicd_runner"),
                 ("oxidex-runners-cache", "cicd_runner"), ("builder-cache", "rust_builder"))
        for (name, role), ordered, include_name in itertools.product(
                cases, itertools.permutations(("builder-a", "oxidex-runners-a")), (True, False)):
            with self.subTest(name=name, users=ordered, include_name=include_name):
                obs = observe_detached_disks(self.catalog(name, ordered, include_name=include_name))
                self.assert_one_disk_charge(obs)
                self.assertEqual(obs[0]["role"], role)
                self.assertIs(obs[0].get("storage_billing_ambiguous"), False)

    def test_single_role_users_remain_unambiguous(self):
        for users, role in ((("builder-a", "builder-b"), "rust_builder"),
                            (("oxidex-runners-a", "spot-runners-b"), "cicd_runner")):
            for ordered in itertools.permutations(users):
                with self.subTest(users=ordered):
                    obs = observe_detached_disks(self.catalog("custom-cache", ordered))
                    self.assert_one_disk_charge(obs)
                    self.assertEqual(obs[0]["role"], role)
                    self.assertIs(obs[0].get("storage_billing_ambiguous"), False)

    def test_attached_source_suppresses_split_snapshot_charge(self):
        catalog = self.catalog("custom-cache", ("builder-a", "oxidex-runners-a"))
        self.assert_one_disk_charge(observe_detached_disks(catalog))
        self.assertEqual(observe_detached_disks(catalog, attached_sources=frozenset(catalog)), [])

    def test_detached_detail_carries_its_roles_and_physical_trust_metadata(self):
        for name, dtype in itertools.product(("custom-cache", "builder-cache"), ("pd-extreme", None)):
            with self.subTest(name=name, dtype=dtype):
                catalog = self.catalog(name, ("builder-a", "oxidex-runners-a"))
                raw = next(iter(catalog.values()))
                raw.update(type=dtype, regional=True, provisionedIops=12000)
                obs = observe_detached_disks(catalog)
                self.assert_one_disk_charge(obs)
                row = obs[0]
                detail = row["disks_detail"][0]
                self.assertEqual(detail.get("storage_billing_ambiguous_roles"),
                                 ["cicd_runner", "rust_builder"] if name == "custom-cache" else [])
                self.assertEqual(row["storage_billing_ambiguous_roles"], detail["storage_billing_ambiguous_roles"])
                self.assertIs(detail.get("regional"), True)
                self.assertEqual(detail.get("unpriced_disk_performance"), dtype == "pd-extreme")
                self.assertEqual(detail.get("disk_types_known"), dtype is not None)
                self.assertEqual(detail.get("provisionedIops"), 12000)
                self.assertEqual(row["disk_types_known"], detail["disk_types_known"])
                self.assertEqual(row["regional_disks"], detail["regional"])
                self.assertEqual(row["unpriced_disk_performance"], detail["unpriced_disk_performance"])


class BuilderPhysicalShapeTests(unittest.TestCase):
    def test_builder_shape_requires_exact_physical_capacity_and_no_explicit_unknown_specs(self):
        from lib.fleet_inventory import builder_shape_compatible

        base = {"machine_type": "n4-highcpu-16", "vcpu": 16, "ram_gib": 32.0}
        for changes, expected in (({}, True), ({"specs_known": True}, True), ({"specs_known": False}, False),
                                  ({"machine_type": "n4-standard-16"}, False), ({"vcpu": 8}, False),
                                  ({"ram_gib": 64.0}, False), ({"ram_gib": 28.0}, False),
                                  ({"ram_gib": float("nan")}, False), ({"vcpu": "16"}, False),
                                  ({"runner_vcpu": 1, "runner_ram_gib": 1}, True)):
            with self.subTest(changes=changes):
                self.assertIs(builder_shape_compatible(dict(base, **changes)), expected)
        self.assertFalse(builder_shape_compatible({}))


if __name__ == "__main__":
    unittest.main()
