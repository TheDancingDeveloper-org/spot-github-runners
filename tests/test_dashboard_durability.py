"""Durability tests for the control-VM dashboard: hydration, KV write-behind, freeze/import, idempotency,
container-friendly credentials, and the browser wait-and-retry wrapper. No network access."""

from __future__ import annotations

import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
import urllib.parse

from lib import billing_catalog, compute_api
from lib.dashboard_server import (
    IDEMPOTENCY_MAX_KEYS,
    request_fingerprint,
    DashboardStore,
    args_parser,
    create_dashboard_server,
)
from lib.dashboard_ui_assets import render_dashboard_html
from lib.edge_state import (
    KvError,
    KV_ORIGIN_KEY,
    KV_STATE_KEY,
    OriginRecord,
    build_snapshot,
    control_token,
    parse_snapshot,
    unavailable_payload,
)

try:
    from test_edge_state import FakeKvRequester, make_kv
except ModuleNotFoundError:
    from tests.test_edge_state import FakeKvRequester, make_kv

CONTROL_SECRET = "control-secret-for-tests-0123456789abcdef"
PRICING_STUB = {
    "source": "catalog_baseline",
    "fetched_at": 0.0,
    "component_rates": {},
    "machine_hourly": {},
    "disk_monthly_gib": {},
}


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def no_live_pricing(test: unittest.TestCase) -> None:
    patcher = mock.patch("lib.dashboard_server.fetch_live_spot_pricing", return_value=dict(PRICING_STUB))
    patcher.start()
    test.addCleanup(patcher.stop)


def write_state(path: Path, version: int, targets: list[str]) -> None:
    path.write_text(json.dumps({"state_version": version, "controlled_targets": targets}), encoding="utf-8")


def kv_snapshot(version: int, targets: list[str], slot: str = "a") -> str:
    return json.dumps(build_snapshot({"controlled_targets": targets, "project": "elsewhere"}, version, slot, now=1))


class HydrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.fake = FakeKvRequester()
        self.sleeps: list[float] = []

    def store(self, *, kv=True, slot="a") -> DashboardStore:
        return DashboardStore(self.path, control_slot=slot, kv=make_kv(self.fake) if kv else None,
                              sleep=self.sleeps.append)

    def test_starts_unhydrated_and_startup_writeback_keeps_version(self):
        write_state(self.path, 5, ["local/repo"])
        store = self.store()
        self.assertFalse(store.hydrated)
        self.assertEqual(store.state_version(), 5)
        self.assertEqual(json.loads(self.path.read_text())["state_version"], 5)

    def test_kv_newer_wins(self):
        write_state(self.path, 5, ["local/repo"])
        self.fake.values[KV_STATE_KEY] = kv_snapshot(7, ["kv/repo"])
        store = self.store()
        self.assertEqual(store.hydrate(), "kv")
        self.assertTrue(store.hydrated)
        self.assertEqual(store.get_controlled_targets(), ["kv/repo"])
        self.assertEqual(store.state_version(), 7)
        self.assertEqual(store.state["project"], store.project)  # normalized through the load path
        on_disk = json.loads(self.path.read_text())
        self.assertEqual((on_disk["state_version"], on_disk["controlled_targets"]), (7, ["kv/repo"]))

    def test_local_newer_or_tie_wins(self):
        for local_version, kv_version in ((7, 5), (5, 5)):
            write_state(self.path, local_version, ["local/repo"])
            self.fake.values[KV_STATE_KEY] = kv_snapshot(kv_version, ["kv/repo"])
            store = self.store()
            self.assertEqual(store.hydrate(), "local", (local_version, kv_version))
            self.assertEqual(store.get_controlled_targets(), ["local/repo"])
            self.assertEqual(store.state_version(), local_version)

    def test_kv_unreachable_uses_local_after_three_tries(self):
        write_state(self.path, 2, ["local/repo"])
        self.fake.fail = True
        store = self.store()
        self.assertEqual(store.hydrate(), "local")
        self.assertTrue(store.hydrated)
        self.assertEqual(self.fake.count("GET", KV_STATE_KEY), 3)
        self.assertEqual(len(self.sleeps), 2)
        self.assertAlmostEqual(sum(self.sleeps), 9.0)

    def test_no_local_file_takes_kv(self):
        self.fake.values[KV_STATE_KEY] = kv_snapshot(3, ["kv/repo"])
        store = self.store()
        self.assertEqual(store.hydrate(), "kv")
        self.assertEqual(store.state_version(), 3)

    def test_neither_gives_defaults(self):
        store = self.store()
        self.assertEqual(store.hydrate(), "default")
        self.assertTrue(store.hydrated)
        self.assertEqual(store.get_controlled_targets(), [])

    def test_malformed_kv_snapshot_ignored(self):
        write_state(self.path, 1, ["local/repo"])
        self.fake.values[KV_STATE_KEY] = '{"schema": "wrong"}'
        store = self.store()
        self.assertEqual(store.hydrate(), "local")

    def test_kv_without_slot_stays_local(self):
        self.fake.values[KV_STATE_KEY] = kv_snapshot(9, ["kv/repo"])
        store = self.store(slot=None)
        self.assertIsNone(store.kv)
        self.assertEqual(store.hydrate(), "default")
        self.assertEqual(self.fake.calls, [])

    def test_background_start_hydrates_from_kv(self):
        self.fake.values[KV_STATE_KEY] = kv_snapshot(4, ["kv/repo"])
        store = self.store()
        store.start()
        self.addCleanup(store.close)
        deadline = time.time() + 5
        while not store.hydrated and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(store.hydrated)
        self.assertEqual(store.get_controlled_targets(), ["kv/repo"])


class WriteBehindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.fake = FakeKvRequester()
        self.clock = FakeClock()
        self.store = DashboardStore(self.path, control_slot="a", kv=make_kv(self.fake), clock=self.clock,
                                    sleep=lambda _s: None)
        self.store.hydrate()

    def mutate(self, name: str = "o/r") -> None:
        self.store.set_controlled_targets([name])

    def written_version(self) -> int | None:
        raw = self.fake.values.get(KV_STATE_KEY)
        return parse_snapshot(raw)["state_version"] if raw else None

    def test_state_version_increments_on_every_persist(self):
        start = self.store.state_version()
        self.mutate("o/one")
        self.mutate("o/two")
        self.assertEqual(self.store.state_version(), start + 2)
        self.assertEqual(json.loads(self.path.read_text())["state_version"], start + 2)
        reloaded = DashboardStore(self.path)
        self.assertEqual(reloaded.state_version(), start + 2)

    def test_debounced_write_when_origin_unset(self):
        self.assertTrue(self.store.kv_writer)
        self.mutate()
        self.assertFalse(self.store.kv_tick())  # within the 1 s debounce
        self.clock.now += 1.1
        self.assertTrue(self.store.kv_tick())
        snap = parse_snapshot(self.fake.values[KV_STATE_KEY])
        self.assertEqual(snap["state_version"], self.store.state_version())
        self.assertEqual(snap["writer_slot"], "a")
        self.assertEqual(snap["state"]["controlled_targets"], ["o/r"])
        self.clock.now += 5
        self.assertFalse(self.store.kv_tick())  # nothing dirty

    def test_continuous_changes_flush_within_max_delay(self):
        self.mutate("o/first")
        for i in range(10):
            self.clock.now += 0.6
            self.mutate(f"o/n{i}")
            if self.store.kv_tick():
                break
        self.assertIsNotNone(self.written_version())

    def test_only_the_active_slot_writes(self):
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=1).to_json()
        self.clock.now += 31
        self.mutate()
        self.clock.now += 2
        self.assertFalse(self.store.kv_tick())
        self.assertFalse(self.store.kv_writer)
        self.assertNotIn(KV_STATE_KEY, self.fake.values)
        # Origin flips to this slot: picked up at the next 30 s recheck, then the pending change is written.
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("a", now=2).to_json()
        self.clock.now += 10
        self.assertFalse(self.store.kv_tick())
        self.clock.now += 25
        self.assertTrue(self.store.kv_tick())
        self.assertTrue(self.store.kv_writer)

    def test_malformed_origin_is_not_writer(self):
        self.fake.values[KV_ORIGIN_KEY] = "garbage"
        self.assertFalse(self.store.refresh_writer())

    def test_no_writes_while_frozen_or_unhydrated(self):
        self.mutate()
        self.clock.now += 2
        self.store.frozen = True
        self.assertFalse(self.store.kv_tick())
        self.store.frozen = False
        self.store.hydrated = False
        self.assertFalse(self.store.kv_tick())
        self.store.hydrated = True
        self.assertTrue(self.store.kv_tick())

    def test_failed_put_stays_dirty_and_backs_off(self):
        self.mutate()
        self.clock.now += 2
        self.fake.fail_puts = True
        self.assertFalse(self.store.kv_tick())
        self.fake.fail_puts = False
        self.clock.now += 1
        self.assertFalse(self.store.kv_tick())  # backing off
        self.clock.now += 5
        self.assertTrue(self.store.kv_tick())
        self.assertEqual(self.written_version(), self.store.state_version())

    def test_no_kv_config_is_local_only(self):
        store = DashboardStore(Path(self.tmp.name) / "other.json", control_slot="a")
        store.hydrate()
        store.set_controlled_targets(["o/r"])
        self.assertFalse(store.kv_tick())
        self.assertFalse(store.kv_writer)

    def test_freeze_flushes_and_import_rules(self):
        self.mutate("o/frozen")
        result = self.store.freeze()
        self.assertTrue(result["kv_flushed"])
        self.assertTrue(self.store.frozen)
        self.assertEqual(self.written_version(), result["state_version"])
        self.assertEqual(result["snapshot"]["state"]["controlled_targets"], ["o/frozen"])

        target = DashboardStore(Path(self.tmp.name) / "new.json", control_slot="b")
        self.assertFalse(target.hydrated)
        imported = target.import_snapshot(result["snapshot"])
        self.assertEqual(imported, {"state_version": result["state_version"], "imported": True})
        self.assertTrue(target.hydrated)
        self.assertEqual(target.get_controlled_targets(), ["o/frozen"])
        older = build_snapshot({"controlled_targets": ["o/old"]}, result["state_version"] - 1, "a")
        self.assertEqual(target.import_snapshot(older)["imported"], False)
        self.assertEqual(target.get_controlled_targets(), ["o/frozen"])
        equal = build_snapshot({"controlled_targets": ["o/equal"]}, result["state_version"], "a")
        self.assertTrue(target.import_snapshot(equal)["imported"])
        with self.assertRaises(ValueError):
            target.import_snapshot({"schema": "nope"})
        for bad_state in ({"instances": "not-a-list"}, {"profiles": []}, {"instances": [7]}):
            with self.assertRaises(ValueError, msg=bad_state):
                target.import_snapshot(build_snapshot(bad_state, 99, "a"))
        self.assertEqual(target.get_controlled_targets(), ["o/equal"])
        self.assertEqual(target.state_version(), result["state_version"])


