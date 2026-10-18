"""Regional rate trust is independent of whether a tax rate is known."""

import unittest

from lib.dashboard_models import REGIONS_CATALOG, calculate_spot_pricing


class DashboardRegionalRateTrustTests(unittest.TestCase):
    region = "outside-catalog1"

    def price(self, *, live=None, **overrides):
        args = dict(
            family="n2", vcpu=4, ram_gib=16, region=self.region,
            machine_type="n2-standard-4", boot_disk_type="pd-balanced",
            boot_disk_gb=10, data_disk_type="pd-ssd", data_disk_gb=100,
            live_pricing={} if live is None else live,
        )
        args.update(overrides)
        return calculate_spot_pricing(**args)

    def live_disks(self):
        return {(self.region, "pd-balanced"): "0.1", (self.region, "pd-ssd"): "0.2"}

    def components(self, kind="spot"):
        return {(kind, self.region, "n2", "CPU"): "0.01",
                (kind, self.region, "n2", "RAM"): "0.001"}

    def assert_region_trusted(self, result):
        self.assertNotIn("regional_compute_rate_unknown", result)
        self.assertNotIn("regional_disk_rate_unknown", result)

    def test_live_machine_and_disks_are_trusted_outside_catalog(self):
        self.assertNotIn(self.region, {r["region"] for r in REGIONS_CATALOG})
        for machine_key in ((self.region, "n2-standard-4"), f"{self.region}/n2-standard-4"):
            with self.subTest(machine_key=machine_key):
                result = self.price(live={"machine_hourly": {machine_key: "0.15"},
                                          "disk_monthly_gib": self.live_disks()})
                self.assert_region_trusted(result)
                self.assertNotIn("complete", result)
                self.assertEqual(result["pricing_source"], "gcp_capacity_history")
                self.assertEqual(result["compute_hourly_usd"], 0.15)
                self.assertEqual(result["disk_monthly_usd"], 21.0)

    def test_complete_live_components_and_disks_are_trusted(self):
        result = self.price(live={"component_rates": self.components(),
                                 "disk_monthly_gib": self.live_disks()})
        self.assert_region_trusted(result)
        self.assertNotIn("complete", result)
        self.assertEqual(result["compute_hourly_usd"], 0.056)

    def test_static_compute_and_disk_fallbacks_are_untrusted_outside_catalog(self):
        result = self.price()
        self.assertTrue(result.get("regional_compute_rate_unknown"))
        self.assertTrue(result.get("regional_disk_rate_unknown"))
        self.assertFalse(result["complete"])

    def test_each_positive_disk_leg_requires_live_rate(self):
        for missing_type in ("pd-balanced", "pd-ssd"):
            with self.subTest(missing_type=missing_type):
                disks = self.live_disks()
                del disks[(self.region, missing_type)]
                result = self.price(live={"component_rates": self.components(),
                                         "disk_monthly_gib": disks})
                self.assertNotIn("regional_compute_rate_unknown", result)
                self.assertTrue(result.get("regional_disk_rate_unknown"))
                self.assertFalse(result["complete"])

    def test_each_positive_compute_component_requires_live_rate(self):
        for missing_component in ("CPU", "RAM"):
            with self.subTest(missing_component=missing_component):
                components = self.components()
                del components[("spot", self.region, "n2", missing_component)]
                result = self.price(live={"component_rates": components,
                                         "disk_monthly_gib": self.live_disks()})
                self.assertTrue(result.get("regional_compute_rate_unknown"))
                self.assertNotIn("regional_disk_rate_unknown", result)
                self.assertFalse(result["complete"])

    def test_zero_compute_quantity_does_not_require_its_component_rate(self):
        for component, quantities, expected in (
            ("CPU", {"vcpu": 4, "ram_gib": 0}, 0.04),
            ("RAM", {"vcpu": 0, "ram_gib": 16}, 0.016),
        ):
            with self.subTest(component=component):
                key = ("spot", self.region, "n2", component)
                result = self.price(live={"component_rates": {key: self.components()[key]}},
                                    boot_disk_gb=0, data_disk_gb=0, **quantities)
                self.assert_region_trusted(result)
                self.assertNotIn("complete", result)
                self.assertEqual(result["compute_hourly_usd"], expected)

    def test_zero_disk_quantity_does_not_require_its_rate(self):
        for boot_gb, data_gb, disk_type in ((0, 100, "pd-ssd"), (10, 0, "pd-balanced")):
            with self.subTest(boot_gb=boot_gb, data_gb=data_gb):
                result = self.price(live={"component_rates": self.components(),
                                         "disk_monthly_gib": {(self.region, disk_type): "0.1"}},
                                    boot_disk_gb=boot_gb, data_disk_gb=data_gb)
                self.assert_region_trusted(result)
                self.assertNotIn("complete", result)

    def test_all_zero_quantities_need_no_live_rates(self):
        result = self.price(vcpu=0, ram_gib=0, boot_disk_gb=0, data_disk_gb=0)
        self.assert_region_trusted(result)
        self.assertNotIn("complete", result)
        self.assertEqual(result["total_hourly_usd"], 0.0)

    def test_positive_ram_without_cpus_still_requires_a_live_rate(self):
        result = self.price(vcpu=0, ram_gib=16, boot_disk_gb=0, data_disk_gb=0)
        self.assertTrue(result.get("regional_compute_rate_unknown"))
        self.assertNotIn("regional_disk_rate_unknown", result)
        self.assertFalse(result["complete"])

    def test_zero_live_rates_are_authoritative(self):
        result = self.price(live={
            "component_rates": {key: 0 for key in self.components()},
            "disk_monthly_gib": {key: 0 for key in self.live_disks()},
        })
        self.assert_region_trusted(result)
        self.assertNotIn("complete", result)
        self.assertEqual(result["total_hourly_usd"], 0.0)

    def test_known_region_baseline_payload_does_not_gain_flags(self):
        result = self.price(region="us-south1")
        self.assert_region_trusted(result)
        self.assertNotIn("complete", result)
        self.assertEqual(len(result), 14)

    def test_on_demand_ignores_spot_machine_and_component_rates(self):
        result = self.price(provisioning="ondemand", live={
            "machine_hourly": {(self.region, "n2-standard-4"): "0.15"},
            "component_rates": self.components(), "disk_monthly_gib": self.live_disks(),
        })
        self.assertTrue(result.get("regional_compute_rate_unknown"))
        self.assertNotIn("regional_disk_rate_unknown", result)
        self.assertFalse(result["complete"])
        self.assertEqual(result["pricing_source"], "spot_baseline_for_on_demand_vm")

    def test_on_demand_with_live_components_is_complete(self):
        result = self.price(provisioning="ondemand", live={
            "component_rates": self.components("ondemand"), "disk_monthly_gib": self.live_disks(),
        })
        self.assert_region_trusted(result)
        self.assertTrue(result["complete"])

    def test_missing_live_disk_overrides_on_demand_compute_completeness(self):
        result = self.price(provisioning="ondemand", live={
            "component_rates": self.components("ondemand"),
        })
        self.assertNotIn("regional_compute_rate_unknown", result)
        self.assertTrue(result.get("regional_disk_rate_unknown"))
        self.assertFalse(result["complete"])

    def test_custom_and_extended_shapes_remain_incomplete_with_series_rates(self):
        for machine_type in ("n2-custom-4-16384", "n2-custom-4-16384-ext"):
            for provisioning in ("spot", "ondemand"):
                with self.subTest(machine_type=machine_type, provisioning=provisioning):
                    result = self.price(machine_type=machine_type, provisioning=provisioning, live={
                        "component_rates": self.components(provisioning),
                        "disk_monthly_gib": self.live_disks(),
                    })
                    self.assertFalse(result["complete"])

    def test_custom_shape_with_live_machine_rate_remains_trusted(self):
        result = self.price(machine_type="n2-custom-4-16384", live={
            "machine_hourly": {(self.region, "n2-custom-4-16384"): "0.15"},
            "disk_monthly_gib": self.live_disks(),
        })
        self.assert_region_trusted(result)
        self.assertNotIn("complete", result)


if __name__ == "__main__":
    unittest.main()
