"""Runner floor keeper: configuration, credentials, planner decisions, the per-target lease, the multi-tick launch
state machine, the time budget, and the autoscaler's opt-in lease.

Every provider is a fake: Cloud Storage is the in-memory FakeGcsServer, GitHub and Compute Engine are fakes below,
and no gcloud, gh, or SSH process runs (PATH points at refusing stubs in CI).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gcs_fixtures import BUCKET, TOKEN, FakeGcsServer
from lib import gcs_control_state as gcs
from lib import runner_floor as rf
from lib.https_transport import HttpResponse
from lib.runner_fleet import GitHubRunner
from lib.target_config import target_label_value

PROJECT = "proj"
URI = f"gs://{BUCKET}/runner-floor"
NOW = 1_800_000_000.0
TARGETS_TEXT = ("swack-tools:secret=spot-floor-github-swackhamer,account=swackhamer,group=10;"
                "Appz4Fun:secret=spot-floor-github-xbmc4lyfe,account=xbmc4lyfe;"
                "xbmc4lyfe/newznab-tmux:secret=spot-floor-github-xbmc4lyfe,account=xbmc4lyfe,labels=ubuntu-latest")


def config(text: str = TARGETS_TEXT, **env) -> rf.FloorConfig:
    return rf.FloorConfig.from_env(PROJECT, {rf.ENV_TARGETS: text, rf.ENV_STATE_URI: URI, **env})


def target(name: str = "xbmc4lyfe/newznab-tmux") -> rf.FloorTarget:
    return config().target(name)


def gce_time(epoch: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="milliseconds")


def host(name="oxidex-runners-uscentral1-a-abcd-x1", zone="us-central1-a", status="RUNNING", instance_id="111",
         owner="xbmc4lyfe/newznab-tmux", startup_script=False, machine_type="n2d-standard-4", spot=True,
         started=NOW - 60) -> rf.Host:
    return rf.Host(name, zone, status, instance_id, machine_type, spot,
                   target_label_value(owner) if owner else "", startup_script,
                   gce_time(started) if started is not None else "")


def runner(name, status="online", busy=False, labels=("self-hosted", "Linux", "X64", "spot", "ubuntu-latest"),
           runner_id=None) -> GitHubRunner:
    return GitHubRunner(runner_id or abs(hash(name)) % 10_000_000, name, status, busy, tuple(labels))


def own_record(h: rf.Host, **extra) -> dict:
    return {"host": {"name": h.name, "zone": h.zone, "instance_id": h.instance_id,
                     "runner_names": [f"gha-{h.name}-1"], "labels": ["spot", "n2d-standard-4", "us-central1",
                                                                     "ubuntu-latest"]}, **extra}


# --------------------------------------------------------------------------------------------
# Configuration and credentials
# --------------------------------------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_the_three_floor_targets(self):
        cfg = config()
        self.assertEqual([t.name for t in cfg.targets], ["swack-tools", "Appz4Fun", "xbmc4lyfe/newznab-tmux"])
        swack, appz, newznab = cfg.targets
        self.assertEqual((swack.secret, swack.account, swack.group_id), ("spot-floor-github-swackhamer", "swackhamer", 10))
        self.assertIsNone(appz.group_id)  # the Default group: no oxidex-only group exists there
        self.assertEqual(newznab.labels, ("ubuntu-latest",))
        self.assertTrue(newznab.is_repo)
        self.assertEqual(newznab.custom_labels("n2d-standard-4", "us-central1"),
                         ("spot", "n2d-standard-4", "us-central1", "ubuntu-latest"))
        self.assertEqual(cfg.regions, rf.DEFAULT_REGIONS)
        self.assertEqual(cfg.max_daily_usd, Decimal("1.50"))
        self.assertEqual(cfg.token_source, rf.TOKEN_SOURCE_GH)

    def test_refusals(self):
        bad = {
            "swack-tools": "needs ':secret=",
            "swack-tools:account=x": "Secret Manager secret name",
            "swack-tools:secret=ghp_abcdefghijklmnopqrstuvwxyz0123456789": "never hold a token",
            "swack-tools:secret=github_pat_11AAAAAAA": "never hold a token",
            "o/r:secret=s,group=3": "no runner groups",
            "swack-tools:secret=s,group=0": "positive runner group",
            "swack-tools:secret=s;SWACK-TOOLS:secret=t": "listed twice",
            "swack-tools:secret=s,colour=red": "must be one of",
            "swack-tools:secret=s,secret=t": "given twice",
            "swack-tools:secret=s,labels=bad label": "not a valid runner label",
            "swack-tools:secret=s,labels=self-hosted": "built-in label",
            "swack-tools:secret=s,ram=2": "from 4 to",
            "swack-tools:secret=s,ram=64": "from 4 to",
            "a/b/c:secret=s": "owner/repo",
            "-bad:secret=s": "organization name",
        }
        for text, message in bad.items():
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                rf.parse_targets(text)
            self.assertIn(message, str(caught.exception))
        for env, message in (({rf.ENV_REGIONS: "us central1"}, "invalid region"),
                             ({rf.ENV_MAX_DAILY_USD: "0"}, "above 0"), ({rf.ENV_MAX_DAILY_USD: "x"}, "dollars"),
                             ({rf.ENV_TOKEN_SOURCE: "env"}, rf.TOKEN_SOURCE_FILES),
                             ({rf.ENV_STATE_URI: "s3://x"}, "gs://")):
            with self.subTest(env=env), self.assertRaises(ValueError) as caught:
                rf.FloorConfig.from_env(PROJECT, {rf.ENV_TARGETS: "swack-tools:secret=s", **env})
            self.assertIn(message, str(caught.exception))

    def test_unknown_target_and_empty_config(self):
        self.assertEqual(rf.parse_targets(" ; "), ())
        with self.assertRaises(rf.FloorError):
            config().target("someone-else")
        self.assertEqual(config().target("XBMC4LYFE/NEWZNAB-TMUX").name, "xbmc4lyfe/newznab-tmux")


class TokenSourceTests(unittest.TestCase):
    def test_secret_files_read_only_the_mounted_file_never_the_environment(self):
        t = target()
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
                "GH_TOKEN": "ghp_from_env", "GITHUB_TOKEN": "ghp_env2",
                "SPOT_FLOOR_GITHUB_XBMC4LYFE": "ghp_env3", t.secret: "ghp_env4"}):
            source = rf.TokenSource(rf.TOKEN_SOURCE_FILES, root=root)
            with self.assertRaises(rf.FloorError) as caught:
                source(t)  # not mounted: no environment fallback
            self.assertIn("not mounted", str(caught.exception))
            path = Path(root, t.secret, "token")
            path.parent.mkdir()
            path.write_text("gho_mounted\n")
            self.assertEqual(source(t), "gho_mounted")
            path.write_text("gho_rotated\n")
            self.assertEqual(source(t), "gho_mounted")  # cached for a few minutes
            path.write_text("two words")
            fresh = rf.TokenSource(rf.TOKEN_SOURCE_FILES, root=root)
            with self.assertRaises(rf.FloorError):
                fresh(t)

    def test_gh_mode_asks_gh_for_exactly_the_account(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="gho_token\n", stderr="")
        with patch.dict(os.environ, {"GH_TOKEN": "ghp_wrong_account"}):
            self.assertEqual(rf.TokenSource(rf.TOKEN_SOURCE_GH, runner=run)(target("swack-tools")), "gho_token")
            rf.TokenSource(rf.TOKEN_SOURCE_GH, runner=run, account_override="other")(target("swack-tools"))
        self.assertEqual(calls, [["gh", "auth", "token", "--user", "swackhamer"],
                                 ["gh", "auth", "token", "--user", "other"]])
        no_account = rf.parse_targets("swack-tools:secret=s")[0]
        with self.assertRaises(rf.FloorError):
            rf.TokenSource(rf.TOKEN_SOURCE_GH, runner=run)(no_account)

    def test_the_module_never_reads_a_token_from_the_environment(self):
        source = Path(rf.__file__).read_text()
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "getenv("):
            self.assertNotIn(name, source)


# --------------------------------------------------------------------------------------------
# GitHub and Compute Engine clients
# --------------------------------------------------------------------------------------------

def github_page(runners, total=None):
    return {"total_count": len(runners) if total is None else total,
            "runners": [{"id": r.runner_id, "name": r.name, "status": r.status, "busy": r.busy,
                         "labels": [{"name": label} for label in r.labels]} for r in runners]}


class ClientTests(unittest.TestCase):
    def transport(self, responses, calls):
        def send(method, url, headers, body, timeout):
            calls.append((method, url, headers, body))
            status, payload = responses.pop(0)
            return HttpResponse(status, {}, json.dumps(payload).encode() if payload is not None else b"")
        return send

    def test_runner_inventory_is_strict_and_paginated(self):
        calls = []
        many = [runner(f"r{i}", runner_id=i + 1) for i in range(150)]
        client = rf.GitHubClient(lambda: "tok", transport=self.transport(
            [(200, github_page(many[:100], 150)), (200, github_page(many[100:], 150))], calls))
        self.assertEqual(len(client.list_runners(target().target)), 150)
        self.assertIn("repos/xbmc4lyfe/newznab-tmux/actions/runners?per_page=100&page=2", calls[1][1])
        self.assertEqual(calls[0][2]["Authorization"], "Bearer tok")
        for pages in ([(200, github_page(many[:100], 150)), (200, github_page([], 150))],
                      [(200, {"total_count": 1, "runners": [{"id": 1}]})],
                      [(200, github_page(many[:100], 150)), (200, github_page(many[100:], 151))],
                      [(502, None)]):
            with self.subTest(pages=len(pages)), self.assertRaises(rf.GitHubError):
                rf.GitHubClient(lambda: "tok", transport=self.transport(list(pages), [])).list_runners(target().target)

    def test_mutations_run_the_guard_first(self):
        order = []
        calls = []
        client = rf.GitHubClient(lambda: "tok", transport=self.transport([(201, {"token": "reg"})], calls),
                                 guard=lambda: order.append("guard"))
        self.assertEqual(client.registration_token(target("Appz4Fun").target), "reg")
        self.assertEqual(order, ["guard"])
        self.assertTrue(calls[0][1].endswith("orgs/Appz4Fun/actions/runners/registration-token"))

    def test_compute_inventory_never_accepts_a_partial_answer(self):
        item = {"name": "oxidex-runners-a", "zone": "https://x/zones/us-central1-a", "status": "RUNNING", "id": "9",
                "machineType": "https://x/machineTypes/n2d-standard-4", "scheduling": {"provisioningModel": "SPOT"},
                "labels": {"spot-target": "k"}, "metadata": {"items": [{"key": "startup-script", "value": "x"}]},
                "lastStartTimestamp": "2026-10-18T10:00:00.000-07:00"}
        other = dict(item, name="spot-control-a")
        ok = {"items": {"zones/us-central1-a": {"instances": [item, other]},
                        "zones/us-east1-b": {"warning": {"code": "NO_RESULTS_ON_PAGE"}}}}
        inventory = rf.ComputeInventory(PROJECT, token=lambda: "t", transport=self.transport([(200, ok)], []))
        [found] = inventory.list_hosts()
        self.assertEqual((found.name, found.zone, found.instance_id, found.machine_type, found.spot,
                          found.target_label, found.startup_script), ("oxidex-runners-a", "us-central1-a", "9",
                                                                      "n2d-standard-4", True, "k", True))
        for bad in ({"items": {"zones/x": {"warning": {"code": "UNREACHABLE"}}}}, {"unreachables": ["zones/x"]},
                    {"items": {"zones/x": {"instances": [{"name": "oxidex-runners-b"}]}}}):
            with self.subTest(bad=bad), self.assertRaises(rf.FloorError):
                rf.ComputeInventory(PROJECT, token=lambda: "t",
                                    transport=self.transport([(200, bad)], [])).list_hosts()


# --------------------------------------------------------------------------------------------
# Planner decisions
# --------------------------------------------------------------------------------------------

class PlannerTests(unittest.TestCase):
    def plan(self, hosts, runners=(), record=None, t=None, now=NOW):
        return rf.plan_target(t or target(), hosts, runners, record or {}, now)

    def test_no_host_launches(self):
        self.assertEqual(self.plan([]).action, "launch")

    def test_a_running_or_starting_managed_host_meets_the_floor(self):
        for status in ("RUNNING", "PROVISIONING", "STAGING", "REPAIRING"):
            with self.subTest(status=status):
                plan = self.plan([host(status=status)])
                self.assertEqual(plan.action, "noop")
                self.assertTrue(plan.idle)

    def test_only_the_targets_own_spot_managed_hosts_count(self):
        foreign = host(owner="Appz4Fun")
        unmanaged = host(name="newznab-spot-runners-asia", owner="")
        standard = host(spot=False)
        self.assertEqual(self.plan([foreign, unmanaged, standard]).action, "launch")
        # Unlabeled oxidex-runners-* hosts predate labels and belong to the default organization only.
        legacy = host(owner="")
        self.assertEqual(self.plan([legacy], t=target("swack-tools")).action, "noop")
        self.assertEqual(self.plan([legacy]).action, "launch")

    def test_a_running_host_without_an_online_runner_stops_meeting_the_floor(self):
        old = NOW - rf.LAUNCH_DEADLINE_SECONDS - 1
        broken = host(started=old)
        plan = self.plan([broken], [runner(f"gha-{broken.name}-1", status="offline")])
        self.assertEqual(plan.action, "launch")
        self.assertIn("no runner online", plan.reason)
        for runners in ([runner(f"gha-{broken.name}-1")], [runner(f"gha-{broken.name}-1", busy=True)]):
            self.assertEqual(self.plan([broken], runners).action, "noop")
        self.assertEqual(self.plan([host(started=NOW - 600)], []).action, "noop")  # still bootstrapping
        self.assertEqual(self.plan([host(started=None)], []).action, "noop")  # unknown start: never extra spend
        self.assertEqual(self.plan([host(status="STAGING", started=old)], []).action, "noop")
        # Another host's runners don't count for it.
        self.assertEqual(self.plan([broken], [runner(f"gha-{broken.name}2-1")]).action, "launch")

    def test_a_host_changing_state_waits(self):
        plan = self.plan([host(status="STOPPING")])
        self.assertEqual(plan.action, "wait")

    def test_backoff_after_failures_waits_without_a_lease(self):
        plan = self.plan([], record={"failures": 2, "next_attempt_at": NOW + 60})
        self.assertEqual(plan.action, "wait")
        self.assertTrue(plan.idle)
        self.assertEqual(self.plan([], record={"failures": 2, "next_attempt_at": NOW - 1}).action, "launch")

    def test_own_stopped_host_restarts_only_when_every_guard_holds(self):
        h = host(status="TERMINATED")
        good = [runner(f"gha-{h.name}-1", status="offline",
                       labels=("self-hosted", "spot", "n2d-standard-4", "us-central1", "ubuntu-latest"))]
        self.assertEqual(self.plan([h], good, own_record(h)).action, "restart")
        cases = {
            "startup-script metadata": ([host(status="TERMINATED", startup_script=True)], good, own_record(h)),
            "not this keeper's": ([h], good, own_record(host(instance_id="999"))),
            "no longer registered": ([h], [], own_record(h)),
            "online or busy": ([h], [runner(f"gha-{h.name}-1", status="offline", busy=True)], own_record(h)),
            "lost its scheduling labels": ([h], [runner(f"gha-{h.name}-1", status="offline",
                                                        labels=("self-hosted",))], own_record(h)),
            "earlier start": ([h], good, own_record(h, restart_failed=[h.instance_id])),
        }
        for why, (hosts, runners, record) in cases.items():
            with self.subTest(why=why):
                plan = self.plan(hosts, runners, record)
                self.assertEqual(plan.action, "launch")
                self.assertIn(why, plan.reason)
        suspended = host(status="SUSPENDED")
        self.assertEqual(self.plan([suspended], good, own_record(suspended)).action, "launch")

    def test_a_creating_launch_confirms_waits_or_fails(self):
        op = {"kind": "launch", "phase": "creating", "name": "oxidex-runners-n", "zone": "us-central1-a",
              "expected_runners": ["gha-oxidex-runners-n-1"], "started_at": NOW - 10}
        record = {"operation": op}
        self.assertEqual(self.plan([], record=record).action, "wait")
        self.assertEqual(self.plan([], record=record, now=NOW + rf.CREATE_CONFIRM_SECONDS).action, "launch_failed")
        vm = host(name="oxidex-runners-n", status="STAGING")
        self.assertEqual(self.plan([vm], record=record).action, "confirm_create")

    def test_a_booting_launch(self):
        name = "oxidex-runners-n"
        op = {"kind": "launch", "phase": "booting", "name": name, "zone": "us-central1-a", "instance_id": "111",
              "expected_runners": [f"gha-{name}-1"], "started_at": NOW - 100, "created_at": NOW - 100}
        record = {"operation": op}
        vm = host(name=name, startup_script=True)
        online = [runner(f"gha-{name}-1")]
        self.assertEqual(self.plan([vm], [], record).action, "wait")
        self.assertEqual(self.plan([vm], online, record).action, "finish_launch")
        late = NOW + rf.LAUNCH_DEADLINE_SECONDS
        self.assertEqual(self.plan([vm], [], record, now=late).action, "abandon_launch")
        busy = [runner(f"gha-{name}-1", busy=True, status="offline")]
        self.assertEqual(self.plan([vm], busy, record, now=late).action, "wait")
        self.assertEqual(self.plan([host(name=name, status="TERMINATED")], [], record).action, "abandon_launch")
        self.assertEqual(self.plan([], online, record).action, "launch_lost")
        replaced = self.plan([host(name=name, instance_id="222")], online, record)
        self.assertEqual(replaced.action, "launch_lost")
        self.assertIn("leaves that VM alone", replaced.reason)

    def test_a_restart_operation(self):
        h = host(status="STAGING")
        op = {"kind": "restart", "phase": "starting", "name": h.name, "zone": h.zone, "instance_id": h.instance_id,
              "expected_runners": [f"gha-{h.name}-1"], "started_at": NOW}
        record = {"operation": op}
        self.assertEqual(self.plan([h], [], record).action, "wait")
        self.assertEqual(self.plan([host()], [runner(f"gha-{h.name}-1")], record).action, "finish_restart")
        self.assertEqual(self.plan([h], [], record, now=NOW + rf.RESTART_DEADLINE_SECONDS).action, "fail_restart")
        self.assertEqual(self.plan([], [], record).action, "restart_lost")


class OfferTests(unittest.TestCase):
    def offer(self, zone, total, machine="n2d-standard-4"):
        return rf.FloorOffer(machine, zone.rsplit("-", 1)[0], zone, 16, Decimal(total) / 24, Decimal(0))

    def test_cheapest_strictly_under_the_cap_outside_failed_zones(self):
        offers = [self.offer("us-east1-b", "0.50"), self.offer("us-central1-a", "0.40"),
                  self.offer("us-west1-a", "1.00")]
        self.assertEqual(rf.choose_offer(offers, Decimal("1.00")).zone, "us-central1-a")
        self.assertEqual(rf.choose_offer(offers, Decimal("1.00"), failed_zones=["us-central1-a"]).zone, "us-east1-b")
        self.assertIsNone(rf.choose_offer(offers, Decimal("0.40")))  # equal to the cap does not qualify

    def test_spread_away_from_other_targets_within_the_premium(self):
        offers = [self.offer("us-central1-a", "0.40"), self.offer("us-central1-b", "0.50"),
                  self.offer("us-east1-b", "0.55")]
        spread = rf.choose_offer(offers, Decimal("1"), occupied_zones=["us-central1-a"],
                                 occupied_regions=["us-central1"])
        self.assertEqual(spread.zone, "us-east1-b")  # another zone and another region, within 1.5x
        only_zone = rf.choose_offer(offers, Decimal("1"), occupied_zones=["us-central1-a"])
        self.assertEqual(only_zone.zone, "us-central1-b")
        pricey = [self.offer("us-central1-a", "0.40"), self.offer("us-east1-b", "0.70")]
        self.assertEqual(rf.choose_offer(pricey, Decimal("1"), occupied_zones=["us-central1-a"]).zone,
                         "us-central1-a")  # 1.75x is too much: share the zone

    def test_price_offers_takes_only_four_vcpu_supported_shapes(self):
        rows = [{"name": "n2d-standard-4", "zone": "us-central1-a", "guestCpus": 4, "memoryMb": 16384},
                {"name": "n2d-highcpu-4", "zone": "us-central1-a", "guestCpus": 4, "memoryMb": 4096},
                {"name": "n2d-standard-8", "zone": "us-central1-a", "guestCpus": 8, "memoryMb": 32768},
                {"name": "t2d-standard-4", "zone": "us-central1-a", "guestCpus": 4, "memoryMb": 16384},
                {"name": "e2-standard-4", "zone": "europe-west1-b", "guestCpus": 4, "memoryMb": 16384},
                {"name": "n2-standard-4", "zone": "us-central1-b", "guestCpus": 4, "memoryMb": 16384}]
        storage = {"pd-standard:capacity": Decimal("0.04"), "pd-balanced:capacity": Decimal("0.1"),
                   "pd-ssd:capacity": Decimal("0.17")}
        rates = {"n2d": (Decimal("0.004"), Decimal("0.0005")), "n2": (Decimal("0.005"), Decimal("0.0006"))}
        offers = rf.price_offers(rows, ["us-central1"], 16, rates=lambda r, f: rates[f], storage=lambda r: storage)
        self.assertEqual([o.machine_type for o in offers], ["n2d-standard-4", "n2-standard-4"])
        first = offers[0]
        self.assertEqual(first.hourly_usd, Decimal("0.004") * 4 + Decimal("0.0005") * 16)
        self.assertGreater(first.total_daily_usd, first.hourly_usd * 24)  # storage counts too


# --------------------------------------------------------------------------------------------
# A world of fakes for ticks
# --------------------------------------------------------------------------------------------

class FakeGitHub:
    def __init__(self, world):
        self.world = world

    def list_runners(self, t):
        self.world.log.append(("github", "list"))
        return list(self.world.runners.get(t.name, []))

    def registration_token(self, t):
        self.world.log.append(("github", "registration_token"))
        return "REGTOKEN-one-use"

    def runner_group(self, org, group_id):
        return self.world.groups[(org, group_id)]

    def get_runner(self, t, runner_id):
        for r in self.world.runners.get(t.name, []):
            if r.runner_id == runner_id:
                return {"id": r.runner_id, "name": r.name, "busy": r.busy, "status": r.status}
        return None

    def delete_runner(self, t, runner_id):
        self.world.log.append(("github", "delete_runner", runner_id))
        self.world.runners[t.name] = [r for r in self.world.runners.get(t.name, []) if r.runner_id != runner_id]

    def set_labels(self, t, runner_id, labels):
        self.world.log.append(("github", "set_labels", runner_id, tuple(labels)))
        rows = self.world.runners.get(t.name, [])
        for i, r in enumerate(rows):
            if r.runner_id == runner_id:
                rows[i] = GitHubRunner(r.runner_id, r.name, r.status, r.busy, ("self-hosted", *labels))
        return set(labels)

    def _request(self, method, path, body=None, ok=(200,)):
        self.world.log.append(("github", method, path))
        if method == "DELETE" and path.endswith("/labels"):
            runner_id = int(path.rsplit("/", 2)[-2])
            for name, rows in self.world.runners.items():
                for i, r in enumerate(rows):
                    if r.runner_id == runner_id:
                        rows[i] = GitHubRunner(r.runner_id, r.name, r.status, r.busy, ("self-hosted",))
        return 200, {}


class FakeProvider:
    def __init__(self, world):
        self.world = world

    def list_hosts(self):
        return list(self.world.hosts)

    def describe(self, name, zone):
        return next((h for h in self.world.hosts if h.name == name and h.zone == zone), None)

    def offers(self, cfg, t):
        self.world.log.append(("provider", "offers"))
        return list(self.world.offers)

    def preflight(self, args, zone):
        self.world.log.append(("provider", "preflight", zone))
        if self.world.preflight_error:
            raise RuntimeError(self.world.preflight_error)

    def create(self, args, zone, script):
        self.world.log.append(("provider", "create", args.instance_name))
        self.world.scripts.append(script)
        self.world.created_args.append(args)
        if self.world.create_error:
            raise RuntimeError(self.world.create_error)
        self.world.hosts.append(rf.Host(args.instance_name, zone, "STAGING", str(self.world.next_id),
                                        args.instance_type, True, target_label_value(args.target), True, ""))
        self.world.next_id += 1

    def remove_startup_script(self, h):
        self.world.log.append(("provider", "remove_startup_script", h.name))
        self.world.replace_host(h.name, startup_script=False)

    def record_no_patch(self, args, zone, boot):
        self.world.log.append(("provider", "record_no_patch", args.instance_name))

    def start(self, h):
        self.world.log.append(("provider", "start", h.name))
        self.world.replace_host(h.name, status="STAGING")

    def delete_instance(self, h, all_disks=True):
        self.world.log.append(("provider", "delete_instance", h.name, h.instance_id) + (() if all_disks else ("keep",)))
        self.world.hosts = [x for x in self.world.hosts if x.name != h.name]

    def delete_orphan_disk(self, name, zone):
        self.world.log.append(("provider", "delete_orphan_disk", name))
        return False


class World:
    def __init__(self, text=TARGETS_TEXT):
        self.clock = [NOW]
        self.server = FakeGcsServer(self.clock)
        self.client = gcs.GcsClient(BUCKET, token=lambda: TOKEN, transport=self.server, sleep=lambda s: None)
        self.location = gcs.parse_state_uri(URI)
        self.config = config(text)
        self.hosts: list[rf.Host] = []
        self.runners: dict[str, list[GitHubRunner]] = {}
        self.groups = {("swack-tools", 10): {"id": 10, "name": "oxidex-runners", "visibility": "selected",
                                             "default": False}}
        self.offers = [rf.FloorOffer("n2d-standard-4", "us-central1", "us-central1-a", 16, Decimal("0.02"),
                                     Decimal("4"))]
        self.log: list[tuple] = []
        self.scripts: list[str] = []
        self.created_args: list[argparse.Namespace] = []
        self.preflight_error = ""
        self.create_error = ""
        self.next_id = 500
        self.github = FakeGitHub(self)
        self.provider = FakeProvider(self)
        self.services = rf.Services(self.provider, lambda t, guard: self.guarded_github(guard),
                                    lambda guard, deadline: self.provider, self.client, clock=lambda: self.clock[0],
                                    lease_factory=self.lease)

    def guarded_github(self, guard):
        self.last_guard = guard
        return self.github

    def lease(self, name, owner="watchdog"):
        return gcs.GcsLease(self.client, name, owner=owner, wall=lambda: self.clock[0],
                            monotonic=lambda: self.clock[0])

    def replace_host(self, name, **changes):
        self.hosts = [rf.Host(**{**h.__dict__, **changes}) if h.name == name else h for h in self.hosts]

    def tick(self, apply=True, deadline=None, only=("xbmc4lyfe/newznab-tmux",)):
        return rf.run_tick(self.config, self.services, apply=apply, deadline=deadline, only=only)

    def store(self, name="xbmc4lyfe/newznab-tmux"):
        s = rf.TargetStore(self.client, self.location, PROJECT, self.config.target(name))
        s.load()
        return s

    def bootstrap(self, name, labels=("spot", "n2d-standard-4", "us-central1", "ubuntu-latest"), owner=None):
        self.replace_host(name, status="RUNNING")
        owner = owner or "xbmc4lyfe/newznab-tmux"
        self.runners.setdefault(owner, []).append(
            GitHubRunner(9000 + len(self.log), f"gha-{name}-1", "online", False, ("self-hosted", "Linux", "X64", *labels)))


class TickTests(unittest.TestCase):
    def test_an_idle_tick_takes_no_lease_and_writes_nothing(self):
        world = World()
        world.hosts = [host()]
        code, results = world.tick()
        self.assertEqual((code, results[0]["status"], results[0]["action"]), (0, "idle", "noop"))
        self.assertEqual(world.server.writes("POST") + world.server.writes("DELETE"), [])

    def test_a_preview_plans_but_never_takes_a_lease(self):
        world = World()
        code, results = world.tick(apply=False)
        self.assertEqual((results[0]["status"], results[0]["action"]), ("planned", "launch"))
        self.assertEqual(world.server.writes("POST"), [])
        self.assertNotIn(("provider", "create"), [entry[:2] for entry in world.log])

    def test_launch_is_a_multi_tick_state_machine(self):
        world = World()
        code, results = world.tick()
        self.assertEqual((code, results[0]["status"]), (0, "acted"), results)
        store = world.store()
        op = store.record["operation"]
        self.assertEqual((op["kind"], op["phase"]), ("launch", "booting"))
        self.assertTrue(op["name"].startswith("oxidex-runners-uscentral1-a-"))
        self.assertTrue(op["disk_name"].startswith("runner-data-uscentral1-a-"))
        self.assertEqual(op["expected_runners"], [f"gha-{op['name']}-1"])
        self.assertEqual(op["labels"], ["spot", "n2d-standard-4", "us-central1", "ubuntu-latest"])
        # The intent (phase creating) was saved before the registration token existed.
        kinds = [entry[:2] for entry in world.log]
        self.assertLess(kinds.index(("provider", "preflight")), kinds.index(("github", "registration_token")))
        self.assertLess(kinds.index(("github", "registration_token")), kinds.index(("provider", "create")))
        saved = [json.loads(world.server.objects[store.object_name][0]) for _ in (0,)]
        self.assertEqual(saved[0]["record"]["operation"]["instance_id"], "500")
        first_write = next(c for c in world.server.calls if c[0] == "POST" and c[2]["name"] == store.object_name)
        self.assertEqual(first_write[2]["ifGenerationMatch"], "0")  # created only if absent
        # The token reached the startup script, never the saved state.
        self.assertIn("REGTOKEN-one-use", world.scripts[0])
        self.assertNotIn("REGTOKEN", json.dumps(store.record))
        args = world.created_args[0]
        self.assertEqual((args.runner_count, args.cpu, args.no_service_account, args.runner_group_id),
                         (1, 4, True, None))
        self.assertIn("spot,n2d-standard-4,us-central1,ubuntu-latest", world.scripts[0])
        self.assertIn("https://github.com/xbmc4lyfe/newznab-tmux", world.scripts[0])
        # The lease was released.
        self.assertNotIn(store.lock_name, world.server.objects)

        # Bootstrapping: waiting is idle (no lease, no write).
        world.replace_host(op["name"], status="RUNNING")
        writes = len(world.server.writes("POST"))
        code, results = world.tick()
        self.assertEqual(results[0]["status"], "idle")
        self.assertEqual(len(world.server.writes("POST")), writes)

        world.bootstrap(op["name"])
        world.clock[0] += 400
        code, results = world.tick()
        self.assertEqual(results[0]["status"], "acted", results)
        store = world.store()
        self.assertNotIn("operation", store.record)
        self.assertEqual(store.record["host"]["instance_id"], "500")
        kinds = [entry[:2] for entry in world.log]
        # The one-use token leaves the metadata before anything else is recorded.
        self.assertLess(kinds.index(("provider", "remove_startup_script")), kinds.index(("provider", "record_no_patch")))
        self.assertFalse(world.provider.describe(op["name"], op["zone"]).startup_script)
        code, results = world.tick()
        self.assertEqual(results[0]["action"], "noop")

    def test_a_create_that_lands_late_is_adopted_and_finished(self):
        world = World()
        world.create_error = "deadline exceeded"
        world.tick()
        record = world.store().record
        self.assertNotIn("operation", record)
        late = record["late_creates"][0]
        self.assertEqual(late["until"], NOW + rf.LATE_CREATE_WATCH_SECONDS)
        world.hosts.append(rf.Host(late["name"], late["zone"], "RUNNING", "777", "n2d-standard-4", True,
                                   target_label_value("xbmc4lyfe/newznab-tmux"), True, gce_time(NOW)))
        world.clock[0] += 400  # still inside the backoff: adoption is not a launch
        world.tick()
        record = world.store().record
        self.assertEqual((record["operation"]["phase"], record["operation"]["instance_id"]), ("booting", "777"))
        self.assertEqual(record["late_creates"], [])
        world.bootstrap(late["name"])
        world.tick()
        self.assertEqual(world.store().record["host"]["instance_id"], "777")
        self.assertIn(("provider", "remove_startup_script", late["name"]), world.log)

    def test_a_create_that_fails_with_no_vm_backs_off_that_zone(self):
        world = World()
        world.create_error = "ZONE_RESOURCE_POOL_EXHAUSTED"
        world.tick()
        record = world.store().record
        self.assertNotIn("operation", record)
        self.assertEqual(record["failures"], 1)
        self.assertIn("us-central1-a", record["failed_zones"])
        self.assertEqual(record["next_attempt_at"], NOW + rf.BACKOFF_BASE_SECONDS)
        self.assertEqual(world.tick()[1][0]["action"], "wait")  # backoff, without a lease

    def test_a_registration_token_failure_ends_the_launch_without_a_zone_backoff(self):
        world = World()

        def refuse(t):
            raise rf.GitHubError("GitHub POST registration-token returned HTTP 403", 403)
        world.github.registration_token = refuse
        world.tick()
        record = world.store().record
        self.assertNotIn("operation", record)
        self.assertEqual((record["failures"], record.get("failed_zones")), (1, {}))
        self.assertNotIn(("provider", "create"), [e[:2] for e in world.log])

    def test_preflight_over_the_cap_creates_nothing(self):
        world = World()
        world.preflight_error = "Total compute/storage price is not below $1.00/day"
        world.tick()
        self.assertNotIn(("provider", "create"), [e[:2] for e in world.log])
        self.assertNotIn(("github", "registration_token"), [e[:2] for e in world.log])

    def test_no_affordable_offer_holds(self):
        world = World()
        world.offers = []
        code, results = world.tick()
        self.assertIn("launch_held", results[0]["detail"])
        self.assertNotIn(("provider", "create"), [e[:2] for e in world.log])

    def test_abandon_deletes_only_the_recorded_instance(self):
        world = World()
        world.tick()
        op = world.store().record["operation"]
        world.replace_host(op["name"], status="RUNNING")
        world.clock[0] += rf.LAUNCH_DEADLINE_SECONDS + 1
        world.runners["xbmc4lyfe/newznab-tmux"] = [runner(op["expected_runners"][0], status="offline",
                                                          runner_id=77)]
        world.tick()
        deletes = [e for e in world.log if e[1] == "delete_instance"]
        self.assertEqual(deletes, [("provider", "delete_instance", op["name"], "500")])
        self.assertIn(("github", "delete_runner", 77), world.log)
        record = world.store().record
        self.assertNotIn("operation", record)
        self.assertEqual(record["failures"], 1)

    def test_abandon_never_touches_a_vm_recreated_under_the_name(self):
        world = World()
        world.tick()
        op = world.store().record["operation"]
        world.replace_host(op["name"], instance_id="999", status="RUNNING")
        world.clock[0] += rf.LAUNCH_DEADLINE_SECONDS + 1
        world.tick()
        self.assertFalse([e for e in world.log if e[1] == "delete_instance"])
        self.assertNotIn("operation", world.store().record)

    def keeper(self, world, record):
        t = world.config.target("xbmc4lyfe/newznab-tmux")
        return rf.FloorKeeper(world.config, t, record, lambda: world.log.append(("save",)), provider=world.provider,
                              github=world.github, clock=lambda: world.clock[0])

    def booting_record(self, name="oxidex-runners-n"):
        return {"operation": {"kind": "launch", "phase": "booting", "name": name, "zone": "us-central1-a",
                              "instance_id": "111", "expected_runners": [f"gha-{name}-1"], "disk_name": "runner-data-n",
                              "labels": ["spot", "n2d-standard-4", "us-central1", "ubuntu-latest"],
                              "manager_argv": rf.manager_argv(PROJECT, target(), {
                                  "machine_type": "n2d-standard-4", "region": "us-central1", "zone": "us-central1-a",
                                  "ram_gib": 16}, name, "runner-data-n", Decimal(1)),
                              "started_at": NOW, "created_at": NOW}}

    def test_the_executor_rechecks_identity_and_jobs_before_it_deletes(self):
        # The plan said abandon; by the time the step runs, the name is another instance, or a job started.
        world = World()
        world.hosts = [host(name="oxidex-runners-n", instance_id="222")]
        record = self.booting_record()
        self.keeper(world, record).execute(rf.Plan("abandon_launch", "deadline"), world.hosts)
        self.assertFalse([e for e in world.log if e[1:2] == ("delete_instance",)])
        world = World()
        world.hosts = [host(name="oxidex-runners-n", instance_id="111")]
        # GitHub can still report a job's runner busy while its status already reads offline.
        world.runners["xbmc4lyfe/newznab-tmux"] = [runner("gha-oxidex-runners-n-1", status="offline", busy=True)]
        record = self.booting_record()
        self.keeper(world, record).execute(rf.Plan("abandon_launch", "deadline"), world.hosts)
        self.assertFalse([e for e in world.log if e[1:2] == ("delete_instance",)])
        self.assertIn("operation", record)
        world.runners["xbmc4lyfe/newznab-tmux"] = [runner("gha-oxidex-runners-n-1")]  # came online meanwhile
        self.keeper(world, record).execute(rf.Plan("abandon_launch", "deadline"), world.hosts)
        self.assertFalse([e for e in world.log if e[1:2] == ("delete_instance",)])

    def test_finish_rechecks_the_recorded_running_instance(self):
        for vm in (host(name="oxidex-runners-n", instance_id="222", startup_script=True),
                   host(name="oxidex-runners-n", instance_id="111", status="STOPPING", startup_script=True)):
            with self.subTest(vm=vm.status):
                world = World()
                world.hosts = [vm]
                world.runners["xbmc4lyfe/newznab-tmux"] = [runner("gha-oxidex-runners-n-1")]
                record = self.booting_record()
                self.keeper(world, record).execute(rf.Plan("finish_launch", "online"), world.hosts)
                self.assertNotIn("host", record)
                self.assertFalse([e for e in world.log if e[0] == "provider"])

    def test_a_vm_lost_during_bootstrap_clears_and_cleans_its_registrations(self):
        world = World()
        world.tick()
        op = world.store().record["operation"]
        world.hosts = []
        world.runners["xbmc4lyfe/newznab-tmux"] = [runner(op["expected_runners"][0], status="offline", runner_id=5)]
        world.tick()
        self.assertIn(("github", "delete_runner", 5), world.log)
        self.assertEqual(world.store().record["failures"], 1)

    def test_restart_of_own_stopped_host(self):
        world = World()
        h = host(status="TERMINATED")
        world.hosts = [h]
        world.runners["xbmc4lyfe/newznab-tmux"] = [runner(f"gha-{h.name}-1", status="offline",
                                                          labels=("spot", "n2d-standard-4", "us-central1",
                                                                  "ubuntu-latest"))]
        store = world.store()
        store.record.update(own_record(h))
        lease = world.lease(store.lock_name)
        self.assertTrue(lease.acquire())
        store.save(lease)
        lease.release()
        world.tick()
        self.assertIn(("provider", "start", h.name), world.log)
        self.assertEqual(world.store().record["operation"]["kind"], "restart")
        world.replace_host(h.name, status="RUNNING")
        world.runners["xbmc4lyfe/newznab-tmux"] = [runner(f"gha-{h.name}-1")]
        world.tick()
        self.assertNotIn("operation", world.store().record)
        self.assertFalse([e for e in world.log if e[1] in ("create", "delete_instance")])

    def test_a_held_lease_skips_the_target_and_changes_nothing(self):
        world = World()
        store = world.store()
        other = world.lease(store.lock_name, owner="autoscaler:mac:1")
        self.assertTrue(other.acquire())
        code, results = world.tick()
        self.assertEqual((code, results[0]["status"]), (0, "locked"))
        self.assertIn("autoscaler:mac:1", results[0]["detail"])
        self.assertFalse([e for e in world.log if e[0] == "provider" and e[1] != "offers"])
        self.assertNotIn(store.object_name, world.server.objects)

    def test_each_target_has_its_own_lease_and_state(self):
        world = World()
        names = {rf.object_names(world.location, PROJECT, t) for t in world.config.targets}
        self.assertEqual(len(names), 3)
        for state, lock in names:
            self.assertTrue(state.startswith("runner-floor/floor-proj-") and state.endswith(".json"))
            self.assertEqual(lock, state[:-5] + ".lock")
        # One target's held lease doesn't block another.
        swack = world.store("swack-tools")
        held = world.lease(swack.lock_name, owner="autoscaler:mac:1")
        self.assertTrue(held.acquire())
        code, results = world.tick(only=())
        by = {r["target"]: r["status"] for r in results}
        self.assertEqual(by["swack-tools"], "locked")
        self.assertEqual(by["xbmc4lyfe/newznab-tmux"], "acted")

    def test_launch_zones_spread_across_targets(self):
        world = World()
        world.offers = [rf.FloorOffer("n2d-standard-4", "us-central1", "us-central1-a", 16, Decimal("0.02"),
                                      Decimal(4)),
                        rf.FloorOffer("n2d-standard-4", "us-east1", "us-east1-b", 16, Decimal("0.021"), Decimal(4))]
        world.hosts = [host(owner="Appz4Fun", zone="us-central1-a", name="oxidex-runners-appz")]
        world.tick()
        self.assertEqual(world.store().record["operation"]["zone"], "us-east1-b")

    def test_an_org_group_must_be_selected_repository_scoped(self):
        world = World()
        world.groups[("swack-tools", 10)] = {"id": 10, "name": "Default", "visibility": "all", "default": True}
        code, results = world.tick(only=("swack-tools",))
        self.assertEqual((code, results[0]["status"]), (1, "error"))
        self.assertNotIn(("provider", "create"), [e[:2] for e in world.log])
        self.assertEqual(world.store("swack-tools").record["failures"], 1)  # backs off like any failed launch
        world.groups[("swack-tools", 10)] = {"id": 10, "name": "oxidex-runners", "visibility": "selected"}
        world.log.clear()
        world.clock[0] += rf.BACKOFF_BASE_SECONDS
        world.tick(only=("swack-tools",))
        self.assertIn("RUNNER_GROUP='oxidex-runners'", world.scripts[-1])
        self.assertEqual(world.created_args[-1].runner_group_id, 10)

    def test_a_state_conflict_stops_the_step(self):
        world = World()
        store = world.store()

        def interleave(method, name, query):
            if method == "POST" and name == store.object_name and query.get("ifGenerationMatch") == "0":
                world.server.before = None
                world.server.put_raw(name, b'{"schema": "x"}')  # another writer got there first
        world.server.before = interleave
        code, results = world.tick()
        self.assertEqual((code, results[0]["status"]), (1, "error"))
        self.assertNotIn(("github", "registration_token"), [e[:2] for e in world.log])

    def test_mutating_github_calls_run_behind_the_lease(self):
        world = World()
        world.tick()
        self.assertIsNotNone(world.last_guard)
        self.assertEqual(world.last_guard.__self__.__class__, gcs.GcsLease)


class BudgetTests(unittest.TestCase):
    def test_launch_waits_for_the_next_tick_when_the_budget_is_short(self):
        world = World()
        wall = [NOW]
        deadline = rf.Deadline(NOW + rf.LAUNCH_MIN_SECONDS - 1, wall=lambda: wall[0])
        code, results = world.tick(deadline=deadline)
        self.assertEqual((code, results[0]["status"]), (0, "skipped"))
        self.assertNotIn(("provider", "create"), [e[:2] for e in world.log])
        self.assertNotIn("operation", world.store().record)

    def test_targets_are_skipped_once_the_budget_is_gone(self):
        world = World()
        deadline = rf.Deadline(NOW + 5, wall=lambda: NOW)
        _, results = world.tick(deadline=deadline, only=())
        self.assertEqual({r["status"] for r in results}, {"skipped"})

    def test_deadline_from_the_server_environment(self):
        deadline = rf.Deadline.from_env({rf.ENV_TICK_DEADLINE: str(NOW + 840)}, wall=lambda: NOW)
        self.assertEqual(deadline.remaining(), 840 - rf.DEADLINE_MARGIN_SECONDS)
        self.assertEqual(rf.Deadline.from_env({}, 100, wall=lambda: NOW).remaining(), 100)
        self.assertEqual(rf.Deadline.from_env({rf.ENV_TICK_DEADLINE: str(NOW + 840)}, 60, wall=lambda: NOW).remaining(), 60)
        self.assertEqual(rf.Deadline.from_env({}).remaining(), float("inf"))
        self.assertEqual(rf.Deadline(NOW + 100, wall=lambda: NOW).cap(600), 100)
        with self.assertRaises(rf.BudgetExhausted):
            rf.Deadline(NOW + 5, wall=lambda: NOW).cap(600)

    def test_the_tick_fits_inside_the_lease(self):
        from lib import control_watchdog_server as server
        dead_by = server.TICK_TIMEOUT_SECONDS + server.TERM_GRACE_SECONDS + server.KILL_WAIT_SECONDS
        # A floor lease taken at any point in the tick can't be broken before the tick's process group is gone.
        self.assertLess(dead_by, gcs.LEASE_TTL_SECONDS + gcs.LEASE_STALE_GRACE_SECONDS)
        self.assertGreater(server.TICK_TIMEOUT_SECONDS, rf.LAUNCH_MIN_SECONDS + rf.DEADLINE_MARGIN_SECONDS)
        # A launch's whole create-to-online window spans several ticks, never one.
        self.assertGreater(rf.LAUNCH_DEADLINE_SECONDS, server.TICK_TIMEOUT_SECONDS)

    def test_hard_deadline_kills_a_hung_child_and_nests(self):
        started = time.monotonic()
        with self.assertRaises(rf.DeadlineExceeded):
            with rf.hard_deadline(0.3):
                subprocess.run([sys.executable, "-c", "import time; time.sleep(30)"], check=False)
        self.assertLess(time.monotonic() - started, 10)
        with self.assertRaises(rf.DeadlineExceeded):
            with rf.hard_deadline(0.4):
                with rf.hard_deadline(30):  # the inner deadline never outlives the outer one
                    time.sleep(5)
        with rf.hard_deadline(float("inf")):
            pass
        with self.assertRaises(rf.DeadlineExceeded):  # an inner block that ends re-arms the outer deadline
            with rf.hard_deadline(0.4):
                with rf.hard_deadline(30):
                    pass
                time.sleep(5)


class ManagerArgsTests(unittest.TestCase):
    def test_floor_hosts_size_to_one_runner_with_no_service_account(self):
        t = target()
        offer = rf.FloorOffer("n2d-standard-4", "us-central1", "us-central1-a", 16, Decimal("0.02"), Decimal(4))
        argv = rf.manager_argv(PROJECT, t, offer, "oxidex-runners-x", "runner-data-x", Decimal("1.00"))
        args = rf.manager_args(argv, t)
        self.assertEqual((args.runner_count, args.runner_cpu, args.runner_memory_gb), (1, 4, 12))
        self.assertTrue(args.no_service_account)
        self.assertIsNone(args.runner_group_id)
        self.assertEqual(args.extra_runner_labels, ("ubuntu-latest",))
        self.assertEqual(args.max_total_daily_usd, Decimal("1.00"))
        self.assertEqual(argv[-2:], ["--runner-count", "1"])
        bigger = argv[:-2]  # without the explicit runner count, an 8-vCPU shape would size to two runners
        bigger[bigger.index("--cpu") + 1], bigger[bigger.index("--ram") + 1] = "8", "32"
        bigger[bigger.index("--instance-type") + 1] = "n2d-standard-8"
        with self.assertRaises(rf.FloorError):
            rf.manager_args(bigger, t)  # never more than one runner on a floor host
        appz = rf.manager_args(rf.manager_argv(PROJECT, target("Appz4Fun"), offer, "oxidex-runners-y",
                                               "runner-data-y", Decimal(1)), target("Appz4Fun"))
        self.assertIsNone(appz.runner_group_id)  # never the parser's oxidex default group

    def test_the_create_command_attaches_no_service_account(self):
        from lib import runner_manager
        t = target()
        offer = rf.FloorOffer("n2d-standard-4", "us-central1", "us-central1-a", 16, Decimal("0.02"), Decimal(4))
        args = rf.manager_args(rf.manager_argv(PROJECT, t, offer, "oxidex-runners-x", "runner-data-x",
                                               Decimal(1)), t)
        commands = []

        def gcloud(argv, quiet=False):
            commands.append(argv)
            return "[]" if argv[:3] == ["compute", "disks", "list"] else ""
        missing = SimpleNamespace(returncode=1, stdout="", stderr="not found")
        with patch.object(runner_manager, "gcloud", gcloud), \
                patch.object(runner_manager.subprocess, "run", lambda *a, **k: missing):
            runner_manager.apply_startup(args, "us-central1-a", "#!/bin/bash\n")
        create = next(c for c in commands if c[:3] == ["compute", "instances", "create"])
        self.assertIn("--no-service-account", create)
        self.assertIn("--no-scopes", create)
        self.assertIn("--instance-termination-action=DELETE", create)
        metadata = next(a for a in create if a.startswith("--metadata=") and "spot-runner" in a)
        self.assertIn("spot-runner-extra-labels=ubuntu-latest", metadata)
        self.assertIn("spot-runner-count=1", metadata)
        # Other callers keep their service account.
        args.no_service_account = False
        commands.clear()
        with patch.object(runner_manager, "gcloud", gcloud), \
                patch.object(runner_manager.subprocess, "run", lambda *a, **k: missing):
            runner_manager.apply_startup(args, "us-central1-a", "#!/bin/bash\n")
        create = next(c for c in commands if c[:3] == ["compute", "instances", "create"])
        self.assertNotIn("--no-service-account", create)

    def test_extra_labels_survive_a_later_label_sync(self):
        from lib.runner_fleet import runner_deployment_from_gce_item
        item = {"metadata": {"items": [{"key": "spot-runner-extra-labels", "value": "ubuntu-latest+fast"}]}}
        self.assertEqual(runner_deployment_from_gce_item(item)["runner_extra_labels"], ["ubuntu-latest", "fast"])
        bad = {"metadata": {"items": [{"key": "spot-runner-extra-labels", "value": "a b"}]}}
        self.assertNotIn("runner_extra_labels", runner_deployment_from_gce_item(bad))
        from lib import runner_manager
        args = SimpleNamespace(runner_label="spot", instance_type="n2d-standard-4", region="us-central1",
                               extra_runner_labels=("ubuntu-latest",), runner_patch=None, patch_file=None,
                               patch_repo=None)
        self.assertEqual(runner_manager._desired_runner_labels(args),
                         ["spot", "n2d-standard-4", "us-central1", "ubuntu-latest"])


class AutoscalerLeaseTests(unittest.TestCase):
    def test_the_autoscaler_and_the_keeper_exclude_each_other(self):
        world = World()
        t = world.config.target("xbmc4lyfe/newznab-tmux")
        holder = rf.AutoscalerTargetLease(rf.target_lease(world.client, world.location, PROJECT, t.name,
                                                          owner="autoscaler:mac:1"))
        holder.lease.wall = holder.lease.monotonic = lambda: world.clock[0]
        self.assertTrue(holder.acquire())
        self.assertEqual(world.tick()[1][0]["status"], "locked")
        holder.guard()
        holder.release()
        self.assertEqual(world.tick()[1][0]["status"], "acted")

    def test_a_lost_lease_holds_every_fleet_mutation(self):
        from lib import spot_runner_autoscaler as autoscaler
        world = World()
        holder = rf.AutoscalerTargetLease(world.lease(world.store().lock_name, owner="autoscaler:mac:1"))
        self.assertTrue(holder.acquire())
        world.server.objects.pop(holder.lease.name)  # someone broke it
        world.clock[0] += gcs.LEASE_REVALIDATE_SECONDS + 1
        with patch.object(autoscaler, "_FLOOR_LEASE_GUARD", holder.guard):
            with self.assertRaises(RuntimeError) as caught:
                autoscaler.guard_fleet_mutation({"runner_inventory_observed_at": time.time()})
        self.assertIn("runner floor lease was lost", str(caught.exception))
        with self.assertRaises(RuntimeError):
            holder.guard()  # stays lost

    def test_without_the_variable_the_autoscaler_takes_no_lease(self):
        from lib import spot_runner_autoscaler as autoscaler
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(rf.ENV_STATE_URI, None)
            self.assertIsNone(autoscaler.acquire_floor_lease(argparse.Namespace(project=PROJECT, target=None,
                                                                                org="swack-tools", once=True)))

    def test_the_autoscaler_waits_for_a_floor_launch_in_progress(self):
        from lib import spot_runner_autoscaler as autoscaler
        world = World()
        store = world.store()
        lease = world.lease(store.lock_name)
        self.assertTrue(lease.acquire())
        store.record["operation"] = {"kind": "launch", "phase": "booting", "name": "oxidex-runners-n",
                                     "started_at": time.time(), "created_at": time.time()}
        store.save(lease)
        lease.release()
        holder = rf.AutoscalerTargetLease.for_target(PROJECT, "xbmc4lyfe/newznab-tmux", URI, client=world.client)
        self.assertEqual(holder.pending_operation()["name"], "oxidex-runners-n")
        args = argparse.Namespace(project=PROJECT, target="xbmc4lyfe/newznab-tmux", org="swack-tools", once=True,
                                  interval_seconds=300)
        with patch.dict(os.environ, {rf.ENV_STATE_URI: URI}), \
                patch.object(rf.AutoscalerTargetLease, "for_target", lambda *a, **k: holder):
            self.assertIs(autoscaler.acquire_floor_lease(args), False)
        self.assertNotIn(store.lock_name, world.server.objects)  # it let go again
        store.load()
        lease = world.lease(store.lock_name)
        self.assertTrue(lease.acquire())
        store.record.pop("operation")
        store.save(lease)
        lease.release()
        with patch.dict(os.environ, {rf.ENV_STATE_URI: URI}), \
                patch.object(rf.AutoscalerTargetLease, "for_target", lambda *a, **k: holder):
            held = autoscaler.acquire_floor_lease(args)
        try:
            self.assertIs(held, holder)
            self.assertIsNotNone(autoscaler._FLOOR_LEASE_GUARD)
        finally:
            autoscaler.release_floor_lease(holder)
        self.assertIsNone(autoscaler._FLOOR_LEASE_GUARD)

    def test_once_returns_when_the_keeper_holds_the_target(self):
        from lib import spot_runner_autoscaler as autoscaler

        class Held:
            def __init__(self, *a, **k):
                pass

            @classmethod
            def for_target(cls, *a, **k):
                return cls()

            def acquire(self):
                return False

            def holder(self):
                return "watchdog"
        args = argparse.Namespace(project=PROJECT, target="xbmc4lyfe/newznab-tmux", org="swack-tools", once=True,
                                  interval_seconds=300)
        with patch.dict(os.environ, {rf.ENV_STATE_URI: URI}), patch.object(rf, "AutoscalerTargetLease", Held):
            self.assertIs(autoscaler.acquire_floor_lease(args), False)
        self.assertIsNone(autoscaler._FLOOR_LEASE_GUARD)


class PricingFailureTests(unittest.TestCase):
    def test_a_catalog_failure_is_an_error_not_a_missing_price(self):
        rows = [{"name": "n2d-standard-4", "zone": "us-central1-a", "guestCpus": 4, "memoryMb": 16384}]

        def broken(region, family):
            raise OSError("HTTP Error 403: cloudbilling.googleapis.com is disabled")
        with self.assertRaises(OSError):
            rf.price_offers(rows, ["us-central1"], 16, rates=broken, storage=lambda r: {})
        from lib import billing_catalog
        provider = rf.GcloudProvider(PROJECT, guard=lambda: None, deadline=rf.Deadline(), inventory=None,
                                     runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(rows),
                                                                            stderr=""))
        with patch.object(billing_catalog, "catalog_skus", side_effect=OSError("403")):
            with self.assertRaises(rf.FloorError) as caught:
                provider.offers(config(), target())
        self.assertIn("billing catalog is unavailable", str(caught.exception))


class CommandLineTests(unittest.TestCase):
    def test_a_used_up_budget_skips_without_failing_the_tick(self):
        out = io.StringIO()
        env = {rf.ENV_TARGETS: "swack-tools:secret=s", rf.ENV_STATE_URI: URI, "K_SERVICE": "x",
               rf.ENV_TICK_DEADLINE: str(time.time() + 5)}
        with patch.dict(os.environ, env), redirect_stdout(out), \
                patch.object(rf, "default_services", side_effect=AssertionError("no services when skipped")):
            self.assertEqual(rf.main(["tick", "--apply"]), 0)
        self.assertIn("skipped", out.getvalue())

    def test_no_targets_does_nothing(self):
        out = io.StringIO()
        with patch.dict(os.environ, {rf.ENV_TARGETS: "", "K_SERVICE": "x"}), redirect_stdout(out):
            self.assertEqual(rf.main(["tick", "--apply"]), 0)
        self.assertIn("No floor targets", out.getvalue())

    def test_tick_without_a_state_uri_fails_closed(self):
        with patch.dict(os.environ, {rf.ENV_TARGETS: "swack-tools:secret=s", rf.ENV_STATE_URI: "", "K_SERVICE": "x"}):
            with self.assertRaises(rf.FloorError):
                rf.main(["tick"])


if __name__ == "__main__":
    unittest.main()


GRANDCHILD_SCRIPT = """
import os, signal, subprocess, sys, time
path = sys.argv[1]
script = ("import os, signal, sys, time\\n"
          "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"  # like a gcloud that ignores SIGTERM
          "open(sys.argv[1], 'w').write(str(os.getpid()))\\n"
          "time.sleep(120)\\n")