class DurableFreezeTests(unittest.TestCase):
    """A freeze must survive a container restart until an explicit unfreeze or a successful import."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.marker = Path(self.tmp.name) / "state.json.frozen"
        self.fake = FakeKvRequester()

    def store(self) -> DashboardStore:
        store = DashboardStore(self.path, control_slot="a", kv=make_kv(self.fake), clock=FakeClock(),
                               sleep=lambda _s: None)
        store.hydrate()
        return store

    def test_restart_while_frozen_stays_frozen_until_unfreeze(self):
        from lib.dashboard_server import DashboardFrozenError

        first = self.store()
        first.set_controlled_targets(["o/before"])
        result = first.freeze()
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.marker.read_text())["state_version"], result["state_version"])
        self.assertEqual(list(Path(self.tmp.name).glob(".state.json.frozen.*.tmp")), [])

        restarted = self.store()  # container restart before cutover
        self.assertTrue(restarted.frozen)
        self.assertEqual((restarted.health()["status"], restarted.health()["frozen"]), ("frozen", True))
        with self.assertRaises(DashboardFrozenError):
            restarted.run_request("k" * 16, "POST", "/api/targets", lambda: (200, {}))
        self.assertEqual(restarted.state_version(), result["state_version"])  # no write was accepted
        self.assertEqual(restarted.get_controlled_targets(), ["o/before"])
        restarted._mark_kv_dirty()
        self.assertFalse(restarted.kv_tick())

        self.assertEqual(restarted.unfreeze(), {"frozen": False})
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.store().frozen)

    def test_import_clears_durable_freeze(self):
        first = self.store()
        frozen = first.freeze()
        restarted = self.store()
        self.assertTrue(restarted.frozen)
        newer = build_snapshot({"controlled_targets": ["o/newer"]}, frozen["state_version"] + 2, "b")
        self.assertTrue(restarted.import_snapshot(newer)["imported"])
        self.assertFalse(restarted.frozen)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.store().frozen)
        self.assertEqual(restarted.health()["status"], "ok")

    def test_failed_import_keeps_freeze(self):
        self.store().freeze()
        restarted = self.store()
        with self.assertRaises(ValueError):
            restarted.import_snapshot({"schema": "nope"})
        self.assertTrue(restarted.frozen)
        self.assertTrue(self.marker.exists())

    def test_marker_write_failure_fails_freeze_but_stays_frozen(self):
        store = self.store()
        with mock.patch.object(DashboardStore, "_write_freeze_marker", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.freeze()
        self.assertTrue(store.frozen)

    def test_marker_removal_failure_keeps_freeze(self):
        store = self.store()
        store.freeze()
        with mock.patch.object(Path, "unlink", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                store.unfreeze()
        self.assertTrue(store.frozen)
        self.assertTrue(self.marker.exists())

    def test_symlinked_marker_counts_as_frozen_and_is_not_followed(self):
        target = Path(self.tmp.name) / "elsewhere"
        self.marker.symlink_to(target)
        store = self.store()
        self.assertTrue(store.frozen)
        with self.assertRaises(OSError):
            store.freeze()
        self.assertFalse(target.exists())

    def test_in_memory_store_freezes_without_marker(self):
        store = DashboardStore(None, control_slot="a")
        store.hydrate()
        store.freeze()
        self.assertTrue(store.frozen)
        self.assertEqual(store.unfreeze(), {"frozen": False})


class HydrationSafetyTests(unittest.TestCase):
    """A fresh VM never serves or publishes version-0 defaults over newer KV state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.fake = FakeKvRequester()
        self.clock = FakeClock()

    def store(self) -> DashboardStore:
        store = DashboardStore(self.path, control_slot="b", kv=make_kv(self.fake), clock=self.clock,
                               sleep=lambda _s: None)
        store.hydrate_retry_delays = (0.0, 0.0)
        return store

    def test_fresh_vm_with_kv_unreachable_stays_unhydrated(self):
        self.fake.fail = True
        store = self.store()
        self.assertFalse(self.path.exists())  # no version-0 defaults on disk before hydration
        self.assertEqual(store.hydrate(), "unavailable")
        self.assertFalse(store.hydrated)
        self.assertEqual(store.health()["status"], "starting")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.store().loaded_from_file)  # a restart does not mistake defaults for state
        self.fake.fail = False
        self.fake.values[KV_STATE_KEY] = kv_snapshot(9, ["kv/repo"])
        self.assertEqual(store.hydrate(), "kv")
        self.assertEqual((store.hydrated, store.state_version()), (True, 9))

    def test_kv_reporting_no_snapshot_uses_defaults(self):
        store = self.store()
        self.assertEqual(store.hydrate(), "default")
        self.assertTrue(store.hydrated)

    def test_background_hydration_retries_until_kv_returns(self):
        self.fake.fail = True
        store = self.store()
        with mock.patch("lib.dashboard_server.HYDRATE_BACKOFF_INITIAL_SECONDS", 0.01), \
                mock.patch("lib.dashboard_server.HYDRATE_BACKOFF_MAX_SECONDS", 0.02):
            store.start()
            self.addCleanup(store.close)
            time.sleep(0.1)
            self.assertFalse(store.hydrated)
            with self.fake.lock:
                self.fake.values[KV_STATE_KEY] = kv_snapshot(9, ["kv/repo"])
                self.fake.fail = False
            deadline = time.time() + 5
            while not store.hydrated and time.time() < deadline:
                time.sleep(0.01)
        self.assertTrue(store.hydrated)
        self.assertEqual(store.state_version(), 9)

    def test_import_during_pending_hydration_is_kept(self):
        self.fake.fail = True
        store = self.store()
        store.hydrate()
        store.import_snapshot(build_snapshot({"controlled_targets": ["o/imported"]}, 5, "a"))
        self.fake.fail = False
        self.fake.values[KV_STATE_KEY] = kv_snapshot(3, ["kv/older"])
        self.assertEqual(store.hydrate(), "import")
        self.assertEqual(store.get_controlled_targets(), ["o/imported"])

    def test_new_writer_adopts_newer_kv_snapshot_instead_of_overwriting(self):
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("a", now=1).to_json()
        store = self.store()  # slot b, not the writer
        self.assertEqual(store.hydrate(), "default")
        store.set_controlled_targets(["local/change"])
        self.fake.values[KV_STATE_KEY] = kv_snapshot(9, ["kv/repo"])  # slot a kept writing
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=2).to_json()  # b becomes writer
        self.clock.now += 60
        self.assertFalse(store.kv_tick())
        self.assertEqual(self.fake.count("PUT", KV_STATE_KEY), 0)
        self.assertEqual((store.state_version(), store.get_controlled_targets()), (9, ["kv/repo"]))

    def test_first_change_after_cutover_catches_up_with_kv(self):
        from lib.dashboard_server import DashboardFrozenError

        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("a", now=1).to_json()
        self.fake.values[KV_STATE_KEY] = kv_snapshot(7, ["kv/v7"])
        store = self.store()  # slot b hydrated at v7 while a was active
        store.hydrate()
        self.fake.values[KV_STATE_KEY] = kv_snapshot(8, ["kv/v8"])  # a wrote v8 before it died
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=2).to_json()  # cutover to b
        real_get = store.kv.get

        def snapshot_unreadable(key):
            if key == KV_STATE_KEY:
                raise KvError("snapshot read failed")
            return real_get(key)

        with mock.patch.object(store.kv, "get", side_effect=snapshot_unreadable):
            with self.assertRaises(DashboardFrozenError):  # new writer cannot confirm KV: refuse the change
                store.run_request("k" * 16, "POST", "/api/targets",
                                  lambda: (200, {"targets": store.set_controlled_targets(["o/new"])}))
        self.assertEqual(store.get_controlled_targets(), ["kv/v7"])
        status, _body, _ = store.run_request(
            "k" * 16, "POST", "/api/targets", lambda: (200, {"targets": store.set_controlled_targets(
                store.get_controlled_targets() + ["o/new"])}))
        self.assertEqual(status, 200)
        self.assertEqual(store.get_controlled_targets(), ["kv/v8", "o/new"])
        self.assertGreater(store.state_version(), 8)

    def test_never_writes_older_than_last_written(self):
        store = self.store()
        store.hydrate()
        store.set_controlled_targets(["o/r"])
        store.kv_last_written_version = store.state_version() + 5
        self.clock.now += 10
        with self.assertLogs("spot_dashboard", level="ERROR"):
            self.assertFalse(store.kv_tick())
        self.assertEqual(self.fake.count("PUT", KV_STATE_KEY), 0)


