"""Cloud Storage control VM state and lease: generation preconditions, stale breaks, contenders, migration.

Every request goes to the in-memory FakeGcsServer; nothing touches Cloud Storage, GCP, or Cloudflare.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gcs_fixtures import BUCKET, TOKEN, FakeGcsServer
from lib import control_vm as cv
from lib import gcs_control_state as gcs

URI = f"gs://{BUCKET}/control"
RealLease = gcs.GcsLease  # tests patch gcs.GcsLease to inject clocks; World always builds the real one
PROJECT = "proj"
TTL = gcs.LEASE_TTL_SECONDS
GRACE = gcs.LEASE_STALE_GRACE_SECONDS


def tearDownModule():
    # test_control_vm (imported for its harness) owns a temporary state directory that its own tearDownModule
    # removes; when only this module runs, remove it here (cleanup is idempotent).
    import sys
    module = sys.modules.get("test_control_vm")
    if module is not None:
        module._CONTROL_STATE_DIR.cleanup()


class World:
    """A fake server plus clocks: ``clock`` drives the server, the holders' wall clock, and their monotonic clock."""

    def __init__(self):
        self.clock = [1_700_000_000.0]
        self.server = FakeGcsServer(self.clock)
        self.client = gcs.GcsClient(BUCKET, token=lambda: TOKEN, transport=self.server, sleep=lambda s: None)
        self.location = gcs.parse_state_uri(URI)

    def lease(self, owner, **kw):
        return RealLease(self.client, self.location.lock_object(PROJECT), owner=owner,
                            wall=lambda: self.clock[0], monotonic=lambda: self.clock[0], **kw)

    def lock(self, owner):
        return gcs.GcsProjectControlLock(PROJECT, URI, client=self.client, lease=self.lease(owner))

    def advance(self, seconds):
        self.clock[0] += seconds

    def init_state(self, state=None):
        """What migrate-state --apply leaves behind: the state object exists."""
        self.server.put_raw(self.state_name, json.dumps({"schema": gcs.STATE_SCHEMA, "project": PROJECT,
                                                         "state": state or {}}).encode())

    @property
    def lock_name(self):
        return self.location.lock_object(PROJECT)

    @property
    def state_name(self):
        return self.location.state_object(PROJECT)


class UriTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        location = gcs.parse_state_uri("gs://my-bucket/a/b/")
        self.assertEqual((location.bucket, location.prefix), ("my-bucket", "a/b"))
        self.assertEqual(location.state_object("homelab-424523"), "a/b/control-vm-homelab-424523.json")
        self.assertEqual(location.lock_object("example.com:p"), "a/b/control-vm-example.com__p.lock")
        self.assertEqual(gcs.parse_state_uri("gs://bkt").state_object("p"), "control-vm-p.json")
        for bad in ("", "s3://b/x", "gs://", "gs://B", "gs://goog-x", "gs://b/../x", "gs://ok-bucket/a b",
                    "/tmp/x"):
            with self.assertRaises(ValueError, msg=bad):
                gcs.parse_state_uri(bad)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_create_only_and_generation_match(self):
        first = self.w.client.put("o", b"1", if_generation_match=0)
        with self.assertRaises(gcs.GcsPreconditionFailed):
            self.w.client.put("o", b"2", if_generation_match=0)
        with self.assertRaises(gcs.GcsPreconditionFailed):
            self.w.client.put("o", b"2", if_generation_match=first.generation + 7)
        second = self.w.client.put("o", b"2", if_generation_match=first.generation)
        self.assertGreater(second.generation, first.generation)
        got = self.w.client.get("o")
        self.assertEqual((got.data, got.generation), (b"2", second.generation))
        self.assertEqual(self.w.client.delete("o", if_generation_match=first.generation), "precondition")
        self.assertEqual(self.w.client.delete("o", if_generation_match=second.generation), "deleted")
        self.assertEqual(self.w.client.delete("o", if_generation_match=second.generation), "missing")
        self.assertIsNone(self.w.client.get("o"))
        for call in self.w.server.writes("POST") + self.w.server.writes("DELETE"):
            self.assertIn("ifGenerationMatch", call[2])  # never an unconditional write

    def test_ambiguous_and_definite_write_failures(self):
        self.w.server.faults.append(("POST", "drop"))
        with self.assertRaises(gcs.GcsAmbiguous):
            self.w.client.put("o", b"1", if_generation_match=0)
        self.w.server.faults.append(("POST", 503))
        with self.assertRaises(gcs.GcsAmbiguous):
            self.w.client.put("o", b"1", if_generation_match=0)
        self.w.server.faults.append(("POST", 403))
        with self.assertRaises(gcs.GcsError) as ctx:
            self.w.client.put("o", b"1", if_generation_match=0)
        self.assertNotIsInstance(ctx.exception, gcs.GcsAmbiguous)
        with self.assertRaises(ValueError):
            self.w.client.put("o", b"1", if_generation_match=None)

    def test_reads_retry_and_pin_a_generation_without_the_header(self):
        self.w.server.put_raw("o", b"x")
        self.w.server.faults += [("GET", 503), ("GET", "drop")]
        self.assertEqual(self.w.client.get("o").data, b"x")
        self.w.server.generation_header = False
        got = self.w.client.get("o")
        self.assertEqual(got.generation, self.w.server.objects["o"][1])
        self.w.server.faults += [("GET", 503)] * 3
        with self.assertRaises(gcs.GcsError):
            self.w.client.get("o")

    def test_access_token_sources(self):
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="ya29.token\n", stderr="")
        token = gcs.AccessToken({}, runner=runner, monotonic=lambda: 0.0)
        self.assertEqual(token(), "ya29.token")
        self.assertEqual(token(), "ya29.token")
        self.assertEqual(calls, [["gcloud", "auth", "print-access-token"]])  # cached
        with patch("lib.compute_api.metadata_access_token", return_value="meta-token"):
            self.assertEqual(gcs.AccessToken({"SPOT_GCP_TOKEN_SOURCE": "metadata"})(), "meta-token")
        failing = gcs.AccessToken({}, runner=lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="no"))
        with self.assertRaises(gcs.GcsError):
            failing()


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_acquire_is_create_only_and_a_second_owner_is_refused(self):
        a, b = self.w.lease("a"), self.w.lease("b")
        self.assertTrue(a.acquire())
        self.assertEqual(self.w.server.writes()[0][2]["ifGenerationMatch"], "0")
        self.assertFalse(b.acquire())
        self.assertEqual(b.holder.owner, "a")
        self.assertFalse(b.holder.stale)
        self.assertIn("breakable in", b.holder.describe())
        content = self.w.server.json(self.w.lock_name)
        self.assertEqual((content["owner"], content["ttl_seconds"], content["schema"]), ("a", TTL, gcs.LOCK_SCHEMA))
        self.assertEqual(content["expires_at"], self.w.clock[0] + TTL)
        a.release()
        self.assertNotIn(self.w.lock_name, self.w.server.objects)
        self.assertTrue(b.acquire())

    def test_stale_lease_is_broken_only_after_expiry_plus_grace(self):
        a, b = self.w.lease("a"), self.w.lease("b")
        self.assertTrue(a.acquire())
        stale_generation = self.w.server.objects[self.w.lock_name][1]
        self.w.advance(TTL + GRACE - 1)
        self.assertFalse(b.acquire(), "a live lease, even past its ttl, waits for the grace period")
        self.w.advance(1)
        self.assertTrue(b.acquire())
        self.assertEqual(b.broke.owner, "a")
        breaking = self.w.server.writes()[-1]
        self.assertEqual(breaking[2]["ifGenerationMatch"], str(stale_generation))  # conditioned on the stale one
        # The old holder can never act again: its renewal and its release are conditioned on its generation.
        with self.assertRaises(gcs.LeaseLost):
            a.renew()
        self.assertFalse(a.held)
        a.generation = stale_generation
        with self.assertLogs("spot_runner_autoscaler.control_vm", "ERROR"):
            a.release()
        self.assertEqual(self.w.server.json(self.w.lock_name)["owner"], "b")

    def test_holder_claimed_expiry_never_shortens_and_is_capped(self):
        a, b = self.w.lease("a"), self.w.lease("b")
        self.assertTrue(a.acquire())
        # A lock whose content claims a later expiry is honored up to LEASE_MAX_TTL_SECONDS.
        content = self.w.server.json(self.w.lock_name)
        content["expires_at"] = self.w.clock[0] + 10 * gcs.LEASE_MAX_TTL_SECONDS
        data, generation, updated = self.w.server.objects[self.w.lock_name]
        self.w.server.objects[self.w.lock_name] = (json.dumps(content).encode(), generation, updated)
        self.w.advance(TTL + GRACE + 5)
        self.assertFalse(b.acquire())
        self.w.advance(gcs.LEASE_MAX_TTL_SECONDS)
        self.assertTrue(b.acquire())

    def test_unreadable_lock_waits_the_longest_lease(self):
        self.w.server.put_raw(self.w.lock_name, b"garbage")
        b = self.w.lease("b")
        self.w.advance(TTL + GRACE + 1)
        self.assertFalse(b.acquire())
        self.w.advance(gcs.LEASE_MAX_TTL_SECONDS)
        self.assertTrue(b.acquire())

    def test_two_contenders_break_a_stale_lease_and_only_one_wins(self):
        a = self.w.lease("a")
        self.assertTrue(a.acquire())
        self.w.advance(TTL + GRACE + 1)
        b, c = self.w.lease("b"), self.w.lease("c")
        state = {"raced": False}

        def before(method, name, query):
            # C breaks the stale lease between B's inspection and B's break.
            if method == "POST" and query.get("ifGenerationMatch") not in (None, "0") and not state["raced"]:
                state["raced"] = True
                self.w.server.before = None
                self.assertTrue(c.acquire())
                self.w.server.before = before
        self.w.server.before = before
        self.assertFalse(b.acquire())
        self.assertTrue(state["raced"])
        self.assertTrue(c.held)
        self.assertEqual(b.holder.owner, "c")
        self.assertEqual(self.w.server.json(self.w.lock_name)["owner"], "c")

    def test_simultaneous_creates_have_one_winner(self):
        b, c = self.w.lease("b"), self.w.lease("c")
        state = {"raced": False}

        def before(method, name, query):
            if method == "POST" and not state["raced"]:
                state["raced"] = True
                self.w.server.before = None
                self.assertTrue(c.acquire())
        self.w.server.before = before
        self.assertFalse(b.acquire())
        self.assertTrue(c.held)

    def test_ambiguous_create_counts_only_when_read_back_proves_it(self):
        a = self.w.lease("a")
        self.w.server.faults.append(("POST", "lost"))  # applied, answer lost
        with self.assertLogs("spot_runner_autoscaler.control_vm", "WARNING"):
            self.assertTrue(a.acquire())
        self.assertEqual(a.generation, self.w.server.objects[self.w.lock_name][1])
        b = self.w.lease("b")
        self.w.server.faults.append(("POST", "drop"))  # never applied
        a.release()
        with self.assertLogs("spot_runner_autoscaler.control_vm", "WARNING"):
            self.assertTrue(b.acquire())  # read-back finds nothing, so it creates again
        self.assertEqual(self.w.server.json(self.w.lock_name)["owner"], "b")

    def test_ensure_renews_before_the_lease_gets_short(self):
        a = self.w.lease("a")
        self.assertTrue(a.acquire())
        writes = len(self.w.server.writes())
        self.w.advance(TTL - gcs.LEASE_MIN_REMAINING_SECONDS - 1)
        a.ensure()
        self.assertEqual(len(self.w.server.writes()), writes, "enough left: no write")
        held_generation = a.generation
        self.w.advance(2)
        a.ensure()
        self.assertEqual(len(self.w.server.writes()), writes + 1)
        self.assertGreaterEqual(a.remaining(), TTL - 1)
        self.assertEqual(self.w.server.writes()[-1][2]["ifGenerationMatch"], str(held_generation))
        self.assertGreater(a.generation, held_generation)

    def test_ensure_after_takeover_raises_before_any_call(self):
        a, b = self.w.lease("a"), self.w.lease("b")
        self.assertTrue(a.acquire())
        self.w.advance(TTL + GRACE + 1)
        self.assertTrue(b.acquire())
        with self.assertRaises(gcs.LeaseLost):
            a.ensure()
        with self.assertRaises(gcs.LeaseLost):
            a.ensure()  # stays lost

    def test_ambiguous_renewal(self):
        a = self.w.lease("a")
        self.assertTrue(a.acquire())
        self.w.server.faults.append(("POST", "lost"))
        with self.assertLogs("spot_runner_autoscaler.control_vm", "WARNING"):
            a.renew()
        self.assertEqual(a.generation, self.w.server.objects[self.w.lock_name][1])
        self.w.server.faults.append(("POST", "drop"))
        with self.assertLogs("spot_runner_autoscaler.control_vm", "WARNING"), self.assertRaises(gcs.LeaseLost):
            a.renew()
        self.assertFalse(a.held)
        # The object is still this lease's (the dropped renewal never landed): acquiring finds its own token and
        # trusts it only after a renewal conditioned on that generation.
        self.assertTrue(a.acquire())
        self.w.server.faults.append(("POST", 403))
        with self.assertRaises(gcs.LeaseLost):
            a.renew()

    def test_timings_are_validated(self):
        for kw in ({"ttl": 100, "min_remaining": 100}, {"ttl": gcs.LEASE_MAX_TTL_SECONDS + 1},
                   {"min_remaining": 0}):
            with self.assertRaises(ValueError, msg=kw):
                self.w.lease("x", **kw)
        self.assertGreater(gcs.LEASE_MIN_REMAINING_SECONDS, 600, "must outlast default_gcloud's 600 s timeout")


