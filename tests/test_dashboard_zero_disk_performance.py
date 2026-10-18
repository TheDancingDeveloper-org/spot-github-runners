"""Only positive billed disk sizes require performance pricing."""

import unittest

from lib.dashboard_models import calculate_spot_pricing


class DashboardZeroDiskPerformanceTests(unittest.TestCase):
    def price(self, **overrides):
        args = dict(
            family="n2", vcpu=4, ram_gib=16, region="us-south1",
            machine_type="n2-standard-4", boot_disk_type="pd-balanced",
            boot_disk_gb=0, data_disk_type="pd-balanced", data_disk_gb=0,
            live_pricing={
                "machine_hourly": {("us-south1", "n2-standard-4"): "0.15"},
                "component_rates": {
                    (kind, "us-south1", "n2", component): rate
                    for kind in ("spot", "ondemand")
                    for component, rate in (("CPU", "0.01"), ("RAM", "0.001"))
                },
                "disk_monthly_gib": {
                    ("us-south1", "pd-balanced"): "0.1",
                    ("us-south1", "pd-extreme"): "0.2",
                    ("us-south1", "hyperdisk-throughput"): "0.2",
                },
            },
        )
        args.update(overrides)
        return calculate_spot_pricing(**args)

    def test_zero_boot_performance_disk_does_not_make_pricing_incomplete(self):
        for disk_type in ("pd-extreme", "hyperdisk-throughput"):
            with self.subTest(disk_type=disk_type):
                result = self.price(boot_disk_type=disk_type, data_disk_gb=100)
                self.assertNotIn("complete", result)
                self.assertEqual(result["compute_hourly_usd"], 0.15)
                self.assertEqual(result["disk_monthly_usd"], 10.0)

    def test_positive_boot_performance_disk_remains_incomplete_with_live_rate(self):
        for disk_type in ("pd-extreme", "hyperdisk-throughput"):
            with self.subTest(disk_type=disk_type):
                result = self.price(boot_disk_type=disk_type, boot_disk_gb=10)
                self.assertFalse(result["complete"])
                self.assertEqual(result["disk_monthly_usd"], 2.0)

    def test_zero_data_performance_disk_does_not_make_pricing_incomplete(self):
        for disk_type in ("pd-extreme", "hyperdisk-throughput"):
            with self.subTest(disk_type=disk_type):
                result = self.price(data_disk_type=disk_type, boot_disk_gb=10)
                self.assertNotIn("complete", result)
                self.assertEqual(result["disk_monthly_usd"], 1.0)

    def test_positive_data_performance_disk_remains_incomplete_with_live_rate(self):
        for disk_type in ("pd-extreme", "hyperdisk-throughput"):
            with self.subTest(disk_type=disk_type):
                result = self.price(data_disk_type=disk_type, data_disk_gb=100)
                self.assertFalse(result["complete"])
                self.assertEqual(result["disk_monthly_usd"], 20.0)

    def test_both_zero_performance_disk_types_do_not_make_pricing_incomplete(self):
        for boot_type in ("pd-extreme", "hyperdisk-throughput"):
            for data_type in ("pd-extreme", "hyperdisk-throughput"):
                with self.subTest(boot_type=boot_type, data_type=data_type):
                    result = self.price(boot_disk_type=boot_type, data_disk_type=data_type)
                    self.assertNotIn("complete", result)
                    self.assertEqual(result["disk_monthly_usd"], 0.0)

    def test_zero_performance_disks_preserve_custom_shape_guard(self):
        for machine_type in ("n2-custom-4-16384", "n2-custom-4-16384-ext"):
            for provisioning in ("spot", "ondemand"):
                with self.subTest(machine_type=machine_type, provisioning=provisioning):
                    result = self.price(
                        machine_type=machine_type, provisioning=provisioning,
                        boot_disk_type="pd-extreme", data_disk_type="hyperdisk-throughput",
                    )
                    self.assertFalse(result["complete"])

    def test_zero_performance_disks_preserve_missing_on_demand_rates_guard(self):
        result = self.price(
            provisioning="ondemand", boot_disk_type="pd-extreme",
            data_disk_type="hyperdisk-throughput",
            live_pricing={"machine_hourly": {("us-south1", "n2-standard-4"): "0.15"}},
        )
        self.assertFalse(result["complete"])
        self.assertEqual(result["pricing_source"], "spot_baseline_for_on_demand_vm")

    def test_zero_performance_disks_allow_complete_on_demand_component_pricing(self):
        result = self.price(
            provisioning="ondemand", boot_disk_type="pd-extreme",
            data_disk_type="hyperdisk-throughput",
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["compute_hourly_usd"], 0.056)


if __name__ == "__main__":
    unittest.main()