class ReconcileAfterLocalFallbackTests(unittest.TestCase):
    """Starting from the local file while KV is unreachable never lets local history overwrite KV."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.fake = FakeKvRequester()
        self.clock = FakeClock()
        write_state(self.path, 5, ["local/repo"])
        self.fake.fail = True
        self.store = DashboardStore(self.path, control_slot="a", kv=make_kv(self.fake), clock=self.clock,
                                    sleep=lambda _s: None)
        self.store.hydrate_retry_delays = (0.0, 0.0)
        self.assertEqual(self.store.hydrate(), "local")

    def test_unreconciled_start_is_read_only_and_not_healthy(self):
        from lib.dashboard_server import DashboardFrozenError

        store = self.store
        self.assertTrue(store.hydrated)
        self.assertFalse(store.reconciled)
        health = store.health()
        self.assertEqual((health["status"], health["reconciled"]), ("starting", False))
        with self.assertRaises(DashboardFrozenError):
            store.run_request("k" * 16, "POST", "/api/targets", lambda: (200, {}))
        self.clock.now += 100
        self.assertFalse(store.kv_tick())  # KV still down: stays unreconciled, nothing written
        self.assertFalse(store.reconciled)

    def test_newer_or_equal_kv_snapshot_is_imported(self):
        for kv_version in (9, 5):
            with self.subTest(kv_version=kv_version):
                self.setUp()
                self.fake.fail = False
                self.fake.values[KV_STATE_KEY] = kv_snapshot(kv_version, ["kv/repo"])
                self.clock.now += 100
                self.store.kv_tick()
                self.assertTrue(self.store.reconciled)
                self.assertEqual(self.store.get_controlled_targets(), ["kv/repo"])
                self.assertEqual(self.store.state_version(), kv_version)
                self.assertEqual(self.store.health()["status"], "ok")
                self.assertEqual(self.fake.count("PUT", KV_STATE_KEY), 0)

    def test_local_counter_growth_never_authorizes_overwrite(self):
        # Internal persists while unreconciled (not user mutations) push the local counter past KV.
        for _ in range(6):
            self.store._persist()
        self.assertEqual(self.store.state_version(), 11)
        self.fake.fail = False
        self.fake.values[KV_STATE_KEY] = kv_snapshot(9, ["kv/repo"])
        self.clock.now += 100
        self.store.kv_tick()
        self.assertTrue(self.store.reconciled)
        self.assertEqual(self.store.get_controlled_targets(), ["kv/repo"])
        self.assertEqual(parse_snapshot(self.fake.values[KV_STATE_KEY])["state_version"], 9)

    def test_older_or_absent_kv_gets_local_state_published(self):
        self.fake.fail = False
        self.fake.values[KV_STATE_KEY] = kv_snapshot(3, ["kv/old"])
        self.clock.now += 100
        self.store.kv_tick()
        self.assertTrue(self.store.reconciled)
        self.assertEqual(self.store.get_controlled_targets(), ["local/repo"])
        written = parse_snapshot(self.fake.values[KV_STATE_KEY])
        self.assertEqual((written["state_version"], written["state"]["controlled_targets"]), (5, ["local/repo"]))

    def test_backoff_between_reconcile_attempts(self):
        self.clock.now += 100
        self.store.kv_tick()
        gets = self.fake.count("GET", KV_STATE_KEY)
        self.store.kv_tick()  # inside the backoff window: no new KV read
        self.assertEqual(self.fake.count("GET", KV_STATE_KEY), gets)

    def test_control_import_reconciles(self):
        self.store.import_snapshot(build_snapshot({"controlled_targets": ["o/imported"]}, 7, "b"))
        self.assertTrue(self.store.reconciled)
        self.assertEqual(self.store.health()["status"], "ok")


class ImportPersistFailureTests(unittest.TestCase):
    def test_failed_local_write_fails_import_and_keeps_prior_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = DashboardStore(Path(tmp.name) / "state.json", control_slot="b")
        store.set_controlled_targets(["o/prior"])
        before = store.state_version()
        envelope = build_snapshot({"controlled_targets": ["o/imported"]}, before + 5, "a")
        with mock.patch.object(DashboardStore, "_persist", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.import_snapshot(envelope)
        self.assertFalse(store.hydrated)
        self.assertEqual(store.get_controlled_targets(), ["o/prior"])
        self.assertEqual(store.state_version(), before)


class SerializedWriteRecheckTests(unittest.TestCase):
    """A background write that waited for the write lock re-checks everything before its put."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeKvRequester()
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("a", now=1).to_json()
        self.clock = FakeClock()
        self.store = DashboardStore(Path(self.tmp.name) / "state.json", control_slot="a", kv=make_kv(self.fake),
                                    clock=self.clock, sleep=lambda _s: None)
        self.store.hydrate()
        self.store.set_controlled_targets(["o/a"])
        self.clock.now += 10
        self.assertTrue(self.store.kv_tick())  # an earlier successful write as the writer
        self.puts = self.fake.count("PUT", KV_STATE_KEY)
        self.published = self.fake.values[KV_STATE_KEY]
        self.store.set_controlled_targets(["o/a2"])
        self.clock.now += 10

    def run_paused_tick(self, while_paused):
        """Start kv_tick while the write lock is held, change the world, then let the tick continue."""
        self.store._kv_write_lock.acquire()
        result: list[bool] = []
        worker = threading.Thread(target=lambda: result.append(self.store.kv_tick()))
        worker.start()
        time.sleep(0.1)  # the tick passed its unlocked checks and now waits for the lock
        while_paused()
        self.store._kv_write_lock.release()
        worker.join(5)
        return result[0]

    def assert_no_new_put(self):
        self.assertEqual(self.fake.count("PUT", KV_STATE_KEY), self.puts)

    def test_freeze_while_waiting_aborts_the_write(self):
        self.assertFalse(self.run_paused_tick(lambda: setattr(self.store, "frozen", True)))
        self.assert_no_new_put()
        self.assertEqual(self.fake.values[KV_STATE_KEY], self.published)

    def test_lost_writer_ownership_aborts_the_write(self):
        def hand_origin_to_b():
            self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=2).to_json()

        self.assertFalse(self.run_paused_tick(hand_origin_to_b))
        self.assert_no_new_put()
        self.assertFalse(self.store.kv_writer)

    def test_newer_kv_snapshot_aborts_the_write_even_after_earlier_writes(self):
        # Repro shape: the replacement published a newer version; the old slot must not move KV back.
        def replacement_published():
            self.fake.values[KV_STATE_KEY] = kv_snapshot(self.store.state_version() + 1, ["o/b"], slot="b")

        with self.assertLogs("spot_dashboard", level="ERROR"):
            self.assertFalse(self.run_paused_tick(replacement_published))
        self.assert_no_new_put()
        kept = parse_snapshot(self.fake.values[KV_STATE_KEY])
        self.assertEqual((kept["writer_slot"], kept["state"]["controlled_targets"]), ("b", ["o/b"]))