class SuspendAwareLeaseTests(unittest.TestCase):
    """The holder's clock: macOS time.monotonic() stops during system sleep; the wall clock doesn't."""

    def setUp(self):
        self.w = World()
        self.mono = [500.0]  # the holder's monotonic clock; the server and wall clocks are self.w.clock

    def lease(self, owner):
        return RealLease(self.w.client, self.w.lock_name, owner=owner, wall=lambda: self.w.clock[0],
                         monotonic=lambda: self.mono[0])

    def sleep(self, wall_seconds, mono_seconds=1.0):
        self.w.advance(wall_seconds)
        self.mono[0] += mono_seconds

    def test_sleeping_past_the_expiry_fails_the_guard(self):
        mac, watchdog = self.lease("mac"), self.lease("watchdog")
        self.assertTrue(mac.acquire())
        self.sleep(TTL + GRACE + 5)  # the lid was closed; monotonic time barely moved
        self.assertTrue(watchdog.acquire())  # the watchdog broke the expired lease
        self.assertLessEqual(mac.remaining(), 0)
        with self.assertRaises(gcs.LeaseLost):
            mac.ensure()
        self.assertFalse(mac.held)
        watchdog.ensure()  # only the new holder passes its guard
        self.assertEqual(self.w.server.json(self.w.lock_name)["owner"], "watchdog")

    def test_a_short_sleep_renews_before_acting(self):
        mac = self.lease("mac")
        self.assertTrue(mac.acquire())
        generation = mac.generation
        self.sleep(60)
        writes = len(self.w.server.writes())
        mac.ensure()
        self.assertEqual(len(self.w.server.writes()), writes + 1, "a suspected suspend renews the lease")
        self.assertEqual(self.w.server.writes()[-1][2]["ifGenerationMatch"], str(generation))
        self.sleep(60)
        self.w.server.put_raw(self.w.lock_name, b'{"owner": "intruder"}')  # replaced while asleep
        with self.assertRaises(gcs.LeaseLost):
            mac.ensure()

    def test_the_guard_rereads_the_lease_periodically(self):
        mac = self.lease("mac")
        self.assertTrue(mac.acquire())

        def tick(seconds):
            self.w.advance(seconds)
            self.mono[0] += seconds
        reads = lambda: len(self.w.server.writes("GET"))  # noqa: E731
        before = reads()
        tick(gcs.LEASE_REVALIDATE_SECONDS - 1)
        mac.ensure()
        self.assertEqual(reads(), before, "confirmed recently: no read")
        tick(1)
        mac.ensure()
        self.assertGreater(reads(), before, "re-read once the revalidation interval passed")
        writes = len(self.w.server.writes())
        tick(gcs.LEASE_REVALIDATE_SECONDS)
        self.w.server.put_raw(self.w.lock_name, self.w.server.objects[self.w.lock_name][0])  # new generation
        with self.assertRaises(gcs.LeaseLost):
            mac.ensure()
        self.assertEqual(len(self.w.server.writes()), writes, "a lost lease is detected by reading, not writing")

    def test_a_slow_revalidation_that_eats_the_margin_renews_before_returning(self):
        mac = self.lease("mac")
        self.assertTrue(mac.acquire())
        self.w.advance(gcs.LEASE_REVALIDATE_SECONDS)
        self.mono[0] += gcs.LEASE_REVALIDATE_SECONDS
        self.assertGreater(mac.remaining(), gcs.LEASE_MIN_REMAINING_SECONDS)  # enough before the re-read

        def slow(method, name, query):
            if method == "GET":  # each of the two re-reads takes 300 s (retries, a stalled connection)
                self.w.advance(300)
                self.mono[0] += 300
        self.w.server.before = slow
        writes = len(self.w.server.writes())
        mac.ensure()
        self.w.server.before = None
        self.assertEqual(len(self.w.server.writes()), writes + 1, "the margin was gone after the re-read: renew")
        self.assertGreaterEqual(mac.remaining(), gcs.LEASE_MIN_REMAINING_SECONDS)
        # The same, but the renewal can't be proven: the guard fails instead of returning with too little left.
        self.w.advance(gcs.LEASE_REVALIDATE_SECONDS)
        self.mono[0] += gcs.LEASE_REVALIDATE_SECONDS
        self.w.server.before = slow
        self.w.server.faults.append(("POST", 403))
        with self.assertRaises(gcs.LeaseLost):
            mac.ensure()

    def test_the_guard_margin_covers_the_longest_provider_call(self):
        self.assertGreaterEqual(gcs.LEASE_MIN_REMAINING_SECONDS, cv.GCLOUD_TIMEOUT_SECONDS + 60)

    def test_a_wall_clock_stepped_back_keeps_monotonic_time(self):
        mac = self.lease("mac")
        self.assertTrue(mac.acquire())
        self.w.advance(-1000)  # NTP step backwards
        self.mono[0] += TTL - gcs.LEASE_MIN_REMAINING_SECONDS + 1
        writes = len(self.w.server.writes())
        mac.ensure()  # the monotonic delta still says the lease is short: renew
        self.assertEqual(len(self.w.server.writes()), writes + 1)


