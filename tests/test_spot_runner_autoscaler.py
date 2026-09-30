from decimal import Decimal
import unittest

from lib.compute_api import Offer
from lib.autoscaling_policy import affordable_offers, vm_count_for_floor
from lib.runner_labels import counts_toward_capacity, custom_labels_for_runner
from lib.spot_runner_autoscaler import eligible_offers, runner_capacity


class AutoscalerPolicyTests(unittest.TestCase):
    def test_offers_are_filtered_by_strict_per_vm_daily_budget_and_ranked(self):
        offers = [
            Offer("us-west3", "c2d-standard-32", 32, 128.0, 0.20),
            Offer("asia-south2", "n2-standard-32", 32, 128.0, 0.168064),
            Offer("us-east1", "n2-standard-32", 32, 128.0, 5.0 / 24.0),
        ]
        selected = affordable_offers(offers, max_daily_usd=Decimal("5"))
        self.assertEqual([offer.region for offer in selected], ["asia-south2", "us-west3"])

    def test_offer_filter_requires_exact_host_size_and_daily_budget(self):
        offers = [
            Offer("us-west3", "c2d-standard-32", 32, 128.0, 0.157184),
            Offer("us-west4", "e2-standard-32", 32, 128.0, 0.159360),
            Offer("asia-south2", "n2-standard-16", 16, 64.0, 0.08),
            Offer("us-east1", "n2-standard-32", 32, 256.0, 0.21),
            Offer("us-east2", "n2-standard-32", 32, 128.0, 5.0 / 24.0),
        ]
        selected = eligible_offers(offers, Decimal("5"))
        self.assertEqual([offer.region for offer in selected], ["us-west3", "us-west4"])

    def test_host_capacity_excludes_two_docker_runners(self):
        self.assertEqual(runner_capacity(32), 6)

    def test_floor_planner_rounds_up_to_whole_hosts(self):
        self.assertEqual(vm_count_for_floor(25, 25, 8), 0)
        self.assertEqual(vm_count_for_floor(19, 25, 8), 1)
        self.assertEqual(vm_count_for_floor(9, 25, 8), 2)
        self.assertEqual(vm_count_for_floor(0, 25, 8), 4)

    def test_floor_planner_rejects_invalid_capacity_inputs(self):
        for values in ((-1, 25, 8), (1, 0, 8), (1, 25, 0)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                vm_count_for_floor(*values)

    def test_docker_runners_use_a_separate_pool_without_spot(self):
        self.assertEqual(custom_labels_for_runner(1, 2, ["spot", "n2-standard-32", "asia-south2"]),
                         ["docker", "n2-standard-32", "asia-south2"])
        self.assertEqual(custom_labels_for_runner(3, 2, ["spot", "n2-standard-32", "asia-south2"]),
                         ["spot", "n2-standard-32", "asia-south2"])

    def test_docker_runners_do_not_count_toward_the_spot_fleet_floor(self):
        self.assertFalse(counts_toward_capacity(["docker"], "spot"))
        self.assertTrue(counts_toward_capacity(["spot"], "spot"))
        self.assertFalse(counts_toward_capacity(["other"], "spot"))


if __name__ == "__main__":
    unittest.main()