class LocalAheadOfKvTests(unittest.TestCase):
    def test_newer_local_file_is_published_after_hydration(self):
        for kv_value in (kv_snapshot(3, ["kv/old"]), None):
            with self.subTest(kv=bool(kv_value)):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                path = Path(tmp.name) / "state.json"
                write_state(path, 5, ["local/saved"])
                fake = FakeKvRequester()
                if kv_value:
                    fake.values[KV_STATE_KEY] = kv_value
                clock = FakeClock()
                store = DashboardStore(path, control_slot="a", kv=make_kv(fake), clock=clock, sleep=lambda _s: None)
                self.assertEqual(store.hydrate(), "local")
                clock.now += 10
                self.assertTrue(store.kv_tick())
                written = parse_snapshot(fake.values[KV_STATE_KEY])
                self.assertEqual((written["state_version"], written["state"]["controlled_targets"]),
                                 (5, ["local/saved"]))

    def test_equal_versions_do_not_republish(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "state.json"
        write_state(path, 5, ["local/saved"])
        fake = FakeKvRequester()
        fake.values[KV_STATE_KEY] = kv_snapshot(5, ["local/saved"])
        clock = FakeClock()
        store = DashboardStore(path, control_slot="a", kv=make_kv(fake), clock=clock, sleep=lambda _s: None)
        store.hydrate()
        clock.now += 10
        self.assertFalse(store.kv_tick())
        self.assertEqual(fake.count("PUT", KV_STATE_KEY), 0)


class IdempotencyRecordIsolationTests(unittest.TestCase):
    def test_stored_response_is_not_changed_by_later_mutations(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "state.json"
        store = DashboardStore(path)
        store.hydrate()
        live = {"min_runners": 2}
        store.state["shared"] = live

        def set_min(value):
            def fn():
                live["min_runners"] = value
                store._persist()
                return 200, {"policy": live}  # the response shares an object with live state
            return fn

        self.assertEqual(store.run_request("key-first-0001", "POST", "/api/scaling", set_min(2))[1],
                         {"policy": {"min_runners": 2}})
        store.run_request("key-second-002", "POST", "/api/scaling", set_min(3))
        status, body, replayed = store.run_request("key-first-0001", "POST", "/api/scaling", set_min(99))
        self.assertEqual((status, body, replayed), (200, {"policy": {"min_runners": 2}}, True))
        body["policy"]["min_runners"] = 42  # mutating a replayed body does not touch the record
        self.assertEqual(store.run_request("key-first-0001", "POST", "/api/scaling", set_min(99))[1],
                         {"policy": {"min_runners": 2}})
        on_disk = json.loads(path.read_text())["idempotency"]["key-first-0001"]["b"]
        self.assertEqual(on_disk, {"policy": {"min_runners": 2}})


class BlockingKvRequester(FakeKvRequester):
    """Blocks the first snapshot PUT until released, to model a slow in-flight background write."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocked = False

    def __call__(self, method, url, headers, body, timeout):
        key = urllib.parse.unquote(url.rsplit("/values/", 1)[1])
        if method == "PUT" and key == KV_STATE_KEY:
            if not self.blocked:
                self.blocked = True
                self.entered.set()
                self.release.wait(5)
        return super().__call__(method, url, headers, body, timeout)


class KvWriteOrderingTests(unittest.TestCase):
    def test_freeze_waits_for_inflight_background_write(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = BlockingKvRequester()
        clock = FakeClock()
        store = DashboardStore(Path(tmp.name) / "state.json", control_slot="a", kv=make_kv(fake), clock=clock,
                               sleep=lambda _s: None)
        store.hydrate()
        store.set_controlled_targets(["o/first"])
        clock.now += 10
        background = threading.Thread(target=store.kv_tick)
        background.start()
        self.assertTrue(fake.entered.wait(5))  # background captured version N and is mid-PUT
        store.set_controlled_targets(["o/second"])  # version N+1
        newest = store.state_version()
        results: list[dict] = []
        freezer = threading.Thread(target=lambda: results.append(store.freeze()))
        freezer.start()
        time.sleep(0.1)
        self.assertTrue(freezer.is_alive())  # freeze waits for the in-flight write
        fake.release.set()
        background.join(5)
        freezer.join(5)
        self.assertTrue(results[0]["kv_flushed"])
        written = parse_snapshot(fake.values[KV_STATE_KEY])
        self.assertEqual(written["state_version"], newest)
        self.assertEqual(written["state"]["controlled_targets"], ["o/second"])
        self.assertEqual(store.kv_last_written_version, newest)


class ServerHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        no_live_pricing(self)

    def start(self, **kwargs):
        kwargs.setdefault("state_path", self.path)
        server, store = create_dashboard_server(host="127.0.0.1", port=0, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        self.port = server.server_address[1]
        return store

    def request(self, method, path, *, headers=None, body=None, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read()
            try:
                parsed = json.loads(payload) if payload else None
            except ValueError:
                parsed = payload.decode("utf-8", "replace")
            return resp.status, resp.msg, parsed
        finally:
            conn.close()

    def assert_wait_response(self, status, msg, body):
        self.assertEqual(status, 503)
        self.assertEqual(msg["Retry-After"], "10")
        self.assertEqual(msg["Cache-Control"], "no-store")
        self.assertTrue(msg["Content-Type"].startswith("application/json"))
        for key, value in unavailable_payload().items():
            self.assertEqual(body[key], value)


class HealthAndGateTests(ServerHarness):
    def test_health_ok(self):
        self.start()
        status, msg, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"status", "hydrated", "frozen", "reconciled", "active", "ownership",
                                     "state_version", "slot", "kv_writer"})
        self.assertTrue(body["active"])
        self.assertTrue(body["reconciled"])
        self.assertEqual((body["status"], body["hydrated"], body["frozen"], body["slot"], body["kv_writer"]),
                         ("ok", True, False, None, False))

    def test_not_hydrated_gate(self):
        store = self.start(control_slot="a", control_secret=CONTROL_SECRET)
        store.hydrated = False
        for method, path, body in (("GET", "/api/overview", None), ("GET", "/", None),
                                   ("POST", "/api/budget", {}), ("PUT", "/api/budget", None)):
            status, msg, payload = self.request(method, path, body=body)
            self.assert_wait_response(status, msg, payload)
            self.assertEqual(set(payload), set(unavailable_payload()), path)
        status, msg, payload = self.request("GET", "/api/health")
        self.assert_wait_response(status, msg, payload)
        self.assertEqual((payload["status"], payload["hydrated"], payload["slot"]), ("starting", False, "a"))
        token = control_token(CONTROL_SECRET, "snapshot", "a")
        status, _, payload = self.request("GET", "/api/control/snapshot", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(status, 200)

    def test_frozen_gate(self):
        store = self.start()
        store.frozen = True
        status, msg, payload = self.request("POST", "/api/budget", body={"max_hourly_usd": 1})
        self.assert_wait_response(status, msg, payload)
        status, msg, payload = self.request("DELETE", "/api/budget")
        self.assert_wait_response(status, msg, payload)
        self.assertEqual(self.request("GET", "/api/overview")[0], 200)
        status, msg, payload = self.request("GET", "/api/health")
        self.assert_wait_response(status, msg, payload)
        self.assertEqual((payload["status"], payload["frozen"], payload["hydrated"]), ("frozen", True, True))

    def test_frozen_rechecked_inside_store_lock(self):
        store = self.start()
        store.frozen = True
        from lib.dashboard_server import DashboardFrozenError

        with self.assertRaises(DashboardFrozenError):
            store.run_request(None, "POST", "/api/budget", lambda: (200, {}))
        self.assertEqual(store.run_request(None, "POST", "/api/qualify", lambda: (200, {"x": 1}), mutating=False),
                         (200, {"x": 1}, False))

    def test_background_hydration_from_kv_through_server(self):
        fake = FakeKvRequester()
        fake.values[KV_STATE_KEY] = kv_snapshot(6, ["kv/repo"])
        store = self.start(control_slot="b", kv=make_kv(fake))
        deadline = time.time() + 5
        status = 0
        while time.time() < deadline:
            status, _, body = self.request("GET", "/api/health")
            if status == 200:
                break
            time.sleep(0.02)
        self.assertEqual(status, 200)
        self.assertEqual((body["state_version"], body["slot"], body["kv_writer"]), (6, "b", True))
        self.assertEqual(store.get_controlled_targets(), ["kv/repo"])


class InactiveSlotTests(unittest.TestCase):
    """A slot that origin:active does not name keeps serving reads but refuses browser changes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeKvRequester()
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=1).to_json()
        self.store = DashboardStore(Path(self.tmp.name) / "state.json", control_slot="a", kv=make_kv(self.fake),
                                    clock=FakeClock(), sleep=lambda _s: None)
        self.store.hydrate()

    def change(self):
        return self.store.run_request("k" * 16, "POST", "/api/targets",
                                      lambda: (200, {"t": self.store.set_controlled_targets(["o/x"])}))

    def test_known_inactive_slot_rejects_changes_but_reports_healthy(self):
        from lib.dashboard_server import DashboardFrozenError

        self.assertTrue(self.store.known_inactive)
        health = self.store.health()
        self.assertEqual((health["status"], health["active"]), ("ok", False))  # controller probes still pass
        with self.assertRaises(DashboardFrozenError):
            self.change()
        self.assertEqual(self.store.get_controlled_targets(), [])

    def test_cutover_to_this_slot_is_seen_on_the_next_change(self):
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("a", now=2).to_json()
        self.assertEqual(self.change()[0], 200)
        self.assertFalse(self.store.known_inactive)
        self.assertTrue(self.store.health()["active"])

    def test_unset_or_malformed_origin_is_not_known_inactive(self):
        for raw in (None, "garbage"):
            if raw is None:
                self.fake.values.pop(KV_ORIGIN_KEY, None)
            else:
                self.fake.values[KV_ORIGIN_KEY] = raw
            self.store.refresh_writer()
            self.assertFalse(self.store.known_inactive, raw)

    def test_unreachable_kv_keeps_last_successful_reading(self):
        from lib.dashboard_server import DashboardFrozenError

        self.fake.fail = True
        with self.assertRaises(DashboardFrozenError):
            self.change()


class OwnershipTriStateTests(unittest.TestCase):
    """P1-a: with KV configured, a VM must positively confirm it owns origin:active before any change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeKvRequester()
        self.path = Path(self.tmp.name) / "state.json"

    def store(self):
        return DashboardStore(self.path, control_slot="a", kv=make_kv(self.fake), clock=FakeClock(),
                              sleep=lambda _s: None)

    def change(self, store):
        return store.run_request("k" * 16, "POST", "/api/targets",
                                 lambda: (200, {"t": store.set_controlled_targets(["o/x"])}))

    def test_origin_reads_failing_after_hydration_reject_changes(self):
        from lib.dashboard_server import DashboardFrozenError

        self.fake.values[KV_STATE_KEY] = kv_snapshot(3, ["kv/repo"])
        store = self.store()
        real = self.fake.__call__

        def origin_down(method, url, headers, body, timeout):
            if url.endswith("/values/" + urllib.parse.quote(KV_ORIGIN_KEY, safe="")):
                raise KvError("origin read failed")
            return real(method, url, headers, body, timeout)

        store.kv.requester = origin_down
        self.assertEqual(store.hydrate(), "kv")
        self.assertEqual((store.ownership, store.known_inactive, store.kv_writer), ("unknown", False, False))
        self.assertEqual(store.health()["ownership"], "unknown")
        with self.assertRaises(DashboardFrozenError):
            self.change(store)
        self.assertEqual(store.get_controlled_targets(), ["kv/repo"])
        self.assertEqual(store.state_version(), 3)
        store.kv.requester = real  # origin:active readable again (unset: bootstrap, this VM may write)
        self.assertEqual(self.change(store)[0], 200)
        self.assertEqual(store.ownership, "active")

    def test_malformed_origin_record_is_unknown_not_active(self):
        from lib.dashboard_server import DashboardFrozenError

        self.fake.values[KV_ORIGIN_KEY] = "garbage"
        store = self.store()
        store.hydrate()
        self.assertEqual(store.ownership, "unknown")
        with self.assertRaises(DashboardFrozenError):
            self.change(store)
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=1).to_json()
        store.refresh_writer()
        self.assertEqual(store.ownership, "inactive")
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("a", now=2).to_json()
        self.assertEqual(self.change(store)[0], 200)
        self.assertEqual(store.ownership, "active")


class FreshOwnershipTests(unittest.TestCase):
    """Codex round 10 P1-a: every mutation re-reads origin:active; stale writer flags never authorize a change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeKvRequester()
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("a", now=1).to_json()
        self.store = DashboardStore(Path(self.tmp.name) / "state.json", control_slot="a", kv=make_kv(self.fake),
                                    clock=FakeClock(), sleep=lambda _s: None)
        self.store.hydrate()
        self.assertEqual(self.change("o/first")[0], 200)
        self.assertTrue(self.store.kv_writer and self.store._writer_synced)  # the stale state of an old slot

    def change(self, target):
        return self.store.run_request(None, "POST", "/api/targets",
                                      lambda: (200, {"t": self.store.set_controlled_targets([target])}))

    def origin_reads(self):
        return [c for c in self.fake.calls if c == ("GET", KV_ORIGIN_KEY)]

    def test_cutover_to_the_other_slot_is_seen_by_the_next_mutation(self):
        from lib.dashboard_server import DashboardFrozenError

        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=2).to_json()  # recovery cut over to B
        with self.assertRaises(DashboardFrozenError):
            self.change("o/lost")
        self.assertEqual(self.store.get_controlled_targets(), ["o/first"])  # never acknowledged
        self.assertTrue(self.store.known_inactive)
        self.assertEqual(self.store.ownership, "inactive")

    def test_unreachable_kv_refuses_instead_of_trusting_old_flags(self):
        from lib.dashboard_server import DashboardFrozenError

        self.fake.fail = True
        with self.assertRaises(DashboardFrozenError):
            self.change("o/lost")
        with self.assertRaises(DashboardFrozenError):
            self.store.run_external("ext-key-0001", "POST", "/api/patches/save", lambda: (200, {"ran": True}))
        self.fake.fail = False
        self.assertEqual(self.store.get_controlled_targets(), ["o/first"])

    def test_malformed_origin_is_unknown_not_unowned(self):
        from lib.dashboard_server import DashboardFrozenError

        for raw in ("not json", json.dumps({"slot": "z"}), json.dumps(["a"])):
            self.fake.values[KV_ORIGIN_KEY] = raw
            with self.subTest(raw=raw), self.assertRaises(DashboardFrozenError):
                self.change("o/lost")
            self.assertEqual((self.store.ownership, self.store.known_inactive, self.store.kv_writer),
                             ("unknown", False, False))
        # A missing key is the only "no owner" case (bootstrap), and the change then goes through.
        self.fake.values.pop(KV_ORIGIN_KEY)
        self.assertEqual(self.change("o/bootstrap")[0], 200)

    def test_each_mutation_reads_origin_again(self):
        before = len(self.origin_reads())
        self.change("o/second")
        self.change("o/third")
        self.assertGreaterEqual(len(self.origin_reads()) - before, 2)


