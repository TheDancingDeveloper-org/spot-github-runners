"""The tdd-org Spot poller stops creating VMs at a daily USD cap (WI-1914)."""

import datetime as dt
import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).resolve().parents[1] / "deploy" / "tdd-org-gcp" / "poller" / "gha_spot_poller.py"


def load_poller():
    env = {"GH_ORG": "Org", "REPOS": "vogt", "GCP_PROJECT": "p", "SMALL_TEMPLATE": "gha-small-*",
           "LARGE_TEMPLATE": "gha-large-*", "DAILY_USD_CAP": "5"}
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("gha_spot_poller_spend_under_test", POLLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


P = load_poller()
NOON = dt.datetime(2026, 10, 19, 12, tzinfo=dt.timezone.utc).timestamp()
PRICES = {"large": {"hour": "1", "disk_gb": "0"}, "small": {"hour": "0.5", "disk_gb": "0"}}


def vm(cls, status="RUNNING", created=NOON - 86400, name="vm"):
    return {"name": name, "status": status, "labels": {"gha-class": cls},
            "creationTimestamp": dt.datetime.fromtimestamp(created, dt.timezone.utc).isoformat()}


class AccrueSpendTests(unittest.TestCase):
    def test_running_vm_accrues_its_hourly_price_since_the_last_poll(self):
        state = {"spend_day": "2026-10-19", "spend_usd": 1.0, "spend_poll": NOON - 3600}
        self.assertAlmostEqual(P.accrue_spend(state, [vm("large")], NOON, PRICES), 2.0)
        self.assertEqual(state["spend_poll"], NOON)

    def test_charge_starts_no_earlier_than_creation(self):
        state = {"spend_day": "2026-10-19", "spend_usd": 0.0, "spend_poll": NOON - 7200}
        self.assertAlmostEqual(P.accrue_spend(state, [vm("small", created=NOON - 3600)], NOON, PRICES), 0.5)

    def test_stopped_vm_pays_only_for_its_disk(self):
        prices = {"small": {"hour": "1", "disk_gb": "730"}}
        with mock.patch.object(P, "DISK_USD_PER_GB_MONTH", "1"):
            state = {"spend_day": "2026-10-19", "spend_usd": 0.0, "spend_poll": NOON - 3600}
            self.assertAlmostEqual(P.accrue_spend(state, [vm("small", "TERMINATED")], NOON, prices), 1.0)

    def test_resets_at_utc_midnight_and_charges_only_today(self):
        state = {"spend_day": "2026-10-18", "spend_usd": 4.9, "spend_poll": NOON - 13 * 3600}
        # 12 h since midnight at $1/h; yesterday's 4.9 and the hour before midnight are gone.
        self.assertAlmostEqual(P.accrue_spend(state, [vm("large")], NOON, PRICES), 12.0)
        self.assertEqual(state["spend_day"], "2026-10-19")

    def test_fresh_state_charges_live_vms_from_midnight(self):
        self.assertAlmostEqual(P.accrue_spend({}, [vm("small")], NOON, PRICES), 6.0)

    def test_unpriced_class_or_bad_price_raises(self):
        with self.assertRaises(ValueError):
            P.accrue_spend({}, [vm("medium")], NOON, PRICES)
        with self.assertRaises(ValueError):
            P.accrue_spend({}, [vm("large")], NOON, {"large": {"hour": "nan", "disk_gb": "0"}})
        with self.assertRaises(ValueError):
            P.accrue_spend({}, [vm("large")], NOON, {"large": {"hour": "-1", "disk_gb": "0"}})


class SpendGateTests(unittest.TestCase):
    def test_below_cap_allows(self):
        state = {"spend_day": "2026-10-19", "spend_usd": 1.0, "spend_poll": NOON}
        self.assertIsNone(P.spend_gate(state, [], NOON))

    def test_at_cap_blocks(self):
        state = {"spend_day": "2026-10-19", "spend_usd": 5.0, "spend_poll": NOON}
        self.assertIn("daily cap", P.spend_gate(state, [], NOON))

    def test_fails_closed_when_the_estimate_is_unavailable(self):
        state = {"spend_day": "2026-10-19", "spend_usd": "garbage", "spend_poll": NOON}
        self.assertIn("unavailable", P.spend_gate(state, [], NOON))

    def test_cap_zero_disables(self):
        with mock.patch.object(P, "DAILY_USD_CAP", 0.0):
            self.assertIsNone(P.spend_gate({"spend_day": "2026-10-19", "spend_usd": 99}, [], NOON))


class PollEphemeralCapTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        mock.patch.object(P, "log", self.logs.append).start()
        mock.patch.object(P, "budget_exhausted", lambda: False).start()
        mock.patch.object(P, "pending_jobs", lambda classes: [("vogt#1", "small")]).start()
        mock.patch.object(P, "delete_vm", mock.Mock()).start()
        self.create = mock.patch.object(P, "create_vm", mock.Mock(return_value=True)).start()
        self.addCleanup(mock.patch.stopall)

    def run_poll(self, state, vms):
        with mock.patch.object(P, "ephemeral_vms", lambda: vms), mock.patch.object(P.time, "time", lambda: NOON):
            P.poll_ephemeral(state)

    def test_capped_poll_creates_nothing_and_leaves_running_vms(self):
        state = {"spend_day": "2026-10-19", "spend_usd": 5.0, "spend_poll": NOON}
        self.run_poll(state, [vm("small", created=NOON - 60)])
        self.create.assert_not_called()
        P.delete_vm.assert_not_called()
        self.assertEqual(sum("ALERT: daily cap" in m for m in self.logs), 1)
        self.run_poll(state, [vm("small", created=NOON - 60)])
        self.assertEqual(sum("ALERT: daily cap" in m for m in self.logs), 1)  # once per change

    def test_unavailable_estimate_creates_nothing(self):
        with mock.patch.object(P, "PRICES", {}):
            self.run_poll({}, [vm("small", created=NOON - 60)])
        self.create.assert_not_called()
        self.assertTrue(any("unavailable" in m for m in self.logs))

    def test_below_cap_creates(self):
        self.run_poll({"spend_day": "2026-10-19", "spend_usd": 0.0, "spend_poll": NOON}, [])
        self.create.assert_called_once_with("vogt#1", "small")


if __name__ == "__main__":
    unittest.main()
