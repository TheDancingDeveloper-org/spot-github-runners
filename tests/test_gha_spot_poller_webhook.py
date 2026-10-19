"""Webhook intake and reconcile cadence of the tdd-org Spot poller (WI-1891)."""

import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).resolve().parents[1] / "deploy" / "tdd-org-gcp" / "poller" / "gha_spot_poller.py"


def load_poller():
    env = {"GH_ORG": "Org", "REPOS": "vogt,rustTorrent", "GCP_PROJECT": "p"}
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("gha_spot_poller_under_test", POLLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


P = load_poller()
CLASSES = {"large": {"labels": ["rust-heavy", "bigmem"]}, "small": {"labels": ["gcp-rust"]}}
SECRET = b"s3cret"


def payload(action, job_id, labels=("self-hosted", "gcp-rust"), repo="vogt", owner="Org",
            created="2026-10-19T00:00:00Z"):
    return {"action": action, "repository": {"name": repo, "owner": {"login": owner}},
            "workflow_job": {"id": job_id, "labels": list(labels), "created_at": created}}


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


T0 = P.gh_time("2026-10-19T00:00:00Z")


class SignatureTests(unittest.TestCase):
    def test_accepts_only_the_matching_hmac(self):
        body = b'{"a":1}'
        self.assertTrue(P.verify_signature(SECRET, body, sign(body)))
        self.assertFalse(P.verify_signature(SECRET, body + b" ", sign(body)))
        self.assertFalse(P.verify_signature(b"other", body, sign(body)))
        self.assertFalse(P.verify_signature(SECRET, body, None))
        self.assertFalse(P.verify_signature(SECRET, body, "sha1=" + sign(body)[7:]))
        self.assertFalse(P.verify_signature(b"", body, sign(body, b"")))
        self.assertFalse(P.verify_signature(SECRET, body, "sha256=\u00e9"))


class JobTableTests(unittest.TestCase):
    def setUp(self):
        self.table = JobTableFactory()

    def test_queued_then_started_job_leaves_the_queue(self):
        self.assertTrue(self.table.apply(payload("queued", 1), now=T0))
        self.assertFalse(self.table.apply(payload("queued", 1), now=T0))
        self.assertEqual(self.table.pending(), [("vogt#1", "small")])
        self.table.apply(payload("in_progress", 1), now=T0 + 5)
        self.assertEqual(self.table.pending(), [])

    def test_late_queued_delivery_does_not_revive_a_started_job(self):
        self.table.apply(payload("completed", 2), now=T0)
        self.assertFalse(self.table.apply(payload("queued", 2), now=T0 + 1))
        self.assertEqual(self.table.pending(), [])

    def test_ignores_other_orgs_unknown_repos_and_unserved_labels(self):
        self.assertFalse(self.table.apply(payload("queued", 3, owner="Elsewhere"), now=T0))
        self.assertFalse(self.table.apply(payload("queued", 4, repo="secret-repo"), now=T0))
        self.assertFalse(self.table.apply(payload("queued", 5, labels=("self-hosted",)), now=T0))
        self.assertFalse(self.table.apply(payload("queued", 6, labels=("self-hosted", "publish")), now=T0))
        self.assertEqual(self.table.pending(), [])

    def test_repo_name_matches_case_insensitively_and_keeps_the_configured_ref(self):
        self.table.apply(payload("queued", 7, repo="rusttorrent", labels=("rust-heavy",)), now=T0)
        self.assertEqual(self.table.pending(), [("rustTorrent#7", "large")])

    def test_pending_is_oldest_first(self):
        self.table.apply(payload("queued", 9, created="2026-10-19T00:05:00Z"), now=T0)
        self.table.apply(payload("queued", 8, created="2026-10-19T00:01:00Z"), now=T0)
        self.assertEqual([r for r, _ in self.table.pending()], ["vogt#8", "vogt#9"])

    def test_duplicate_delivery_ids_are_dropped(self):
        self.assertTrue(self.table.first_delivery("d1", now=T0))
        self.assertFalse(self.table.first_delivery("d1", now=T0))
        self.assertEqual(self.table.snapshot()["duplicate_deliveries_total"], 1)

    def test_delivery_memory_is_bounded(self):
        self.table.DELIVERY_MEMORY = 3
        for i in range(5):
            self.table.first_delivery(f"d{i}", now=T0)
        self.assertEqual(list(self.table.deliveries), ["d2", "d3", "d4"])


def JobTableFactory(started=T0 - 3600):
    return P.JobTable(CLASSES, now=started)


class ReconcileTests(unittest.TestCase):
    def test_counts_an_old_unannounced_job_as_missed_once(self):
        table = JobTableFactory()
        found = [("vogt#10", "small", T0)]
        self.assertEqual(table.reconcile(found, started=T0 + 300, now=T0 + 300), ["vogt#10"])
        self.assertEqual(table.reconcile(found, started=T0 + 600, now=T0 + 600), [])
        self.assertEqual(table.snapshot()["missed_webhooks_total"], 1)
        self.assertEqual(table.pending(), [("vogt#10", "small")])

    def test_young_or_announced_jobs_are_not_missed(self):
        table = JobTableFactory()
        table.apply(payload("queued", 11), now=T0 + 1)
        found = [("vogt#11", "small", T0), ("vogt#12", "small", T0 + 250)]
        self.assertEqual(table.reconcile(found, started=T0 + 300, now=T0 + 300), [])
        self.assertEqual(table.snapshot().get("missed_webhooks_total", 0), 0)

    def test_jobs_queued_before_the_intake_started_are_not_missed(self):
        table = JobTableFactory(started=T0 + 100)
        self.assertEqual(table.reconcile([("vogt#13", "small", T0)], started=T0 + 400, now=T0 + 400), [])

    def test_listing_drops_stale_entries_but_keeps_deliveries_newer_than_it(self):
        table = JobTableFactory()
        table.apply(payload("queued", 14), now=T0)          # before the listing started
        table.apply(payload("queued", 15), now=T0 + 310)    # arrived while it ran
        table.reconcile([], started=T0 + 300, now=T0 + 320)
        self.assertEqual(table.pending(), [("vogt#15", "small")])

    def test_queued_runs_listing_keeps_jobs_of_started_runs(self):
        table = JobTableFactory()
        table.apply(payload("queued", 17), now=T0)  # e.g. a matrix job waiting for quota
        table.reconcile([], started=T0 + 300, now=T0 + 300, complete=False)
        self.assertEqual(table.pending(), [("vogt#17", "small")])

    def test_young_listed_job_is_checked_again_and_counted_once_old(self):
        table = JobTableFactory()
        found = [("vogt#18", "small", T0)]
        self.assertEqual(table.reconcile(found, started=T0 + 60, now=T0 + 60), [])
        self.assertEqual(table.reconcile(found, started=T0 + 360, now=T0 + 360), ["vogt#18"])
        self.assertEqual(table.reconcile(found, started=T0 + 660, now=T0 + 660), [])

    def test_listing_does_not_requeue_a_started_job(self):
        table = JobTableFactory()
        table.apply(payload("in_progress", 16), now=T0 + 290)
        table.reconcile([("vogt#16", "small", T0)], started=T0 + 280, now=T0 + 300)
        self.assertEqual(table.pending(), [])


class CadenceTests(unittest.TestCase):
    def test_reconciles_every_300_s_while_deliveries_flow(self):
        self.assertEqual(P.reconcile_cadence(1000, 900, 990, True), (False, False))
        self.assertEqual(P.reconcile_cadence(1200, 900, 990, True), (True, False))

    def test_falls_back_to_the_poll_interval_after_15_silent_minutes_with_jobs_queued(self):
        self.assertEqual(P.reconcile_cadence(2000, 1930, 2000 - 900, True), (True, True))
        self.assertEqual(P.reconcile_cadence(2000, 1950, 2000 - 900, True), (False, True))

    def test_silence_with_nothing_queued_is_not_a_fallback(self):
        self.assertEqual(P.reconcile_cadence(5000, 4800, 0, False), (False, False))


class PendingJobsTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(P._intake)
        self.addCleanup(lambda: P._intake.update(self.saved))

    def test_without_webhooks_lists_github_every_poll(self):
        P._intake["table"] = None
        with mock.patch.object(P, "queued_jobs", return_value=[("vogt#1", "small")]) as listing:
            self.assertEqual(P.pending_jobs(CLASSES), [("vogt#1", "small")])
        listing.assert_called_once()

    def test_with_webhooks_lists_only_when_the_reconcile_is_due(self):
        table = JobTableFactory()
        table.apply(payload("queued", 20), now=T0)
        P._intake.update(table=table, last_listing=0.0, last_in_progress=0.0, queued_seen=False, silent=False)
        table.last_delivery = T0
        with mock.patch.object(P, "scan_jobs", return_value=[("vogt#20", "small", T0)]) as scan, \
                mock.patch.object(P.time, "time", return_value=T0 + 10):
            self.assertEqual(P.pending_jobs(CLASSES), [("vogt#20", "small")])
            self.assertEqual(P.pending_jobs(CLASSES), [("vogt#20", "small")])
        scan.assert_called_once_with(CLASSES, ["queued", "in_progress"])  # first listing is complete
        with mock.patch.object(P, "scan_jobs", return_value=[]) as scan, \
                mock.patch.object(P.time, "time", return_value=T0 + 400):
            self.assertEqual(P.pending_jobs(CLASSES), [("vogt#20", "small")])  # partial listing keeps it
        scan.assert_called_once_with(CLASSES, ["queued"])

    def test_silent_webhooks_add_the_in_progress_scan(self):
        table = JobTableFactory()
        table.last_delivery = T0 - 2000
        P._intake.update(table=table, last_listing=T0 - 61, last_in_progress=0.0, queued_seen=True, silent=False)
        with mock.patch.object(P, "scan_jobs", return_value=[]) as scan, \
                mock.patch.object(P.time, "time", return_value=T0):
            P.pending_jobs(CLASSES)
        scan.assert_called_once_with(CLASSES, ["queued", "in_progress"])
        self.assertTrue(P._intake["silent"])

    def test_rate_limited_reconcile_keeps_serving_the_table(self):
        table = JobTableFactory()
        table.apply(payload("queued", 21), now=T0)
        P._intake.update(table=table, last_listing=0.0, queued_seen=False, silent=False)
        with mock.patch.object(P, "scan_jobs", side_effect=P.RateLimited(T0 + 60)):
            self.assertEqual(P.pending_jobs(CLASSES), [("vogt#21", "small")])
        self.assertEqual(P._intake["last_listing"], 0.0)


class WebhookServerTests(unittest.TestCase):
    def setUp(self):
        self.table = JobTableFactory()
        self.wake = threading.Event()
        with mock.patch.object(P, "log"):
            self.server = P.start_webhook_server(self.table, SECRET, self.wake, bind="127.0.0.1", port=0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def post(self, body, path="/webhook/github", event="workflow_job", delivery="d-1", signature=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "Content-Type": "application/json"}
        if signature is not False:
            headers["X-Hub-Signature-256"] = signature or sign(body)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_signed_queued_delivery_queues_the_job_and_wakes_the_loop(self):
        body = json.dumps(payload("queued", 30)).encode()
        self.assertEqual(self.post(body), 200)
        self.assertEqual(self.table.pending(), [("vogt#30", "small")])
        self.assertTrue(self.wake.is_set())

    def test_unsigned_or_wrongly_signed_deliveries_are_refused(self):
        body = json.dumps(payload("queued", 31)).encode()
        with mock.patch.object(P, "log"):
            self.assertEqual(self.post(body, signature=False), 401)
            self.assertEqual(self.post(body, signature=sign(body, b"wrong")), 401)
        self.assertEqual(self.table.pending(), [])
        self.assertEqual(self.table.snapshot()["rejected_deliveries_total"], 2)
        self.assertFalse(self.wake.is_set())

    def test_redelivery_is_acknowledged_but_not_applied_twice(self):
        body = json.dumps(payload("queued", 32)).encode()
        self.assertEqual(self.post(body, delivery="same"), 200)
        self.table.apply(payload("in_progress", 32))
        self.assertEqual(self.post(body, delivery="same"), 200)
        self.assertEqual(self.table.pending(), [])

    def test_other_paths_methods_and_events(self):
        body = b"{}"
        self.assertEqual(self.post(body, path="/webhook/github/x"), 404)
        self.assertEqual(self.post(body, path="/"), 404)
        self.assertEqual(self.post(body, event="push", delivery="d-2"), 202)
        with mock.patch.object(P, "log"):
            self.assertEqual(self.post(body, event="ping", delivery="d-3"), 200)
        self.assertEqual(self.post(b"[1]", delivery="d-4"), 400)
        bad = json.dumps({"action": "queued", "repository": {"name": "vogt", "owner": {"login": "Org"}},
                          "workflow_job": {"id": 1, "labels": 5}}).encode()
        self.assertEqual(self.post(bad, delivery="d-5"), 400)
        self.assertTrue(self.table.first_delivery("d-5"))  # a fixed redelivery is not a duplicate
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/webhook/github")
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()

    def test_oversized_bodies_are_refused_before_reading(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/webhook/github")
        conn.putheader("Content-Length", str(P.WEBHOOK_MAX_BODY + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()


if __name__ == "__main__":
    unittest.main()