class PublishSafetyTests(unittest.TestCase):
    """Codex round 11: publish requires fresh ownership, never overwrites a different snapshot at the same or a
    newer version, and verifies the write by reading it back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeKvRequester()

    def store(self, slot="a", name="a.json"):
        store = DashboardStore(Path(self.tmp.name) / name, control_slot=slot, kv=make_kv(self.fake),
                               clock=FakeClock(), sleep=lambda _s: None)
        store.hydrate()
        return store

    def puts(self):
        return [c for c in self.fake.calls if c == ("PUT", KV_STATE_KEY)]

    def test_publish_needs_fresh_ownership_and_its_own_writer_slot(self):
        store = self.store()
        store.set_controlled_targets(["o/a"])
        for raw in (OriginRecord.for_slot("b", now=2).to_json(), "malformed"):
            self.fake.values[KV_ORIGIN_KEY] = raw
            with self.subTest(origin=raw):
                self.assertIsNone(store._publish_snapshot())
                self.assertEqual(self.puts(), [])
                self.assertNotEqual(store.ownership, "active")
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("a", now=3).to_json()
        envelope = store.snapshot_envelope()
        envelope["writer_slot"] = "b"  # a snapshot claiming another writer is never published
        with self.assertLogs("spot_dashboard", "ERROR"):
            self.assertIsNone(store._publish_snapshot(envelope))
        self.assertEqual(self.puts(), [])
        self.assertEqual(store._publish_snapshot(), store.state_version())
        self.assertEqual(len(self.puts()), 1)

    def test_two_stores_at_the_same_version_never_overwrite_each_other(self):
        self.fake.values[KV_STATE_KEY] = kv_snapshot(7, ["base/repo"])
        a, b = self.store("a", "a.json"), self.store("b", "b.json")
        self.assertEqual((a.state_version(), b.state_version()), (7, 7))
        b.set_controlled_targets(["b/policy"])  # both now hold different content at version 8
        a.set_controlled_targets(["a/policy"])
        # Ownership briefly looks unset to both (the window the VM fence closes): b publishes first.
        self.assertEqual(b._publish_snapshot(), 8)
        with self.assertLogs("spot_dashboard", "WARNING"):
            self.assertIsNone(a._publish_snapshot())
        stored = parse_snapshot(self.fake.values[KV_STATE_KEY])
        self.assertEqual((stored["writer_slot"], stored["state"]["controlled_targets"]), ("b", ["b/policy"]))
        self.assertEqual(len(self.puts()), 1)

    def test_identical_content_at_the_same_version_is_an_idempotent_republish(self):
        store = self.store()
        store.set_controlled_targets(["o/same"])
        envelope = store.snapshot_envelope()
        self.assertEqual(store._publish_snapshot(envelope), store.state_version())
        store.kv_last_written_version = None  # for example a restarted process republishing what it wrote
        envelope = {**envelope, "written_at": envelope["written_at"] + 60,
                    "state": {**envelope["state"], "updated_at": "2099-01-01T00:00:00Z"}}
        puts = len(self.puts())
        self.assertEqual(store._publish_snapshot(envelope), store.state_version())
        self.assertEqual(len(self.puts()), puts)  # nothing rewritten

    def test_handover_target_never_overwrites_the_snapshot_it_imported(self):
        old = self.store("a", "a.json")
        old.set_controlled_targets(["o/handover"])
        self.assertEqual(old._publish_snapshot(), old.state_version())
        new = DashboardStore(Path(self.tmp.name) / "b.json", control_slot="b", kv=make_kv(self.fake),
                             clock=FakeClock(), sleep=lambda _s: None)
        new.import_snapshot(old.snapshot_envelope())
        self.fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=5).to_json()
        puts = len(self.puts())
        with self.assertLogs("spot_dashboard", "WARNING"):
            new._publish_snapshot()  # equal version: KV already holds that history, so nothing is rewritten
        self.assertEqual(len(self.puts()), puts)
        self.assertEqual(parse_snapshot(self.fake.values[KV_STATE_KEY])["writer_slot"], "a")
        new.set_controlled_targets(["o/handover", "o/next"])  # the first new change publishes normally
        self.assertEqual(new._publish_snapshot(), old.state_version() + 1)
        self.assertEqual(parse_snapshot(self.fake.values[KV_STATE_KEY])["writer_slot"], "b")

    def test_read_back_mismatch_fences_the_store_until_kv_shows_our_write(self):
        from lib.dashboard_server import DashboardFrozenError

        store = self.store()
        store.set_controlled_targets(["o/ours"])
        real = self.fake.__call__
        intruder = kv_snapshot(store.state_version(), ["o/theirs"], slot="b")

        def overwritten(method, url, headers, body, timeout):
            result = real(method, url, headers, body, timeout)
            if method == "PUT":
                self.fake.values[KV_STATE_KEY] = intruder  # another writer landed right after ours
            return result

        store.kv.requester = overwritten
        with self.assertLogs("spot_dashboard", "ERROR") as logs:
            self.assertIsNone(store._publish_snapshot())
        self.assertTrue(any("PUBLISH CONFLICT" in line for line in logs.output))
        self.assertFalse(store.kv_writer)
        health = store.health()
        self.assertEqual(health["status"], "state_conflict")
        self.assertIn("slot b", health["reason"])
        with self.assertRaises(DashboardFrozenError):  # changes wait while fenced
            store.run_request(None, "POST", "/api/targets",
                              lambda: (200, {"t": store.set_controlled_targets(["o/next"])}))
        store.kv.requester = real
        self.assertFalse(store.kv_tick())  # still fenced: KV does not show our write
        self.assertTrue(store.publish_conflict)
        ours = store.snapshot_envelope()
        self.fake.values[KV_STATE_KEY] = json.dumps(ours)  # e.g. eventual consistency caught up
        with self.assertLogs("spot_dashboard", "WARNING"):
            self.assertEqual(store.run_request(None, "POST", "/api/targets",
                                               lambda: (200, {"t": store.set_controlled_targets(["o/next"])}))[0], 200)
        self.assertEqual(store.publish_conflict, "")
        self.assertEqual(store.health()["status"], "ok")

    def test_unreadable_read_back_also_fences(self):
        store = self.store()
        store.set_controlled_targets(["o/ours"])
        real = self.fake.__call__
        state = {"after_put": False}

        def flaky(method, url, headers, body, timeout):
            if method == "GET" and state["after_put"] and url.endswith("/values/" + urllib.parse.quote(KV_STATE_KEY, safe="")):
                raise KvError("read-back failed")
            result = real(method, url, headers, body, timeout)
            if method == "PUT":
                state["after_put"] = True
            return result

        store.kv.requester = flaky
        with self.assertLogs("spot_dashboard", "WARNING"):
            self.assertIsNone(store._publish_snapshot())
        self.assertIn("unreadable", store.publish_conflict)


class StrictSnapshotClassificationTests(unittest.TestCase):
    """P1-b: an existing KV snapshot that fails verification is never treated as "no snapshot"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeKvRequester()
        self.path = Path(self.tmp.name) / "state.json"

    def store(self):
        return DashboardStore(self.path, control_slot="a", kv=make_kv(self.fake), clock=FakeClock(),
                              sleep=lambda _s: None)

    BAD = {
        "malformed": "{not json",
        "wrong_schema": json.dumps({"schema": "other", "state_version": 4, "state": {}}),
        "invalid_state": json.dumps(build_snapshot({"controlled_targets": "not-a-list"}, 4, "a", now=1)),
    }

    def test_new_vm_never_starts_from_defaults_over_an_invalid_snapshot(self):
        for name, raw in self.BAD.items():
            self.fake.values[KV_STATE_KEY] = raw
            store = self.store()
            with self.subTest(name):
                self.assertEqual(store.hydrate(), "unavailable")
                self.assertFalse(store.hydrated)
                health = store.health()
                self.assertEqual(health["status"], "state_unavailable")
                self.assertIn("refusing to start from defaults", health["reason"])
                self.assertFalse(self.path.exists())
                self.assertEqual(self.fake.values[KV_STATE_KEY], raw)  # the real snapshot is untouched
        # Only a positive "key does not exist" allows defaults.
        self.fake.values.pop(KV_STATE_KEY)
        store = self.store()
        self.assertEqual(store.hydrate(), "default")
        self.assertTrue(store.hydrated)
        self.assertNotIn("reason", store.health())

    def test_local_state_with_invalid_kv_snapshot_serves_reads_but_refuses_changes(self):
        from lib.dashboard_server import DashboardFrozenError

        write_state(self.path, 2, ["local/repo"])
        self.fake.values[KV_STATE_KEY] = self.BAD["invalid_state"]
        store = self.store()
        self.assertEqual(store.hydrate(), "local")
        self.assertTrue(store.hydrated)
        self.assertFalse(store.reconciled)
        self.assertIn("failed verification", store.health()["reason"])
        with self.assertRaises(DashboardFrozenError):
            store.run_request("k" * 16, "POST", "/api/targets",
                              lambda: (200, {"t": store.set_controlled_targets(["o/x"])}))
        for name, raw in self.BAD.items():
            self.fake.values[KV_STATE_KEY] = raw
            with self.subTest(name), mock.patch.object(store, "_publish_snapshot",
                                                       side_effect=AssertionError("must not publish")):
                self.assertFalse(store.reconcile())  # still not verifiable: KV is never overwritten
                self.assertEqual(self.fake.values[KV_STATE_KEY], raw)
        self.fake.values[KV_STATE_KEY] = kv_snapshot(5, ["kv/repo"])
        self.assertTrue(store.reconcile())
        self.assertEqual(store.get_controlled_targets(), ["kv/repo"])
        self.assertNotIn("reason", store.health())

    def test_writer_never_overwrites_an_invalid_snapshot(self):
        store = self.store()
        self.assertEqual(store.hydrate(), "default")
        store.set_controlled_targets(["o/x"])
        self.fake.values[KV_STATE_KEY] = self.BAD["malformed"]
        self.assertIsNone(store._publish_snapshot())
        self.assertEqual(self.fake.values[KV_STATE_KEY], self.BAD["malformed"])
        store._writer_synced = False
        self.assertFalse(store.sync_with_kv_as_writer())


