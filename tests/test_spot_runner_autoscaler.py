from decimal import Decimal
import unittest

from pricing_fixtures import storage_prices_for
from lib.compute_api import Offer
from lib.autoscaling_policy import affordable_offers, vm_count_for_floor
from lib.runner_fleet import GitHubRunner
from lib.spot_runner_autoscaler import (
    eligible_offers,
    host_runners,
    maximum_runner_name_length,
    runner_capacity,
    scaling_decision,
)


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
        selected = eligible_offers(offers, Decimal("5"), storage_rates=storage_prices_for(offers))
        self.assertEqual([offer.region for offer in selected], ["us-west3"])

    def test_host_capacity_counts_all_runners_on_the_machine(self):
        self.assertEqual(runner_capacity(32), 8)

    def test_floor_planner_rounds_up_to_whole_hosts(self):
        self.assertEqual(vm_count_for_floor(25, 25, 8), 0)
        self.assertEqual(vm_count_for_floor(19, 25, 8), 1)
        self.assertEqual(vm_count_for_floor(9, 25, 8), 2)
        self.assertEqual(vm_count_for_floor(0, 25, 8), 4)

    def test_floor_planner_rejects_invalid_capacity_inputs(self):
        for values in ((-1, 25, 8), (1, 0, 8), (1, 25, 0)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                vm_count_for_floor(*values)

    def test_existing_docker_labeled_spot_runners_count_as_managed_capacity(self):
        from lib.spot_runner_autoscaler import utilization_counts

        runners = [GitHubRunner(1, "spot-1", "online", False, ("docker", "n2-standard-32", "X64")),
                   GitHubRunner(2, "spot-2", "online", True, ("spot", "n2-standard-32", "X64"))]
        self.assertEqual(utilization_counts(runners), (2, 1))

    def test_total_floor_does_not_count_unmanaged_docker_or_arm_runners(self):
        from lib.spot_runner_autoscaler import count_online_runners
        from unittest.mock import patch

        runners = [
            GitHubRunner(1, "spot-1", "online", False, ("spot", "n2-standard-32", "X64")),
            GitHubRunner(2, "docker-1", "online", False, ("docker", "n2-standard-32", "X64")),
            GitHubRunner(3, "arm-1", "online", False, ("docker", "ARM64")),
        ]
        with patch("lib.spot_runner_autoscaler.list_github_runners", return_value=runners):
            self.assertEqual(count_online_runners("swack-tools", "spot"), 1)

    def test_total_floor_counts_legacy_toronto_spot_labels(self):
        from lib.spot_runner_autoscaler import count_online_runners
        from unittest.mock import patch

        runners = [
            GitHubRunner(1, "oxidex-nane2-1", "online", False,
                         ("spot", "oxidex-spot-7x27", "northamerica-northeast2", "X64")),
        ]
        with patch("lib.spot_runner_autoscaler.list_github_runners", return_value=runners):
            self.assertEqual(count_online_runners("swack-tools", "spot"), 1)

    def test_autoscaler_runner_names_fit_github_limit_with_short_prefix(self):
        instance = "oxidex-runners-northamericanortheast2-a-2147483647"
        self.assertLessEqual(maximum_runner_name_length("gha", instance, 8), 64)

    def test_low_usage_requires_two_loops_then_removes_one_vm(self):
        self.assertEqual(scaling_decision(40, 10, 5, 0, False, 25),
                         ("wait", 0, 1, False))
        self.assertEqual(scaling_decision(40, 10, 5, 1, False, 25),
                         ("down", 1, 0, False))
        self.assertEqual(scaling_decision(16, 4, 2, 1, False, 25),
                         ("down", 1, 0, False))
        self.assertEqual(scaling_decision(8, 1, 1, 0, False, 25),
                         ("wait", 0, 0, False))

    def test_high_usage_adds_one_host_and_rechecks_utilization(self):
        self.assertEqual(scaling_decision(8, 5, 1, 0, False, 25),
                         ("up", 1, 0, False))
        self.assertEqual(scaling_decision(16, 8, 2, 0, True, 25),
                         ("wait", 0, 0, False))
        self.assertEqual(scaling_decision(32, 16, 4, 0, True, 25),
                         ("wait", 0, 0, False))

    def test_vm_runner_matching_prefers_the_longest_name(self):
        from lib.runner_fleet import GceVm

        short = GceVm("oxidex-runners-nane2", "zone-b", "RUNNING", "n2-standard-32", True)
        long = GceVm("oxidex-runners-nane2-a", "zone-a", "RUNNING", "n2-standard-32", True)
        runners = [
            GitHubRunner(1, "oxidex-oxidex-runners-nane2-1", "online", False, ("spot",)),
            GitHubRunner(2, "oxidex-oxidex-runners-nane2-a-1", "online", False, ("spot",)),
        ]
        self.assertEqual(host_runners(short, runners, [short, long]), [runners[0]])
        self.assertEqual(host_runners(long, runners, [short, long]), [runners[1]])


if __name__ == "__main__":
    unittest.main()