class ProjectStateTests(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_uninitialized_state_refuses_and_releases_the_lease(self):
        with self.assertRaisesRegex(cv.ControlStateConflict, "migrate-state --apply"):
            with self.w.lock("a"):
                pass
        self.assertNotIn(self.w.lock_name, self.w.server.objects)
        self.assertNotIn(self.w.state_name, self.w.server.objects)

    def test_lock_loads_and_saves_with_generation_preconditions(self):
        lock = gcs.GcsProjectControlLock(PROJECT, URI, client=self.w.client, lease=self.w.lease("a"),
                                         require_existing=False)
        with lock as project:
            self.assertEqual((project.state, project.generation, project.exists), ({}, 0, False))
            self.assertIs(project.guard.__self__, lock.lease)
            project.state = {"control_vm": {"transition": {"id": "t1"}}}
            project.save()
            first = project.generation
            project.state["control_vm"]["transition"]["step"] = "wait_health"
            project.save()
            self.assertGreater(project.generation, first)
            saves = [c for c in self.w.server.writes() if c[1] == self.w.state_name]
            self.assertEqual([c[2]["ifGenerationMatch"] for c in saves], ["0", str(first)])
        self.assertNotIn(self.w.lock_name, self.w.server.objects)  # released
        envelope = self.w.server.json(self.w.state_name)
        self.assertEqual((envelope["schema"], envelope["project"], envelope["writer"]), (gcs.STATE_SCHEMA, PROJECT, "a"))
        self.assertEqual(gcs.load_state(PROJECT, URI, client=self.w.client).state["control_vm"]["transition"]["step"],
                         "wait_health")

    def test_save_after_another_writer_refuses(self):
        self.w.init_state()
        with self.w.lock("a") as project:
            project.state = {"control_vm": {}}
            project.save()
            self.w.server.put_raw(self.w.state_name, self.w.server.objects[self.w.state_name][0])  # someone else
            with self.assertRaisesRegex(cv.ControlStateConflict, "changed since this controller read it"):
                project.save()

    def test_held_lock_returns_none_and_names_the_holder(self):
        self.w.init_state()
        with self.w.lock("a") as first:
            self.assertIsNotNone(first)
            other = self.w.lock("b")
            with other as second:
                self.assertIsNone(second)
            self.assertIn("a (generation", other.describe_holder())
        self.assertNotIn(self.w.lock_name, self.w.server.objects)

    def test_ambiguous_state_write_is_settled_by_its_write_id(self):
        self.w.init_state()
        with self.w.lock("a") as project:
            project.state = {"control_vm": {"x": 1}}
            self.w.server.faults.append(("POST", "lost"))
            project.save()
            self.assertEqual(project.generation, self.w.server.objects[self.w.state_name][1])
            self.w.server.faults.append(("POST", "drop"))
            with self.assertRaisesRegex(cv.ControlStateConflict, "could not confirm"):
                project.save()

    def test_save_needs_the_lease_and_stops_once_it_is_lost(self):
        read_only = gcs.load_state(PROJECT, URI, client=self.w.client)
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            read_only.save()
        self.w.init_state({"control_vm": {}})
        before = self.w.server.objects[self.w.state_name]
        lock = self.w.lock("a")
        with lock as project:
            self.w.advance(TTL + GRACE + 1)
            self.assertTrue(self.w.lease("b").acquire())
            project.state = {"control_vm": {"x": 1}}
            with self.assertRaises(gcs.LeaseLost):
                project.save()
        self.assertEqual(self.w.server.objects[self.w.state_name], before)  # nothing was written

    def test_wrong_project_or_schema_fails_closed(self):
        self.w.server.put_raw(self.w.state_name, json.dumps(
            {"schema": gcs.STATE_SCHEMA, "project": "other", "state": {}}).encode())
        with self.assertRaisesRegex(cv.ControlStateConflict, "belongs to project 'other'"):
            gcs.load_state(PROJECT, URI, client=self.w.client)
        self.w.server.put_raw(self.w.state_name, b'{"control_vm": {}}')
        with self.assertRaisesRegex(cv.ControlStateConflict, "not a spot-control-vm-state-v1"):
            gcs.load_state(PROJECT, URI, client=self.w.client)
        self.w.server.put_raw(self.w.state_name, b"not json")
        with self.assertRaises(cv.ControlStateConflict):
            gcs.load_state(PROJECT, URI, client=self.w.client)
        # The lock is released when loading fails.
        with self.assertRaises(cv.ControlStateConflict):
            with self.w.lock("a"):
                pass
        self.assertNotIn(self.w.lock_name, self.w.server.objects)

    def test_adopt_moves_legacy_controller_state_into_cloud_storage(self):
        self.w.init_state()
        with self.w.lock("a") as project:
            legacy = {"idle_loops": 1, "control_vm": {"retained_disks": [{"disk": "spot-control-a"}]}}
            self.assertTrue(project.adopt(legacy, "state.json", persist=True))
        self.assertEqual(self.w.server.json(self.w.state_name)["state"]["control_vm"]["retained_disks"][0]["disk"],
                         "spot-control-a")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.local, self.local_lock = cv.project_control_paths(PROJECT, self.dir)

    def migrate(self, apply):
        with patch.object(gcs, "GcsLease", lambda client, name: self.w.lease("migrator")):
            return gcs.migrate_local_state(PROJECT, self.dir, URI, apply=apply, client=self.w.client,
                                           wall=lambda: self.w.clock[0])

    def write_local(self, state):
        self.local.write_text(json.dumps(state))

    def test_upload_once_then_local_backend_refuses(self):
        state = {"control_vm": {"transition": {"id": "t9", "step": "freeze"}, "history": [1]}}
        self.write_local(state)
        code, message = self.migrate(apply=False)
        self.assertEqual(code, 0)
        self.assertIn("dry run, nothing changed: fencing", message)
        self.assertIn("then uploading it to", message)
        self.assertEqual(self.w.server.writes(), [])
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 0, message)
        self.assertEqual(self.w.server.json(self.w.state_name)["state"], state)
        uploads = [c for c in self.w.server.writes() if c[1] == self.w.state_name]
        self.assertEqual([c[2]["ifGenerationMatch"] for c in uploads], ["0"])  # create-only
        marker = json.loads(self.local.read_text())
        self.assertEqual(marker[cv.CONTROL_STATE_MOVED_KEY], URI)
        self.assertEqual(json.loads((self.dir / marker["backup"]).read_text()), state)
        with self.assertRaisesRegex(cv.ControlStateConflict, "moved to gs://"):
            cv.ProjectControlState(self.local).load()
        with self.assertRaisesRegex(cv.ControlStateConflict, "SPOT_CONTROL_STATE_URI"):
            with cv.ProjectControlLock(PROJECT, self.dir):
                pass
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 0)
        self.assertIn("already migrated", message)

    def test_equal_states_only_mark_the_local_file(self):
        state = {"control_vm": {"history": [1]}}
        self.write_local(state)
        self.w.init_state(json.loads(json.dumps(state)))
        before = len(self.w.server.writes())
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 0, message)
        self.assertIn("already equals", message)
        self.assertEqual([c for c in self.w.server.writes()[before:] if c[1] == self.w.state_name], [])
        self.assertIn(cv.CONTROL_STATE_MOVED_KEY, json.loads(self.local.read_text()))

    def test_different_states_refuse_and_change_nothing(self):
        self.write_local({"control_vm": {"transition": {"id": "local"}}})
        self.w.init_state({"control_vm": {"transition": {"id": "remote"}}})
        remote_before = self.w.server.objects[self.w.state_name]
        for apply in (False, True):
            code, message = self.migrate(apply=apply)
            self.assertEqual(code, 1)
            self.assertIn("differ", message)
        self.assertEqual(self.w.server.objects[self.w.state_name], remote_before)
        self.assertEqual(json.loads(self.local.read_text())["control_vm"]["transition"]["id"], "local")

    def test_no_local_state_creates_an_empty_one_once(self):
        code, message = self.migrate(apply=False)
        self.assertIn("creating an empty", message)
        self.assertEqual(self.w.server.writes(), [])
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 0, message)
        self.assertEqual(self.w.server.json(self.w.state_name)["state"], {})
        self.assertIsNone(json.loads(self.local.read_text())["backup"])
        with self.w.lock("watchdog") as project:  # initialized: controllers may run now
            self.assertEqual(project.state, {})
        code, message = self.migrate(apply=True)
        self.assertIn("already migrated", message)

    def test_held_locks_refuse(self):
        self.write_local({"control_vm": {}})
        import fcntl
        with self.local_lock.open("a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, message = self.migrate(apply=True)
        self.assertEqual(code, 1)
        self.assertIn("local controller holds", message)
        self.assertTrue(self.w.lease("watchdog").acquire())
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 1)
        self.assertIn("holds the Cloud Storage lease", message)
        self.assertNotIn(self.w.state_name, self.w.server.objects)
        self.assertEqual(json.loads(self.local.read_text()), {"control_vm": {}})

    def test_the_local_fence_is_durable_before_the_remote_state_exists(self):
        state = {"control_vm": {"transition": {"id": "t9", "step": "freeze"}}}
        self.write_local(state)
        seen = []

        def before(method, name, query):
            if method == "POST" and name == self.w.state_name:
                kind, _ = gcs.read_local(self.local)
                backups = list(self.dir.glob(f"{self.local.name}.migrated-*"))
                seen.append((kind, [json.loads(b.read_text()) for b in backups]))
        self.w.server.before = before
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 0, message)
        self.assertEqual(seen, [("moved", [state])])

    def test_an_upload_that_fails_after_the_fence_resumes_from_the_backup(self):
        state = {"control_vm": {"transition": {"id": "t9"}}}
        self.write_local(state)
        real_put = self.w.client.put
        calls = []

        def put(name, data, *, if_generation_match):
            calls.append(name)
            if name == self.w.state_name and calls.count(name) == 1:
                raise gcs.GcsError("HTTP 403")  # the first state upload fails definitively
            return real_put(name, data, if_generation_match=if_generation_match)
        self.w.client.put = put
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 1)
        self.assertIn("stays fenced", message)
        self.assertNotIn(self.w.state_name, self.w.server.objects)
        with self.assertRaises(cv.ControlStateConflict):  # no local controller can use it meanwhile
            cv.ProjectControlState(self.local).load()
        code, message = self.migrate(apply=False)
        self.assertIn("uploading the fenced backup", message)
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 0, message)
        self.assertEqual(self.w.server.json(self.w.state_name)["state"], state)

    def test_a_failed_marker_install_leaves_the_original_in_place(self):
        state = {"control_vm": {"transition": {"id": "t9"}}}
        self.write_local(state)
        with patch.object(gcs.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.migrate(apply=True)
        self.assertEqual(json.loads(self.local.read_text()), state)  # never missing, never empty
        self.assertNotIn(self.w.state_name, self.w.server.objects)
        self.assertEqual(list(self.dir.glob(".*.moving")), [])
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 0, message)
        self.assertEqual(self.w.server.json(self.w.state_name)["state"], state)

    def test_a_missing_file_next_to_a_backup_is_never_empty_state(self):
        (self.dir / f"{self.local.name}.migrated-20261018T000000Z").write_text("{}")
        with self.assertRaisesRegex(cv.ControlStateConflict, "was migrated"):
            cv.ProjectControlState(self.local).load()
        with self.assertRaisesRegex(cv.ControlStateConflict, "was migrated"):
            with cv.ProjectControlLock(PROJECT, self.dir):
                pass

    def test_a_missing_local_file_next_to_remote_state_gets_fenced(self):
        self.w.init_state({"control_vm": {"transition": {"id": "remote"}}})
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 0, message)
        self.assertIn("fencing the local path", message)
        with self.assertRaisesRegex(cv.ControlStateConflict, "moved to gs://"):
            cv.ProjectControlState(self.local).load()
        self.assertEqual(self.w.server.json(self.w.state_name)["state"]["control_vm"]["transition"]["id"], "remote")

    def test_a_marker_for_another_location_refuses(self):
        self.write_local({"control_vm": {"history": [1]}})
        gcs.write_moved_marker(self.local, "gs://other-bucket/control", self.w.clock[0])
        for apply in (False, True):
            code, message = self.migrate(apply=apply)
            self.assertEqual(code, 1, message)
            self.assertIn("is fenced for gs://other-bucket/control", message)
        self.assertNotIn(self.w.state_name, self.w.server.objects)
        self.w.init_state()
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 1, message)  # also when the current location has state

    def test_a_marker_for_the_same_location_resumes_whatever_its_spelling(self):
        state = {"control_vm": {"history": [1]}}
        self.write_local(state)
        gcs.write_moved_marker(self.local, URI + "/", self.w.clock[0])
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 0, message)
        self.assertEqual(self.w.server.json(self.w.state_name)["state"], state)

    def test_a_missing_marker_next_to_a_backup_never_initializes_empty_state(self):
        (self.dir / f"{self.local.name}.migrated-20261018T000000Z").write_text('{"control_vm": {"history": [1]}}')
        for apply in (False, True):
            code, message = self.migrate(apply=apply)
            self.assertEqual(code, 1, message)
            self.assertIn("shows an earlier migration", message)
        self.assertNotIn(self.w.state_name, self.w.server.objects)
        self.assertFalse(self.local.exists())

    def test_marker_without_remote_state_refuses(self):
        self.local.write_text(json.dumps({cv.CONTROL_STATE_MOVED_KEY: URI}))
        code, message = self.migrate(apply=True)
        self.assertEqual(code, 1)
        self.assertIn("does not exist", message)