class FreezeDrainsExternalChangesTests(unittest.TestCase):
    """Codex round 2 P1: a freeze must not snapshot without the idempotency record of an in-flight external change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DashboardStore(Path(self.tmp.name) / "state.json", control_slot="a", clock=FakeClock())
        self.store.hydrate()
        self.release = threading.Event()
        self.started = threading.Event()
        self.runs = 0

    def external(self):
        self.runs += 1
        self.started.set()
        self.release.wait(10)
        return 200, {"pull_request": {"number": 7}}

    def run_in_thread(self, key="ext-key-0001"):
        out = []
        worker = threading.Thread(target=lambda: out.append(
            self.store.run_external(key, "POST", "/api/patches/save", self.external)))
        worker.start()
        self.assertTrue(self.started.wait(5))
        return worker, out

    def test_freeze_waits_and_snapshots_the_record(self):
        from lib.dashboard_server import DashboardFrozenError

        worker, out = self.run_in_thread()
        frozen = []
        freezer = threading.Thread(target=lambda: frozen.append(self.store.freeze()))
        freezer.start()
        time.sleep(0.2)
        self.assertEqual(frozen, [])  # still waiting for the in-flight change
        self.assertTrue(self.store.frozen)  # but no new change may start meanwhile
        with self.assertRaises(DashboardFrozenError):
            self.store.run_external("ext-key-0002", "POST", "/api/patches/save", self.external)
        self.release.set()
        worker.join(5)
        freezer.join(5)
        self.assertEqual(out[0][:2], (200, {"pull_request": {"number": 7}}))
        snapshot = frozen[0]["snapshot"]
        self.assertIn("ext-key-0001", snapshot["state"]["idempotency"])
        # The replacement imports the snapshot; a retry with the same key replays instead of re-running.
        replacement = DashboardStore(Path(self.tmp.name) / "new.json", control_slot="b", clock=FakeClock())
        replacement.import_snapshot(snapshot)
        status, body, replayed = replacement.run_external("ext-key-0001", "POST", "/api/patches/save", self.external)
        self.assertEqual((status, body["pull_request"]["number"], replayed, self.runs), (200, 7, True, 1))

    def test_freeze_times_out_unfrozen_and_retry_includes_the_record(self):
        from lib.dashboard_server import FreezeBusyError

        self.store.freeze_drain_seconds = 0.2
        worker, _out = self.run_in_thread()
        with self.assertRaises(FreezeBusyError):
            self.store.freeze()
        self.assertFalse(self.store.frozen)
        self.assertFalse(os.path.lexists(self.store.freeze_marker_path))
        self.release.set()
        worker.join(5)
        result = self.store.freeze()
        self.assertIn("ext-key-0001", result["snapshot"]["state"]["idempotency"])

    def test_completed_result_is_kept_when_the_save_fails(self):
        self.release.set()
        with mock.patch.object(self.store, "_persist", side_effect=OSError("disk full")), \
                self.assertLogs("spot_dashboard", "ERROR"):
            status, body, _ = self.store.run_external("ext-key-0003", "POST", "/api/patches/save", self.external)
        self.assertEqual(status, 200)
        self.assertIn("kept in memory", body["warning"])
        self.assertIn("ext-key-0003", self.store.freeze()["snapshot"]["state"]["idempotency"])
        self.store.unfreeze()
        _, _, replayed = self.store.run_external("ext-key-0003", "POST", "/api/patches/save", self.external)
        self.assertTrue(replayed)
        self.assertEqual(self.runs, 1)


class InactiveSlotServerTests(ServerHarness):
    def test_browser_change_on_inactive_slot_gets_wait_body_and_health_stays_200(self):
        fake = FakeKvRequester()
        fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=1).to_json()
        store = self.start(control_slot="a", kv=make_kv(fake))
        deadline = time.time() + 5
        while not store.hydrated and time.time() < deadline:
            time.sleep(0.01)
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual((status, body["active"]), (200, False))
        self.assertEqual(self.request("GET", "/api/overview")[0], 200)
        status, msg, payload = self.request("POST", "/api/targets", body={"targets": ["o/x"]},
                                            headers={"Idempotency-Key": "k" * 16})
        self.assert_wait_response(status, msg, payload)

    def test_old_writer_slot_refuses_changes_right_after_a_cutover(self):
        fake = FakeKvRequester()
        fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("a", now=1).to_json()
        store = self.start(control_slot="a", kv=make_kv(fake))
        deadline = time.time() + 5
        while not store.hydrated and time.time() < deadline:
            time.sleep(0.01)
        budget = {"target": "global", "daily_cap_usd": 7}
        status, _, _ = self.request("POST", "/api/budget", body=budget, headers={"Idempotency-Key": "a" * 16})
        self.assertEqual(status, 200)
        self.assertTrue(store.kv_writer and store._writer_synced)
        fake.values[KV_ORIGIN_KEY] = OriginRecord.for_slot("b", now=2).to_json()  # origin moved to slot B
        version = store.state_version()
        status, msg, payload = self.request("POST", "/api/budget", body=budget, headers={"Idempotency-Key": "b" * 16})
        self.assert_wait_response(status, msg, payload)  # never a 200 that slot B would not receive
        self.assertEqual(store.state_version(), version)


class IdempotencyTests(ServerHarness):
    KEY = "3f0c1d9e-1111-4222-8333-944455556666"

    def budget(self, key=None, amount=1.25, path="/api/budget"):
        headers = {"Idempotency-Key": key} if key else {}
        return self.request("POST", path, headers=headers, body={"max_hourly_usd": amount, "target": "global"})

    def test_replay_returns_stored_response_without_reapplying(self):
        store = self.start()
        before = store.state_version()
        status, msg, first = self.budget(self.KEY)
        self.assertEqual(status, 200)
        self.assertIsNone(msg["Idempotent-Replay"])
        self.assertEqual(store.state_version(), before + 1)  # mutation + record in ONE persist
        on_disk = json.loads(self.path.read_text())
        self.assertEqual(on_disk["state_version"], before + 1)
        self.assertIn(self.KEY, on_disk["idempotency"])
        self.assertEqual(on_disk["global_budget"]["max_hourly_usd"], first["budget"]["max_hourly_usd"])

        status, msg, replay = self.budget(self.KEY)
        self.assertEqual((status, msg["Idempotent-Replay"]), (200, "true"))
        self.assertEqual(replay, first)
        self.assertEqual(store.state_version(), before + 1)
        self.assertEqual(store.state["global_budget"]["max_hourly_usd"], first["budget"]["max_hourly_usd"])

        # The same key with a different body is a different change: 422, never a replay that drops it.
        status, msg, body = self.budget(self.KEY, amount=9.99)
        self.assertEqual((status, msg["Idempotent-Replay"]), (422, None))
        self.assertIn("different request", body["error"])
        self.assertEqual(store.state_version(), before + 1)
        self.assertEqual(store.state["global_budget"]["max_hourly_usd"], first["budget"]["max_hourly_usd"])

        status, _, _ = self.budget(self.KEY, path="/api/scaling")
        self.assertEqual(status, 422)
        status, _, _ = self.budget("bad key!")
        self.assertEqual(status, 400)

    def test_failed_request_is_recorded_and_state_unchanged(self):
        store = self.start()
        before = json.dumps(store.state.get("global_budget"), sort_keys=True)
        status, _, err = self.budget("4f0c1d9e-1111-4222-8333-944455556666", amount=-5)
        self.assertEqual(status, 400)
        self.assertEqual(json.dumps(store.state.get("global_budget"), sort_keys=True), before)
        status, msg, again = self.budget("4f0c1d9e-1111-4222-8333-944455556666", amount=-5)
        self.assertEqual((status, msg["Idempotent-Replay"], again), (400, "true", err))
        status, msg, _ = self.budget("4f0c1d9e-1111-4222-8333-944455556666", amount=1)
        self.assertEqual((status, msg["Idempotent-Replay"]), (422, None))
        self.assertEqual(json.dumps(store.state.get("global_budget"), sort_keys=True), before)

    def test_replay_after_restart_from_persisted_file(self):
        self.start()
        status, _, first = self.budget(self.KEY)
        self.assertEqual(status, 200)
        restarted = DashboardStore(self.path)
        restarted.hydrate()
        calls = []
        fingerprint = request_fingerprint({"max_hourly_usd": 1.25, "target": "global"})
        replay = restarted.run_request(self.KEY, "POST", "/api/budget", lambda: calls.append(1) or (200, {}),
                                       fingerprint=fingerprint)
        self.assertEqual(replay, (200, first, True))
        self.assertEqual(calls, [])
        # The persisted body fingerprint survives the restart: a different body is refused, not replayed.
        mismatch = restarted.run_request(self.KEY, "POST", "/api/budget", lambda: calls.append(1) or (200, {}),
                                         fingerprint=request_fingerprint({"max_hourly_usd": 7, "target": "global"}))
        self.assertEqual(mismatch[0], 422)
        self.assertEqual(calls, [])
        # Through a fresh server on the same state file.
        self.start()
        status, msg, body = self.budget(self.KEY)
        self.assertEqual((status, msg["Idempotent-Replay"], body), (200, "true", first))
        status, msg, _ = self.budget(self.KEY, amount=7)
        self.assertEqual((status, msg["Idempotent-Replay"]), (422, None))

    def test_record_binds_the_canonical_body_fingerprint(self):
        store = self.start()
        status, _, first = self.budget(self.KEY)
        self.assertEqual(status, 200)
        record = json.loads(self.path.read_text())["idempotency"][self.KEY]
        self.assertEqual(record["f"], request_fingerprint({"target": "global", "max_hourly_usd": 1.25}))
        before = store.state_version()
        # Key order and whitespace do not change the canonical fingerprint, so a reordered body replays.
        status, msg, body = self.request(
            "POST", "/api/budget", raw_body=b'{ "target" : "global",  "max_hourly_usd": 1.25 }',
            headers={"Content-Type": "application/json", "Idempotency-Key": self.KEY})
        self.assertEqual((status, msg["Idempotent-Replay"], body), (200, "true", first))
        self.assertEqual(store.state_version(), before)

    def test_same_key_with_a_different_body_never_applies_or_replays(self):
        store = self.start()
        self.assertEqual(self.budget(self.KEY, amount=2)[0], 200)
        for amount in (3, 2.0000001):
            status, msg, body = self.budget(self.KEY, amount=amount)
            self.assertEqual((status, msg["Idempotent-Replay"]), (422, None))
            self.assertIn("different request", body["error"])
        self.assertEqual(store.state["global_budget"]["max_hourly_usd"], 2)

    def test_legacy_record_without_a_fingerprint_fails_closed(self):
        clock = FakeClock()
        store = DashboardStore(Path(self.tmp.name) / "legacy.json", clock=clock)
        store.hydrate()
        store.state["idempotency"] = {"legacy-key-0001": {"m": "POST", "p": "/api/budget", "s": 200,
                                                          "b": {"old": True}, "t": clock.now}}
        calls = []
        result = store.run_request("legacy-key-0001", "POST", "/api/budget", lambda: calls.append(1) or (200, {}),
                                   fingerprint=request_fingerprint({"max_hourly_usd": 1}))
        # Body equality cannot be proven without a stored fingerprint: never replay over a possibly new change.
        self.assertEqual(result[0], 422)
        self.assertEqual(calls, [])
        self.assertNotIn("f", store.state["idempotency"]["legacy-key-0001"])

    def test_records_are_bounded_and_expire(self):
        clock = FakeClock()
        store = DashboardStore(Path(self.tmp.name) / "bounded.json", clock=clock)
        store.hydrate()
        for i in range(IDEMPOTENCY_MAX_KEYS + 20):
            clock.now += 1
            store.run_request(f"key-{i:08d}", "POST", "/api/x", lambda: (200, {"i": 1}))
        records = store.state["idempotency"]
        self.assertEqual(len(records), IDEMPOTENCY_MAX_KEYS)
        self.assertNotIn("key-00000000", records)
        clock.now += 86_401
        store.run_request("key-fresh-001", "POST", "/api/x", lambda: (200, {}))
        self.assertEqual(list(store.state["idempotency"]), ["key-fresh-001"])

    def test_non_mutating_posts_are_not_recorded(self):
        store = self.start()
        status, _, _ = self.request("POST", "/api/cloud-init/parse", headers={"Idempotency-Key": self.KEY},
                                    body={"content": "#cloud-config\n"})
        self.assertEqual(status, 200)
        self.assertNotIn(self.KEY, store.state.get("idempotency", {}))


class ControlEndpointTests(ServerHarness):
    def auth(self, op, slot="a", **kwargs):
        return {"Authorization": f"Bearer {control_token(CONTROL_SECRET, op, slot, **kwargs)}"}

    def test_control_disabled_without_secret(self):
        self.start(control_slot="a")
        status, _, _ = self.request("POST", "/api/control/freeze", headers=self.auth("freeze"))
        self.assertEqual(status, 404)

    def test_token_rules(self):
        store = self.start(control_slot="a", control_secret=CONTROL_SECRET)
        bad = [
            {},
            {"Authorization": "Bearer garbage"},
            self.auth("freeze", slot="b"),
            self.auth("unfreeze"),
            self.auth("freeze", now=time.time() - 1000),
            {"Authorization": f"Bearer {control_token(CONTROL_SECRET + 'x', 'freeze', 'a')}"},
        ]
        for headers in bad:
            status, _, body = self.request("POST", "/api/control/freeze", headers=headers)
            self.assertEqual((status, body), (401, {"error": "unauthorized"}), headers)
        self.assertFalse(store.frozen)
        self.assertEqual(self.request("GET", "/api/control/freeze", headers=self.auth("freeze"))[0], 405)
        self.assertEqual(self.request("POST", "/api/control/bogus", headers=self.auth("freeze"))[0], 404)

    def test_freeze_is_refused_retryably_while_an_external_change_runs(self):
        store = self.start(control_slot="a", control_secret=CONTROL_SECRET)
        store.freeze_drain_seconds = 0.2
        release, started = threading.Event(), threading.Event()

        def external():
            started.set()
            release.wait(10)
            return 200, {"ok": True}

        worker = threading.Thread(target=lambda: store.run_external("ext-key-0009", "POST", "/api/patches/save",
                                                                    external))
        worker.start()
        self.assertTrue(started.wait(5))
        status, _, body = self.request("POST", "/api/control/freeze", headers=self.auth("freeze"))
        self.assertEqual((status, body["retryable"]), (409, True))
        self.assertFalse(store.frozen)
        release.set()
        worker.join(5)
        status, _, body = self.request("POST", "/api/control/freeze", headers=self.auth("freeze"))
        self.assertEqual(status, 200)
        self.assertIn("ext-key-0009", body["snapshot"]["state"]["idempotency"])

    def test_freeze_snapshot_unfreeze_import(self):
        fake = FakeKvRequester()
        store = self.start(control_slot="a", control_secret=CONTROL_SECRET, kv=make_kv(fake))
        deadline = time.time() + 5
        while not store.hydrated and time.time() < deadline:
            time.sleep(0.01)
        store.set_controlled_targets(["o/live"])
        status, _, frozen = self.request("POST", "/api/control/freeze", headers=self.auth("freeze"))
        self.assertEqual(status, 200)
        self.assertEqual(frozen["state_version"], store.state_version())
        self.assertEqual(parse_snapshot(frozen["snapshot"])["state"]["controlled_targets"], ["o/live"])
        self.assertTrue(frozen["kv_flushed"])
        self.assertEqual(parse_snapshot(fake.values[KV_STATE_KEY])["state_version"], frozen["state_version"])
        self.assertTrue(store.frozen)
        self.assertEqual(self.request("POST", "/api/budget", body={"max_hourly_usd": 1})[0], 503)

        status, _, snap = self.request("GET", "/api/control/snapshot", headers=self.auth("snapshot"))
        self.assertEqual((status, snap["state_version"]), (200, frozen["state_version"]))

        status, _, body = self.request("POST", "/api/control/unfreeze", headers=self.auth("unfreeze"))
        self.assertEqual((status, body), (200, {"frozen": False}))
        self.assertFalse(store.frozen)

        newer = build_snapshot({"controlled_targets": ["o/imported"]}, frozen["state_version"] + 3, "b")
        status, _, body = self.request("POST", "/api/control/import", headers=self.auth("import"), body=newer)
        self.assertEqual((status, body), (200, {"state_version": frozen["state_version"] + 3, "imported": True}))
        self.assertEqual(store.get_controlled_targets(), ["o/imported"])
        older = build_snapshot({"controlled_targets": ["o/old"]}, 1, "b")
        status, _, body = self.request("POST", "/api/control/import", headers=self.auth("import"), body=older)
        self.assertEqual((status, body["imported"]), (200, False))
        status, _, _ = self.request("POST", "/api/control/import", headers=self.auth("import"), body={"x": 1})
        self.assertEqual(status, 400)

    def test_restart_while_frozen_through_server(self):
        store = self.start(control_slot="a", control_secret=CONTROL_SECRET)
        store.set_controlled_targets(["o/before"])
        status, _, frozen = self.request("POST", "/api/control/freeze", headers=self.auth("freeze"))
        self.assertEqual(status, 200)

        restarted = self.start(control_slot="a", control_secret=CONTROL_SECRET)  # same state file
        self.assertTrue(restarted.frozen)
        status, msg, payload = self.request("GET", "/api/health")
        self.assert_wait_response(status, msg, payload)
        self.assertEqual((payload["status"], payload["frozen"], payload["hydrated"]), ("frozen", True, True))
        status, msg, payload = self.request("POST", "/api/targets", body={"targets": ["o/after"]})
        self.assert_wait_response(status, msg, payload)
        self.assertEqual(restarted.get_controlled_targets(), ["o/before"])
        self.assertEqual(restarted.state_version(), frozen["state_version"])

        status, _, body = self.request("POST", "/api/control/unfreeze", headers=self.auth("unfreeze"))
        self.assertEqual((status, body), (200, {"frozen": False}))
        self.assertEqual(self.request("GET", "/api/health")[0], 200)

    def test_freeze_reports_500_when_marker_cannot_be_written(self):
        store = self.start(control_slot="a", control_secret=CONTROL_SECRET)
        with mock.patch.object(DashboardStore, "_write_freeze_marker", side_effect=OSError("disk full")):
            status, _, body = self.request("POST", "/api/control/freeze", headers=self.auth("freeze"))
        self.assertEqual(status, 500)
        self.assertIn("freeze marker", body["error"])
        self.assertTrue(store.frozen)

    def test_import_returns_500_when_state_file_write_fails(self):
        store = self.start(control_slot="b", control_secret=CONTROL_SECRET)
        store.hydrated = False
        env = build_snapshot({"controlled_targets": ["o/r"]}, 10, "a")
        with mock.patch.object(DashboardStore, "_persist", side_effect=OSError("disk full")):
            status, _, body = self.request("POST", "/api/control/import", headers=self.auth("import", slot="b"),
                                           body=env)
        self.assertEqual((status, body["imported"]), (500, False))
        self.assertFalse(store.hydrated)
        self.assertEqual(self.request("GET", "/api/health")[0], 503)

    def test_import_hydrates_unhydrated_store(self):
        store = self.start(control_slot="b", control_secret=CONTROL_SECRET)
        store.hydrated = False
        env = build_snapshot({"controlled_targets": ["o/r"]}, 10, "a")
        status, _, body = self.request("POST", "/api/control/import", headers=self.auth("import", slot="b"), body=env)
        self.assertEqual((status, body["imported"]), (200, True))
        self.assertEqual(self.request("GET", "/api/health")[0], 200)


class CliTests(unittest.TestCase):
    def test_control_slot_flag(self):
        self.assertEqual(args_parser().parse_args(["--control-slot", "b"]).control_slot, "b")
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            args_parser().parse_args(["--control-slot", "c"])
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            args_parser().parse_args(["--allowed-admins", "x"])


class _FakeMetadataResponse:
    def __init__(self, body: str, flavor: str = "Google") -> None:
        self._body = body.encode()
        self.status = 200
        self.headers = {"Metadata-Flavor": flavor}

    def read(self, _limit=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeOpener:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _FakeMetadataResponse(self.responses[request.full_url])


class MetadataCredentialTests(unittest.TestCase):
    def setUp(self):
        self.opener = _FakeOpener({
            compute_api.METADATA_TOKEN_URL: json.dumps({"access_token": "meta-token", "expires_in": 3599}),
            compute_api.METADATA_PROJECT_URL: "meta-project",
        })
        patcher = mock.patch("urllib.request.build_opener", return_value=self.opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SPOT_GCP_TOKEN_SOURCE", None)

    def test_get_auth_falls_back_when_gcloud_missing(self):
        with mock.patch("lib.compute_api.subprocess.run", side_effect=FileNotFoundError("gcloud")):
            auth = compute_api.get_auth(None)
        self.assertEqual((auth.token, auth.project), ("meta-token", "meta-project"))
        self.assertEqual(self.opener.requests[0].get_header("Metadata-flavor"), "Google")

    def test_env_forces_metadata_and_skips_gcloud(self):
        os.environ["SPOT_GCP_TOKEN_SOURCE"] = "metadata"
        with mock.patch("lib.compute_api.subprocess.run", side_effect=AssertionError("gcloud must not run")):
            auth = compute_api.get_auth("explicit-project")
        self.assertEqual((auth.token, auth.project), ("meta-token", "explicit-project"))
        with mock.patch("lib.billing_catalog.subprocess.run", side_effect=AssertionError("gcloud must not run")):
            self.assertEqual(billing_catalog._access_token(), "meta-token")

    def test_gcloud_still_preferred_when_present(self):
        def fake_run(argv, **_kwargs):
            out = "gcloud-token" if "print-access-token" in argv else "gcloud-project"
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        with mock.patch("lib.compute_api.subprocess.run", side_effect=fake_run):
            auth = compute_api.get_auth(None)
        self.assertEqual((auth.token, auth.project), ("gcloud-token", "gcloud-project"))
        with mock.patch("lib.billing_catalog.subprocess.run", side_effect=fake_run):
            self.assertEqual(billing_catalog._access_token(), "gcloud-token")
        self.assertEqual(self.opener.requests, [])

    def test_billing_catalog_falls_back_when_gcloud_missing(self):
        with mock.patch("lib.billing_catalog.subprocess.run", side_effect=FileNotFoundError("gcloud")):
            self.assertEqual(billing_catalog._access_token(), "meta-token")

    def test_metadata_failure_is_reported(self):
        def broken(request, timeout=None):
            raise OSError("no route")

        self.opener.open = broken
        with mock.patch("lib.compute_api.subprocess.run", side_effect=FileNotFoundError("gcloud")):
            with self.assertRaisesRegex(RuntimeError, "metadata server"):
                compute_api.get_auth(None)


NODE = shutil.which("node")


class BrowserRetryWrapperTests(unittest.TestCase):
    def setUp(self):
        self.html = render_dashboard_html()

    def test_wrapper_markers(self):
        for marker in ("Idempotency-Key", "crypto.randomUUID", "control_vm_unavailable", "/api/health",
                       "The control VM is restarting or being replaced", "Your change will be retried automatically",
                       "CONTROL_VM_POLL_MS = 5000", "err instanceof TypeError", 'window.location.assign("/")'):
            self.assertIn(marker, self.html)
        self.assertNotIn("location.reload", self.html)
        self.assertNotIn("<script src=", self.html)
        self.assertNotIn('<link rel="stylesheet"', self.html)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_wrapper_waits_and_retries_same_request(self):
        script = self.html[self.html.index("<script>") + len("<script>"):self.html.rindex("</script>")]
        start = script.index("    function getDashboardAuthToken()")
        end = script.index("    function showToast(msg)")
        block = script[start:end]
        harness = """
