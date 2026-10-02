"""Verify price eviction without changing cloud resources."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from lib.price_guard import check_running_prices, delete_overpriced_vm
from lib.runner_fleet import GceVm, GitHubRunner
from lib.spot_runner_autoscaler import read_scale_state, reconcile_prices, utilization_counts


class PriceGuardTests(unittest.TestCase):
    def setUp(self):
        self.vm = GceVm("oxidex-runners-test", "asia-south2-a", "RUNNING", "n2-standard-32", True,
                       disks=({'diskType': 'pd-standard', 'diskSizeGb': 140},))
        self.args = SimpleNamespace(project="project", org="org", max_daily_usd=Decimal("5"), disk_size_gb=100)
        disk_rates = patch('lib.price_guard.storage_rates', return_value={'pd-standard:capacity': Decimal('.04')})
        disk_rates.start()
        self.addCleanup(disk_rates.stop)
        self.shape = SimpleNamespace(stdout=json.dumps({"guestCpus": 32, "memoryMb": 131072}))

    @patch("lib.price_guard.subprocess.run")
    @patch("lib.price_guard.spot_rates")
    def test_price_comparison_and_fresh_lookup_on_each_check(self, rates, command):
        command.return_value = self.shape
        rates.side_effect = [(Decimal("0.005"), Decimal(0)),
                             (Decimal("0.01"), Decimal(0))]
        self.assertEqual(check_running_prices([self.vm], "project", Decimal("3.84") + Decimal('5.6') * 24 / 730), [self.vm])
        self.assertEqual(check_running_prices([self.vm], "project", Decimal("5")), [self.vm])
        self.assertEqual(rates.call_count, 2)

    @patch("lib.price_guard.subprocess.run")
    @patch("lib.price_guard.spot_rates", side_effect=RuntimeError("API unavailable"))
    def test_missing_price_never_marks_a_vm_for_eviction(self, rates, command):
        with self.assertLogs("spot_runner_autoscaler", level="ERROR"):
            self.assertEqual(check_running_prices([self.vm], "project", Decimal("5")), [])
        command.assert_not_called()

    @patch("lib.price_guard.subprocess.run")
    def test_price_eviction_deletes_disks_and_rejects_other_vms(self, command):
        delete_overpriced_vm(self.vm, "project")
        self.assertIn("--delete-disks=all", command.call_args.args[0])
        other = GceVm("unrelated", "asia-south2-a", "RUNNING", "n2-standard-32", True)
        with self.assertRaises(ValueError):
            delete_overpriced_vm(other, "project")
        self.assertEqual(command.call_count, 1)

    def test_alternative_supported_replacement_families_count_as_capacity(self):
        runners = [GitHubRunner(index, f"gha-replacement-{index}", "online", False,
                               ("spot", "X64", shape))
                   for index, shape in enumerate(("e2-standard-32", "c2d-standard-32"))]
        self.assertEqual(utilization_counts(runners), (2, 0))

    def test_busy_price_eviction_saves_replacement_before_deleting(self):
        runner = GitHubRunner(1, "gha-oxidex-runners-test-1", "online", True, ("spot",))
        state = {"idle_loops": 1, "scale_up_active": False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"

            def remove(vm, project):
                self.assertEqual(read_scale_state(path)["price_replacement_target"], 1)

            with patch("lib.spot_runner_autoscaler.check_running_prices", return_value=[self.vm]), \
                    patch("lib.spot_runner_autoscaler.delete_overpriced_vm", side_effect=remove) as delete, \
                    patch("lib.spot_runner_autoscaler.subprocess.run", return_value=SimpleNamespace(returncode=0)), \
                    patch("lib.spot_runner_autoscaler.list_gcloud_vms", return_value=[]), \
                    patch("lib.spot_runner_autoscaler.discover_offers", return_value=["offer"]), \
                    patch("lib.spot_runner_autoscaler.scale_up", return_value=0):
                self.assertTrue(reconcile_prices([self.vm], [runner], self.args, state, path))
                delete.assert_called_once_with(self.vm, "project")
            recovered = read_scale_state(path)
            self.assertEqual(recovered["price_replacement_target"], 1)
            from lib.spot_runner_autoscaler import reconcile_host_operation
            reconcile_host_operation([], [], self.args, recovered, path)
            with patch("lib.spot_runner_autoscaler.check_running_prices", return_value=[]), \
                    patch("lib.spot_runner_autoscaler.list_gcloud_vms", return_value=[]), \
                    patch("lib.spot_runner_autoscaler.discover_offers", return_value=["offer"]), \
                    patch("lib.spot_runner_autoscaler.scale_up", return_value=1) as launch:
                self.assertTrue(reconcile_prices([], [], self.args, recovered, path))
                launch.assert_called_once_with(1, ["offer"], self.args, recovered, path, refresh=ANY)
            self.assertEqual(read_scale_state(path)["price_replacement_target"], 1)
            with patch("lib.spot_runner_autoscaler.check_running_prices", return_value=[]):
                reconcile_prices([self.vm], [], self.args, recovered, path)
            self.assertEqual(read_scale_state(path)["price_replacement_target"], 0)

    @patch("lib.price_guard.subprocess.run")
    @patch("lib.price_guard.spot_rates", side_effect=RuntimeError("API unavailable"))
    def test_strict_pending_eviction_price_check_exposes_lookup_failure(self, rates, command):
        with self.assertRaisesRegex(RuntimeError, "API unavailable"):
            check_running_prices([self.vm], "project", Decimal("5"), raise_on_error=True)
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