class BackendSelectionTests(unittest.TestCase):
    def test_uri_selects_cloud_storage(self):
        self.assertIsInstance(cv.open_project_control_lock(PROJECT, "/tmp", env={}), cv.ProjectControlLock)
        with patch.object(gcs, "AccessToken", lambda *a, **k: (lambda: TOKEN)):
            lock = cv.open_project_control_lock(PROJECT, None, env={cv.ENV_CONTROL_STATE_URI: URI})
        self.assertIsInstance(lock, gcs.GcsProjectControlLock)
        self.assertEqual(lock.lease.name, "control/control-vm-proj.lock")
        with self.assertRaises(ValueError):
            cv.open_project_control_lock(PROJECT, None, env={cv.ENV_CONTROL_STATE_URI: "gs://Bad"})

    def test_guard_runs_before_every_provider_call(self):
        calls = []

        def guard():
            calls.append("guard")
            raise gcs.LeaseLost("lost")
        with patch.object(cv, "default_gcloud", side_effect=lambda argv: calls.append("gcloud")), \
                patch.object(cv.edge_state, "default_requester", side_effect=lambda *a: calls.append("http")):
            manager = cv.build_manager(PROJECT, {}, lambda: None, guard=guard, env={
                "CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_KV_NAMESPACE_ID": "n", "CLOUDFLARE_KV_API_TOKEN": "t"})
            with self.assertRaises(gcs.LeaseLost):
                manager.gcloud(["gcloud", "compute", "instances", "list"])
            with self.assertRaises(gcs.LeaseLost):
                manager.requester("GET", "https://x", {}, None, 1)
            with self.assertRaises(gcs.LeaseLost):
                manager.kv.get("origin:active")
        self.assertEqual(calls, ["guard"] * 3)