const calls = [];
const healthCalls = [];
let backend = [];
const elements = {};
globalThis.setTimeout = (fn) => { fn(); return 0; };
globalThis.sessionStorage = { getItem: () => "", setItem() {}, removeItem() {} };
globalThis.localStorage = globalThis.sessionStorage;
globalThis.document = {
  getElementById: (id) => elements[id] || null,
  createElement: () => ({ setAttribute() {}, style: {}, hidden: false }),
  body: { appendChild: (el) => { elements[el.id] = el; } },
};
globalThis.window = { crypto: globalThis.crypto, prompt: undefined, location: { assign: (p) => calls.push(["assign", p]) } };
const jsonResponse = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const NATIVE_FETCH = async (input, init) => {
  if (input === "/api/health") { healthCalls.push(1); return jsonResponse(healthCalls.length >= 2 ? 200 : 503, {}); }
  if (input === "/api/auth/status") return jsonResponse(200, { oauth: { enabled: true } });
  calls.push([input, init.method, init.headers.get("Idempotency-Key"), init.body]);
  const next = backend.shift();
  if (next === "network") throw new TypeError("Failed to fetch");
  return jsonResponse(next.status, next.body);
};
""" + block + """
(async () => {
  backend = [
    { status: 503, body: { error: "control_vm_unavailable", retry_after_seconds: 10 } },
    "network",
    { status: 200, body: { ok: true } },
  ];
  const res = await window.fetch("/api/budget", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{\\"a\\":1}" });
  const data = await res.json();
  const keys = calls.map((c) => c[2]);
  const banner = elements["control-vm-banner"];
  backend = [{ status: 200, body: {} }];
  await window.fetch("/api/overview");
  const getKey = calls[calls.length - 1][2];
  backend = [{ status: 401, body: {} }];
  await window.fetch("/api/overview");
  console.log(JSON.stringify({ data, keys, bodies: calls.slice(0, 3).map((c) => c[3]), health: healthCalls.length,
                               bannerHidden: banner ? banner.hidden : null, bannerText: banner ? banner.textContent : "",
                               getKey, assigned: calls.filter((c) => c[0] === "assign") }));
})().catch((err) => { console.error(err); process.exit(1); });
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.mjs"
            path.write_text(harness, encoding="utf-8")
            result = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(out["data"], {"ok": True})
        self.assertEqual(len(out["keys"]), 3)
        self.assertEqual(len(set(out["keys"])), 1)
        self.assertRegex(out["keys"][0], r"^[0-9a-f-]{36}$")
        self.assertEqual(out["bodies"], ['{"a":1}'] * 3)
        self.assertGreaterEqual(out["health"], 2)
        self.assertTrue(out["bannerHidden"])
        self.assertIn("restarting or being replaced", out["bannerText"])
        self.assertIsNone(out["getKey"])
        self.assertEqual(out["assigned"], [["assign", "/"]])



class AddedAdminDurabilityTests(unittest.TestCase):
    """Added administrators live in dashboard state, so every snapshot, import, and KV hydration carries them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeKvRequester()
        no_live_pricing(self)

    def store(self, name: str, slot: str = "a", *, kv: bool = False) -> DashboardStore:
        return DashboardStore(Path(self.tmp.name) / name, control_slot=slot, kv=make_kv(self.fake) if kv else None,
                              sleep=lambda _s: None)

    def test_snapshot_import_carries_admins_to_the_other_slot(self):
        old = self.store("a.json")
        old.hydrate()
        old.add_dashboard_admin("alice", 4242, actor="swackhamer", actor_uid=619624)
        envelope = old.freeze()["snapshot"]
        self.assertEqual([a["uid"] for a in envelope["state"]["dashboard_admins"]], [4242])
        new = self.store("b.json", "b")
        self.assertEqual(new.dashboard_admin_uids(), frozenset())
        self.assertTrue(new.import_snapshot(json.loads(json.dumps(envelope)))["imported"])
        self.assertEqual(new.dashboard_admin_uids(), frozenset({4242}))
        self.assertEqual(new.get_dashboard_admins()[0]["added_by"], "swackhamer")
        self.assertEqual([h["action"] for h in new.get_dashboard_admins_history()], ["added"])
        # And the replacement keeps them across its own restart.
        self.assertEqual(self.store("b.json", "b").dashboard_admin_uids(), frozenset({4242}))

    def test_admin_audit_lines_are_written_only_after_the_change_is_durable(self):
        store = self.store("audit.json")
        store.hydrate()
        store.add_dashboard_admin("alice", 4242, actor="swackhamer", actor_uid=619624)
        original_persist = store._persist

        def failing_commit(bump=True):
            if store._defer is not None:
                return original_persist(bump)
            raise OSError("simulated disk full at commit")

        def remove():
            return 200, store.remove_dashboard_admin(4242, actor="swackhamer", actor_uid=619624)

        def add():
            return 200, store.add_dashboard_admin("bob", 5151, actor="swackhamer", actor_uid=619624)

        with mock.patch("lib.dashboard_server.audit_admin_change") as audit:
            with mock.patch.object(store, "_persist", side_effect=failing_commit), \
                    self.assertLogs("spot_dashboard", "ERROR"):
                self.assertEqual(store.run_request("k-rm", "POST", "/api/admins/remove", remove,
                                                   fingerprint="rm")[0], 500)
                self.assertEqual(store.run_request("k-add", "POST", "/api/admins/add", add,
                                                   fingerprint="add")[0], 500)
            audit.assert_not_called()
            self.assertEqual(store.dashboard_admin_uids(), frozenset({4242}))
            status, _, replayed = store.run_request("k-rm2", "POST", "/api/admins/remove", remove, fingerprint="rm2")
            self.assertEqual((status, replayed), (200, False))
            audit.assert_called_once_with("removed", "alice", 4242, "swackhamer", 619624)
            # An idempotent replay of the committed request writes no second audit line.
            self.assertTrue(store.run_request("k-rm2", "POST", "/api/admins/remove", remove, fingerprint="rm2")[2])
            audit.assert_called_once()
        self.assertEqual(store.dashboard_admin_uids(), frozenset())

    def test_a_rolled_back_client_error_writes_no_admin_audit_line(self):
        store = self.store("audit4xx.json")
        store.hydrate()

        def add_then_refuse():
            store.add_dashboard_admin("bob", 5151, actor="swackhamer", actor_uid=619624)
            return 409, {"error": "refused after the change"}

        with mock.patch("lib.dashboard_server.audit_admin_change") as audit:
            self.assertEqual(store.run_request("k", "POST", "/api/admins/add", add_then_refuse, fingerprint="f")[0], 409)
            audit.assert_not_called()
        self.assertEqual(store.dashboard_admin_uids(), frozenset())

    def test_fresh_vm_hydrates_admins_from_the_kv_snapshot(self):
        state = {"dashboard_admins": [{"login": "alice", "uid": 4242, "added_by": "swackhamer",
                                       "added_by_uid": 619624, "added_at": "2026-10-01T00:00:00Z"}]}
        self.fake.values[KV_STATE_KEY] = json.dumps(build_snapshot(state, 9, "a", now=1))
        store = self.store("fresh.json", kv=True)
        self.assertEqual(store.dashboard_admin_uids(), frozenset())  # nothing local before hydration
        self.assertEqual(store.hydrate(), "kv")
        self.assertEqual(store.dashboard_admin_uids(), frozenset({4242}))

    def test_snapshot_without_admins_from_an_older_dashboard_means_none(self):
        self.fake.values[KV_STATE_KEY] = json.dumps(build_snapshot({"controlled_targets": []}, 3, "a", now=1))
        store = self.store("old.json", kv=True)
        self.assertEqual(store.hydrate(), "kv")
        self.assertEqual(store.dashboard_admin_uids(), frozenset())
        self.assertEqual(store.state["dashboard_admins"], [])

    def test_failed_save_rolls_back_the_change(self):
        store = self.store("a.json")
        store.hydrate()
        with mock.patch.object(store, "_persist", side_effect=OSError("disk full")), self.assertRaises(OSError):
            store.add_dashboard_admin("alice", 4242, actor="swackhamer", actor_uid=619624)
        self.assertEqual(store.dashboard_admin_uids(), frozenset())
        self.assertEqual(store.get_dashboard_admins_history(), [])


if __name__ == "__main__":
    unittest.main()