subprocess.Popen([sys.executable, "-c", script, path])
time.sleep(120)
"""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class FakeLease:
    def __init__(self):
        self.lose = False
        self.released = False

    def ensure(self):
        if self.lose:
            raise gcs.LeaseLost("the lease object is no longer this controller's")

    def acquire(self):
        return True

    def release(self):
        self.released = True


class FencingTests(unittest.TestCase):
    """Real processes: a deployment-manager-like child whose grandchild ignores SIGTERM."""

    def start_fenced(self, holder, timeout=60):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        pidfile = Path(directory.name) / "grandchild.pid"
        outcome = {}
        import threading

        def run():
            try:
                outcome["result"] = rf.run_fenced([sys.executable, "-c", GRANDCHILD_SCRIPT, str(pidfile)],
                                                  timeout=timeout, holder=holder)
            except subprocess.TimeoutExpired as exc:
                outcome["timeout"] = exc
        thread = threading.Thread(target=run)
        thread.start()
        for _ in range(500):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.01)
        else:
            self.fail("the grandchild never started")
        return thread, int(pidfile.read_text()), outcome

    def assert_gone(self, pid):
        for _ in range(200):
            if not _alive(pid):
                return
            time.sleep(0.025)
        os.kill(pid, 9)
        self.fail(f"grandchild {pid} survived the lease loss")

    def test_a_lost_lease_kills_the_fenced_childs_whole_group_at_once(self):
        lease = FakeLease()
        holder = rf.AutoscalerTargetLease(lease)
        thread, grandchild, outcome = self.start_fenced(holder)
        holder.guard()  # still held: nothing happens
        self.assertTrue(_alive(grandchild))
        lease.lose = True
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            holder.guard()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 8)
        self.assertLess(outcome["result"].returncode, 0)  # killed by a signal, not finished
        self.assert_gone(grandchild)

    def test_the_heartbeat_fences_without_any_guard_call(self):
        lease = FakeLease()
        holder = rf.AutoscalerTargetLease(lease)
        with patch.object(rf.AutoscalerTargetLease, "HEARTBEAT_SECONDS", 0.05):
            holder.start()
            thread, grandchild, outcome = self.start_fenced(holder)
            lease.lose = True
            thread.join(10)
            holder.release()
        self.assertFalse(thread.is_alive())
        self.assert_gone(grandchild)

    def test_a_child_fenced_after_the_loss_is_killed_immediately(self):
        lease = FakeLease()
        lease.lose = True
        holder = rf.AutoscalerTargetLease(lease)
        with self.assertRaises(RuntimeError):
            holder.guard()
        started = time.monotonic()
        result = rf.run_fenced([sys.executable, "-c", "import time; time.sleep(60)"], timeout=60, holder=holder)
        self.assertLess(result.returncode, 0)  # never got to run its course
        self.assertLess(time.monotonic() - started, 8)

    def test_a_timed_out_fenced_child_loses_its_whole_group(self):
        thread, grandchild, outcome = self.start_fenced(None, timeout=1)
        thread.join(15)
        self.assertIn("timeout", outcome)
        self.assert_gone(grandchild)

    def test_the_heartbeat_checks_every_few_seconds(self):
        # A workstation that wakes from sleep must notice a taken-over lease well inside the 60 s stale grace.
        self.assertLessEqual(rf.AutoscalerTargetLease.HEARTBEAT_SECONDS, 5)


class FencedLaunchTests(unittest.TestCase):
    def test_a_launch_stops_when_the_lease_is_lost_while_the_manager_runs(self):
        import test_launch_failover as tlf
        from lib import spot_runner_autoscaler as controller
        harness = tlf.LaunchFailoverTests("test_failure_memory_survives_a_controller_restart")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        lease = FakeLease()
        holder = rf.AutoscalerTargetLease(lease)
        calls = []

        def fenced(command, *, timeout, holder):
            calls.append((command, timeout, holder))
            result = harness.cloud.manager(command, timeout=timeout)
            lease.lose = True
            with self.assertRaises(RuntimeError):
                holder.guard()  # the heartbeat noticed while the manager ran
            return result
        with patch.object(controller, "_FLOOR_LEASE_HOLDER", holder), patch.object(rf, "run_fenced", fenced):
            with self.assertRaises(RuntimeError) as caught:
                controller.launch_offer(tlf.candidate("us-west4-a"), harness.args, {}, harness.path)
        self.assertIn("runner floor lease was lost", str(caught.exception))
        self.assertIs(calls[0][2], holder)
        # Nothing acted after the loss: no cleanup delete, no second candidate.
        self.assertFalse([c for c in harness.cloud.commands if c[:4] == ["gcloud", "compute", "instances", "delete"]])
        self.assertEqual(len(calls), 1)

    def test_autoscaler_children_end_inside_the_lease_margin(self):
        import ast
        from lib import spot_runner_autoscaler as controller
        source = Path(controller.__file__).read_text()
        bounded, unbounded = [], []
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                    and node.func.attr in ("run", "check_output", "Popen")):
                timeout = next((k.value for k in node.keywords if k.arg == "timeout"), None)
                text = ast.unparse(timeout) if timeout is not None else "none"
                if isinstance(timeout, ast.Constant) and timeout.value <= 600 or text == "docker_timeout()":
                    bounded.append(node.lineno)
                else:
                    unbounded.append((node.lineno, text))
        # The only longer child is the deployment manager, and only on the path without a floor lease; with the
        # lease held it runs fenced (killed on loss) instead.
        self.assertEqual([text for _, text in unbounded], ["deadline + 120"])
        line = unbounded[0][0]
        self.assertIn("if _FLOOR_LEASE_HOLDER is None:", "\n".join(source.splitlines()[line - 3:line]))
        self.assertGreater(len(bounded), 10)
        self.assertGreater(gcs.LEASE_MIN_REMAINING_SECONDS, 600)


class MutationFenceTests(unittest.TestCase):
    """Every mutating child process proves the floor lease first, whichever module starts it."""

    MUTATING_WORDS = {"delete", "add-metadata", "remove-metadata", "add-labels", "remove-labels", "set-scheduling",
                      "start", "stop", "create", "attach-disk", "detach-disk", "reset", "update", "ssh", "scp",
                      "set-disk-auto-delete", "add-iam-policy-binding", "remove-iam-policy-binding"}

    def tearDown(self):
        rf.install_mutation_fence(None)

    def test_classification(self):
        mutating = [["gcloud", "compute", "instances", "delete", "vm", "--zone=z"],
                    ["gcloud", "compute", "disks", "delete", "d"], ["gcloud", "compute", "ssh", "vm", "--command=x"],
                    ["gcloud", "compute", "instances", "add-metadata", "vm"], ["gh", "api", "-X", "DELETE", "p"],
                    ["gh", "api", "--method", "PUT", "p"], ["gh", "api", "--method=POST", "p"], ["gh", "api", "-XPATCH", "p"],
                    ["gh", "api", "p", "--input", "-"], ["gh", "api", "p", "-f", "a=b"], ["gh", "run", "rerun", "1"],
                    ["ssh", "host"], [sys.executable, "src/spot_runner_manager.py", "--apply"],
                    ["gcloud", "compute", "something-new", "x"]]
        reading = [["gcloud", "compute", "instances", "list"], ["gcloud", "compute", "disks", "describe", "d"],
                   ["gcloud", "auth", "print-access-token"], ["gh", "api", "p"], ["gh", "api", "--paginate", "p"],
                   ["gh", "api", "-X", "GET", "p"], ["gh", "auth", "token", "--user", "u"], ["python3", "x.py"],
                   "gcloud compute zones list"]
        for argv in mutating:
            with self.subTest(argv=argv):
                self.assertTrue(rf.is_mutating_command(argv))
        for argv in reading:
            with self.subTest(argv=argv):
                self.assertFalse(rf.is_mutating_command(argv))

    def test_every_mutating_command_in_the_code_is_fenced(self):
        import ast
        found, missed = [], []
        root = Path(rf.__file__).resolve().parent
        for path in sorted(root.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant)
                        and node.elts[0].value in ("gcloud", "gh")):
                    continue
                argv = []
                for element in node.elts:
                    if isinstance(element, ast.Constant):
                        argv.append(str(element.value))
                    elif isinstance(element, ast.JoinedStr):
                        argv.append("".join(str(v.value) if isinstance(v, ast.Constant) else "X"
                                            for v in element.values))
                    elif not isinstance(element, ast.Starred):
                        argv.append("X")
                independent = (bool(self.MUTATING_WORDS & set(argv)) or
                               (argv[:2] == ["gh", "api"] and any(a in ("-X", "--method", "--input") for a in argv)
                                and "GET" not in argv))
                if independent:
                    found.append(f"{path.name}:{node.lineno}")
                    if not rf.is_mutating_command(argv):
                        missed.append(f"{path.name}:{node.lineno} {argv}")
        self.assertEqual(missed, [])
        # The two cleanups the review named are among the commands the fence covers.
        names = "\n".join(found)
        self.assertGreater(len(found), 25)
        self.assertIn("spot_runner_autoscaler.py", names)
        self.assertIn("vm_recovery.py", names)
        self.assertIn("runner_drain.py", names)

    def test_a_lost_lease_stops_a_mutating_child_before_it_starts(self):
        calls = []

        def lost():
            calls.append(1)
            raise RuntimeError("Fleet mutation held: the runner floor lease was lost")
        rf.install_mutation_fence(lost)
        with self.assertRaises(RuntimeError):
            subprocess.run(["gcloud", "compute", "instances", "delete", "vm", "--quiet"], capture_output=True)
        with self.assertRaises(RuntimeError):
            subprocess.Popen(["gh", "api", "-X", "DELETE", "repos/o/r/actions/runners/1"])
        reads = subprocess.run(["gcloud", "compute", "instances", "list"], capture_output=True, text=True)
        self.assertNotEqual(reads.returncode, None)  # reads still run (here against the refusing stub)
        self.assertEqual(len(calls), 2)
        rf.install_mutation_fence(None)
        subprocess.run(["gcloud", "compute", "instances", "delete", "vm"], capture_output=True)  # fence off

    def test_holding_the_lease_installs_the_fence_and_releasing_removes_it(self):
        from lib import spot_runner_autoscaler as autoscaler
        world = World()
        holder = rf.AutoscalerTargetLease.for_target(PROJECT, "xbmc4lyfe/newznab-tmux", URI, client=world.client)
        args = argparse.Namespace(project=PROJECT, target="xbmc4lyfe/newznab-tmux", org="swack-tools", once=True,
                                  interval_seconds=300)
        with patch.dict(os.environ, {rf.ENV_STATE_URI: URI}), \
                patch.object(rf.AutoscalerTargetLease, "for_target", lambda *a, **k: holder):
            held = autoscaler.acquire_floor_lease(args)
        try:
            self.assertIsNotNone(rf._MUTATION_GUARD)
        finally:
            autoscaler.release_floor_lease(held)
        self.assertIsNone(rf._MUTATION_GUARD)

    def test_a_failed_pending_read_releases_the_lease(self):
        from lib import spot_runner_autoscaler as autoscaler
        world = World()
        holder = rf.AutoscalerTargetLease.for_target(PROJECT, "xbmc4lyfe/newznab-tmux", URI, client=world.client)
        store = world.store()
        world.server.put_raw(store.object_name, b"not json")  # malformed floor state
        args = argparse.Namespace(project=PROJECT, target="xbmc4lyfe/newznab-tmux", org="swack-tools", once=True,
                                  interval_seconds=300)
        with patch.dict(os.environ, {rf.ENV_STATE_URI: URI}), \
                patch.object(rf.AutoscalerTargetLease, "for_target", lambda *a, **k: holder):
            with self.assertRaises(rf.FloorStateConflict):
                autoscaler.acquire_floor_lease(args)
        self.assertNotIn(store.lock_name, world.server.objects)  # not held for its time to live
        self.assertIsNone(autoscaler._FLOOR_LEASE_GUARD)


class GuardedCleanupTests(unittest.TestCase):
    """The two cleanups that only checked inventory freshness now prove the floor lease before each delete."""

    def setUp(self):
        import test_launch_failover as tlf
        self.harness = tlf.LaunchFailoverTests("test_failure_memory_survives_a_controller_restart")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def lost(self):
        raise RuntimeError("Fleet mutation held: the runner floor lease was lost")

    def deletes(self):
        return [c for c in self.harness.cloud.commands if "delete" in c or "DELETE" in c]

    def test_abandon_launch_disk_and_registration_cleanup(self):
        from dataclasses import asdict
        from lib import spot_runner_autoscaler as controller
        from lib.runner_fleet import GceVm
        name = "oxidex-runners-uswest4-a-abcd-x"
        vm = GceVm(name, "us-west4-a", "PROVISIONING", "c4-standard-32", True)
        for leftover in ("disk", "runner"):
            with self.subTest(leftover=leftover):
                self.harness.cloud.commands.clear()
                self.harness.cloud.disks = {"runner-data-uswest4-a-abcd-x": []} if leftover == "disk" else {}
                self.harness.cloud.runners = ([GitHubRunner(5, f"gha-{name}-1", "offline", False, ("spot",))]
                                              if leftover == "runner" else [])
                state = {"host_operation": {"kind": "launch", "vm": asdict(vm), "abandon": "deploy failed",
                                            "started_at": 0, "data_disk_name": "runner-data-uswest4-a-abcd-x"},
                         "runner_inventory_observed_at": time.time()}
                with patch.object(controller, "_FLOOR_LEASE_GUARD", self.lost):
                    with self.assertRaises(RuntimeError):
                        controller.abandon_launch(self.harness.args, state, self.harness.path)
                self.assertEqual(self.deletes(), [])
                with patch.object(controller, "_FLOOR_LEASE_GUARD", None):
                    controller.abandon_launch(self.harness.args, state, self.harness.path)
                self.assertTrue(self.deletes())  # the same cleanup runs while the lease is held

    def test_stale_registration_cleanup_after_a_delete(self):
        from dataclasses import asdict
        from lib import spot_runner_autoscaler as controller
        from lib.runner_fleet import GceVm
        name = "oxidex-runners-uswest4-a-abcd-y"
        vm = GceVm(name, "us-west4-a", "RUNNING", "c4-standard-32", True, instance_id="77")
        stale = [GitHubRunner(6, f"gha-{name}-1", "offline", False, ("spot",))]
        state = {"host_operation": {"kind": "delete", "vm": asdict(vm), "runner_ids": [6]},
                 "runner_inventory_observed_at": time.time()}
        with patch.object(controller, "_FLOOR_LEASE_GUARD", self.lost):
            with self.assertRaises(RuntimeError):
                controller.reconcile_host_operation([], stale, self.harness.args, state, self.harness.path)
        self.assertEqual(self.deletes(), [])
        with patch.object(controller, "_FLOOR_LEASE_GUARD", None):
            controller.reconcile_host_operation([], stale, self.harness.args, state, self.harness.path)
        self.assertTrue(self.deletes())


class LabelCaseTests(unittest.TestCase):
    def test_labels_compare_without_case(self):
        for text, message in (("o/r:secret=s,labels=Spot", "duplicates a label"),
                              ("o/r:secret=s,labels=fast+FAST", "listed twice"),
                              ("o/r:secret=s,labels=us-central1", "duplicates a label"),
                              ("o/r:secret=s,labels=N2D-standard-4", "duplicates a label")):
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                rf.parse_targets(text)
            self.assertIn(message, str(caught.exception))
        built = rf.FloorTarget("o/r", "s", labels=("SPOT", "Fast", "fast"))  # not through the parser
        self.assertEqual(built.custom_labels("n2d-standard-4", "us-central1"),
                         ("spot", "n2d-standard-4", "us-central1", "Fast"))
        t = rf.parse_targets("o/r:secret=s,labels=Ubuntu-Latest")[0]
        self.assertEqual(t.custom_labels("n2d-standard-4", "us-central1"),
                         ("spot", "n2d-standard-4", "us-central1", "Ubuntu-Latest"))

    def test_a_launch_finishes_when_github_reports_another_case(self):
        world = World("xbmc4lyfe/newznab-tmux:secret=s,account=xbmc4lyfe,labels=Ubuntu-Latest")
        world.tick()
        op = world.store().record["operation"]
        world.bootstrap(op["name"], labels=("SPOT", "n2d-standard-4", "us-central1", "ubuntu-latest"))
        world.tick()
        record = world.store().record
        self.assertNotIn("operation", record)
        self.assertFalse([e for e in world.log if e[1] == "set_labels"])  # equal without case: no rewrite


class LateCreateTests(unittest.TestCase):
    def late_world(self, runner_status=None):
        world = World()
        world.create_error = "deadline exceeded"
        world.tick()
        late = world.store().record["late_creates"][0]
        # Meanwhile another floor host serves the target.
        world.hosts.append(host(name="oxidex-runners-other", started=NOW))
        world.runners["xbmc4lyfe/newznab-tmux"] = [runner("gha-oxidex-runners-other-1")]
        world.hosts.append(rf.Host(late["name"], late["zone"], "RUNNING", "777", "n2d-standard-4", True,
                                   target_label_value("xbmc4lyfe/newznab-tmux"), True, gce_time(NOW)))
        if runner_status:
            world.runners["xbmc4lyfe/newznab-tmux"].append(
                runner(f"gha-{late['name']}-1", status=runner_status[0], busy=runner_status[1], runner_id=55))
        return world, late

    def test_an_excess_late_vm_is_retired_not_promoted(self):
        world, late = self.late_world(("offline", False))
        plan = rf.plan_target(world.config.target("xbmc4lyfe/newznab-tmux"), world.hosts,
                              world.runners["xbmc4lyfe/newznab-tmux"], world.store().record, NOW)
        self.assertEqual(plan.action, "retire_late")
        world.tick()
        kinds = [e[:2] for e in world.log]
        self.assertLess(kinds.index(("github", "delete_runner")), kinds.index(("provider", "delete_instance")))
        self.assertIn(("provider", "delete_instance", late["name"], "777"), world.log)
        record = world.store().record
        self.assertNotIn("operation", record)
        self.assertEqual(record["late_creates"], [])
        self.assertNotEqual((record.get("host") or {}).get("name"), late["name"])

    def test_a_late_vm_whose_runner_is_online_or_busy_holds(self):
        for status in (("online", False), ("offline", True)):
            with self.subTest(status=status):
                world, late = self.late_world(status)
                world.tick()
                self.assertFalse([e for e in world.log if e[1] in ("delete_instance", "delete_runner")])
                entry = world.store().record["late_creates"][0]
                self.assertEqual(entry["instance_id"], "777")  # pinned for every later check

    def test_a_late_vm_recreated_under_the_name_is_left_alone(self):
        world2, late2 = self.late_world(("online", False))
        world2.tick()  # pins 777 and holds
        world2.replace_host(late2["name"], instance_id="888")
        # The new instance registered a runner under the same name: it must not be deregistered either.
        world2.runners["xbmc4lyfe/newznab-tmux"] = [
            runner(r.name, status="offline", runner_id=55) if r.runner_id == 55 else r
            for r in world2.runners["xbmc4lyfe/newznab-tmux"]]
        world2.tick()
        self.assertFalse([e for e in world2.log if e[1] in ("delete_instance", "delete_runner")])
        self.assertEqual(world2.store().record["late_creates"], [])

    def test_without_another_serving_host_the_late_vm_is_adopted(self):
        world = World()
        world.create_error = "deadline exceeded"
        world.tick()
        late = world.store().record["late_creates"][0]
        world.hosts.append(rf.Host(late["name"], late["zone"], "RUNNING", "777", "n2d-standard-4", True,
                                   target_label_value("xbmc4lyfe/newznab-tmux"), True, gce_time(NOW)))
        world.tick()
        self.assertEqual(world.store().record["operation"]["instance_id"], "777")



class Round6Tests(unittest.TestCase):
    def test_abandon_rereads_github_immediately_before_the_delete(self):
        world = World()
        world.tick()
        op = world.store().record["operation"]
        world.replace_host(op["name"], status="RUNNING")
        world.clock[0] += rf.LAUNCH_DEADLINE_SECONDS + 1
        name = op["expected_runners"][0]
        world.runners["xbmc4lyfe/newznab-tmux"] = [runner(name, status="offline", runner_id=77)]
        original = world.provider.remove_startup_script

        def came_online(h):
            original(h)  # meanwhile the runner registers and takes a job
            world.runners["xbmc4lyfe/newznab-tmux"] = [runner(name, busy=True, runner_id=77)]
        world.provider.remove_startup_script = came_online
        world.tick()
        self.assertFalse([e for e in world.log if e[1] == "delete_instance"])
        self.assertIn("operation", world.store().record)

    def test_retire_late_rereads_github_immediately_before_the_delete(self):
        world, late = LateCreateTests().late_world(("offline", False))
        original = world.provider.remove_startup_script

        def came_online(h):
            original(h)
            world.runners["xbmc4lyfe/newznab-tmux"].append(runner(f"gha-{late['name']}-1", runner_id=56))
        world.provider.remove_startup_script = came_online
        world.tick()
        self.assertFalse([e for e in world.log if e[1] == "delete_instance"])
        self.assertTrue(world.store().record["late_creates"])  # still watched

    def test_the_extra_label_limit_matches_the_metadata_record(self):
        from lib.runner_fleet import MAX_EXTRA_RUNNER_LABELS, runner_deployment_from_gce_item
        self.assertEqual(MAX_EXTRA_RUNNER_LABELS, 16)
        at_limit = "+".join(f"l{i}" for i in range(16))
        self.assertEqual(len(rf.parse_targets(f"o/r:secret=s,labels={at_limit}")[0].labels), 16)
        with self.assertRaises(ValueError) as caught:
            rf.parse_targets(f"o/r:secret=s,labels={at_limit}+l16")
        self.assertIn("at most 16", str(caught.exception))
        # Case-only duplicates are refused before counting, never counted twice or silently dropped.
        with self.assertRaises(ValueError):
            rf.parse_targets(f"o/r:secret=s,labels={at_limit}+L0")
        item = {"metadata": {"items": [{"key": "spot-runner-extra-labels", "value": at_limit}]}}
        self.assertEqual(len(runner_deployment_from_gce_item(item)["runner_extra_labels"]), 16)
        from lib import runner_manager
        args = SimpleNamespace(extra_runner_labels=tuple(f"l{i}" for i in range(17)), target="o/r", runner_prefix="gha",
                               runner_count=1, runner_label="spot", region="us-central1", heavy_runner_pool=None,
                               org="swack-tools", runner_group_id=None)
        with self.assertRaises(RuntimeError):
            runner_manager.runner_patch_record(args, None)

    def test_terminal_events_are_in_the_saved_history(self):
        world = World()
        world.tick()
        op = world.store().record["operation"]
        world.bootstrap(op["name"])
        world.tick()
        events = [row["event"] for row in world.store().record["history"]]
        self.assertEqual(events[-1], "ready")
        world = World()
        world.create_error = "ZONE_RESOURCE_POOL_EXHAUSTED"
        world.tick()
        self.assertEqual(world.store().record["history"][-1]["event"], "launch_failed")
        world = World()
        world.groups[("swack-tools", 10)] = {"id": 10, "name": "Default", "visibility": "all", "default": True}
        world.tick(only=("swack-tools",))
        self.assertEqual(world.store("swack-tools").record["history"][-1]["event"], "launch_held")


class OrphanedOperationTests(unittest.TestCase):
    def test_deadlines(self):
        self.assertEqual(rf.operation_deadline({"kind": "launch", "phase": "creating", "started_at": 100}),
                         100 + rf.CREATE_CONFIRM_SECONDS + rf.ORPHAN_GRACE_SECONDS)
        self.assertEqual(rf.operation_deadline({"kind": "launch", "phase": "booting", "started_at": 50,
                                                "created_at": 100}),
                         100 + rf.LAUNCH_DEADLINE_SECONDS + rf.ORPHAN_GRACE_SECONDS)
        self.assertEqual(rf.operation_deadline({"kind": "restart", "started_at": 100}),
                         100 + rf.RESTART_DEADLINE_SECONDS + rf.ORPHAN_GRACE_SECONDS)
        # A running keeper ends every operation well before that: the grace is several ticks.
        from lib import control_watchdog_deploy as deploy
        self.assertGreaterEqual(rf.ORPHAN_GRACE_SECONDS, 5 * 180)
        self.assertEqual(deploy.DEFAULT_SCHEDULE, "*/3 * * * *")

    def store_op(self, world, operation):
        store = world.store()
        lease = world.lease(store.lock_name)
        self.assertTrue(lease.acquire())
        store.record["operation"] = operation
        store.save(lease)
        lease.release()

    def test_a_target_removed_mid_launch_doesnt_block_the_autoscaler_forever(self):
        from lib import spot_runner_autoscaler as autoscaler
        world = World()
        started = time.time() - rf.LAUNCH_DEADLINE_SECONDS - rf.ORPHAN_GRACE_SECONDS - 1
        operation = {"kind": "launch", "phase": "booting", "name": "oxidex-runners-n", "zone": "us-central1-a",
                     "instance_id": "500", "started_at": started, "created_at": started}
        self.store_op(world, operation)
        holder = rf.AutoscalerTargetLease.for_target(PROJECT, "xbmc4lyfe/newznab-tmux", URI, client=world.client)
        scrubbed = []
        args = argparse.Namespace(project=PROJECT, target="xbmc4lyfe/newznab-tmux", org="swack-tools", once=True,
                                  interval_seconds=300)
        with patch.dict(os.environ, {rf.ENV_STATE_URI: URI}), \
                patch.object(rf.AutoscalerTargetLease, "for_target", lambda *a, **k: holder), \
                patch.object(rf, "scrub_orphaned_token", lambda project, op: scrubbed.append(op) or "scrubbed"):
            held = autoscaler.acquire_floor_lease(args)
        try:
            self.assertIs(held, holder)  # it went ahead instead of waiting
            self.assertEqual(scrubbed[0]["instance_id"], "500")
        finally:
            autoscaler.release_floor_lease(held)
        # The floor record is the keeper's: the autoscaler changed nothing in it.
        self.assertEqual(world.store().record["operation"], operation)

    def test_a_live_operation_still_waits(self):
        world = World()
        self.store_op(world, {"kind": "restart", "name": "x", "started_at": time.time()})
        holder = rf.AutoscalerTargetLease.for_target(PROJECT, "xbmc4lyfe/newznab-tmux", URI, client=world.client)
        self.assertIsNotNone(holder.pending_operation())
        self.assertIsNone(holder.orphaned)
        self.assertIsNone(holder.pending_operation(now=time.time() + rf.RESTART_DEADLINE_SECONDS
                                                   + rf.ORPHAN_GRACE_SECONDS + 1))
        self.assertIsNotNone(holder.orphaned)

    def test_the_scrub_touches_only_the_recorded_instance_and_deletes_nothing(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if "describe" in argv:
                return SimpleNamespace(returncode=0, stdout=json.dumps(
                    {"id": instance, "metadata": {"items": [{"key": "startup-script"}]}}), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        op = {"kind": "launch", "name": "oxidex-runners-n", "zone": "us-central1-a", "instance_id": "500"}
        instance = "500"
        self.assertIn("removed", rf.scrub_orphaned_token(PROJECT, op, run=run))
        self.assertEqual(calls[-1][:4], ["gcloud", "compute", "instances", "remove-metadata"])
        self.assertIn("--keys=startup-script", calls[-1])
        calls.clear()
        instance = "999"
        self.assertIn("another instance", rf.scrub_orphaned_token(PROJECT, op, run=run))
        self.assertEqual(len(calls), 1)
        calls.clear()
        self.assertIn("no confirmed instance", rf.scrub_orphaned_token(PROJECT, dict(op, instance_id=""), run=run))
        self.assertEqual(calls, [])
        self.assertFalse([c for c in calls if "delete" in c])
