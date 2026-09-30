"""Verify append logging and VM observations without changing cloud resources."""

import logging
import signal
import tempfile
import unittest
from pathlib import Path

from lib.fleet_logging import lifecycle_log, observe_vms
from lib.runner_fleet import GceVm
from lib.spot_runner_autoscaler import read_scale_state, write_scale_state


class FleetLoggingTests(unittest.TestCase):
    def test_restarts_append_and_signals_record_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.log"
            path.write_text("existing event\n")
            original = signal.getsignal(signal.SIGTERM)
            with lifecycle_log(path):
                logging.getLogger("spot_runner_autoscaler").info("first run")
            with self.assertRaises(SystemExit), lifecycle_log(path):
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            content = path.read_text()
            self.assertTrue(content.startswith("existing event\n"))
            self.assertIn("first run", content)
            self.assertIn("received SIGTERM", content)
            self.assertEqual(content.count("Controller started:"), 2)
            self.assertEqual(content.count("Controller shutdown:"), 2)
            self.assertEqual(signal.getsignal(signal.SIGTERM), original)

    def test_inventory_changes_survive_restart_and_do_not_repeat(self):
        vm = GceVm("oxidex-runners-test", "asia-south2-a", "RUNNING", "n2-standard-32", True)
        state = {"idle_loops": 0, "scale_up_active": False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with self.assertLogs("spot_runner_autoscaler", level="INFO"):
                observe_vms([vm], state)
            write_scale_state(path, state)
            recovered = read_scale_state(path)
            with self.assertNoLogs("spot_runner_autoscaler"):
                observe_vms([vm], recovered)
            stopped = GceVm(vm.name, vm.zone, "TERMINATED", vm.machine_type, True)
            with self.assertLogs("spot_runner_autoscaler") as logs:
                observe_vms([stopped], recovered)
            self.assertIn("Spot VM is terminated", "\n".join(logs.output))
            with self.assertLogs("spot_runner_autoscaler") as logs:
                observe_vms([], recovered)
            self.assertIn("no longer present", "\n".join(logs.output))
            with self.assertNoLogs("spot_runner_autoscaler"):
                observe_vms([], recovered)

    def test_errors_log_shutdown_and_propagate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.log"
            with self.assertRaises(RuntimeError), lifecycle_log(path):
                raise RuntimeError("failure")
            content = path.read_text()
            self.assertIn("error of type RuntimeError", content)
            self.assertIn("Controller shutdown:", content)


if __name__ == "__main__":
    unittest.main()