class CliOnCloudStorageTests(unittest.TestCase):
    """src/control_vm.py with SPOT_CONTROL_STATE_URI: the job's command line, end to end on the fakes."""

    def setUp(self):
        self.w = World()
        import test_control_vm as tcv
        self.tcv = tcv
        self.w.init_state()
        patches = [patch.dict("os.environ", {cv.ENV_CONTROL_STATE_URI: URI}),
                   patch.object(gcs, "https_request", self.w.server),
                   patch.object(gcs, "AccessToken", lambda *a, **k: (lambda: TOKEN)),
                   patch("lib.environment.load_env")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, argv, h):
        seen = {}

        def factory(project, state, save, guard=None, **overrides):
            seen.setdefault("guards", []).append(guard)
            return cv.ControlVmManager(h.config, state, save, gcloud=h.gcloud, kv=h.kv, requester=h.http,
                                       offer_source=lambda: list(h.offers), clock=lambda: h.clock[0])
        out = io.StringIO()
        with redirect_stdout(out):
            code = cv.main([*argv, "--project", PROJECT, "--state-file", "/nonexistent/state.json"],
                           manager_factory=factory)
        return code, out.getvalue(), seen

    def healthy(self):
        h = self.tcv.Harness()
        h.gcloud.add("a")
        h.kv.set_origin("a")
        h.http.healthy("a")
        return h

    def test_ensure_apply_persists_to_cloud_storage_with_the_lease_guard(self):
        h = self.tcv.Harness()
        code, output, seen = self.run_cli(["ensure", "--apply"], h)
        self.assertEqual(code, 0, output)
        self.assertIn("Executed: bootstrap", output)
        self.assertEqual(self.w.server.json(self.w.state_name)["state"]["control_vm"]["transition"]["step"],
                         "wait_health")
        self.assertIsNotNone(seen["guards"][0])
        self.assertNotIn(self.w.lock_name, self.w.server.objects)

    def test_skip_if_idle_takes_no_lock_and_writes_nothing(self):
        h = self.healthy()
        code, output, _ = self.run_cli(["ensure", "--apply", "--skip-if-idle"], h)  # first run settles the state
        self.assertEqual(code, 0, output)
        writes = len(self.w.server.writes())
        self.assertGreater(writes, 0)
        code, output, _ = self.run_cli(["ensure", "--apply", "--skip-if-idle"], self.healthy())
        self.assertEqual(code, 0)
        self.assertIn("Idle (no lock taken, nothing changed): noop", output)
        self.assertEqual(len(self.w.server.writes()), writes)
        # Anything to do (the active VM went down) takes the lock and saves.
        down = self.tcv.Harness()
        down.gcloud.add("a", status="TERMINATED")
        down.kv.set_origin("a")
        code, output, _ = self.run_cli(["ensure", "--apply", "--skip-if-idle"], down)
        self.assertIn("Executed: start", output)
        self.assertGreater(len(self.w.server.writes()), writes)

    def test_skip_if_locked_exits_zero_and_changes_nothing(self):
        self.assertTrue(self.w.lease("operator-mac").acquire())
        before = self.w.server.objects[self.w.state_name]
        h = self.tcv.Harness()
        code, output, _ = self.run_cli(["ensure", "--apply", "--skip-if-locked"], h)
        self.assertEqual(code, 0)
        self.assertIn("Skipped: Another controller", output)
        self.assertIn("operator-mac", output)
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertEqual(self.w.server.objects[self.w.state_name], before)
        with self.assertRaisesRegex(RuntimeError, "Another controller"):
            self.run_cli(["ensure", "--apply"], h)

    def test_uninitialized_state_refuses_to_act(self):
        self.w.server.objects.pop(self.w.state_name)
        h = self.tcv.Harness()
        with self.assertRaisesRegex(cv.ControlStateConflict, "migrate-state --apply"):
            self.run_cli(["ensure", "--apply", "--skip-if-locked"], h)
        self.assertEqual(h.gcloud.mutating(), [])
        self.assertNotIn(self.w.lock_name, self.w.server.objects)

    def test_plan_and_status_read_without_the_lease(self):
        for command in ("plan", "status"):
            code, _, _ = self.run_cli([command], self.healthy())
            self.assertEqual(code, 0)
        self.assertEqual(self.w.server.writes(), [])

    def test_migrate_state_command(self):
        self.w.server.objects.pop(self.w.state_name)
        with tempfile.TemporaryDirectory() as tmp:
            local, _ = cv.project_control_paths(PROJECT, tmp)
            local.write_text(json.dumps({"control_vm": {"history": [1]}}))
            with patch.object(gcs, "GcsLease", lambda client, name: self.w.lease("migrator")):
                code, output, _ = self.run_cli(["migrate-state", "--control-state-dir", tmp], self.tcv.Harness())
                self.assertEqual(code, 0)
                self.assertIn("dry run", output)
                code, output, _ = self.run_cli(["migrate-state", "--apply", "--control-state-dir", tmp],
                                               self.tcv.Harness())
            self.assertEqual(code, 0, output)
            self.assertEqual(self.w.server.json(self.w.state_name)["state"], {"control_vm": {"history": [1]}})

    def test_autoscaler_uses_the_same_lease_and_previews_without_it(self):
        from lib import spot_runner_autoscaler as autoscaler
        seen = []

        def tick(args, cstate, save, guard=None):
            seen.append(guard)
            cstate.setdefault("control_vm", {})["transition"] = {"id": "t", "step": "wait_health"}
            save()
            return "planned"
        with patch("lib.control_vm.autoscaler_tick", side_effect=tick):
            args = SimpleNamespace(dry_run=True, project=PROJECT, control_state_dir=None)
            self.assertEqual(autoscaler.manage_control_vm(args, {}, Path("/nonexistent/s.json")), "planned")
            self.assertEqual(self.w.server.writes(), [])  # preview: no lease, no write
            args.dry_run = False
            self.assertEqual(autoscaler.manage_control_vm(args, {}, Path("/nonexistent/s.json")), "planned")
        self.assertIsNone(seen[0])
        self.assertIsNotNone(seen[1])
        self.assertEqual(self.w.server.json(self.w.state_name)["state"]["control_vm"]["transition"]["id"], "t")
        # Held by the watchdog: the autoscaler skips its control VM step and keeps managing runners.
        self.assertTrue(self.w.lease("watchdog").acquire())
        with patch("lib.control_vm.autoscaler_tick") as never, \
                self.assertLogs("spot_runner_autoscaler", "INFO") as logs:
            self.assertIsNone(autoscaler.manage_control_vm(args, {}, Path("/nonexistent/s.json")))
        never.assert_not_called()
        self.assertIn("another controller holds", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
