"""Monitoring endpoints and runner inventory of the tdd-org Spot poller (WI-1892)."""

import http.client
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

POLLER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "tdd-org-gcp" / "poller"


def load(name, filename, env):
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location(name, POLLER_DIR / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


ENV = {"GH_ORG": "Org", "REPOS": "vogt", "GCP_PROJECT": "p", "MODE": "ephemeral",
       "LARGE_TEMPLATE": "tl", "LARGE_LABELS": "rust-heavy,bigmem", "SMALL_TEMPLATE": "ts",
       "BUILD_SHA": "abc123"}
P = load("gha_spot_poller_monitor_test", "gha_spot_poller.py", ENV)
M = load("runner_monitor_under_test", "runner_monitor.py", ENV)
T0 = P.gh_time("2026-10-19T00:00:00Z")


def job_payload(action, job_id, **job):
    return {"action": action, "repository": {"name": "vogt", "owner": {"login": "Org"}},
            "workflow_job": dict({"id": job_id, "labels": ["self-hosted", "gcp-rust"],
                                  "created_at": "2026-10-19T00:00:00Z"}, **job)}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Fixture(unittest.TestCase):
    def setUp(self):
        saved = dict(P._intake)
        self.addCleanup(lambda: P._intake.update(saved))
        self.clock = Clock(T0 + 600)
        self.table = P.JobTable(P.CLASSES, now=T0 - 3600)
        P._intake.update(table=self.table, silent=False)
        self.monitor = M.Monitor(P, clock=self.clock)
        self.table.observer = self.monitor.job_event


class ClassTests(unittest.TestCase):
    def test_gcp_runners_by_name_prefix_and_static_by_label(self):
        self.assertEqual(M.runner_class("gha-rh-20261019-1", ["self-hosted", "bigmem"], P.CLASSES), "gcp-large")
        self.assertEqual(M.runner_class("gha-rs-20261019-1", ["gcp-rust"], P.CLASSES), "gcp-small")
        self.assertEqual(M.runner_class("nodeb-bigmem", ["self-hosted", "bigmem"], P.CLASSES), "static-bigmem")
        self.assertEqual(M.runner_class("nodeb-pub-1", ["self-hosted", "publish"], P.CLASSES), "static-publish")
        self.assertEqual(M.runner_class("nodeb-rust", ["self-hosted", "Linux", "rust"], P.CLASSES), "static-rust")
        self.assertEqual(M.runner_class("desk", ["self-hosted", "wsl2-rust"], P.CLASSES), "wsl")
        self.assertEqual(M.runner_class("x", ["self-hosted"], P.CLASSES), "other")

    def test_state(self):
        self.assertEqual(M.runner_state({"status": "offline", "busy": True}), "offline")
        self.assertEqual(M.runner_state({"status": "online", "busy": True}), "busy")
        self.assertEqual(M.runner_state({"status": "online", "busy": False}), "idle")


class HistogramTests(unittest.TestCase):
    def test_cumulative_buckets(self):
        h = M.Histogram((10, 60))
        h.observe({"class": "small"}, 5)
        h.observe({"class": "small"}, 30)
        h.observe({"class": "small"}, 100)
        lines = h.render("x")
        self.assertIn('x_bucket{class="small",le="10"} 1', lines)
        self.assertIn('x_bucket{class="small",le="60"} 2', lines)
        self.assertIn('x_bucket{class="small",le="+Inf"} 3', lines)
        self.assertIn('x_count{class="small"} 3', lines)
        self.assertIn('x_sum{class="small"} 135.0', lines)


class MetricsTests(Fixture):
    def test_queue_wait_duration_and_running_runner(self):
        self.table.apply(job_payload("queued", 1), now=T0)
        self.table.apply(job_payload("queued", 2, created_at="2026-10-19T00:05:00Z"), now=T0)
        text = self.monitor.metrics()
        self.assertIn('runner_manager_jobs_queued{class="gcp-small",repo="vogt",tier="normal"} 2', text)
        self.assertIn('runner_manager_job_oldest_wait_seconds{class="gcp-small",tier="normal"} 600', text)
        self.table.apply(job_payload("in_progress", 1, started_at="2026-10-19T00:01:30Z",
                                     runner_name="gha-rs-1"), now=T0 + 90)
        self.assertEqual(self.monitor.running, {"gha-rs-1": "vogt#1"})
        self.table.apply(job_payload("completed", 1, started_at="2026-10-19T00:01:30Z",
                                     completed_at="2026-10-19T00:11:30Z", runner_name="gha-rs-1"), now=T0 + 690)
        self.assertEqual(self.monitor.running, {})
        text = self.monitor.metrics()
        self.assertIn('runner_manager_job_wait_seconds_count{class="gcp-small",tier="normal"} 1', text)
        self.assertIn('runner_manager_job_wait_seconds_sum{class="gcp-small",tier="normal"} 90', text)
        self.assertIn('runner_manager_job_duration_seconds_sum{class="gcp-small",repo="vogt"} 600', text)
        self.assertIn('runner_manager_jobs_queued{class="gcp-small",repo="vogt",tier="normal"} 1', text)

    def test_gcp_runner_webhook_and_build_series(self):
        self.monitor.set_gcp(vms=[{"class": "small", "status": "RUNNING", "age": 300},
                                  {"class": "small", "status": "TERMINATED", "age": 900}],
                             vcpu_used=8, month_hours=12.5, budget_locked=True)
        self.monitor.set_runners([
            {"name": "gha-rs-1", "status": "online", "busy": True, "labels": [{"name": "gcp-rust"}]},
            {"name": "nodeb-pub-1", "status": "offline", "busy": False, "labels": [{"name": "publish"}]}], rate=4900)
        self.table.first_delivery("d1", now=T0 + 500)
        with mock.patch.dict(P._rate, remaining=4100):
            text = self.monitor.metrics()
        for line in ('runner_manager_vms{class="small",status="RUNNING"} 1',
                     "runner_manager_vm_oldest_age_seconds 900",
                     "runner_manager_vcpu_used 8", "runner_manager_vcpu_quota 32",
                     "runner_manager_month_vm_hours 12.5", "runner_manager_month_vm_hours_cap 250",
                     "runner_manager_budget_locked 1",
                     'runner_manager_runners{class="gcp-small",state="busy"} 1',
                     'runner_manager_runners{class="static-publish",state="offline"} 1',
                     'runner_manager_runners{class="static-publish",state="idle"} 0',
                     'runner_manager_runner_offline{class="static-publish",name="nodeb-pub-1"} 1',
                     "runner_manager_inventory_up 1",
                     'runner_manager_github_rate_remaining{credential="poller"} 4100',
                     'runner_manager_github_rate_remaining{credential="readonly"} 4900',
                     "runner_manager_webhook_last_seconds 100", "runner_manager_webhook_silent 0",
                     "runner_manager_deliveries_total 1",
                     'runner_manager_build_info{sha="abc123"} 1'):
            self.assertIn(line, text)

    def test_late_in_progress_does_not_mark_a_finished_job_running(self):
        self.table.apply(job_payload("completed", 3, runner_name="gha-rs-3"), now=T0)
        self.table.apply(job_payload("in_progress", 3, runner_name="gha-rs-3"), now=T0 + 1)
        self.assertEqual(self.monitor.running, {})

    def test_idle_runner_in_the_inventory_drops_a_stale_current_job(self):
        self.table.apply(job_payload("in_progress", 4, runner_name="gha-rs-4"), now=T0)
        self.monitor.set_runners([{"name": "gha-rs-4", "status": "online", "busy": False, "labels": []}])
        self.assertIsNone(self.monitor.runner_rows()[0]["current_job"])
        self.assertEqual(self.monitor.running, {})

    def test_counters_start_from_zero_after_a_restore(self):
        self.table.restore({"deliveries_total": 5000})
        self.table.first_delivery("d9", now=T0)
        self.assertIn("runner_manager_deliveries_total 1\n", self.monitor.metrics())

    def test_one_help_and_type_per_family_and_exact_values(self):
        self.table.apply(job_payload("in_progress", 5, started_at="2026-10-19T00:01:00Z"), now=T0)
        self.monitor.set_gcp(month_hours=1234567.25)
        text = self.monitor.metrics()
        types = [l.split()[2] for l in text.splitlines() if l.startswith("# TYPE")]
        helps = [l.split()[2] for l in text.splitlines() if l.startswith("# HELP")]
        self.assertEqual(len(types), len(set(types)))
        self.assertEqual(sorted(types), sorted(helps))
        self.assertIn("runner_manager_month_vm_hours 1234567.25", text)
        self.assertEqual(M.num(1234567), "1234567")

    def test_label_values_are_escaped(self):
        self.assertEqual(M.labels_text({"a": 'x"y\\z'}), '{a="x\\"y\\\\z"}')


class HealthTests(Fixture):
    def test_healthy_until_three_intervals_pass_without_a_loop(self):
        self.monitor.loop_done()
        self.clock.now += 3 * P.INTERVAL
        self.assertTrue(self.monitor.healthy())
        self.clock.now += 60
        self.assertFalse(self.monitor.healthy())


class InventoryTests(unittest.TestCase):
    def test_loop_survives_any_error_and_inventory_goes_stale(self):
        clock = Clock(T0)
        monitor = M.Monitor(P, clock=clock)
        monitor.set_runners([])
        stop = mock.MagicMock()
        calls = []

        def wait(_):
            calls.append(1)
            stop.is_set.return_value = len(calls) >= 2

        stop.is_set.return_value = False
        stop.wait.side_effect = wait
        with mock.patch.object(M, "ro_token", side_effect=RuntimeError("openssl failed")), \
                mock.patch.object(P, "log"):
            M.inventory_loop(monitor, P, stop)
        self.assertEqual(len(calls), 2)
        self.assertEqual(monitor.inventory_error, "openssl failed")
        self.assertIn("runner_manager_inventory_up 0", monitor.metrics())
        monitor.set_runners([])
        clock.now += 4 * M.INVENTORY_INTERVAL
        self.assertIn("runner_manager_inventory_up 0", monitor.metrics())

    def test_pages_until_a_short_page(self):
        pages = [[{"name": f"r{i}"} for i in range(100)], [{"name": "last"}]]
        seen = []

        def opener(req, timeout):
            seen.append(req.full_url)
            resp = mock.MagicMock()
            resp.__enter__.return_value = resp
            resp.headers = {"X-RateLimit-Remaining": "4800"}
            resp.read.return_value = json.dumps({"runners": pages[len(seen) - 1]}).encode()
            return resp

        with mock.patch.object(M.json, "load", side_effect=lambda r: json.loads(r.read())):
            runners, remaining = M.list_runners(P, "tok", opener=opener)
        self.assertEqual(len(runners), 101)
        self.assertEqual(remaining, 4800)
        self.assertIn("orgs/Org/actions/runners?per_page=100&page=2", seen[1])


class EndpointTests(Fixture):
    def setUp(self):
        super().setUp()
        with mock.patch.object(P, "log"):
            self.server, self.stop = M.start(self.monitor, P, bind="127.0.0.1", port=0, inventory=False)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp.status, body

    def test_endpoints(self):
        self.table.apply(job_payload("queued", 7), now=T0)
        self.monitor.set_runners([{"name": "nodeb-rust", "status": "online", "busy": False,
                                   "labels": [{"name": "self-hosted"}, {"name": "rust"}]}])
        status, body = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("# TYPE runner_manager_job_wait_seconds histogram", body)
        status, body = self.get("/api/v1/queue")
        self.assertEqual(json.loads(body)["jobs"][0]["ref"], "vogt#7")
        status, body = self.get("/api/v1/runners")
        row = json.loads(body)["runners"][0]
        self.assertEqual((row["name"], row["class"], row["state"]), ("nodeb-rust", "static-rust", "idle"))
        self.assertEqual(self.get("/healthz")[0], 200)
        self.clock.now += 10 * P.INTERVAL
        self.assertEqual(self.get("/healthz")[0], 503)
        self.assertEqual(self.get("/webhook/github")[0], 404)


if __name__ == "__main__":
    unittest.main()
