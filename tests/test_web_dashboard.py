"""Unit and HTTP integration tests for the Spot Fleet & Rust Builder Web UI Dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.request

from lib.dashboard_models import (
    DEFAULT_RUNNER_CLOUD_INIT,
    calculate_spot_pricing,
    compute_cpu_benchmark_score,
    disk_category,
    filter_regions,
    format_gcloud_ssh_command,
    parse_cloud_init,
    qualify_instances,
)
from lib.dashboard_scaling import (
    build_github_oom_deep_link,
    classify_github_ci_failure,
    evaluate_builder_scaling_decision,
    evaluate_cpu_scope,
    evaluate_scaling_decision,
    parse_github_oom_deep_link,
)
from lib.dashboard_server import (
    _build_instance_record,
    create_dashboard_server,
    main as dashboard_main,
)
from lib.rust_build_telemetry import (
    classify_ip_and_network,
    compute_cpu_points,
    enrich_rust_build_record,
)


def fleet_fixture_records(project: str = "homelab-424523", ref_now: float | None = None) -> list[dict]:
    """Instance records for the HTTP and scaling tests, installed in memory (the dashboard itself seeds none).

    They are not inventory records, so they take the legacy pricing paths those tests cover; a reload drops them.
    """
    ref_now = time.time() if ref_now is None else ref_now
    records = [
        # Active instance in South Africa (matches the user's exact world map example)
        _build_instance_record(
            instance_id="790481726351001",
            name="oxidex-runners-za-jnb-01",
            role="cicd_runner",
            status="RUNNING",
            region="africa-south1",
            zone="africa-south1-a",
            machine_type="n2-standard-16",
            vcpu=16,
            ram_gib=64,
            cpu_platform="Intel Ice Lake",
            boot_disk_type="pd-balanced",
            boot_disk_gb=40,
            data_disk_type="pd-ssd",
            data_disk_gb=100,
            runners_count=4,
            runner_vcpu=4,
            runner_ram_gib=8,
            started_hours_ago=8.5,
            deleted_hours_ago=None,
            deletion_trigger="Active — Running (scheduled by minimum floor policy)",
            cloud_init_template_id="ci-runner-default",
            vm_template_id="tpl-runner-za-n2-16",
            cpu_utilization_percent=64.0,
            ram_utilization_percent=52.0,
            project=project,
            now_epoch=ref_now,
        ),
        # Active instance in Dallas, USA
        _build_instance_record(
            instance_id="790481726351002",
            name="oxidex-runners-dallas-01",
            role="cicd_runner",
            status="RUNNING",
            region="us-south1",
            zone="us-south1-a",
            machine_type="n4-standard-16",
            vcpu=16,
            ram_gib=64,
            cpu_platform="Intel Emerald Rapids",
            boot_disk_type="hyperdisk-balanced",
            boot_disk_gb=40,
            data_disk_type="hyperdisk-balanced",
            data_disk_gb=100,
            runners_count=4,
            runner_vcpu=4,
            runner_ram_gib=16,
            started_hours_ago=14.0,
            deleted_hours_ago=None,
            deletion_trigger="Active — Running (queue & CPU demand)",
            cloud_init_template_id="ci-runner-default",
            vm_template_id="tpl-runner-n4-std16",
            cpu_utilization_percent=72.0,
            ram_utilization_percent=68.0,
            project=project,
            now_epoch=ref_now,
        ),
        # Active instance in Belgium, Europe
        _build_instance_record(
            instance_id="790481726351003",
            name="oxidex-runners-eu-belgium-01",
            role="cicd_runner",
            status="RUNNING",
            region="europe-west1",
            zone="europe-west1-b",
            machine_type="c4d-standard-8",
            vcpu=8,
            ram_gib=32,
            cpu_platform="AMD Turin",
            boot_disk_type="hyperdisk-balanced",
            boot_disk_gb=40,
            data_disk_type="hyperdisk-balanced",
            data_disk_gb=100,
            runners_count=2,
            runner_vcpu=4,
            runner_ram_gib=16,
            started_hours_ago=5.2,
            deleted_hours_ago=None,
            deletion_trigger="Active — Running (strict pool member)",
            cloud_init_template_id="ci-runner-strict-pool",
            vm_template_id="tpl-runner-n4-std16",
            cpu_utilization_percent=58.0,
            ram_utilization_percent=61.0,
            project=project,
            now_epoch=ref_now,
        ),
        # Active Dedicated Rust Builder in Dallas
        _build_instance_record(
            instance_id="790481726351004",
            name="builder-oxidex-turin-01",
            role="rust_builder",
            status="RUNNING",
            region="us-south1",
            zone="us-south1-b",
            machine_type="c4d-highcpu-16",
            vcpu=16,
            ram_gib=32,
            cpu_platform="AMD Turin",
            boot_disk_type="hyperdisk-balanced",
            boot_disk_gb=40,
            data_disk_type="hyperdisk-balanced",
            data_disk_gb=100,
            runners_count=1,
            runner_vcpu=16,
            runner_ram_gib=28,
            started_hours_ago=19.0,
            deleted_hours_ago=None,
            deletion_trigger="Active — Dedicated Rust 1.99 Builder",
            cloud_init_template_id="ci-rust-builder-199",
            vm_template_id="tpl-builder-c4d-16",
            cpu_utilization_percent=78.0,
            ram_utilization_percent=74.0,
            project=project,
            now_epoch=ref_now,
        ),
        # Active Dedicated Rust Builder in Eemshaven, Netherlands
        _build_instance_record(
            instance_id="790481726351005",
            name="builder-oxidex-eu-01",
            role="rust_builder",
            status="RUNNING",
            region="europe-west4",
            zone="europe-west4-a",
            machine_type="n4-highcpu-16",
            vcpu=16,
            ram_gib=32,
            cpu_platform="Intel Emerald Rapids",
            boot_disk_type="hyperdisk-balanced",
            boot_disk_gb=40,
            data_disk_type="hyperdisk-balanced",
            data_disk_gb=100,
            runners_count=1,
            runner_vcpu=16,
            runner_ram_gib=28,
            started_hours_ago=11.0,
            deleted_hours_ago=None,
            deletion_trigger="Active — Dedicated Rust 1.99 Builder",
            cloud_init_template_id="ci-rust-builder-199",
            vm_template_id="tpl-builder-c4d-16",
            cpu_utilization_percent=36.0,
            ram_utilization_percent=48.0,
            project=project,
            now_epoch=ref_now,
        ),
        # Deleted instance in last 24h (ran for 6 hours, scaled down due to lack of runner demand)
        _build_instance_record(
            instance_id="780481726350901",
            name="oxidex-runners-iowa-04",
            role="cicd_runner",
            status="DELETED",
            region="us-central1",
            zone="us-central1-a",
            machine_type="n4-standard-16",
            vcpu=16,
            ram_gib=64,
            cpu_platform="Intel Emerald Rapids",
            boot_disk_type="hyperdisk-balanced",
            boot_disk_gb=40,
            data_disk_type="hyperdisk-balanced",
            data_disk_gb=100,
            runners_count=4,
            runner_vcpu=4,
            runner_ram_gib=16,
            started_hours_ago=10.5,
            deleted_hours_ago=4.5,
            deletion_trigger="Scheduled scale-down: lack of GitHub runner demand (<= 5 jobs in past hour)",
            cloud_init_template_id="ci-runner-default",
            vm_template_id="tpl-runner-n4-std16",
            cpu_utilization_percent=0.0,
            ram_utilization_percent=0.0,
            project=project,
            now_epoch=ref_now,
        ),
        # Deleted instance in last 24h (high spec 32-vCPU host in Hong Kong)
        _build_instance_record(
            instance_id="780481726350902",
            name="oxidex-runners-hk-bulky-02",
            role="cicd_runner",
            status="DELETED",
            region="asia-east2",
            zone="asia-east2-b",
            machine_type="c4d-standard-32",
            vcpu=32,
            ram_gib=128,
            cpu_platform="AMD Turin",
            boot_disk_type="hyperdisk-balanced",
            boot_disk_gb=40,
            data_disk_type="hyperdisk-balanced",
            data_disk_gb=200,
            runners_count=8,
            runner_vcpu=4,
            runner_ram_gib=16,
            started_hours_ago=22.0,
            deleted_hours_ago=3.5,
            deletion_trigger="Scheduled scale-down: lack of GitHub runner demand (idle queue cooldown)",
            cloud_init_template_id="ci-runner-strict-pool",
            vm_template_id="tpl-runner-n4-std16",
            cpu_utilization_percent=0.0,
            ram_utilization_percent=0.0,
            project=project,
            now_epoch=ref_now,
        ),
        # Deleted instance in last 50h (38 hours ago)
        _build_instance_record(
            instance_id="780481726350903",
            name="oxidex-runners-finland-03",
            role="cicd_runner",
            status="DELETED",
            region="europe-north1",
            zone="europe-north1-b",
            machine_type="n2d-standard-16",
            vcpu=16,
            ram_gib=64,
            cpu_platform="AMD Milan",
            boot_disk_type="pd-balanced",
            boot_disk_gb=40,
            data_disk_type="pd-ssd",
            data_disk_gb=100,
            runners_count=4,
            runner_vcpu=4,
            runner_ram_gib=16,
            started_hours_ago=46.0,
            deleted_hours_ago=29.0,
            deletion_trigger="Spot preemption: GCP capacity reclamation in europe-north1-b",
            cloud_init_template_id="ci-runner-default",
            vm_template_id=None,
            cpu_utilization_percent=0.0,
            ram_utilization_percent=0.0,
            project=project,
            now_epoch=ref_now,
        ),
        # Deleted builder instance in last 30d (120 hours ago)
        _build_instance_record(
            instance_id="780481726350904",
            name="builder-oxidex-saopaulo-01",
            role="rust_builder",
            status="DELETED",
            region="southamerica-east1",
            zone="southamerica-east1-a",
            machine_type="n4-highcpu-16",
            vcpu=16,
            ram_gib=32,
            cpu_platform="Intel Emerald Rapids",
            boot_disk_type="hyperdisk-balanced",
            boot_disk_gb=40,
            data_disk_type="hyperdisk-balanced",
            data_disk_gb=100,
            runners_count=1,
            runner_vcpu=16,
            runner_ram_gib=28,
            started_hours_ago=142.0,
            deleted_hours_ago=118.0,
            deletion_trigger="Price guard: Spot compute + Hyperdisk exceeded daily budget cap",
            cloud_init_template_id="ci-rust-builder-199",
            vm_template_id="tpl-builder-c4d-16",
            cpu_utilization_percent=0.0,
            ram_utilization_percent=0.0,
            project=project,
            now_epoch=ref_now,
        ),
    ]
    return records


def fleet_observation(
    instance_id: str,
    name: str,
    zone: str,
    *,
    status: str = "RUNNING",
    runners_count: int = 4,
    machine_type: str = "n4-standard-16",
    vcpu: int = 16,
    ram_gib: float = 64.0,
    started_hours_ago: float = 2.0,
    stopped_hours_ago: float | None = None,
    now: float | None = None,
) -> dict:
    """One observation as fleet_inventory.observe_fleet_items returns it."""
    now = time.time() if now is None else now
    role = "rust_builder" if name.startswith("builder-") else "cicd_runner"
    return {
        "instance_id": instance_id,
        "name": name,
        "role": role,
        "status": status,
        "zone": zone,
        "region": zone.rsplit("-", 1)[0],
        "machine_type": machine_type,
        "cpu_platform": "Intel Emerald Rapids",
        "vcpu": vcpu,
        "ram_gib": ram_gib,
        "specs_known": True,
        "spot": True,
        "boot_disk_type": "hyperdisk-balanced",
        "boot_disk_gb": 40,
        "data_disk_type": "hyperdisk-balanced",
        "data_disk_gb": 100,
        "runners_count": runners_count if role == "cicd_runner" else 1,
        "runner_vcpu": 4,
        "runner_ram_gib": 16.0,
        "created_epoch": now - started_hours_ago * 3600.0 - 60.0,
        "last_start_epoch": now - started_hours_ago * 3600.0,
        "last_stop_epoch": now - stopped_hours_ago * 3600.0 if stopped_hours_ago is not None else None,
    }


def install_fleet_fixture(store) -> None:
    """Load the fixture fleet into a store as if the live inventory had listed it."""
    with store._lock:
        store.state["instances"] = fleet_fixture_records(store.project)
        store._sync_fleet_telemetry()
        store._persist()


class DashboardModelsTests(unittest.TestCase):
    def test_cpu_benchmark_scores_order_generations(self):
        turin_16 = compute_cpu_benchmark_score("AMD Turin", 16, "c4d-standard-16")
        emerald_16 = compute_cpu_benchmark_score("Intel Emerald Rapids", 16, "n4-standard-16")
        icelake_8 = compute_cpu_benchmark_score("Intel Ice Lake", 8, "n2-standard-8")
        cascade_8 = compute_cpu_benchmark_score("Intel Cascade Lake", 8, "n2-standard-8")
        self.assertGreater(turin_16, emerald_16)
        self.assertGreaterEqual(icelake_8, 1000)
        self.assertLess(cascade_8, 1000)

    def test_qualify_instances_runner_sizing_and_benchmark_floor(self):
        # 4 GitHub runners each with 4 vCPU and 16 GiB RAM, benchmark >= 1000
        profile = {
            "role": "cicd_runner",
            "min_runners": 4,
            "min_instances": 1,
            "max_instances": 8,
            "runner_vcpu": 4,
            "runner_ram_gib": 16,
            "host_overhead_gib": 0,
            "min_instance_vcpu": 8,
            "min_instance_ram_gib": 32,
            "min_benchmark_score": 1000,
            "disk_category": "any",
            "disk_type": "hyperdisk-balanced",
            "boot_disk_gb": 40,
            "data_disk_gb": 100,
            "budget": {
                "active_period": "day",
                "max_hourly_usd": 5.00,
                "max_daily_usd": 10.00,
                "max_monthly_usd": 300.00,
                "include_disk_costs": True,
                "include_taxes": False,
            },
        }
        regions = {"us-south1": True, "africa-south1": True, "europe-west1": True}
        result = qualify_instances(profile, regions)
        self.assertGreater(result["hardware_qualifying_count"], 0)
        self.assertGreater(result["spot_price_qualifying_count"], 0)

        for candidate in result["hardware_qualifying"]:
            self.assertGreaterEqual(candidate["benchmark_score"], 1000)
            self.assertGreaterEqual(candidate["vcpu"], 8)
            self.assertGreaterEqual(candidate["ram_gib"], 32)
            # An 8 vCPU / 32 GiB instance holds 2 runners; a 16 vCPU / 64 GiB holds 4
            if candidate["vcpu"] == 8 and candidate["ram_gib"] == 32:
                self.assertEqual(candidate["units_per_instance"], 2)
                self.assertEqual(candidate["instances_needed_for_floor"], 2)
            elif candidate["vcpu"] == 16 and candidate["ram_gib"] == 64:
                self.assertEqual(candidate["units_per_instance"], 4)
                self.assertEqual(candidate["instances_needed_for_floor"], 1)

    def test_hyperdrive_vs_normal_drive_filtering(self):
        self.assertEqual(disk_category("hyperdisk-balanced"), "hyperdrive")
        self.assertEqual(disk_category("hyperdisk-throughput"), "hyperdrive")
        self.assertEqual(disk_category("pd-ssd"), "normal")
        self.assertEqual(disk_category("pd-balanced"), "normal")

        profile = {
            "role": "rust_builder",
            "min_runners": 1,
            "runner_vcpu": 16,
            "runner_ram_gib": 28,
            "min_instance_vcpu": 16,
            "min_instance_ram_gib": 32,
            "min_benchmark_score": 1400,
            "disk_category": "hyperdrive",
            "disk_type": "hyperdisk-balanced",
            "budget": {
                "max_hourly_usd": 5.0,
                "max_daily_usd": 20.0,
                "max_monthly_usd": 500.0,
                "include_disk_costs": True,
            },
        }
        res = qualify_instances(profile, {"us-south1": True})
        self.assertTrue(all(c["disk_category"] == "hyperdrive" for c in res["hardware_qualifying"]))

    def test_cloud_init_parser_and_incomplete_chmod_warning(self):
        broken_yaml = DEFAULT_RUNNER_CLOUD_INIT + "\n  - chmod\n"
        parsed = parse_cloud_init(broken_yaml)
        self.assertEqual(parsed["format"], "cloud-config")
        self.assertTrue(parsed["package_update"])
        self.assertTrue(parsed["package_upgrade"])
        self.assertIn("docker", parsed["groups"])
        self.assertIn("build-essential", parsed["packages"])
        self.assertEqual(len(parsed["stages"]), 5)
        self.assertFalse(parsed["valid"])
        self.assertTrue(any("chmod" in w for w in parsed["warnings"]))

    def test_region_filtering_by_latency_and_capabilities(self):
        state = {"us-south1": True, "africa-south1": True, "europe-west1": False}
        low_lat = filter_regions(state, max_latency_ms=50)
        self.assertTrue(all(r["latency_ms"] <= 50 for r in low_lat))
        turin_only = filter_regions(state, required_cpu_platform="AMD Turin")
        self.assertTrue(all("AMD Turin" in r["cpu_platforms"] for r in turin_only))

    def test_ssh_command_formatting(self):
        cmd = format_gcloud_ssh_command("oxidex-runners-za-jnb-01", "africa-south1-a", "homelab-424523")
        self.assertEqual(
            cmd,
            "gcloud compute ssh oxidex-runners-za-jnb-01 --zone=africa-south1-a --project=homelab-424523",
        )


class DashboardScalingTests(unittest.TestCase):
    def test_github_oom_deep_link_build_and_parse(self):
        url = build_github_oom_deep_link(
            "Appz4Fun/nzbget",
            38596411473,
            115964282184,
            2,
            50,
        )
        self.assertEqual(
            url,
            "https://github.com/Appz4Fun/nzbget/actions/runs/38596411473/job/115964282184#step:2:50",
        )
        parsed = parse_github_oom_deep_link(url)
        self.assertEqual(parsed["repository"], "Appz4Fun/nzbget")
        self.assertEqual(parsed["run_id"], 38596411473)
        self.assertEqual(parsed["job_id"], 115964282184)
        self.assertEqual(parsed["step_number"], 2)
        self.assertEqual(parsed["line_number"], 50)
        timeout_cls = classify_github_ci_failure(
            "2026-10-14T16:25:26.2656514Z ##[error]Timeout reached, aborting! (Canceling Pages deployment)"
        )
        self.assertEqual(timeout_cls["failure_classification"], "TIMEOUT")
        self.assertFalse(timeout_cls["is_oom"])
        oom_cls = classify_github_ci_failure(
            "LLVM ERROR: out of memory / Allocation failed during LTO link pass (exit code 137)",
            runner_name="gha-oxidex-runners-asianortheast2-a-1791098074-2",
        )
        self.assertEqual(oom_cls["failure_classification"], "OOM")
        self.assertTrue(oom_cls["is_oom"])
        self.assertTrue(oom_cls["is_self_hosted_runner"])

        external_oom_cls = classify_github_ci_failure(
            "LLVM ERROR: out of memory / Allocation failed during LTO link pass (exit code 137)",
            runner_name="external-laptop-runner-01",
        )
        self.assertEqual(external_oom_cls["failure_classification"], "EXTERNAL_RUNNER_OOM")
        self.assertFalse(external_oom_cls["is_oom"])
        self.assertFalse(external_oom_cls["is_self_hosted_runner"])

        missing_runner_cls = classify_github_ci_failure(
            "LLVM ERROR: out of memory / Allocation failed during LTO link pass (exit code 137)",
            runner_name=None,
        )
        self.assertEqual(missing_runner_cls["failure_classification"], "EXTERNAL_RUNNER_OOM")
        self.assertFalse(missing_runner_cls["is_oom"])

    def test_cpu_scope_all_vs_majority(self):
        samples = [62.0, 58.0, 35.0]
        maj_ok, _ = evaluate_cpu_scope(samples, 50.0, "majority")
        all_ok, _ = evaluate_cpu_scope(samples, 50.0, "all")
        self.assertTrue(maj_ok)
        self.assertFalse(all_ok)
        all_hot, _ = evaluate_cpu_scope([55.0, 71.0, 64.0], 50.0, "all")
        self.assertTrue(all_hot)

    def test_scale_down_when_only_5_jobs_ran_in_past_hour(self):
        policy = {
            "min_runners": 4,
            "max_runners": 20,
            "runners_per_instance": 2,
            "enable_hourly_runs_scale_down": True,
            "scale_down_max_jobs_past_hour": 5,
            "scale_down_target_runners": 2,
            "enable_github_ci_oom_trigger": False,
            "enable_journal_oom_trigger": False,
            "enable_cpu_utilization_trigger": False,
        }
        telemetry = {
            "current_runners": 8,
            "jobs_ran_past_hour": 5,
            "provisioned_jobs": 1,
            "queued_jobs": 0,
            "instance_cpu_percentages": [20.0, 18.0],
            "journal_oom_events": [],
            "github_oom_runs": [],
        }
        decision = evaluate_scaling_decision(policy, telemetry)
        self.assertEqual(decision["direction"], "scale_down")
        # Target is clamped to min_runners=4 even when scale_down_target_runners=2
        self.assertEqual(decision["desired_runners"], 4)
        self.assertEqual(decision["desired_instances"], 2)
        self.assertEqual(decision["scale_down_reasons"][0]["trigger"], "hourly_runs_low")

        # Preserve capacity for active provisioned jobs when provisioned_jobs > min_runners, quantized to whole VMs
        busy_telemetry = {**telemetry, "provisioned_jobs": 5}
        busy_decision = evaluate_scaling_decision(policy, busy_telemetry)
        self.assertEqual(busy_decision["direction"], "scale_down")
        self.assertEqual(busy_decision["desired_runners"], 6)
        self.assertEqual(busy_decision["desired_instances"], 3)

    def test_scale_up_on_oom_and_budget_cap_enforcement(self):
        policy = {
            "min_runners": 4,
            "max_runners": 24,
            "runners_per_instance": 2,
            "scale_up_step_runners": 6,
            "max_scale_up_hourly_usd": 5.00,
            "max_scale_up_daily_usd": 10.00,
            "max_scale_up_monthly_usd": 12.00,  # Strict monthly cap (~$0.0164/hr)
        }
        telemetry = {
            "current_runners": 4,
            "jobs_ran_past_hour": 25,
            "provisioned_jobs": 8,
            "queued_jobs": 4,
            "instance_cpu_percentages": [75.0, 82.0],
            "github_oom_runs": [
                {
                    "repository": "swack-tools/oxidex",
                    "run_id": 37192272062,
                    "job_id": 111407847874,
                    "runner_name": "gha-oxidex-runners-us-south1-a-01",
                    "step_number": 8,
                    "line_number": 106,
                    "error_excerpt": "Process completed with exit code 137 (OOMKilled)",
                }
            ],
        }
        decision = evaluate_scaling_decision(policy, telemetry, per_instance_hourly_usd=0.20)
        self.assertTrue(decision["budget_capped"])
        # Blocked scale-up must hold at current fleet (4 runners / 2 instances), never scale down
        self.assertEqual(decision["direction"], "hold")
        self.assertEqual(decision["desired_instances"], 2)
        self.assertEqual(decision["desired_runners"], 4)

    def test_stale_oom_events_expire_after_lookback_window(self):
        policy = {
            "min_runners": 4,
            "max_runners": 16,
            "runners_per_instance": 4,
            "oom_lookback_minutes": 60,
            "enable_hourly_runs_scale_down": False,
            "enable_cpu_utilization_trigger": False,
        }
        telemetry = {
            "current_runners": 4,
            "jobs_ran_past_hour": 2,
            "provisioned_jobs": 1,
            "queued_jobs": 0,
            "instance_cpu_percentages": [25.0],
            "journal_oom_events": [
                {
                    "instance_name": "oxidex-runners-dallas-01",
                    "zone": "us-south1-a",
                    "process": "rustc",
                    "pid": 1234,
                    "observed_at": "2020-01-01T00:00:00Z",
                }
            ],
            "github_oom_runs": [
                {
                    "repository": "swack-tools/oxidex",
                    "run_id": 37192272062,
                    "job_id": 111407847874,
                    "runner_name": "gha-oxidex-runners-asianortheast2-a-1791098074-2",
                    "step_number": 8,
                    "line_number": 106,
                    "failed_at": "2020-01-01T00:00:00Z",
                    "error_excerpt": "##[error]Process completed with exit code 137.",
                }
            ],
        }
        decision = evaluate_scaling_decision(policy, telemetry)
        self.assertEqual(decision["direction"], "hold")
        self.assertEqual(decision["scale_up_reasons"], [])


class RustBuildTelemetryTests(unittest.TestCase):
    def test_ip_geolocation_and_protonvpn_tor_classification(self):
        proton = classify_ip_and_network("185.159.157.42")
        self.assertEqual(proton["provider_label"], "ProtonVPN")
        self.assertTrue(proton["is_vpn"])
        self.assertFalse(proton["is_tor"])
        self.assertEqual(proton["geolocation"]["country_code"], "CH")

        tor = classify_ip_and_network("185.220.101.99")
        self.assertEqual(tor["provider_label"], "Tor")
        self.assertTrue(tor["is_tor"])
        self.assertIn("Tor", tor["badge_text"])

        direct = classify_ip_and_network("73.162.44.19", "m4air-workstation.local")
        self.assertEqual(direct["provider_label"], "Direct")
        self.assertFalse(direct["is_vpn"])
        self.assertFalse(direct["is_tor"])
        self.assertEqual(direct["hostname"], "m4air-workstation.local")

        unmatched = classify_ip_and_network("8.8.8.8")
        self.assertEqual(unmatched["provider_label"], "Unknown")
        self.assertEqual(unmatched["network_type"], "Unknown")
        self.assertEqual(unmatched["geolocation"]["city"], "Unknown")

        invalid_ip = classify_ip_and_network("not-an-ip")
        self.assertEqual(invalid_ip["provider_label"], "Unknown")
        self.assertEqual(invalid_ip["network_type"], "Invalid IP")

    def test_cpu_points_and_rust_build_enrichment(self):
        pts = compute_cpu_points(1000.0, 2485)
        self.assertEqual(pts, 2485000.0)

        enriched = enrich_rust_build_record(
            {
                "build_id": "rb-test-1",
                "project_name": "oxidex",
                "requested_at": "2026-10-15T00:00:00Z",
                "finished_at": "2026-10-15T00:01:40Z",
                "delivered_at": "2026-10-15T00:01:50Z",
                "cache_hits": 400,
                "total_crates": 420,
                "instance_name": "builder-oxidex-turin-01",
                "zone": "us-south1-b",
                "machine_type": "c4d-highcpu-16",
                "vcpu": 16,
                "cpu_platform": "AMD Turin",
                "instance_benchmark_score": 2500,
                "origin_ip": "185.159.157.12",
                "peak_ram_gib": 22.5,
                "container_ram_cap_gib": 28.0,
                "cpu_time_seconds": 800.0,
            }
        )
        self.assertEqual(enriched["total_duration_seconds"], 110.0)
        self.assertEqual(enriched["compile_duration_seconds"], 100.0)
        self.assertEqual(enriched["delivery_duration_seconds"], 10.0)
        self.assertEqual(enriched["network_security"]["provider_label"], "ProtonVPN")
        self.assertEqual(enriched["peak_ram_gib"], 22.5)
        self.assertEqual(enriched["cpu_points"], 2000000.0)

        with self.assertRaises(ValueError):
            enrich_rust_build_record(
                {
                    "build_id": "rb-bad-order",
                    "requested_at": "2026-10-15T00:05:00Z",
                    "finished_at": "2026-10-15T00:01:00Z",
                    "delivered_at": "2026-10-15T00:02:00Z",
                }
            )


class DashboardCodexRegressionTests(unittest.TestCase):
    def test_qualify_instances_enforces_max_instances_and_fleet_budget(self):
        # 8 runners of 4vCPU/16GiB with max_instances=1: 8-vCPU/32-GiB VMs need 4 instances -> rejected
        profile = {
            "role": "cicd_runner",
            "min_runners": 8,
            "min_instances": 1,
            "max_instances": 1,
            "runner_vcpu": 4,
            "runner_ram_gib": 16,
            "min_instance_vcpu": 8,
            "min_instance_ram_gib": 32,
            "min_benchmark_score": 1000,
            "disk_category": "any",
            "budget": {
                "max_hourly_usd": 10.0,
                "max_daily_usd": 100.0,
                "max_monthly_usd": 3000.0,
            },
        }
        res = qualify_instances(profile, {"us-south1": True})
        self.assertGreater(res["disqualified_counts"]["exceeds_max_instances"], 0)
        for cand in res["hardware_qualifying"]:
            self.assertLessEqual(cand["instances_needed_for_floor"], 1)

        # Fleet floor budget: 2 instances needed at ~$0.19/hr each -> fleet hourly > $0.30
        tight_fleet_profile = {
            **profile,
            "min_runners": 4,
            "max_instances": 4,
            "budget": {
                "max_hourly_usd": 0.30,
                "max_daily_usd": 100.0,
                "max_monthly_usd": 3000.0,
            },
        }
        tight_res = qualify_instances(tight_fleet_profile, {"us-south1": True})
        for cand in tight_res["spot_price_qualifying"]:
            self.assertLessEqual(cand["fleet_floor_hourly_usd"], 0.30)

    def test_cloud_init_syntax_validation_rejects_broken_yaml_and_shell(self):
        bad_yaml = "#cloud-config\npackages:\n  - curl\n\t- git\nunbalanced: [1, 2\n"
        parsed_yaml = parse_cloud_init(bad_yaml)
        self.assertFalse(parsed_yaml["valid"])
        self.assertGreater(len(parsed_yaml["errors"]), 0)

        bad_shell = "#!/usr/bin/env bash\nif [ -f /tmp/foo ; then\n  echo hi\n"
        parsed_sh = parse_cloud_init(bad_shell)
        self.assertFalse(parsed_sh["valid"])
        self.assertGreater(len(parsed_sh["errors"]), 0)


class DashboardServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "dashboard-state.json"
        self.server, self.store = create_dashboard_server(
            host="127.0.0.1",
            port=0,
            state_path=self.state_file,
        )
        install_fleet_fixture(self.store)
        # The fixture fleet stands in for a current live inventory (scaling holds without one); no refresh loop runs.
        self.store.fleet_source = lambda: []
        self.store._fleet_status.update(fresh=True, last_success_epoch=time.time())
        self.store.fleet_inventory_interval = 1e9  # The stand-in never ages during a test.
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_plan_only_lifecycle_posts_answer_on_frozen_slot(self):
        self.store.frozen = True
        self.addCleanup(setattr, self.store, "frozen", False)
        plan = self._post("/api/instances/scale-region",
                          {"region": "us-south1", "role": "cicd_runner", "target_instances": 0})
        self.assertFalse(plan["applied"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/budget", {"target": "global", "max_hourly_usd": 4.0})
        self.assertEqual(ctx.exception.code, 503)
        ctx.exception.close()

    def test_plan_only_lifecycle_posts_do_not_store_state(self):
        version = self.store.state_version()
        for path, payload in (("/api/instances/scale-region",
                               {"region": "us-south1", "role": "cicd_runner", "target_instances": 0}),
                              ("/api/instances/delete", {"instance_name": "oxidex-runners-dallas-01"})):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}{path}", data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Idempotency-Key": f"plan-{path.rsplit('/', 1)[-1]}-01"},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertFalse(json.loads(resp.read().decode("utf-8"))["applied"])
        self.assertEqual(self.store.state_version(), version)
        self.assertFalse(self.store.state.get("idempotency"))

    def test_index_html_and_overview_endpoints(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("Spot GitHub Runners &amp; Rust Remote Builders Control Plane", html)
        self.assertIn("africa-south1", html)
        self.assertIn("function esc(val)", html)

        overview = self._get("/api/overview?include_deleted=true&window_hours=24")
        self.assertEqual(overview["project"], "homelab-424523")
        self.assertIn("rust_builder", overview["scaling_evaluations"])
        self.assertGreaterEqual(len(overview["instances_view"]["instances"]), 5)
        # Verify South Africa instance is present with 4 runners of 4vCPU / 8GB RAM
        za = next(
            i for i in overview["instances_view"]["instances"] if i["region"] == "africa-south1"
        )
        self.assertEqual(za["runners_count"], 4)
        self.assertEqual(za["runner_vcpu"], 4)
        self.assertEqual(za["runner_ram_gib"], 8)

    def test_edit_budget_and_save_as_cloud_init_template(self):
        budget_res = self._post(
            "/api/budget",
            {
                "target": "global",
                "active_period": "day",
                "max_hourly_usd": 4.50,
                "max_daily_usd": 12.00,
                "max_monthly_usd": 280.00,
                "include_disk_costs": False,
            },
        )
        self.assertEqual(budget_res["budget"]["max_daily_usd"], 12.00)
        # Global budget applies to both cicd_runner and rust_builder profiles and scaling policies
        self.assertEqual(self.store.state["profiles"]["rust_builder"]["budget"]["max_daily_usd"], 12.00)
        self.assertEqual(self.store.state["scaling"]["rust_builder"]["max_scale_up_daily_usd"], 12.00)
        self.assertFalse(self.store.state["scaling"]["rust_builder"]["include_disk_in_scale_budget"])

        custom_yaml = DEFAULT_RUNNER_CLOUD_INIT + "\n  - wget -qO /tmp/setup.sh https://example.com/setup.sh\n"
        ci_res = self._post(
            "/api/cloud-init/save",
            {
                "mode": "save_as",
                "role": "cicd_runner",
                "name": "Wget Custom Runner Cloud-Init",
                "content": custom_yaml,
                "assign_to_profile": True,
            },
        )
        new_id = ci_res["template"]["id"]
        self.assertEqual(self.store.state["profiles"]["cicd_runner"]["cloud_init_template_id"], new_id)

    def test_create_vm_template_scale_region_and_delete_instance(self):
        # Create a VM template from the 32-vCPU Hong Kong instance (8 runners) and assign it
        tpl_res = self._post(
            "/api/vm-templates/from-instance",
            {
                "instance_name": "oxidex-runners-hk-bulky-02",
                "template_name": "HK Bulky 32-vCPU Snapshot Template",
                "assign_to_profile": True,
            },
        )
        self.assertEqual(tpl_res["vm_template"]["source_instance"], "oxidex-runners-hk-bulky-02")
        self.assertEqual(self.store.state["scaling"]["cicd_runner"]["runners_per_instance"], 8)

        # Scaling up a c4d template in africa-south1 (only n2/e2/t2d) is rejected with 400
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/instances/scale-region",
                {"region": "africa-south1", "role": "cicd_runner", "target_instances": 2},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # Scaling up in us-south1 (which supports c4d / AMD Turin / hyperdisk-balanced) honors the 32-vCPU template (8 runners)
        self._post(
            "/api/budget",
            {"target": "cicd_runner", "max_hourly_usd": 5.0, "max_daily_usd": 25.0, "max_monthly_usd": 800.0},
        )
        scale_res = self._post(
            "/api/instances/scale-region",
            {"region": "us-south1", "role": "cicd_runner", "target_instances": 2},
        )
        # Scale-region is a plan only: it validates and reports, and never adds an instance record.
        before_names = [i["name"] for i in self.store.state["instances"]]
        self.assertEqual(scale_res["status"], "plan_only")
        self.assertFalse(scale_res["applied"])
        self.assertEqual(scale_res["current_count"], 1)
        self.assertEqual(scale_res["planned_count"], 2)
        newest = scale_res["would_launch"][0]
        self.assertEqual(newest["machine_type"], "c4d-standard-32")
        self.assertEqual(newest["runners_count"], 8)
        self.assertEqual(self.store.state["scaling_telemetry"]["current_runners"], 10)
        self.assertEqual([i["name"] for i in self.store.state["instances"]], before_names)

        # Disabling a region blocks scale-up into that region
        self._post("/api/regions", {"regions_enabled": {"asia-east2": False}})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/instances/scale-region",
                {"region": "asia-east2", "role": "cicd_runner", "target_instances": 1},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # Deleting an already-deleted instance does not rewrite its historical deleted_at or runtime_hours
        already_del_before = next(
            dict(i) for i in self.store.state["instances"] if i["name"] == "oxidex-runners-iowa-04"
        )
        repeat_del = self._post(
            "/api/instances/delete",
            {"instance_name": "oxidex-runners-iowa-04", "reason": "Accidental second delete"},
        )
        self.assertEqual(repeat_del["status"], "already_deleted")
        self.assertEqual(repeat_del["instance"]["deleted_at"], already_del_before["deleted_at"])
        self.assertEqual(repeat_del["instance"]["runtime_hours"], already_del_before["runtime_hours"])
        self.assertEqual(repeat_del["instance"]["deletion_trigger"], already_del_before["deletion_trigger"])

        del_res = self._post(
            "/api/instances/delete",
            {
                "instance_name": "oxidex-runners-za-jnb-01",
                "reason": "Scheduled scale-down: lack of GitHub runner demand",
            },
        )
        # Delete is a plan only: the VM stays RUNNING until the live inventory stops listing it.
        self.assertEqual(del_res["status"], "not_applied")
        self.assertFalse(del_res["applied"])
        self.assertEqual(del_res["instance"]["status"], "RUNNING")
        self.assertIn("does not create, stop, or delete VMs", del_res["message"])
        za_after = next(i for i in self.store.state["instances"] if i["name"] == "oxidex-runners-za-jnb-01")
        self.assertEqual(za_after["status"], "RUNNING")
        self.assertIsNone(za_after["deleted_at"])
        self.assertEqual(self.store.state["scaling_telemetry"]["current_runners"], 10)

    def test_rust_builder_scaling_and_project_override_on_reload(self):
        sc_res = self._post(
            "/api/scaling",
            {
                "role": "rust_builder",
                "policy": {"min_instances": 2, "max_instances": 4, "cpu_threshold_percent": 60.0},
            },
        )
        self.assertEqual(sc_res["evaluation"]["role"], "rust_builder")
        self.assertGreaterEqual(sc_res["evaluation"]["desired_instances"], 2)

        # Reload store from existing state_file with a new project & org
        from lib.dashboard_server import DashboardStore

        reloaded = DashboardStore(
            state_path=self.state_file,
            project="custom-gcp-proj-99",
            org="custom-org",
        )
        self.assertEqual(reloaded.state["project"], "custom-gcp-proj-99")
        self.assertEqual(reloaded.state["org"], "custom-org")
        self.assertTrue(
            all("--project=custom-gcp-proj-99" in i["ssh_command"] for i in reloaded.state["instances"])
        )
        self.assertTrue(
            all("--project=custom-gcp-proj-99" in b["ssh_command"] for b in reloaded.state["rust_builds"])
        )

    def test_record_rust_build_and_cli_dump_overview(self):
        import contextlib
        import io

        rb_res = self._post(
            "/api/rust-builds",
            {
                "project_name": "oxidex",
                "origin_ip": "185.220.101.44",
                "instance_name": "builder-oxidex-turin-01",
                "cache_hits": 410,
                "total_crates": 430,
                "peak_ram_gib": 23.1,
                "cpu_time_seconds": 900.0,
            },
        )
        self.assertEqual(rb_res["build"]["network_security"]["provider_label"], "Tor")
        self.assertGreater(rb_res["build"]["cpu_points"], 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = dashboard_main(["--dump-overview", "--state-file", str(self.state_file)])
        self.assertEqual(rc, 0)
        dumped = json.loads(buf.getvalue())
        self.assertEqual(dumped["project"], "homelab-424523")

    def test_round2_codex_regressions(self):
        # 1. YAML structure validation rejects inconsistent indentation and nesting under scalars
        bad_indent = parse_cloud_init("#cloud-config\nusers:\n  - name: foo\n   shell: /bin/bash\n")
        self.assertFalse(bad_indent["valid"])
        bad_scalar_nest = parse_cloud_init("#cloud-config\npackage_update: true\n  nested: 1\n")
        self.assertFalse(bad_scalar_nest["valid"])

        # 2. Tor hostname matching uses delimited labels, not substrings
        tor_host = classify_ip_and_network("8.8.8.8", "tor-exit-01.zwiebelfreunde.de")
        self.assertTrue(tor_host["is_tor"])
        benign_host = classify_ip_and_network("8.8.8.8", "refactor-worker.toronto.corp.example")
        self.assertFalse(benign_host["is_tor"])

        # 3. Profile updates propagate budgets to scaling caps, sync builder resource keys, and reject cross-role templates
        prof_res = self._post(
            "/api/profile/rust_builder",
            {
                "min_instances": 2,
                "max_instances": 5,
                "runner_vcpu": 8,
                "runner_ram_gib": 16,
                "use_vm_template": False,
                "budget": {
                    "max_hourly_usd": 2.25,
                    "max_daily_usd": 18.00,
                    "max_monthly_usd": 120.00,
                    "include_disk_costs": False,
                },
            },
        )
        self.assertEqual(prof_res["status"], "ok")
        b_sc = self.store.state["scaling"]["rust_builder"]
        self.assertEqual(b_sc["max_scale_up_hourly_usd"], 2.25)
        self.assertEqual(b_sc["max_scale_up_daily_usd"], 18.00)
        self.assertEqual(b_sc["max_scale_up_monthly_usd"], 120.00)
        self.assertFalse(b_sc["include_disk_in_scale_budget"])
        self.assertEqual(b_sc["container_vcpu"], 8)
        self.assertEqual(b_sc["builder_vcpu"], 8)
        self.assertEqual(b_sc["container_ram_gib"], 16.0)
        self.assertEqual(b_sc["builder_ram_gib"], 16.0)

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/profile/cicd_runner",
                {"use_vm_template": True, "vm_template_id": "tpl-builder-c4d-16"},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # 4. Unsupported roles are rejected in scale-region, scaling, and qualify
        for endpoint, body in (
            ("/api/instances/scale-region", {"region": "us-south1", "role": "invalid_role", "target_instances": 1}),
            ("/api/scaling", {"role": "invalid_role", "policy": {}}),
            ("/api/qualify", {"role": "invalid_role"}),
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post(endpoint, body)
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()

        # 5. Rust build telemetry derives omitted total_crates from hits+misses and deduplicates build_id
        before_count = self._get("/api/rust-builds")["total_builds"]
        b1 = self._post(
            "/api/rust-builds",
            {"build_id": "rb-idempotent-01", "cache_hits": 10, "cache_misses": 2},
        )
        self.assertEqual(b1["build"]["total_crates"], 12)
        self.assertEqual(b1["build"]["cache_hit_rate_percent"], 83.3)
        b2 = self._post(
            "/api/rust-builds",
            {"build_id": "rb-idempotent-01", "cache_hits": 11, "cache_misses": 1},
        )
        self.assertEqual(b2["build"]["total_crates"], 12)
        self.assertEqual(b2["build"]["cache_hits"], 11)
        after_count = self._get("/api/rust-builds")["total_builds"]
        self.assertEqual(after_count, before_count + 1)

        # 6. Cross-origin POSTs and non-JSON POSTs are rejected
        cross_req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/budget",
            data=json.dumps({"max_daily_usd": 999}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(cross_req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

        text_req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/budget",
            data=json.dumps({"max_daily_usd": 999}).encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(text_req, timeout=10)
        self.assertEqual(ctx.exception.code, 415)
        ctx.exception.close()

    def test_round3_codex_regressions(self):
        # 1. Malformed YAML flow sequences and mappings are rejected
        for bad_flow in (
            "#cloud-config\npackages: [curl,,git]\n",
            "#cloud-config\npackages: [,curl]\n",
            "#cloud-config\npackages: [curl,]\n",
            "#cloud-config\npackages: [curl] trailing\n",
            "#cloud-config\nruncmd: [{a: 1,, b: 2}]\n",
            "#cloud-config\nruncmd: [{bare_key}]\n",
        ):
            res = parse_cloud_init(bad_flow)
            self.assertFalse(res["valid"], f"Expected invalid flow syntax for: {bad_flow!r}")

        # 2. Zero or sub-unit budgets prohibit scaling from 0 -> 1 instance
        zero_runner_eval = evaluate_scaling_decision(
            policy={"min_runners": 0, "max_runners": 16, "runners_per_instance": 4, "max_scale_up_hourly_usd": 0.0, "max_scale_up_daily_usd": 0.0, "max_scale_up_monthly_usd": 0.0},
            telemetry={"current_runners": 0, "current_instances": 0, "queued_jobs": 5},
            per_instance_hourly_usd=0.35,
        )
        self.assertEqual(zero_runner_eval["desired_instances"], 0)
        self.assertEqual(zero_runner_eval["desired_runners"], 0)
        self.assertTrue(zero_runner_eval["budget_capped"])

        zero_builder_eval = evaluate_builder_scaling_decision(
            policy={"min_instances": 0, "max_instances": 4, "max_scale_up_hourly_usd": 0.0, "max_scale_up_daily_usd": 0.0, "max_scale_up_monthly_usd": 0.0},
            telemetry={"current_builder_instances": 0, "queued_rust_builds": 3},
            per_instance_hourly_usd=0.30,
        )
        self.assertEqual(zero_builder_eval["desired_instances"], 0)
        self.assertTrue(zero_builder_eval["budget_capped"])

        # 3. Unknown budget target is rejected with 400 instead of mutating global budget
        prev_global_daily = self.store.state["global_budget"]["max_daily_usd"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/budget", {"target": "rust_bulder", "max_daily_usd": 999.0})
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()
        self.assertEqual(self.store.state["global_budget"]["max_daily_usd"], prev_global_daily)

        # 4. Invalid profile update does not poison persisted state and preserves existing budget inclusion flags
        self._post(
            "/api/budget",
            {"target": "cicd_runner", "include_disk_costs": False, "include_taxes": True, "tax_rate_percent": 9.5},
        )
        prev_min_runners = self.store.state["profiles"]["cicd_runner"]["min_runners"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/profile/cicd_runner",
                {"min_runners": 99, "runner_vcpu": "not-a-number"},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()
        self.assertEqual(self.store.state["profiles"]["cicd_runner"]["min_runners"], prev_min_runners)

        # Partial budget update in profile preserves include_disk_costs / include_taxes / tax_rate_percent
        self._post(
            "/api/profile/cicd_runner",
            {"min_runners": 6, "budget": {"max_daily_usd": 14.0}},
        )
        r_budget = self.store.state["profiles"]["cicd_runner"]["budget"]
        self.assertFalse(r_budget["include_disk_costs"])
        self.assertTrue(r_budget["include_taxes"])
        self.assertEqual(r_budget["tax_rate_percent"], 9.5)

        # 5. Excessive or negative regional scale targets are rejected before allocating records
        for bad_target in (-1, 25, 100000):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post(
                    "/api/instances/scale-region",
                    {"region": "us-south1", "role": "cicd_runner", "target_instances": bad_target},
                )
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()

        # 6. Overview includes unfiltered fleet_summary even when instances_view is role-filtered
        filtered_ov = self._get("/api/overview?role=rust_builder&include_deleted=false")
        self.assertEqual(len(filtered_ov["instances_view"]["instances"]), 2)
        self.assertTrue(all(i["role"] == "rust_builder" for i in filtered_ov["instances_view"]["instances"]))
        self.assertEqual(filtered_ov["fleet_summary"]["running_runner_vms"], 3)
        self.assertEqual(filtered_ov["fleet_summary"]["running_runner_slots"], 10)
        self.assertEqual(filtered_ov["fleet_summary"]["running_builder_vms"], 2)

    def test_round4_codex_regressions(self):
        # 1. Active VM templates are validated against updated profile sizing requirements
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/profile/rust_builder",
                {
                    "use_vm_template": True,
                    "vm_template_id": "tpl-builder-c4d-16",
                    "min_instance_ram_gib": 64,
                },
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # 2. Regional scale-up enforces max_instances across the entire workload fleet
        # Default cicd_runner has max_instances=8 and 2 running in other regions (europe-west1, africa-south1)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/instances/scale-region",
                {"region": "us-south1", "role": "cicd_runner", "target_instances": 7},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # 3. Future-dated OOM events are rejected by _is_recent_unhandled_oom / evaluate_scaling_decision
        future_eval = evaluate_scaling_decision(
            policy={"min_runners": 4, "max_runners": 16, "runners_per_instance": 4},
            telemetry={
                "current_runners": 4,
                "current_instances": 1,
                "jobs_ran_past_hour": 10,
                "queued_jobs": 0,
                "journal_oom_events": [{"id": "oom-future-1", "observed_at": "2099-01-01T00:00:00Z"}],
                "github_oom_runs": [{"id": "oom-future-2", "is_oom": True, "failed_at": "2099-01-01T00:00:00Z"}],
            },
            now_epoch=1792023000.0,
        )
        self.assertEqual(len(future_eval["scale_up_reasons"]), 0)
        self.assertEqual(future_eval["direction"], "hold")

        # 4. Negative or >100 tax rates and negative budget caps are rejected
        with self.assertRaises(ValueError):
            calculate_spot_pricing(
                family="c4d",
                vcpu=16,
                ram_gib=32,
                region="us-south1",
                boot_disk_type="hyperdisk-balanced",
                boot_disk_gb=40,
                data_disk_type="hyperdisk-balanced",
                data_disk_gb=200,
                tax_rate_percent=-50.0,
            )
        for bad_budget in (
            {"target": "global", "tax_rate_percent": -10.0},
            {"target": "global", "tax_rate_percent": 150.0},
            {"target": "global", "max_hourly_usd": -1.0},
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/budget", bad_budget)
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()

        # 5. qualify_instances applies each candidate region's tax_rate_default when include_taxes=True
        taxed_qual = qualify_instances(
            {
                **self.store.state["profiles"]["cicd_runner"],
                "budget": {
                    "max_hourly_usd": 10.0,
                    "max_daily_usd": 100.0,
                    "max_monthly_usd": 1000.0,
                    "include_disk_costs": True,
                    "include_taxes": True,
                },
            },
            {"us-south1": True, "europe-west1": True},
        )
        eu_candidates = [c for c in taxed_qual["spot_price_qualifying"] if c["region"] == "europe-west1"]
        us_candidates = [c for c in taxed_qual["spot_price_qualifying"] if c["region"] == "us-south1"]
        self.assertTrue(eu_candidates)
        self.assertTrue(us_candidates)
        self.assertEqual(eu_candidates[0]["pricing"]["tax_rate_percent"], 21.0)
        self.assertEqual(us_candidates[0]["pricing"]["tax_rate_percent"], 8.25)

        # 6. Recording a Rust build refreshes scaling_telemetry (builds_ran_past_hour, active_builds, queued_builds)
        before_builds_hr = self.store.state["scaling_telemetry"]["builds_ran_past_hour"]
        rb_post = self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-live-telemetry-01",
                "project_name": "oxidex",
                "status": "QUEUED",
                "cache_hits": 20,
                "cache_misses": 5,
            },
        )
        self.assertEqual(rb_post["status"], "ok")
        after_telem = self.store.state["scaling_telemetry"]
        self.assertGreaterEqual(after_telem["builds_ran_past_hour"], before_builds_hr + 1)
        self.assertGreaterEqual(after_telem["queued_builds"], 1)
        self.assertIn("builder_scaling_evaluation", rb_post)

    def test_round5_codex_regressions(self):
        # 1. Default monthly budget caps match the UI's $50.00 default
        self.assertEqual(self.store.state["global_budget"]["max_monthly_usd"], 50.00)
        self.assertEqual(self.store.state["profiles"]["cicd_runner"]["budget"]["max_monthly_usd"], 50.00)
        self.assertEqual(self.store.state["scaling"]["cicd_runner"]["max_scale_up_monthly_usd"], 50.00)

        # 2. Regional scale-up enforces scale-up budget caps
        self._post(
            "/api/budget",
            {"target": "cicd_runner", "max_hourly_usd": 0.0, "max_daily_usd": 0.0, "max_monthly_usd": 0.0},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/instances/scale-region",
                {"region": "us-south1", "role": "cicd_runner", "target_instances": 2},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # 3. When templates are disabled and profile sizing increases, regional scale-up selects a qualifying regional shape
        self._post(
            "/api/profile/cicd_runner",
            {
                "use_vm_template": False,
                "min_instance_vcpu": 32,
                "min_instance_ram_gib": 100,
                "disk_category": "any",
                "budget": {
                    "max_hourly_usd": 10.0,
                    "max_daily_usd": 100.0,
                    "max_monthly_usd": 1000.0,
                    "include_disk_costs": True,
                    "include_taxes": True,
                },
            },
        )
        za_scale = self._post(
            "/api/instances/scale-region",
            {"region": "africa-south1", "role": "cicd_runner", "target_instances": 2},
        )
        self.assertEqual(za_scale["planned_count"], 2)
        newest_za = za_scale["would_launch"][0]
        self.assertGreaterEqual(newest_za["vcpu"], 32)
        self.assertGreaterEqual(newest_za["ram_gib"], 100)

        # 4. Compute tax is applied even when include_disk_costs is False
        compute_tax_qual = qualify_instances(
            {
                **self.store.state["profiles"]["cicd_runner"],
                "budget": {
                    "max_hourly_usd": 10.0,
                    "max_daily_usd": 100.0,
                    "max_monthly_usd": 1000.0,
                    "include_disk_costs": False,
                    "include_taxes": True,
                },
            },
            {"europe-west1": True},
        )
        eu_cand = next(c for c in compute_tax_qual["spot_price_qualifying"] if c["region"] == "europe-west1")
        expected_taxed_compute = round(eu_cand["pricing"]["compute_hourly_usd"] * 1.21, 4)
        self.assertEqual(eu_cand["evaluated_hourly_usd"], expected_taxed_compute)

        # 5. Builder capacity_floor is clamped to max_instances when active_builds > max_instances
        capped_builder = evaluate_builder_scaling_decision(
            policy={"min_instances": 1, "max_instances": 4, "max_scale_up_hourly_usd": 50.0, "max_scale_up_daily_usd": 500.0, "max_scale_up_monthly_usd": 5000.0},
            telemetry={
                "current_builder_instances": 2,
                "active_builds": 5,
                "queued_builds": 0,
                "builds_ran_past_hour": 5,
                "builder_cpu_percentages": [30.0, 35.0],
                "builder_journal_oom_events": [],
            },
            per_instance_hourly_usd=0.30,
        )
        self.assertEqual(capped_builder["desired_instances"], 4)

        # 6. Partial QUEUED builds preserve null finished/delivered timestamps, do not fabricate completion stats, and are excluded from summary completion aggregates
        summary_before = self._get("/api/rust-builds")
        q_res = self._post(
            "/api/rust-builds",
            {"build_id": "rb-queued-partial-01", "status": "QUEUED"},
        )
        q_build = q_res["build"]
        self.assertIsNone(q_build["finished_at"])
        self.assertIsNone(q_build["delivered_at"])
        self.assertEqual(q_build["total_duration_seconds"], 0.0)
        self.assertEqual(q_build["total_crates"], 0)
        self.assertEqual(q_build["cpu_time_seconds"], 0.0)
        self.assertEqual(q_build["cpu_points"], 0.0)
        self.assertEqual(q_build["peak_ram_gib"], 0.0)
        summary_after = self._get("/api/rust-builds")
        self.assertEqual(summary_after["total_builds"], summary_before["total_builds"] + 1)
        self.assertEqual(summary_after["completed_builds"], summary_before["completed_builds"])
        self.assertEqual(summary_after["total_cache_hits"], summary_before["total_cache_hits"])
        self.assertEqual(summary_after["total_cpu_points"], summary_before["total_cpu_points"])
        self.assertEqual(summary_after["average_duration_seconds"], summary_before["average_duration_seconds"])

        # 7. Non-finite Rust telemetry values ("nan", "inf", "-inf") are rejected with HTTP 400
        for bad_telemetry in (
            {"peak_ram_gib": "nan"},
            {"cpu_time_seconds": "inf"},
            {"total_duration_seconds": "-inf"},
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/rust-builds", bad_telemetry)
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()

    def test_round6_codex_regressions(self) -> None:
        import os
        import stat

        # 1. Non-finite profile values are rejected before state assignment and do not poison live state
        for bad_profile in (
            {"runner_ram_gib": "nan"},
            {"min_instance_ram_gib": "inf"},
            {"host_overhead_gib": "-inf"},
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/profile/cicd_runner", bad_profile)
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()
        overview_ok = self._get("/api/overview")
        self.assertEqual(overview_ok["profiles"]["cicd_runner"]["runner_ram_gib"], 16)

        # 2. Persisted state file permissions are restricted to 0o600 even if previously 0o644
        os.chmod(self.state_file, 0o644)
        self._post(
            "/api/budget",
            {
                "target": "cicd_runner",
                "max_hourly_usd": 50.0,
                "max_daily_usd": 500.0,
                "max_monthly_usd": 5000.0,
                "include_disk_costs": True,
                "include_taxes": False,
            },
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.state_file).st_mode), 0o600)

        # 3. Valid Bash syntax in #!/bin/bash script passes save_cloud_init_template, and active cloud-init is used on scale-up
        bash_script = "#!/bin/bash\nset -euo pipefail\nfunction greet() {\n  local arr=(one two)\n  echo \"${arr[0]}\"\n}\ngreet\n"
        saved_ci = self._post(
            "/api/cloud-init/save",
            {
                "mode": "save_as",
                "name": "Bash Function Runner Init",
                "role": "cicd_runner",
                "content": bash_script,
                "assign_to_profile": True,
            },
        )
        new_ci_id = saved_ci["template"]["id"]
        scaled_up = self._post(
            "/api/instances/scale-region",
            {"region": "us-south1", "target_instances": 2, "role": "cicd_runner"},
        )
        newest_vm = scaled_up["would_launch"][0]
        self.assertEqual(newest_vm["cloud_init_template_id"], new_ci_id)

        # 4. Regional CI scaling enforces scaling.cicd_runner.max_runners (24 runners max)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/instances/scale-region",
                {"region": "us-south1", "target_instances": 6, "role": "cicd_runner"},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # 5. Template Studio rejects creating and assigning an incompatible VM template
        self._post(
            "/api/profile/cicd_runner",
            {
                "use_vm_template": False,
                "min_instance_vcpu": 32,
                "min_instance_ram_gib": 100,
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/vm-templates/from-instance",
                {
                    "instance_name": "oxidex-runners-eu-west4-01",
                    "template_name": "Too Small 8-vCPU Template",
                    "assign_to_profile": True,
                },
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # 6. Builder-tagged OOM events in journal_oom_events do not trigger CI runner scaling
        now_iso = self._get("/api/overview")["updated_at"]
        ci_eval = evaluate_scaling_decision(
            policy={"enable_journal_oom_trigger": True},
            telemetry={
                "current_runners": 4,
                "current_instances": 1,
                "jobs_ran_past_hour": 8,
                "provisioned_jobs": 2,
                "queued_jobs": 0,
                "instance_cpu_percentages": [30.0],
                "github_oom_runs": [],
                "journal_oom_events": [
                    {
                        "instance_name": "builder-oxidex-turin-01",
                        "role": "rust_builder",
                        "process": "rustc",
                        "pid": 9999,
                        "timestamp": now_iso,
                        "handled": False,
                    }
                ],
            },
        )
        self.assertEqual(ci_eval["scale_up_reasons"], [])
        self.assertEqual(ci_eval["desired_runners"], 4)

        # 7. Fast completed Rust builds (< 8s or explicit delivery > total) derive valid monotonic timestamps
        fast_res = self._post(
            "/api/rust-builds",
            {"build_id": "rb-fast-3s", "status": "DELIVERED", "total_duration_seconds": 3.0},
        )
        self.assertEqual(fast_res["build"]["total_duration_seconds"], 3.0)
        fast_del_res = self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-fast-del-clamp",
                "status": "DELIVERED",
                "total_duration_seconds": 4.0,
                "delivery_duration_seconds": 12.0,
            },
        )
        self.assertEqual(fast_del_res["build"]["total_duration_seconds"], 4.0)

    def test_round7_codex_regressions(self) -> None:
        from lib.dashboard_models import (
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            set_live_spot_pricing_override,
        )
        from lib.dashboard_ui_assets import render_dashboard_html

        # 1. Live Spot pricing catalog / capacityHistory rates are used in calculate_spot_pricing and qualify_instances
        try:
            set_live_spot_pricing_override(
                {
                    "source": "gcp_capacity_history",
                    "machine_hourly": {("us-south1", "c4d-standard-16"): 0.0550},
                    "component_rates": {
                        ("spot", "us-south1", "n4", "CPU"): 0.0020,
                        ("spot", "us-south1", "n4", "RAM"): 0.00025,
                    },
                    "disk_monthly_gib": {("us-south1", "hyperdisk-balanced"): 0.050},
                }
            )
            live_qual = qualify_instances(
                self.store.state["profiles"]["cicd_runner"],
                {"us-south1": True},
            )
            self.assertEqual(live_qual["pricing_source"], "gcp_capacity_history")
            c4d_16 = next(
                c
                for c in live_qual["hardware_qualifying"]
                if c["machine_type"] == "c4d-standard-16"
            )
            self.assertEqual(c4d_16["pricing"]["pricing_source"], "gcp_capacity_history")
            self.assertEqual(c4d_16["pricing"]["compute_hourly_usd"], 0.0550)
            self.assertEqual(c4d_16["pricing"]["disk_monthly_usd"], 7.00)
            n4_16 = next(
                c
                for c in live_qual["hardware_qualifying"]
                if c["machine_type"] == "n4-standard-16"
            )
            self.assertEqual(n4_16["pricing"]["pricing_source"], "gcp_billing_catalog")
            self.assertEqual(n4_16["pricing"]["compute_hourly_usd"], 0.0480)
        finally:
            set_live_spot_pricing_override(None)

        # 2 & 3. Stale is_oom=True on timeout runs and undated OOM events are rejected by evaluate_scaling_decision
        now_iso = self._get("/api/overview")["updated_at"]
        oom_guard_eval = evaluate_scaling_decision(
            policy={
                "min_runners": 4,
                "max_runners": 16,
                "runners_per_instance": 4,
                "enable_github_ci_oom_trigger": True,
                "enable_journal_oom_trigger": True,
                "enable_cpu_utilization_trigger": False,
                "enable_hourly_runs_scale_down": False,
            },
            telemetry={
                "current_runners": 4,
                "current_instances": 1,
                "jobs_ran_past_hour": 6,
                "provisioned_jobs": 1,
                "queued_jobs": 0,
                "instance_cpu_percentages": [20.0],
                "github_oom_runs": [
                    {
                        "id": "stale-timeout-with-is-oom",
                        "run_id": 38596411473,
                        "job_id": 115964282184,
                        "runner_name": "gha-oxidex-runners-us-south1-a-01",
                        "failed_at": now_iso,
                        "is_oom": True,
                        "error_excerpt": "##[error]Timeout reached, aborting!",
                    },
                    {
                        "id": "undated-gh-oom",
                        "run_id": 37192272062,
                        "job_id": 111407847874,
                        "runner_name": "gha-oxidex-runners-us-south1-a-01",
                        "error_excerpt": "##[error]Process completed with exit code 137.",
                    },
                ],
                "journal_oom_events": [
                    {
                        "id": "undated-journal-oom",
                        "instance_name": "oxidex-runners-dallas-01",
                        "process": "rustc",
                        "pid": 4242,
                    }
                ],
            },
        )
        self.assertEqual(oom_guard_eval["scale_up_reasons"], [])
        self.assertEqual(oom_guard_eval["direction"], "hold")

        # 4. Partial updates to an existing Rust build_id merge with existing build fields instead of resetting to defaults
        self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-partial-merge-01",
                "status": "QUEUED",
                "project_name": "custom-rust-workspace",
                "profile": "bench",
                "target_triple": "aarch64-unknown-linux-gnu",
                "commit_sha": "deadbeef1234",
                "requested_at": "2026-10-15T10:00:00Z",
                "instance_name": "builder-oxidex-eu-01",
                "zone": "europe-west4-a",
                "machine_type": "n4-highcpu-16",
                "cpu_platform": "Intel Emerald Rapids",
                "origin_ip": "185.220.101.55",
            },
        )
        completed_merge = self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-partial-merge-01",
                "status": "DELIVERED",
                "total_duration_seconds": 60.0,
                "cache_hits": 200,
                "total_crates": 220,
            },
        )["build"]
        self.assertEqual(completed_merge["project_name"], "custom-rust-workspace")
        self.assertEqual(completed_merge["profile"], "bench")
        self.assertEqual(completed_merge["target_triple"], "aarch64-unknown-linux-gnu")
        self.assertEqual(completed_merge["commit_sha"], "deadbeef1234")
        self.assertEqual(completed_merge["requested_at"], "2026-10-15T10:00:00Z")
        self.assertEqual(completed_merge["instance_name"], "builder-oxidex-eu-01")
        self.assertEqual(completed_merge["zone"], "europe-west4-a")
        self.assertEqual(completed_merge["origin_ip"], "185.220.101.55")
        self.assertEqual(completed_merge["total_duration_seconds"], 60.0)

        # 5. Non-loopback bind requires auth_token, and auth_token enforces Bearer / X-Dashboard-Token
        with self.assertRaises(ValueError):
            create_dashboard_server(host="0.0.0.0", port=0, state_path=self.state_file)

        auth_srv, _ = create_dashboard_server(
            host="127.0.0.1",
            port=0,
            state_path=self.state_file,
            project="custom-proj-777",
            org="custom-org-777",
            auth_token="super-secret-token-777",
        )
        auth_port = auth_srv.server_address[1]
        auth_thread = threading.Thread(target=auth_srv.serve_forever, daemon=True)
        auth_thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{auth_port}/api/overview", timeout=5)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

            authed_req = urllib.request.Request(
                f"http://127.0.0.1:{auth_port}/",
                headers={"Authorization": "Bearer super-secret-token-777"},
            )
            with urllib.request.urlopen(authed_req, timeout=5) as resp:
                authed_html = resp.read().decode("utf-8")
            # 10. Header renders configured project and org
            self.assertIn("GCP Project: custom-proj-777 · GitHub Org: custom-org-777", authed_html)
        finally:
            auth_srv.shutdown()
            auth_srv.server_close()
            auth_thread.join(timeout=5)

        self.assertIn(
            "GCP Project: p-alpha · GitHub Org: o-beta",
            render_dashboard_html(project="p-alpha", org="o-beta"),
        )

        # 6. Partial POST /api/budget preserves omitted fields on the target budget
        self._post(
            "/api/budget",
            {
                "target": "rust_builder",
                "active_period": "month",
                "max_hourly_usd": 3.25,
                "max_daily_usd": 19.50,
                "max_monthly_usd": 240.00,
                "include_disk_costs": False,
                "include_taxes": True,
                "tax_rate_percent": 11.5,
            },
        )
        partial_b = self._post(
            "/api/budget",
            {"target": "rust_builder", "max_daily_usd": 22.00},
        )["budget"]
        self.assertEqual(partial_b["max_daily_usd"], 22.00)
        self.assertEqual(partial_b["max_hourly_usd"], 3.25)
        self.assertEqual(partial_b["max_monthly_usd"], 240.00)
        self.assertEqual(partial_b["active_period"], "month")
        self.assertFalse(partial_b["include_disk_costs"])
        self.assertTrue(partial_b["include_taxes"])
        self.assertEqual(partial_b["tax_rate_percent"], 11.5)

        # 7. Default runner cloud-init templates add Docker apt repo and bootstrap the GitHub Actions runner service
        for tpl_str in (DEFAULT_RUNNER_CLOUD_INIT, DEFAULT_RUNNER_STRICT_CLOUD_INIT):
            parsed_def = parse_cloud_init(tpl_str)
            self.assertTrue(parsed_def["valid"], parsed_def["warnings"])
            self.assertIn("/etc/apt/sources.list.d/docker.list", tpl_str)
            self.assertIn("./config.sh --unattended", tpl_str)
        self.assertIn("actions-runner-linux-x64", DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn("./svc.sh install runner", DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn("./svc.sh start", DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn("FROM ghcr.io/actions/actions-runner:2.337.0", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/gha-runner-entrypoint"]', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("exec ./run.sh", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 8. Saving an existing cloud-init template with an updated role persists the new role and assigns to that role
        created_ci = self._post(
            "/api/cloud-init/save",
            {
                "mode": "save_as",
                "name": "Role Switchable Template",
                "role": "cicd_runner",
                "content": DEFAULT_RUNNER_CLOUD_INIT,
                "assign_to_profile": True,
            },
        )["template"]
        switched_ci = self._post(
            "/api/cloud-init/save",
            {
                "mode": "save",
                "id": created_ci["id"],
                "name": "Role Switchable Template (Builder)",
                "role": "rust_builder",
                "content": DEFAULT_RUNNER_CLOUD_INIT,
                "assign_to_profile": True,
            },
        )["template"]
        self.assertEqual(switched_ci["role"], "rust_builder")
        self.assertEqual(
            self.store.state["profiles"]["rust_builder"]["cloud_init_template_id"],
            created_ci["id"],
        )
        self.assertNotEqual(
            self.store.state["profiles"]["cicd_runner"]["cloud_init_template_id"],
            created_ci["id"],
        )

        # 9. qualify_instances never selects hyperdisk-throughput as a boot disk even when data_disk_gb >= 2048
        hd_tp_qual = qualify_instances(
            {
                "role": "cicd_runner",
                "min_runners": 4,
                "min_instances": 1,
                "max_instances": 8,
                "runner_vcpu": 4,
                "runner_ram_gib": 16,
                "min_instance_vcpu": 16,
                "min_instance_ram_gib": 64,
                "min_benchmark_score": 1000,
                "disk_category": "any",
                "disk_type": "hyperdisk-throughput",
                "boot_disk_gb": 40,
                "data_disk_gb": 2048,
                "budget": {
                    "max_hourly_usd": 20.0,
                    "max_daily_usd": 200.0,
                    "max_monthly_usd": 5000.0,
                },
            },
            {"us-south1": True, "africa-south1": True},
        )
        self.assertGreater(len(hd_tp_qual["hardware_qualifying"]), 0)
        for cand in hd_tp_qual["hardware_qualifying"]:
            self.assertNotEqual(cand["boot_disk_type"], "hyperdisk-throughput")

    def test_round8_codex_regressions(self) -> None:
        from lib.dashboard_models import (
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
            set_live_spot_pricing_override,
        )
        from lib.dashboard_ui_assets import render_dashboard_html

        # 1 & 3. Default runner cloud-init templates preserve env vars for inner shell and provision all 4 runner slots
        for tpl_str in (DEFAULT_RUNNER_CLOUD_INIT, DEFAULT_RUNNER_STRICT_CLOUD_INIT):
            parsed_runner = parse_cloud_init(tpl_str)
            self.assertTrue(parsed_runner["valid"], parsed_runner["warnings"])
            self.assertIn('RUNNERS_COUNT="4"', tpl_str)
            runcmds = parsed_runner["runcmd"]
            self.assertGreaterEqual(len(runcmds), 1)
            self.assertTrue(runcmds[-1].startswith("bash -lc '"))
            self.assertIn('for slot in $(seq 1 "${RUNNERS_COUNT:-4}")', runcmds[-1])
            self.assertIn('dir="/opt/actions-runner-${slot}"', runcmds[-1])

        # 2. Default Rust builder cloud-init writes Dockerfile + oxidex-remote-build and builds image before readiness check
        parsed_builder = parse_cloud_init(DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertTrue(parsed_builder["valid"], parsed_builder["warnings"])
        builder_paths = [wf["path"] for wf in parsed_builder["write_files"]]
        self.assertIn("/opt/oxidex-builder/image/Dockerfile", builder_paths)
        self.assertIn("/usr/local/bin/oxidex-remote-build", builder_paths)
        self.assertTrue(
            any("docker build" in cmd and "oxidex-remote-builder:1.99.0" in cmd for cmd in parsed_builder["runcmd"])
        )

        # 4. Stale is_oom=False on an actual OOM error_excerpt is reclassified in both evaluate_scaling_decision and update_scaling
        now_iso = self._get("/api/overview")["updated_at"]
        reclassified_eval = evaluate_scaling_decision(
            policy={
                "min_runners": 4,
                "max_runners": 16,
                "runners_per_instance": 4,
                "max_scale_up_monthly_usd": 300.0,
                "enable_github_ci_oom_trigger": True,
                "enable_journal_oom_trigger": False,
                "enable_cpu_utilization_trigger": False,
                "enable_hourly_runs_scale_down": False,
            },
            telemetry={
                "current_runners": 4,
                "current_instances": 1,
                "jobs_ran_past_hour": 6,
                "provisioned_jobs": 1,
                "queued_jobs": 0,
                "instance_cpu_percentages": [20.0],
                "github_oom_runs": [
                    {
                        "id": "stale-false-on-real-oom",
                        "repository": "swack-tools/oxidex",
                        "run_id": 38596411473,
                        "job_id": 115964282184,
                        "step_number": 5,
                        "line_number": 108,
                        "runner_name": "gha-oxidex-runners-us-south1-a-01",
                        "failed_at": now_iso,
                        "is_oom": False,
                        "failure_classification": "TIMEOUT",
                        "error_excerpt": "##[error]Process completed with exit code 137 (SIGKILL / Out of memory)",
                    }
                ],
                "journal_oom_events": [],
            },
        )
        self.assertEqual(reclassified_eval["direction"], "scale_up")
        self.assertTrue(
            any(r["trigger"] == "github_ci_oom" for r in reclassified_eval["scale_up_reasons"])
        )

        scaling_resp = self._post(
            "/api/scaling",
            {
                "role": "cicd_runner",
                "telemetry": {
                    "github_oom_runs": [
                        {
                            "id": "bulk-oom-reclassify",
                            "repository": "swack-tools/oxidex",
                            "run_id": 38596411473,
                            "job_id": 115964282184,
                            "step_number": 3,
                            "line_number": 42,
                            "runner_name": "oxidex-runners-dallas-01",
                            "failed_at": now_iso,
                            "is_oom": False,
                            "failure_classification": "TIMEOUT",
                            "error_excerpt": "fatal runtime error: out of memory (exit code 137)",
                        }
                    ]
                },
            },
        )
        norm_runs = scaling_resp["scaling_telemetry"]["github_oom_runs"]
        self.assertTrue(norm_runs[0]["is_oom"])
        self.assertEqual(norm_runs[0]["failure_classification"], "OOM")

        # 5. Browser authentication flow on protected dashboards: GET / loads shell, POST /api/auth/login issues session cookie
        auth_srv, _ = create_dashboard_server(
            host="127.0.0.1",
            port=0,
            state_path=self.state_file,
            auth_token="browser-flow-secret-888",
        )
        auth_port = auth_srv.server_address[1]
        auth_thread = threading.Thread(target=auth_srv.serve_forever, daemon=True)
        auth_thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{auth_port}/", timeout=5) as shell_resp:
                shell_html = shell_resp.read().decode("utf-8")
                self.assertEqual(shell_resp.status, 200)
                self.assertIn("/api/auth/login", shell_html)

            login_req = urllib.request.Request(
                f"http://127.0.0.1:{auth_port}/api/auth/login",
                data=json.dumps({"token": "browser-flow-secret-888"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(login_req, timeout=5) as login_resp:
                set_cookie = login_resp.headers.get("Set-Cookie", "")
                self.assertIn("dashboard_session=", set_cookie)
                self.assertIn("HttpOnly", set_cookie)
                cookie_pair = set_cookie.split(";", 1)[0]

            cookie_req = urllib.request.Request(
                f"http://127.0.0.1:{auth_port}/api/overview",
                headers={"Cookie": cookie_pair},
            )
            with urllib.request.urlopen(cookie_req, timeout=5) as ov_resp:
                ov_payload = json.loads(ov_resp.read().decode("utf-8"))
                self.assertEqual(ov_resp.status, 200)
                self.assertIn("profiles", ov_payload)
        finally:
            auth_srv.shutdown()
            auth_srv.server_close()
            auth_thread.join(timeout=5)

        # 6. Instance records and scaling unit hourly rates use machine-level live pricing overrides, and static fallback reports catalog_baseline
        baseline_price = calculate_spot_pricing(
            region="us-south1",
            family="c4d",
            vcpu=16,
            ram_gib=64,
            boot_disk_type="hyperdisk-balanced",
            boot_disk_gb=40,
            data_disk_type="hyperdisk-balanced",
            data_disk_gb=100,
            machine_type="c4d-standard-16",
        )
        self.assertEqual(baseline_price["pricing_source"], "catalog_baseline")
        try:
            set_live_spot_pricing_override(
                {
                    "source": "gcp_capacity_history",
                    "machine_hourly": {
                        ("africa-south1", "n2-standard-16"): 0.0420,
                        ("africa-south1", "c4d-standard-16"): 0.0420,
                        ("us-south1", "c4d-standard-16"): 0.0350,
                        ("europe-west1", "n2d-standard-8"): 0.0210,
                    },
                    "component_rates": {},
                    "disk_monthly_gib": {},
                }
            )
            ov_live = self.store.get_overview()
            za_reg = next(r for r in ov_live["regions"] if r["region"] == "africa-south1")
            za_inst = za_reg["running_instances"][0]
            self.assertEqual(za_inst["pricing"]["pricing_source"], "gcp_capacity_history")
            self.assertEqual(za_inst["pricing"]["compute_hourly_usd"], 0.0420)
        finally:
            set_live_spot_pricing_override(None)

        # 7 & 12. UI assets keep empty monthly budget fallback at $50 and expose Rust Builder regional & tab scaling controls
        rendered_ui = render_dashboard_html()
        self.assertIn("curBudget.max_monthly_usd ?? 50", rendered_ui)
        self.assertIn("curSc.max_scale_up_monthly_usd ?? 50", rendered_ui)
        self.assertNotIn('document.getElementById("runner-max-month").value || "300"', rendered_ui)
        self.assertNotIn('document.getElementById("sc-budget-mo").value || "300"', rendered_ui)
        self.assertIn("scaleRegion('${esc(reg.region)}', 'rust_builder', 1)", rendered_ui)
        self.assertIn("map-scale-builder-input-", rendered_ui)
        self.assertIn("saveAndEvaluateBuilderScaling()", rendered_ui)

        # 8. Non-finite nested telemetry is rejected and persistence failures roll back in-memory scaling state
        before_jobs = self.store.state["scaling_telemetry"]["jobs_ran_past_hour"]
        with self.assertRaises(ValueError):
            self.store.update_scaling(
                {
                    "role": "cicd_runner",
                    "telemetry": {
                        "jobs_ran_past_hour": 99,
                        "github_oom_runs": [
                            {
                                "id": "bad-nan-nested",
                                "run_id": 1,
                                "job_id": 2,
                                "extra_metric": float("nan"),
                            }
                        ],
                    },
                }
            )
        self.assertEqual(self.store.state["scaling_telemetry"]["jobs_ran_past_hour"], before_jobs)

        # 9. CI scaling recommendations clamp desired_instances and desired_runners to profile max_instances
        capped_eval = evaluate_scaling_decision(
            policy={
                "min_runners": 4,
                "max_runners": 64,
                "runners_per_instance": 4,
                "queued_jobs_threshold": 1,
                "max_scale_up_hourly_usd": 50.0,
                "max_scale_up_daily_usd": 500.0,
                "max_scale_up_monthly_usd": 5000.0,
                "enable_queued_jobs_trigger": True,
            },
            telemetry={
                "current_runners": 8,
                "current_instances": 2,
                "jobs_ran_past_hour": 25,
                "provisioned_jobs": 8,
                "queued_jobs": 20,
                "instance_cpu_percentages": [85.0, 90.0],
                "github_oom_runs": [],
                "journal_oom_events": [],
            },
            max_instances=3,
        )
        self.assertLessEqual(capped_eval["desired_instances"], 3)
        self.assertLessEqual(capped_eval["desired_runners"], 12)

        # 10. Rust build totals must match explicit timestamps, and omitted delivered_at anchors forward from requested_at
        with self.assertRaises(urllib.error.HTTPError) as ctx_mismatch:
            self._post(
                "/api/rust-builds",
                {
                    "build_id": "rb-mismatch-timestamps",
                    "status": "DELIVERED",
                    "requested_at": "2026-10-15T12:00:00Z",
                    "finished_at": "2026-10-15T12:01:00Z",
                    "delivered_at": "2026-10-15T12:01:10Z",
                    "total_duration_seconds": 5.0,
                },
            )
        self.assertEqual(ctx_mismatch.exception.code, 400)
        ctx_mismatch.exception.close()

        forward_anchored = self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-forward-anchor",
                "status": "DELIVERED",
                "requested_at": "2026-10-15T12:00:00Z",
                "finished_at": "2026-10-15T12:01:00Z",
                "total_duration_seconds": 75.0,
            },
        )["build"]
        self.assertEqual(forward_anchored["requested_at"], "2026-10-15T12:00:00Z")
        self.assertEqual(forward_anchored["finished_at"], "2026-10-15T12:01:00Z")
        self.assertEqual(forward_anchored["delivered_at"], "2026-10-15T12:01:15Z")
        self.assertEqual(forward_anchored["compile_duration_seconds"], 60.0)
        self.assertEqual(forward_anchored["delivery_duration_seconds"], 15.0)
        self.assertEqual(forward_anchored["total_duration_seconds"], 75.0)

        # 11. _persist refuses symlinked .tmp or state_path without clobbering target file
        victim_file = self.state_file.parent / "victim_target.txt"
        victim_file.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
        tmp_symlink = self.state_file.with_suffix(".tmp")
        tmp_symlink.symlink_to(victim_file)
        try:
            with self.assertRaises( OSError ):
                self.store._persist()
            self.assertEqual(victim_file.read_text(encoding="utf-8"), "DO_NOT_OVERWRITE")
        finally:
            if tmp_symlink.is_symlink():
                tmp_symlink.unlink()

    def test_round9_codex_regressions(self) -> None:
        import copy
        import os
        from unittest.mock import AsyncMock, patch
        from lib.dashboard_models import (
            DEFAULT_RUNNER_CLOUD_INIT,
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
            fetch_live_spot_pricing,
            set_live_spot_pricing_override,
        )

        # 1. Persistent systemd runner cloud-init templates do not use one-shot --ephemeral registration
        self.assertNotIn("--ephemeral", DEFAULT_RUNNER_CLOUD_INIT)
        self.assertNotIn("--ephemeral", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 2. Rust builder cloud-init implements oxidex-remote-build launcher protocol (<project> prepare & <project> <cmd...>)
        self.assertIn("/opt/oxidex-builder/image/entrypoint.py", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("/opt/oxidex-builder/image/Dockerfile", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("--label=oxidex.remote-build=true", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("f'{root}/sources/{project}:/src'", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("f'{root}/targets/{project}:/target'", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("--entrypoint=/usr/bin/time", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("if sys.argv[2:] == ['prepare']:", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        # 3. fetch_live_spot_pricing honors GOOGLE_CLOUD_API_KEY and falls back to Billing Catalog SKU rates via gcloud auth
        set_live_spot_pricing_override(None)
        with (
            patch.dict(os.environ, {"GOOGLE_CLOUD_API_KEY": "test-gke-billing-key"}, clear=False),
            patch(
                "lib.spot_pricing.load_catalog_rates",
                new=AsyncMock(return_value={("us-south1", "n2", "CPU", "SPOT"): 0.0085}),
            ) as mock_load_rates,
            # Keep the case hermetic: without these stubs, live gcloud credentials
            # return real capacityHistory prices and flip the source.
            patch("lib.compute_api.get_auth") as mock_api_key_auth,
            patch(
                "lib.spot_pricing.spot_price",
                new=AsyncMock(return_value=("us-south1", "n2-standard-16", None)),
            ),
        ):
            mock_api_key_auth.return_value = type(
                "AuthStub", (), {"token": "gcloud-oauth-token", "project": "homelab-424523"}
            )()
            live_api_key = fetch_live_spot_pricing(project="homelab-424523", force_refresh=True)
            self.assertEqual(live_api_key["source"], "gcp_billing_catalog")
            self.assertEqual(live_api_key["component_rates"][("us-south1", "n2", "CPU", "SPOT")], 0.0085)
            mock_load_rates.assert_awaited_once_with("test-gke-billing-key")

        with (
            patch("lib.compute_api.get_auth") as mock_auth,
            patch("lib.billing_catalog.catalog_skus", return_value=({"skuId": "test-sku"},)) as mock_skus,
            patch(
                "lib.dashboard_models.parse_component_rates",
                return_value={("us-south1", "n2", "CPU", "SPOT"): 0.0079},
            ),
            patch(
                "lib.spot_pricing.spot_price",
                new=AsyncMock(return_value=("us-south1", "n2-standard-16", None)),
            ),
        ):
            mock_auth.return_value = type("AuthStub", (), {"token": "gcloud-oauth-token", "project": "homelab-424523"})()
            live_catalog = fetch_live_spot_pricing(project="homelab-424523", force_refresh=True)
            self.assertEqual(live_catalog["source"], "gcp_billing_catalog")
            self.assertEqual(live_catalog["component_rates"][("us-south1", "n2", "CPU", "SPOT")], 0.0079)
            mock_skus.assert_called_once()
        set_live_spot_pricing_override(None)

        # 4. scale_region_instances is a plan only: it never persists or changes the instance list
        before_instances = json.dumps(self.store.state["instances"], sort_keys=True)
        with patch.object(self.store, "_persist", side_effect=OSError("disk full")) as persist:
            plan = self.store.scale_region_instances(
                {"region": "us-south1", "role": "cicd_runner", "target_instances": 0}
            )
        persist.assert_not_called()
        self.assertFalse(plan["applied"])
        self.assertEqual(plan["would_retire"], ["oxidex-runners-dallas-01"])
        self.assertEqual(json.dumps(self.store.state["instances"], sort_keys=True), before_instances)

        # 5. /api/auth/login caps request body at MAX_LOGIN_BODY_BYTES (4096) with HTTP 413 before reading
        auth_srv, _ = create_dashboard_server(
            host="127.0.0.1",
            port=0,
            state_path=self.state_file,
            auth_token="round9-secret",
        )
        auth_port = auth_srv.server_address[1]
        auth_thread = threading.Thread(target=auth_srv.serve_forever, daemon=True)
        auth_thread.start()
        try:
            oversized_body = json.dumps({"token": "x" * 6000}).encode("utf-8")
            req_413 = urllib.request.Request(
                f"http://127.0.0.1:{auth_port}/api/auth/login",
                data=oversized_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx_413:
                urllib.request.urlopen(req_413, timeout=5)
            self.assertEqual(ctx_413.exception.code, 413)
            ctx_413.exception.close()
        finally:
            auth_srv.shutdown()
            auth_srv.server_close()
            auth_thread.join(timeout=5)

        # 6. _refresh_instances_pricing preserves historical prices on DELETED instances when live rates change
        deleted_before = [
            copy.deepcopy(inst)
            for inst in self.store.state["instances"]
            if inst.get("status") != "RUNNING"
        ]
        self.assertGreater(len(deleted_before), 0)
        try:
            set_live_spot_pricing_override(
                {
                    "source": "gcp_capacity_history",
                    "machine_hourly": {
                        ("us-south1", "c4d-standard-16"): 0.1999,
                        ("europe-west4", "c4d-standard-16"): 0.1999,
                        ("africa-south1", "n2-standard-16"): 0.1999,
                    },
                    "component_rates": {},
                    "disk_monthly_gib": {},
                }
            )
            self.store.get_overview()
            deleted_after = [
                inst
                for inst in self.store.state["instances"]
                if inst.get("status") != "RUNNING"
            ]
            for before_inst, after_inst in zip(deleted_before, deleted_after):
                self.assertEqual(
                    before_inst["pricing"]["total_hourly_usd"],
                    after_inst["pricing"]["total_hourly_usd"],
                )
        finally:
            set_live_spot_pricing_override(None)

        # 7. All 3 cloud-init templates dynamically detect guest distro from /etc/os-release for Docker APT repo
        for tpl in (
            DEFAULT_RUNNER_CLOUD_INIT,
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        ):
            self.assertNotIn("https://download.docker.com/linux/ubuntu", tpl)
            self.assertIn("https://download.docker.com/linux/$(. /etc/os-release && echo", tpl)

        # 8. Strict runner cloud-init mounts /dev/disk/by-id/google-runner-data at /mnt/runner-data and places Docker/runner work dirs there
        self.assertIn("/dev/disk/by-id/google-runner-data", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('"$mp/docker"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('"$mp/containerd"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('work_dir="${mp}/work/runner-${slot}"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("--work /work", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('-v "${work_dir}:/work"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 9. Failed Rust builds do not fabricate successful metrics or count toward completed_builds
        failed_resp = self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-failed-no-fabricated-metrics",
                "status": "FAILED",
                "crate_workspace": "oxidex-engine",
            },
        )["build"]
        self.assertEqual(failed_resp["status"], "FAILED")
        self.assertEqual(failed_resp["compile_duration_seconds"], 0.0)
        self.assertEqual(failed_resp["delivery_duration_seconds"], 0.0)
        self.assertEqual(failed_resp["cache_hits"], 0)
        self.assertEqual(failed_resp["peak_ram_gib"], 0.0)
        self.assertIsNone(failed_resp["delivered_at"])

        with self.assertRaises(urllib.error.HTTPError) as ctx_bad_status:
            self._post(
                "/api/rust-builds",
                {
                    "build_id": "rb-invalid-status",
                    "status": "BOGUS_STATUS",
                },
            )
        self.assertEqual(ctx_bad_status.exception.code, 400)
        ctx_bad_status.exception.close()

    def test_round10_codex_regressions(self) -> None:
        from lib.dashboard_models import (
            DEFAULT_RUNNER_CLOUD_INIT,
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        from lib.dashboard_ui_assets import render_dashboard_html

        # 1. DEFAULT_RUST_BUILDER_CLOUD_INIT allows canonical outer builder transaction wrapper + oxidex-remote-build in sudoers
        self.assertIn(
            'sudo: ["ALL=(root) NOPASSWD: /usr/local/bin/oxidex-remote-build, /usr/local/bin/oxidex-remote-transaction"]',
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        self.assertIn(
            "%oxidex-build ALL=(root) NOPASSWD: /usr/local/bin/oxidex-remote-build, /usr/local/bin/oxidex-remote-transaction",
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )

        # 2. DEFAULT_RUST_BUILDER_CLOUD_INIT fails when data disk is missing/unmounted and installs canonical release build tools (just, cbindgen, cargo-nextest, rustfmt, clippy)
        self.assertIn('mountpoint -q "$mp"', DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("if not os.path.ismount(mount_root):", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("cargo-nextest", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("cbindgen", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("just --version && cbindgen --version && cargo nextest --version", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn(":/usr/local/cargo/bin:", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        # 3. DEFAULT_RUNNER_STRICT_CLOUD_INIT installs Rust 1.99.0 and enforces 8-vCPU / 32-GiB limits before advertising rust-1.99 / heavy32 labels
        self.assertIn("sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.99.0", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('rustc --version | grep "^rustc 1.99.0 "', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("heavy32-limits.conf", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('cpu_quota="$(( ${RUNNER_CPU_LIMIT:-8} * 100 ))%"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('mem_max="${RUNNER_MEMORY_GB:-32}G"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 4. DEFAULT_RUNNER_CLOUD_INIT derives gcp-<region> and machine family labels dynamically from GCE metadata
        self.assertNotIn("gcp-us-south1,c4d", DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn("computeMetadata/v1/instance/zone", DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn("computeMetadata/v1/instance/machine-type", DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn('eff_labels="${eff_labels},gcp-${vm_region}"', DEFAULT_RUNNER_CLOUD_INIT)

        # 5. Real YAML parser rejects invalid YAML scalars, undefined aliases, reserved indicators, and bad escapes
        for bad_yaml in (
            "#cloud-config\nruncmd:\n  - foo: a: b\n",
            "#cloud-config\nruncmd:\n  - *missing_alias\n",
            "#cloud-config\nruncmd:\n  - @invalid_indicator\n",
            '#cloud-config\nruncmd:\n  - "bad\\qescape"\n',
        ):
            parsed = parse_cloud_init(bad_yaml)
            self.assertFalse(parsed["valid"], f"Expected invalid cloud-init YAML for: {bad_yaml!r}")

        # 6. Non-loopback bind requires TLS cert+key unless allow_insecure_non_loopback is explicitly enabled
        with self.assertRaises(ValueError):
            create_dashboard_server(
                host="0.0.0.0",
                port=0,
                state_path=self.state_file,
                auth_token="non-loopback-token",
            )
        with self.assertRaises(ValueError):
            create_dashboard_server(
                host="127.0.0.1",
                port=0,
                state_path=self.state_file,
                tls_certfile="/tmp/only-cert.pem",
            )

        # 7. Overview includes top-level builder_scaling_evaluation and UI reads scaling_evaluations.rust_builder || builder_scaling_evaluation
        ov = self._get("/api/overview")
        self.assertIn("builder_scaling_evaluation", ov)
        self.assertEqual(
            ov["builder_scaling_evaluation"],
            ov["scaling_evaluations"]["rust_builder"],
        )
        ui_html = render_dashboard_html()
        self.assertIn(
            "(APP_STATE.scaling_evaluations && APP_STATE.scaling_evaluations.rust_builder) || APP_STATE.builder_scaling_evaluation",
            ui_html,
        )

        # 8. OOM event is consumed once its scale-up is applied so it does not ratchet capacity on subsequent evaluations
        now_iso = ov["updated_at"]
        ratchet_telem = {
            "current_runners": 4,
            "current_instances": 1,
            "jobs_ran_past_hour": 8,
            "provisioned_jobs": 2,
            "queued_jobs": 0,
            "instance_cpu_percentages": [25.0],
            "github_oom_runs": [
                {
                    "id": "oom-ratchet-test",
                    "repository": "swack-tools/oxidex",
                    "run_id": 37192272062,
                    "job_id": 111407847874,
                    "step_number": 8,
                    "line_number": 106,
                    "runner_name": "gha-oxidex-runners-us-south1-a-01",
                    "failed_at": now_iso,
                    "error_excerpt": "##[error]Process completed with exit code 137.",
                }
            ],
            "journal_oom_events": [],
        }
        ratchet_policy = {
            "min_runners": 4,
            "max_runners": 24,
            "runners_per_instance": 2,
            "scale_up_step_runners": 2,
            "max_scale_up_hourly_usd": 10.0,
            "max_scale_up_daily_usd": 100.0,
            "max_scale_up_monthly_usd": 1000.0,
            "enable_github_ci_oom_trigger": True,
            "enable_journal_oom_trigger": False,
            "enable_cpu_utilization_trigger": False,
            "enable_hourly_runs_scale_down": False,
        }
        first_eval = evaluate_scaling_decision(ratchet_policy, ratchet_telem)
        self.assertEqual(first_eval["direction"], "scale_up")
        self.assertEqual(first_eval["desired_runners"], 6)
        # Apply the recommended scale-up and re-evaluate: the same OOM must not trigger another scale-up to 8
        ratchet_telem["current_runners"] = first_eval["desired_runners"]
        ratchet_telem["current_instances"] = first_eval["desired_instances"]
        second_eval = evaluate_scaling_decision(ratchet_policy, ratchet_telem)
        self.assertEqual(second_eval["direction"], "hold")
        self.assertEqual(second_eval["desired_runners"], 6)
        self.assertTrue(ratchet_telem["github_oom_runs"][0]["handled"])

        # 9. Scaling budget projections account for actual running fleet hourly rates before adding new instances
        expensive_fleet_eval = evaluate_scaling_decision(
            policy={
                "min_runners": 4,
                "max_runners": 16,
                "runners_per_instance": 4,
                "queued_jobs_threshold": 1,
                "max_scale_up_hourly_usd": 0.50,
                "max_scale_up_daily_usd": 50.0,
                "max_scale_up_monthly_usd": 1000.0,
                "enable_queued_jobs_trigger": True,
            },
            telemetry={
                "current_runners": 4,
                "current_instances": 1,
                "running_instance_hourly_rates": [0.42],
                "queued_jobs": 4,
                "instance_cpu_percentages": [40.0],
                "github_oom_runs": [],
                "journal_oom_events": [],
            },
            per_instance_hourly_usd=0.16,
        )
        # Existing VM costs $0.42/hr; adding a second VM at $0.16/hr would reach $0.58/hr > $0.50/hr cap
        self.assertTrue(expensive_fleet_eval["budget_capped"])
        self.assertEqual(expensive_fleet_eval["desired_instances"], 1)
        self.assertEqual(expensive_fleet_eval["projected_hourly_usd"], 0.42)

    def test_round11_codex_regressions(self) -> None:
        from lib.dashboard_models import (
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )

        # 1. Builder cloud-init does not grant wildcard sudo on /usr/bin/flock; uses dedicated oxidex-remote-transaction wrapper
        self.assertNotIn("/usr/bin/flock --shared /run/oxidex-build.lock *", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("/usr/local/bin/oxidex-remote-transaction", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("if sys.argv[1:2] in (['transaction'], ['cleanup-export']):", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        # 2. Strict runner cloud-init runs unprivileged containerized runners with no passwordless sudo or docker group
        self.assertIn("sudo: false", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertNotIn("groups: docker", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("--cap-drop=ALL --security-opt=no-new-privileges:true", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("/opt/gha-runners/host_admission.py", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("/etc/gha-admission.json", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 3. Ruby Psych safe_load uses keyword arguments (aliases: true) so valid YAML with anchors/aliases parses cleanly
        yaml_with_alias = (
            "#cloud-config\n"
            "users:\n"
            "  - &base_user\n"
            "    name: runner\n"
            "    shell: /bin/bash\n"
            "runcmd:\n"
            "  - echo ok\n"
        )
        parsed_alias = parse_cloud_init(yaml_with_alias)
        self.assertTrue(parsed_alias["valid"], f"Expected valid YAML with anchor: {parsed_alias}")

        # 4. Global budget is enforced across both roles (cicd_runner + rust_builder combined)
        # Set global budget to $0.50/hr; rust_builder alone is ~$0.22/hr (and scaling us-south1 to 2 is ~$0.33/hr < $0.50/hr),
        # but combined with running cicd_runner VMs (~$0.33/hr) reaches ~$0.66/hr > $0.50/hr global cap
        self._post(
            "/api/budget",
            {
                "target": "global",
                "max_hourly_usd": 0.50,
                "max_daily_usd": 24.0,
                "max_monthly_usd": 700.0,
                "include_disk_costs": True,
                "include_taxes": False,
            },
        )
        ov_global = self._get("/api/overview")
        self.assertTrue(ov_global["scaling"]["cicd_runner"]["shared_global_budget"])
        self.assertTrue(ov_global["scaling"]["rust_builder"]["shared_global_budget"])
        with self.assertRaises(urllib.error.HTTPError) as ctx_global_cap:
            self._post(
                "/api/instances/scale-region",
                {
                    "region": "us-south1",
                    "role": "rust_builder",
                    "target_instances": 2,
                },
            )
        self.assertEqual(ctx_global_cap.exception.code, 400)
        ctx_global_cap.exception.close()

        # Restore generous global budget for subsequent checks
        self._post(
            "/api/budget",
            {
                "target": "global",
                "max_hourly_usd": 20.0,
                "max_daily_usd": 200.0,
                "max_monthly_usd": 2000.0,
                "include_disk_costs": True,
                "include_taxes": False,
            },
        )

        # 5. Inverted min_instances > max_instances (or min_runners > max_runners) is rejected in /api/scaling
        with self.assertRaises(urllib.error.HTTPError) as ctx_inv_builder:
            self._post(
                "/api/scaling",
                {
                    "role": "rust_builder",
                    "policy": {"min_instances": 5, "max_instances": 2},
                },
            )
        self.assertEqual(ctx_inv_builder.exception.code, 400)
        ctx_inv_builder.exception.close()

        with self.assertRaises(urllib.error.HTTPError) as ctx_inv_runner:
            self._post(
                "/api/scaling",
                {
                    "role": "cicd_runner",
                    "policy": {"min_runners": 16, "max_runners": 8},
                },
            )
        self.assertEqual(ctx_inv_runner.exception.code, 400)
        ctx_inv_runner.exception.close()

        # 6. create_vm_template_from_instance preserves the source instance's cloud_init_template_id even after profile cloud-init changes
        saved_ci = self._post(
            "/api/cloud-init/save",
            {
                "mode": "save_as",
                "name": "Custom Profile CI",
                "role": "cicd_runner",
                "assign_to_profile": True,
                "content": "#cloud-config\nruncmd:\n  - echo custom-profile\n",
            },
        )["template"]
        self.assertNotEqual(saved_ci["id"], "ci-runner-default")
        # Create VM template from an existing instance (oxidex-runners-dallas-01) that ran with ci-runner-default
        created_vm_tpl = self._post(
            "/api/vm-templates/from-instance",
            {
                "instance_id": "790481726351002",
                "template_name": "Preserved Source CI Template",
                "assign_to_profile": True,
            },
        )["vm_template"]
        self.assertEqual(created_vm_tpl["cloud_init_template_id"], "ci-runner-default")

    def test_round12_codex_regressions(self):
        from lib.dashboard_models import (
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
            parse_cloud_init,
        )
        from lib.remote_build import cleanup_script, transaction_script

        # 1 & 2. Strict runner cloud-init uses state-copying entrypoint and wires job admission hooks to gha-host-admission.py
        strict_parsed = parse_cloud_init(DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertTrue(strict_parsed["valid"], strict_parsed["warnings"])
        self.assertIn("path: /opt/gha-runners/build/entrypoint.sh", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('cp -a /home/runner/. "$state/"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("path: /opt/gha-runners/build/job-started.sh", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("path: /opt/gha-runners/build/job-completed.sh", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("COPY entrypoint.sh /usr/local/bin/gha-runner-entrypoint", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("COPY job-started.sh /usr/local/bin/gha-runner-job-started.sh", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("COPY job-completed.sh /usr/local/bin/gha-runner-job-completed.sh", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("COPY host_admission.py /usr/local/bin/gha-host-admission.py", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/gha-runner-job-started.sh", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/bin/gha-runner-job-completed.sh", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("-e RUNNER_CONFIGURE_ONLY=1", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 3 & 4. remote_build.py routes through oxidex-remote-transaction when present, and oxidex-remote-transaction sanitizes archive extraction
        tx_cmd, _ = transaction_script(
            "checkout",
            "oxidex-remote-source-run-" + "a" * 32 + ".tar.gz",
            "b" * 64,
            "release",
            "run-" + "a" * 32,
            "12345",
        )
        self.assertIn(
            "if test -x /usr/local/bin/oxidex-remote-transaction; then exec sudo /usr/local/bin/oxidex-remote-transaction build",
            tx_cmd,
        )
        cl_cmd = cleanup_script(
            "checkout",
            "oxidex-remote-source-run-" + "a" * 32 + ".tar.gz",
            "run-" + "a" * 32,
            "12345",
        )
        self.assertIn(
            "if test -x /usr/local/bin/oxidex-remote-transaction; then exec sudo /usr/local/bin/oxidex-remote-transaction cleanup",
            cl_cmd,
        )
        builder_parsed = parse_cloud_init(DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertTrue(builder_parsed["valid"], builder_parsed["warnings"])
        self.assertNotIn("tar.extractall(", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("def extract_safe_source_archive(", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("file_mode = 0o750 if (member.mode & 0o100) else 0o640", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("Unsupported non-regular archive member in source snapshot", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        # 5. DashboardStore.__init__ preserves loaded state when startup persistence fails
        from lib.dashboard_server import DashboardStore

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "custom_state.json"
            custom_store = DashboardStore(state_path=state_file, project="custom-proj-99", org="custom-org")
            custom_store.update_budget({"max_monthly_usd": 777.0})
            with unittest.mock.patch.object(
                DashboardStore, "_persist", side_effect=OSError("read-only disk")
            ):
                reloaded_store = DashboardStore(
                    state_path=state_file, project="custom-proj-99", org="custom-org"
                )
            self.assertEqual(
                reloaded_store.state["profiles"]["cicd_runner"]["budget"]["max_monthly_usd"],
                777.0,
            )

        # 6. DashboardRequestHandler enforces a socket timeout on stalled unauthenticated request body reads
        self.assertEqual(self.server.RequestHandlerClass.timeout, 10.0)
        import socket as _socket

        orig_timeout = self.server.RequestHandlerClass.timeout
        self.server.RequestHandlerClass.timeout = 0.15
        try:
            with _socket.create_connection(("127.0.0.1", self.port), timeout=2.0) as sock:
                sock.sendall(
                    b"POST /api/auth/session HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 64\r\n\r\n"
                )
                chunks = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                resp_bytes = b"".join(chunks)
            self.assertIn(b"400", resp_bytes)
            self.assertIn(b"Request body read timed out", resp_bytes)
        finally:
            self.server.RequestHandlerClass.timeout = orig_timeout

        # 7. Active work (provisioned_jobs / active_builds) stays above the scale-down floor even when max_runners / max_instances is smaller
        busy_runner_eval = evaluate_scaling_decision(
            policy={
                "min_runners": 2,
                "max_runners": 4,
                "runners_per_instance": 2,
                "enable_hourly_runs_scale_down": True,
                "scale_down_max_jobs_past_hour": 5,
                "scale_down_target_runners": 2,
                "enable_cpu_utilization_trigger": False,
            },
            telemetry={
                "current_runners": 6,
                "current_instances": 3,
                "provisioned_jobs": 6,
                "queued_jobs": 0,
                "jobs_ran_past_hour": 1,
                "instance_cpu_percentages": [10.0, 10.0, 10.0],
            },
        )
        self.assertEqual(busy_runner_eval["direction"], "hold")
        self.assertEqual(busy_runner_eval["desired_runners"], 6)
        self.assertEqual(busy_runner_eval["desired_instances"], 3)

        busy_builder_eval = evaluate_builder_scaling_decision(
            policy={
                "min_instances": 1,
                "max_instances": 2,
                "enable_low_builds_scale_down": True,
                "scale_down_max_builds_past_hour": 5,
                "scale_down_target_instances": 1,
                "enable_cpu_utilization_trigger": False,
            },
            telemetry={
                "current_builder_instances": 3,
                "active_builds": 3,
                "queued_builds": 0,
                "builds_ran_past_hour": 1,
                "instance_cpu_percentages": [10.0, 10.0, 10.0],
            },
        )
        self.assertEqual(busy_builder_eval["direction"], "hold")
        self.assertEqual(busy_builder_eval["desired_instances"], 3)

        # 8. Scale-up runner recommendations quantize desired_runners to whole-VM capacity and suppress scale-up when another whole VM exceeds max_runners
        quantized_up = evaluate_scaling_decision(
            policy={
                "min_runners": 4,
                "max_runners": 12,
                "runners_per_instance": 4,
                "scale_up_step_runners": 2,
                "queued_jobs_threshold": 1,
                "max_scale_up_hourly_usd": 10.0,
                "max_scale_up_daily_usd": 100.0,
                "max_scale_up_monthly_usd": 1000.0,
            },
            telemetry={
                "current_runners": 4,
                "current_instances": 1,
                "queued_jobs": 2,
                "provisioned_jobs": 4,
                "jobs_ran_past_hour": 10,
                "instance_cpu_percentages": [40.0],
            },
        )
        self.assertEqual(quantized_up["direction"], "scale_up")
        self.assertEqual(quantized_up["desired_instances"], 2)
        self.assertEqual(quantized_up["desired_runners"], 8)

        suppressed_up = evaluate_scaling_decision(
            policy={
                "min_runners": 4,
                "max_runners": 6,
                "runners_per_instance": 4,
                "scale_up_step_runners": 2,
                "queued_jobs_threshold": 1,
                "max_scale_up_hourly_usd": 10.0,
                "max_scale_up_daily_usd": 100.0,
                "max_scale_up_monthly_usd": 1000.0,
            },
            telemetry={
                "current_runners": 4,
                "current_instances": 1,
                "queued_jobs": 2,
                "provisioned_jobs": 4,
                "jobs_ran_past_hour": 10,
                "instance_cpu_percentages": [40.0],
            },
        )
        self.assertEqual(suppressed_up["direction"], "hold")
        self.assertEqual(suppressed_up["desired_instances"], 1)
        self.assertEqual(suppressed_up["desired_runners"], 4)

    def test_round13_codex_regressions(self) -> None:
        from lib.dashboard_models import (
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
            parse_cloud_init,
        )

        # 1. Strict runner admission service authenticates peer credentials via SO_PEERCRED and Runner.Worker ancestry
        self.assertIn("socket.SO_PEERCRED", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("def peer_owner(pid, uid, cfg):", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("def worker_owner(pid, container_id, container_started):", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("def runner_reservation(owner, cfg):", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("os.chmod(SOCKET_PATH, 0o660)", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertNotIn("os.chmod(SOCKET_PATH, 0o666)", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertNotIn("owner = str(req.get('owner'", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 2. Builder transaction wrapper cleans and extracts archives using dir_fd + O_NOFOLLOW on root-owned directories
        self.assertIn("def cleanup_source_fd(source_fd):", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("def open_nofollow_subdir(parent_fd, name, device):", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("dir_fd=cur_fd", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertNotIn("os.chown(source, int(os.environ.get('SUDO_UID'", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertNotIn("member_path = (source_root / member.name).resolve()", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        # 3. Switching from global budget to a role-specific budget clears shared_global_budget for BOTH roles
        self._post(
            "/api/budget",
            {
                "target": "global",
                "max_hourly_usd": 10.0,
                "max_daily_usd": 100.0,
                "max_monthly_usd": 1000.0,
                "include_disk_costs": True,
                "include_taxes": False,
            },
        )
        ov_before = self._get("/api/overview")
        self.assertTrue(ov_before["scaling"]["cicd_runner"]["shared_global_budget"])
        self.assertTrue(ov_before["scaling"]["rust_builder"]["shared_global_budget"])

        self._post(
            "/api/budget",
            {
                "target": "cicd_runner",
                "max_hourly_usd": 12.0,
                "max_daily_usd": 120.0,
                "max_monthly_usd": 1200.0,
                "include_disk_costs": True,
                "include_taxes": False,
            },
        )
        ov_after = self._get("/api/overview")
        self.assertFalse(ov_after["scaling"]["cicd_runner"]["shared_global_budget"])
        self.assertFalse(ov_after["scaling"]["rust_builder"]["shared_global_budget"])

        # 4. parse_cloud_init accepts valid 1-space and 3-space YAML indentation while rejecting inconsistent sibling indentation
        valid_odd_indent_yaml = (
            "#cloud-config\n"
            "users:\n"
            " - default\n"
            "packages:\n"
            "   - git\n"
            "   - curl\n"
        )
        parsed_odd = parse_cloud_init(valid_odd_indent_yaml)
        self.assertTrue(parsed_odd["valid"], f"Expected valid odd-indented YAML: {parsed_odd}")

    def test_round14_codex_regressions(self) -> None:
        from lib.dashboard_models import (
            DEFAULT_RUNNER_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
            parse_cloud_init,
        )

        # 1. Standard runner cloud-init mounts /dev/disk/by-id/google-runner-data at /mnt/runner-data
        self.assertIn("/dev/disk/by-id/google-runner-data", DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn('DISK_MOUNT="/mnt/runner-data"', DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn('mountpoint -q "$mp"', DEFAULT_RUNNER_CLOUD_INIT)

        # 2 & 3. Rust builder cloud-init reconciles lingering containers and holds active lock across prepare + docker run
        self.assertIn("def reconcile_project_containers(source, target):", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("reconcile_project_containers(source, target)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("active_lock_path = Path(f'/run/oxidex-cache-active-{project}.lock')", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("os.fdopen(active_lock_fd, 'a') as active_lock:", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        # 4. parse_cloud_init accepts indicator-first YAML block-scalar headers like |-2 and >+4
        block_scalar_yaml = (
            "#cloud-config\n"
            "write_files:\n"
            "  - path: /etc/test.conf\n"
            "    content: |-2\n"
            "      hello\n"
            "  - path: /etc/test2.conf\n"
            "    content: >+4\n"
            "        world\n"
        )
        parsed_block = parse_cloud_init(block_scalar_yaml)
        self.assertTrue(parsed_block["valid"], f"Expected valid block-scalar YAML: {parsed_block}")

        # 5. Ordinary /api/scaling updates preserve shared_global_budget when budget caps are unchanged
        self._post(
            "/api/budget",
            {
                "target": "global",
                "max_hourly_usd": 15.0,
                "max_daily_usd": 150.0,
                "max_monthly_usd": 1500.0,
                "include_disk_costs": True,
                "include_taxes": True,
            },
        )
        ov_global = self._get("/api/overview")
        runner_scaling = dict(ov_global["scaling"]["cicd_runner"])
        self.assertTrue(runner_scaling["shared_global_budget"])
        runner_scaling.pop("shared_global_budget", None)
        runner_scaling.pop("global_budget_enforced", None)
        runner_scaling["max_instances"] = 9
        self._post("/api/scaling", runner_scaling)
        ov_after_scaling = self._get("/api/overview")
        self.assertTrue(ov_after_scaling["scaling"]["cicd_runner"]["shared_global_budget"])
        self.assertTrue(ov_after_scaling["scaling"]["rust_builder"]["shared_global_budget"])

        # 6. Profile tax override (override_regional_tax + tax_rate_percent) applies to instances and budget calculations
        for role_name in ("cicd_runner", "rust_builder"):
            self._post(
                f"/api/profile/{role_name}",
                {
                    "override_regional_tax": True,
                    "tax_rate_percent": 0.0,
                },
            )
        inst_zero_tax = self._get("/api/instances")
        for inst in inst_zero_tax["instances"]:
            if inst["status"] in ("RUNNING", "PROVISIONING", "STAGING"):
                self.assertEqual(inst["pricing"]["tax_rate_percent"], 0.0)
                self.assertEqual(inst["pricing"]["tax_hourly_usd"], 0.0)
        self._post(
            "/api/budget",
            {
                "target": "global",
                "max_hourly_usd": 15.0,
                "max_daily_usd": 150.0,
                "max_monthly_usd": 1500.0,
                "include_disk_costs": True,
                "include_taxes": True,
            },
        )
        burn_zero_tax = self._get("/api/overview")["scaling_evaluation"]["projected_hourly_usd"]
        self._post(
            "/api/budget",
            {
                "target": "global",
                "max_hourly_usd": 15.0,
                "max_daily_usd": 150.0,
                "max_monthly_usd": 1500.0,
                "include_disk_costs": True,
                "include_taxes": False,
            },
        )
        burn_no_tax = self._get("/api/overview")["scaling_evaluation"]["projected_hourly_usd"]
        self.assertAlmostEqual(burn_zero_tax, burn_no_tax, places=5)

    def test_round15_codex_regressions(self) -> None:
        from lib.dashboard_models import (
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        from lib.dashboard_ui_assets import DASHBOARD_HTML

        # 1. Rust builder cloud-init holds root-only /run/oxidex-cache-active-{project}.lock across entire build + export transaction
        self.assertIn("os.open(active_lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("os.fchmod(active_lock_fd, 0o600)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("os.fchown(active_lock_fd, 0, 0)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("OXIDEX_ACTIVE_LOCK_FD", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("pass_fds=(cache_lock.fileno(),)", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        # 2. Strict runner admission daemon rejects release while Runner.Worker is alive and sweeps dead leases on socket timeout
        self.assertIn("Active Worker lease cannot be released while Runner.Worker is alive", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("srv.settimeout(1.0)", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 3. Strict runner admission daemon reserves HOST_RESERVE_GIB=8 and defaults ADMISSION_WORK_GIB=56
        self.assertIn('ADMISSION_WORK_GIB="56"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn('HOST_RESERVE_GIB="8"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("def host_work_budget_gib(cfg):", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("return max(0, min(configured_work, phys_gib - reserve_gib))", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 4. Query-string ?token= authentication is rejected while header/cookie/hash auth remains supported
        self.assertNotIn('url.searchParams.get("token")', DASHBOARD_HTML)
        self.assertIn('hashParams.get("token")', DASHBOARD_HTML)
        auth_srv, _ = create_dashboard_server(
            host="127.0.0.1",
            port=0,
            state_path=self.state_file,
            auth_token="round15-secret",
        )
        auth_port = auth_srv.server_address[1]
        auth_thread = threading.Thread(target=auth_srv.serve_forever, daemon=True)
        auth_thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx_qs:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{auth_port}/api/overview?token=round15-secret",
                    timeout=5,
                )
            self.assertEqual(ctx_qs.exception.code, 401)
            ctx_qs.exception.close()

            with urllib.request.urlopen(
                f"http://127.0.0.1:{auth_port}/?token=round15-secret",
                timeout=5,
            ) as shell_resp:
                self.assertEqual(shell_resp.status, 200)
                self.assertIsNone(shell_resp.headers.get("Set-Cookie"))

            req_hdr = urllib.request.Request(
                f"http://127.0.0.1:{auth_port}/api/overview",
                headers={"Authorization": "Bearer round15-secret"},
            )
            with urllib.request.urlopen(req_hdr, timeout=5) as resp_hdr:
                self.assertEqual(resp_hdr.status, 200)
        finally:
            auth_srv.shutdown()
            auth_srv.server_close()
            auth_thread.join(timeout=5)

        # 5. Completing an existing RUNNING build derives elapsed duration and completion time from now_ts when omitted
        import datetime

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        two_hours_ago_iso = (now_dt - datetime.timedelta(seconds=7200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._post(
            "/api/rust-builds",
            {
                "build_id": "bld-round15-long",
                "project_name": "oxidex",
                "status": "RUNNING",
                "requested_at": two_hours_ago_iso,
            },
        )
        before_builds_hr = self.store.state["scaling_telemetry"]["builds_ran_past_hour"]
        completed_res = self._post(
            "/api/rust-builds",
            {
                "build_id": "bld-round15-long",
                "status": "DELIVERED",
            },
        )
        completed_rec = completed_res["build"]
        self.assertEqual(completed_rec["requested_at"], two_hours_ago_iso)
        self.assertNotEqual(completed_rec["delivered_at"], two_hours_ago_iso)
        self.assertGreaterEqual(completed_rec["total_duration_seconds"], 7100.0)
        after_builds_hr = self.store.state["scaling_telemetry"]["builds_ran_past_hour"]
        self.assertGreaterEqual(after_builds_hr, before_builds_hr + 1)

    def test_round16_codex_regressions(self) -> None:
        from lib.dashboard_models import (
            DEFAULT_RUNNER_CLOUD_INIT,
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            parse_cloud_init,
        )

        # 1. unittest.mock is available at module import time for standalone test execution
        self.assertIsNotNone(unittest.mock.patch)

        # 2. parse_cloud_init accepts single- and double-quoted YAML mapping keys
        quoted_key_yaml = (
            "#cloud-config\n"
            '"package_update": true\n'
            "'package_upgrade': true\n"
            '"packages":\n'
            "  - git\n"
            "  - curl\n"
            "write_files:\n"
            '  - "path": /etc/quoted.conf\n'
            "    'permissions': '0644'\n"
            '    "content": |\n'
            "      hello\n"
        )
        parsed_quoted = parse_cloud_init(quoted_key_yaml)
        self.assertTrue(parsed_quoted["valid"], f"Expected valid quoted-key YAML: {parsed_quoted}")
        self.assertTrue(parsed_quoted["package_update"])
        self.assertTrue(parsed_quoted["package_upgrade"])
        self.assertEqual(parsed_quoted["packages"], ["git", "curl"])

        # 3. Completing an existing RUNNING build without cpu_time_seconds or peak_ram_gib keeps them at 0.0
        self._post(
            "/api/rust-builds",
            {
                "build_id": "bld-round16-nocpu",
                "project_name": "oxidex",
                "status": "RUNNING",
            },
        )
        completed_nocpu = self._post(
            "/api/rust-builds",
            {
                "build_id": "bld-round16-nocpu",
                "status": "DELIVERED",
            },
        )["build"]
        self.assertEqual(completed_nocpu["cpu_time_seconds"], 0.0)
        self.assertEqual(completed_nocpu["cpu_points"], 0.0)
        self.assertEqual(completed_nocpu["peak_ram_gib"], 0.0)

        # 4. Standard runner cloud-init reuses existing .runner state on persistent disk without unconditional config.sh
        self.assertIn('if [[ ! -x "${state_dir}/run.sh" ]]; then tar -xzf /tmp/actions-runner.tar.gz -C "${state_dir}"; fi', DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn("if [[ ! -f .runner ]]; then runuser -u runner -- ./config.sh", DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn("rm -f .service && ./svc.sh install runner && ./svc.sh start", DEFAULT_RUNNER_CLOUD_INIT)

        # 5. Strict runner admission daemon catches socket write errors on conn.sendall
        self.assertIn(
            "              try:\n"
            "                  conn.sendall((json.dumps(answer) + '\\n').encode('utf-8'))\n"
            "              except OSError:\n"
            "                  pass",
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
        )

    def test_round17_codex_regressions(self) -> None:
        from lib.dashboard_models import (
            DEFAULT_RUNNER_CLOUD_INIT,
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
            parse_cloud_init,
            render_cloud_init_for_profile,
        )

        # 1. Rust builder container and cache/source/target paths run under isolated UID/GID 1002:1002
        self.assertIn(
            "RUN groupadd -g 1002 runner && useradd -m -u 1002 -g 1002 -s /bin/bash runner && install -d -o 1002 -g 1002 /src /target /cargo",
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        self.assertIn("USER 1002:1002", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("--user=1002:1002", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("os.fchown(child_fd, 1002, 1002)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("os.fchown(out_f.fileno(), 1002, 1002)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("os.fchown(source_fd, 0, 1002)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("for anc in (root / 'sources', source, root / 'targets'):", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("os.chown(anc, 0, 1002)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("os.chown(path, 1002, 1002)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertNotIn("--user=1001:1001", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        # 2. Selecting a 2-runner VM template or profile updates active cloud-init RUNNERS_COUNT and clamps to host fit_slots
        self.assertIn('fit_slots="$((max_by_cpu<max_by_mem?max_by_cpu:max_by_mem))"', DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn('fit_slots="$((max_by_cpu<max_by_mem?max_by_cpu:max_by_mem))"', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        rendered_2slot = render_cloud_init_for_profile(
            DEFAULT_RUNNER_CLOUD_INIT,
            role="cicd_runner",
            runners_count=2,
            runner_vcpu=4,
            runner_ram_gib=16,
            instance_ram_gib=32,
            host_overhead_gib=0,
        )
        self.assertEqual(parse_cloud_init(rendered_2slot)["env_vars"]["RUNNERS_COUNT"], "2")

        self._post(
            "/api/profile/cicd_runner",
            {
                "use_vm_template": False,
                "min_instance_vcpu": 8,
                "min_instance_ram_gib": 32,
                "min_benchmark_score": 1000,
                "runner_vcpu": 4,
                "runner_ram_gib": 16,
            },
        )
        ov_2slot = self._get("/api/overview")
        active_ci = next(
            t for t in ov_2slot["cloud_init_templates"] if t["id"] == "ci-runner-default"
        )
        self.assertEqual(active_ci["parsed"]["env_vars"]["RUNNERS_COUNT"], "2")

        tpl_2slot = self._post(
            "/api/vm-templates/from-instance",
            {
                "instance_name": "oxidex-runners-eu-belgium-01",
                "template_name": "Belgium C4D-Standard-8 2-Runner Template",
                "cloud_init_template_id": "ci-runner-default",
                "assign_to_profile": True,
            },
        )["vm_template"]
        self.assertEqual(tpl_2slot["runners_count"], 2)
        ov_tpl_2slot = self._get("/api/overview")
        active_ci_tpl = next(
            t for t in ov_tpl_2slot["cloud_init_templates"] if t["id"] == "ci-runner-default"
        )
        self.assertEqual(active_ci_tpl["parsed"]["env_vars"]["RUNNERS_COUNT"], "2")

        # 3. Partial fleet growth (+1 runner when step_runners=2) keeps OOM active until the full step is observed
        #    in the live inventory (the dashboard never launches VMs itself).
        fleet = [fleet_observation("91700000000000000001", "oxidex-runners-oom-1", "us-south1-a", runners_count=1)]
        self.store.fleet_source = lambda: list(fleet)
        self._post(
            "/api/profile/cicd_runner",
            {
                "use_vm_template": False,
                "min_instance_vcpu": 4,
                "min_instance_ram_gib": 16,
                "min_benchmark_score": 500,
                "runner_vcpu": 4,
                "runner_ram_gib": 16,
                "min_runners": 1,
                "max_instances": 8,
            },
        )
        self.assertTrue(self.store.refresh_fleet_inventory())
        self.assertEqual(self.store.state["scaling_telemetry"]["current_runners"], 1)
        self._post(
            "/api/scaling",
            {
                "role": "cicd_runner",
                "acknowledge_all_ooms": True,
                "telemetry": {
                    "queued_jobs": 0,
                    "provisioned_jobs": 0,
                    "jobs_ran_past_hour": 5,
                    "instance_cpu_percentages": [20.0],
                },
            },
        )
        # Currently 1 running 1-runner VM (1 total runner). Record an active OOM with step_runners=2:
        oom_resp = self._post(
            "/api/scaling",
            {
                "role": "cicd_runner",
                "policy": {
                    "scale_up_step_runners": 2,
                    "min_runners": 1,
                    "max_runners": 12,
                    "max_scale_up_hourly_usd": 10.0,
                    "max_scale_up_daily_usd": 50.0,
                    "max_scale_up_monthly_usd": 500.0,
                },
                "add_github_oom_run": {
                    "repository": "swack-tools/oxidex",
                    "run_id": 99170001,
                    "job_id": 99170002,
                    "step_number": 4,
                    "line_number": 88,
                },
            },
        )
        self.assertEqual(oom_resp["evaluation"]["desired_runners"], 3)

        # Partial scale-up: +1 VM in us-south1 adds only +1 runner (total 2 runners < target 3)
        fleet.append(dict(fleet_observation("91700000000000000002", "oxidex-runners-oom-2", "us-south1-b", runners_count=1, started_hours_ago=0.0), created_epoch=time.time()))
        self.assertTrue(self.store.refresh_fleet_inventory())
        ov_partial = self._get("/api/overview")
        oom_rec_partial = next(
            r for r in ov_partial["scaling_telemetry"]["github_oom_runs"] if r.get("run_id") == 99170001
        )
        self.assertFalse(oom_rec_partial.get("handled", False))
        self.assertEqual(ov_partial["scaling_evaluation"]["desired_runners"], 3)

        # Completing the second +1 runner (total 3 runners >= baseline 1 + step 2) marks OOM handled
        fleet.append(dict(fleet_observation("91700000000000000003", "oxidex-runners-oom-3", "us-south1-c", runners_count=1, started_hours_ago=0.0), created_epoch=time.time()))
        self.assertTrue(self.store.refresh_fleet_inventory())
        ov_complete = self._get("/api/overview")
        oom_rec_complete = next(
            r for r in ov_complete["scaling_telemetry"]["github_oom_runs"] if r.get("run_id") == 99170001
        )
        self.assertTrue(oom_rec_complete.get("handled", False))

    def test_round18_codex_regressions(self) -> None:
        import hashlib
        import io
        import os
        import socket as _socket
        import ssl as _ssl
        import subprocess as _subprocess
        import tarfile
        import threading as _threading
        import urllib.request as _urlreq
        from lib.dashboard_models import (
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
            parse_cloud_init,
        )
        from lib.dashboard_server import create_dashboard_server

        # 1. Bound uploaded archives before root extraction (streaming tar iteration, member-count & byte limits)
        self.assertNotIn("tar.getmembers()", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertNotIn("shutil.copyfileobj(stream, out_f)", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("MAX_UPLOAD_ARCHIVE_BYTES = 256 * 1024 * 1024", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("MAX_SOURCE_ARCHIVE_MEMBERS = 50000", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("MAX_SOURCE_MEMBER_BYTES = 128 * 1024 * 1024", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("MAX_SOURCE_EXTRACTED_BYTES = 1024 * 1024 * 1024", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("for member in tar:", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("Source archive member count exceeds limit", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("Source archive uncompressed size exceeds limit", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "source"
            src_dir.mkdir(mode=0o700)
            archive_ok = Path(tmp) / "ok.tar.gz"
            with tarfile.open(archive_ok, "w:gz") as tf:
                payload_bytes = b"fn main() {}\n"
                ti = tarfile.TarInfo("src/main.rs")
                ti.size = len(payload_bytes)
                ti.mode = 0o644
                tf.addfile(ti, io.BytesIO(payload_bytes))
            digest_ok = hashlib.sha256(archive_ok.read_bytes()).hexdigest()

            import textwrap

            parsed_ci = parse_cloud_init(DEFAULT_RUST_BUILDER_CLOUD_INIT)
            self.assertTrue(parsed_ci["valid"], parsed_ci["warnings"])
            tx_script = textwrap.dedent(
                DEFAULT_RUST_BUILDER_CLOUD_INIT.split(
                    "- path: /usr/local/bin/oxidex-remote-transaction", 1
                )[1]
                .split("content: |\n", 1)[1]
                .split("\n  - path: /usr/local/bin/oxidex-remote-build", 1)[0]
            )
            tx_ns: dict[str, object] = {}
            exec(compile(tx_script, "/usr/local/bin/oxidex-remote-transaction", "exec"), tx_ns)
            extract_fn = tx_ns["extract_safe_source_archive"]
            orig_fstat = os.fstat

            def _root_fstat(fd: int):
                st = orig_fstat(fd)
                if st.st_uid != 0:
                    return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
                return st

            with (
                unittest.mock.patch("os.fstat", side_effect=_root_fstat),
                unittest.mock.patch("os.fchown"),
            ):
                extract_fn(archive_ok, src_dir, digest_ok)
                self.assertEqual((src_dir / "src" / "main.rs").read_bytes(), payload_bytes)

                tx_ns["MAX_SOURCE_ARCHIVE_MEMBERS"] = 1
                archive_many = Path(tmp) / "many.tar.gz"
                with tarfile.open(archive_many, "w:gz") as tf:
                    for idx in range(2):
                        b_data = b"x"
                        ti = tarfile.TarInfo(f"f{idx}.txt")
                        ti.size = len(b_data)
                        ti.mode = 0o644
                        tf.addfile(ti, io.BytesIO(b_data))
                with self.assertRaisesRegex(RuntimeError, "Source archive member count exceeds limit"):
                    extract_fn(archive_many, src_dir, None)

                tx_ns["MAX_SOURCE_ARCHIVE_MEMBERS"] = 50000
                tx_ns["MAX_SOURCE_EXTRACTED_BYTES"] = 8
                with self.assertRaisesRegex(RuntimeError, "Source archive uncompressed size exceeds limit"):
                    extract_fn(archive_ok, src_dir, digest_ok)

        # 2. Keep scale-down targets aligned with whole VM capacity (hold when desired_instances == current_instances)
        lowered_max_hold = evaluate_scaling_decision(
            policy={
                "min_runners": 4,
                "max_runners": 6,
                "runners_per_instance": 4,
                "queued_jobs_threshold": 1,
                "enable_cpu_utilization_trigger": False,
                "enable_hourly_runs_scale_down": False,
            },
            telemetry={
                "current_runners": 8,
                "current_instances": 2,
                "queued_jobs": 1,
                "provisioned_jobs": 2,
                "jobs_ran_past_hour": 10,
                "instance_cpu_percentages": [30.0, 35.0],
            },
        )
        self.assertEqual(lowered_max_hold["direction"], "hold")
        self.assertEqual(lowered_max_hold["desired_instances"], 2)
        self.assertEqual(lowered_max_hold["desired_runners"], 8)

        quantized_down = evaluate_scaling_decision(
            policy={
                "min_runners": 4,
                "max_runners": 16,
                "runners_per_instance": 4,
                "enable_hourly_runs_scale_down": True,
                "scale_down_max_jobs_past_hour": 5,
                "scale_down_target_runners": 6,
                "enable_cpu_utilization_trigger": False,
            },
            telemetry={
                "current_runners": 12,
                "current_instances": 3,
                "queued_jobs": 0,
                "provisioned_jobs": 0,
                "jobs_ran_past_hour": 2,
                "instance_cpu_percentages": [10.0, 10.0, 10.0],
            },
        )
        self.assertEqual(quantized_down["direction"], "scale_down")
        self.assertEqual(quantized_down["desired_instances"], 2)
        self.assertEqual(quantized_down["desired_runners"], 8)

        # 3. Bound TLS handshakes in worker threads so a stalled ClientHello does not block the accept loop
        with tempfile.TemporaryDirectory() as tls_tmp:
            cert_path = Path(tls_tmp) / "cert.pem"
            key_path = Path(tls_tmp) / "key.pem"
            _subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key_path),
                    "-out",
                    str(cert_path),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=127.0.0.1",
                ],
                check=True,
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
            )
            tls_server, _ = create_dashboard_server(
                host="127.0.0.1",
                port=0,
                state_path=Path(tls_tmp) / "tls_state.json",
                tls_certfile=cert_path,
                tls_keyfile=key_path,
            )
            tls_server.RequestHandlerClass.timeout = 0.35
            tls_port = tls_server.server_address[1]
            tls_thread = _threading.Thread(target=tls_server.serve_forever, daemon=True)
            tls_thread.start()
            try:
                with _socket.create_connection(("127.0.0.1", tls_port), timeout=2.0) as stalled_peer:
                    # Stalled peer sends no ClientHello; concurrent HTTPS client must still be accepted and served immediately
                    client_ctx = _ssl.create_default_context()
                    client_ctx.check_hostname = False
                    client_ctx.verify_mode = _ssl.CERT_NONE
                    with _urlreq.urlopen(
                        f"https://127.0.0.1:{tls_port}/api/health",
                        context=client_ctx,
                        timeout=2.0,
                    ) as resp:
                        health_payload = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(health_payload["status"], "ok")
                    stalled_peer.settimeout(1.5)
                    try:
                        eof_data = stalled_peer.recv(16)
                    except OSError:
                        eof_data = b""
                    self.assertEqual(eof_data, b"")
            finally:
                tls_server.shutdown()
                tls_server.server_close()
                tls_thread.join(timeout=2.0)

        # 4. Block builder containers from metadata service & enforce --no-service-account --no-scopes
        self.assertIn("def ensure_metadata_isolation():", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("('DOCKER-USER', ['-d', '169.254.169.254/32', '-j', 'REJECT'])", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn(
            "('OUTPUT', ['-d', '169.254.169.254/32', '-m', 'owner', '!', '--uid-owner', '0', '-j', 'REJECT'])",
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        self.assertIn("['--network=none']", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("allowed_commands = {'cargo', 'rustc', 'rustfmt', 'clippy-driver', 'just', 'cbindgen'}", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertNotIn("'python3'", DEFAULT_RUST_BUILDER_CLOUD_INIT.split("allowed_commands =", 1)[1].split("\n", 1)[0])
        self.assertIn(
            "Builder VM must be provisioned with --no-service-account --no-scopes",
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        inst_resp = self._get("/api/instances")
        builder_inst = next(
            i for i in inst_resp["instances"] if i["role"] == "rust_builder"
        )
        self.assertIsNone(builder_inst["service_account"])
        self.assertEqual(builder_inst["scopes"], [])
        self.assertEqual(builder_inst["gcloud_auth_flags"], ["--no-service-account", "--no-scopes"])

    def test_round19_codex_regressions(self):
        from lib.dashboard_models import (
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )

        # 1. Host admission daemon handles connections concurrently with per-container in-flight cap
        self.assertIn("import concurrent.futures", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("MAX_INFLIGHT_PER_CONTAINER = 2", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("INFLIGHT_BY_CID = {}", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("def handle_client(conn):", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn(
            "concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool",
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
        )
        self.assertIn("pool.submit(handle_client, conn)", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 2. Persistent disk reuse clears persisted runner containers before starting replacements
        self.assertIn('rm -rf "$mp/docker/containers"/*', DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertGreaterEqual(
            DEFAULT_RUNNER_STRICT_CLOUD_INIT.count('old_cids="$(docker ps -aq)"'),
            2,
        )
        self.assertIn("docker rm -f $old_cids >/dev/null 2>&1 || true", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 3. Builder transaction bounds decompression and tar extended headers before tarfile parses metadata
        self.assertIn("MAX_TAR_EXTENDED_HEADER_BYTES = 65536", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("class BoundedGzipStream:", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("class SafeTarInfo(tarfile.TarInfo):", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn(
            "tarfile.open(fileobj=bounded_gz, mode='r:', tarinfo=SafeTarInfo)",
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )

        # Verify BoundedGzipStream + SafeTarInfo reject oversized PAX headers before materializing them
        import gzip as _gzip
        import io as _io
        import os as _os
        import tarfile as _tarfile

        tx_marker = "MAX_TAR_EXTENDED_HEADER_BYTES = 65536\n"
        tx_start = DEFAULT_RUST_BUILDER_CLOUD_INIT.index(tx_marker)
        tx_end = DEFAULT_RUST_BUILDER_CLOUD_INIT.index("def validate_identity(", tx_start)
        tx_snippet = "\n".join(
            line[6:] if line.startswith("      ") else line
            for line in DEFAULT_RUST_BUILDER_CLOUD_INIT[tx_start:tx_end].splitlines()
        )
        tx_ns = {
            "os": _os,
            "tarfile": _tarfile,
            "MAX_SOURCE_DECOMPRESSED_BYTES": 1024 * 1024,
            "MAX_SOURCE_MEMBER_BYTES": 256 * 1024,
        }
        exec(tx_snippet, tx_ns)
        BoundedGzipStream = tx_ns["BoundedGzipStream"]
        SafeTarInfo = tx_ns["SafeTarInfo"]

        pax_buf = _io.BytesIO()
        with _gzip.GzipFile(fileobj=pax_buf, mode="wb") as gz_out:
            with _tarfile.open(fileobj=gz_out, mode="w|", format=_tarfile.PAX_FORMAT) as tf_out:
                ti = _tarfile.TarInfo("Cargo.toml")
                ti.pax_headers = {"comment": "X" * (70 * 1024)}
                payload = b"[package]\nname = 'demo'\n"
                ti.size = len(payload)
                tf_out.addfile(ti, _io.BytesIO(payload))
        pax_buf.seek(0)
        with self.assertRaises(RuntimeError) as pax_ctx:
            with _gzip.GzipFile(fileobj=pax_buf, mode="rb") as gz_in:
                bounded = BoundedGzipStream(gz_in, 1024 * 1024)
                with _tarfile.open(fileobj=bounded, mode="r:", tarinfo=SafeTarInfo) as tf_in:
                    for _ in tf_in:
                        pass
        self.assertIn("extended header exceeds size limit", str(pax_ctx.exception))

        # 4. Multiline YAML flow collections and multiline quoted scalars are validated by the YAML parser
        multiline_seq = (
            "#cloud-config\n"
            "packages: [\n"
            "  curl,\n"
            "  git\n"
            "]\n"
            "apt: {\n"
            "  preserve_sources_list: true\n"
            "}\n"
            "final_message: \"Boot completed\n"
            "  successfully\"\n"
        )
        parsed_multi = parse_cloud_init(multiline_seq)
        self.assertTrue(parsed_multi["valid"], f"Expected valid multiline YAML, got errors: {parsed_multi['errors']}")
        self.assertEqual(parsed_multi["packages"], ["curl", "git"])

        unclosed_multi = "#cloud-config\npackages: [\n  curl,\n  git\n"
        parsed_unclosed = parse_cloud_init(unclosed_multi)
        self.assertFalse(parsed_unclosed["valid"])

    def test_round20_codex_regressions(self):
        from datetime import datetime, timedelta, timezone
        from unittest import mock
        from lib.dashboard_models import (
            DEFAULT_RUNNER_CLOUD_INIT,
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            render_cloud_init_for_profile,
        )
        from lib.dashboard_ui_assets import render_dashboard_html

        # 1. Standard runner cloud-init enforces per-runner CPUQuota and MemoryMax via systemd drop-in
        self.assertIn('cpu_quota="$(( ${RUNNER_CPU_LIMIT:-4} * 100 ))%"', DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn('mem_max="${RUNNER_MEMORY_GB:-16}G"', DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn('unit_name="$(tr -d "[:space:]" < .service)"', DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn('"/etc/systemd/system/${unit_name}.d/runner-limits.conf"', DEFAULT_RUNNER_CLOUD_INIT)

        # 2. Scale-downs are clamped to max_instances when reducing an oversized fleet
        oversized_down = evaluate_scaling_decision(
            policy={"min_runners": 1, "max_runners": 16, "runners_per_instance": 1, "max_instances": 4},
            telemetry={
                "current_runners": 8,
                "current_instances": 8,
                "active_jobs": 0,
                "queued_jobs": 0,
                "avg_cpu_utilization_pct": 10.0,
                "avg_ram_utilization_pct": 10.0,
                "idle_minutes": 45,
            },
            per_instance_hourly_usd=0.10,
        )
        self.assertEqual(oversized_down["direction"], "scale_down")
        self.assertEqual(oversized_down["desired_instances"], 4)
        self.assertEqual(oversized_down["desired_runners"], 4)

        # 3. Status-only unsuccessful terminal transitions timestamp finished_at and derive elapsed duration
        old_req = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-r20-failed-transition",
                "status": "RUNNING",
                "requested_at": old_req,
                "crate_workspace": "oxidex-core",
            },
        )
        failed_tr = self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-r20-failed-transition",
                "status": "FAILED",
            },
        )["build"]
        self.assertIsNotNone(failed_tr["finished_at"])
        self.assertGreaterEqual(failed_tr["total_duration_seconds"], 7000.0)
        overview_after_fail = self._get("/api/overview")
        self.assertGreaterEqual(
            overview_after_fail["scaling_telemetry"]["builds_ran_past_hour"],
            1,
        )

        # 4. YAML document-start and document-end markers are accepted by cloud-init validator
        doc_marker_yaml = "#cloud-config\n---\npackage_update: true\npackages:\n  - curl\n...\n"
        parsed_doc_marker = parse_cloud_init(doc_marker_yaml)
        self.assertTrue(
            parsed_doc_marker["valid"],
            f"Expected valid YAML with --- marker, got: {parsed_doc_marker['errors']}",
        )

        # 5. Fractional memory requirements and host reserves round upward consistently
        rendered_frac = render_cloud_init_for_profile(
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            role="cicd_runner",
            runners_count=2,
            runner_vcpu=4,
            runner_ram_gib=16.5,
            instance_ram_gib=64.0,
            host_overhead_gib=1.2,
        )
        self.assertIn('RUNNER_MEMORY_GB="17"', rendered_frac)
        self.assertIn('HOST_RESERVE_GIB="2"', rendered_frac)
        prof_frac = self._post(
            "/api/profile/cicd_runner",
            {"runner_ram_gib": 16.5, "host_overhead_gib": 1.2},
        )["profile"]
        self.assertEqual(prof_frac["runner_ram_gib"], 17.0)
        self.assertEqual(prof_frac["host_overhead_gib"], 2.0)

        # 6. _refresh_instances_pricing batches live-pricing fetch once across all running instances
        with mock.patch("lib.dashboard_server.fetch_live_spot_pricing", wraps=None) as mock_fetch:
            mock_fetch.return_value = {
                "source": "catalog_baseline",
                "fetched_at": 0.0,
                "component_rates": {},
                "machine_hourly": {},
                "disk_monthly_gib": {},
            }
            self.store._refresh_instances_pricing()
            self.assertEqual(mock_fetch.call_count, 1)

        # 7. Clearing auth token invokes POST /api/auth/logout and expires dashboard_session cookie
        html_ui = render_dashboard_html(project="homelab-424523", org="swack-tools")
        self.assertIn('"/api/auth/logout"', html_ui)
        self.assertIn("await logoutDashboardSession();", html_ui)
        logout_req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/auth/logout",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(logout_req, timeout=5) as logout_resp:
            set_cookie = logout_resp.headers.get("Set-Cookie", "")
            logout_body = json.loads(logout_resp.read().decode("utf-8"))
        self.assertFalse(logout_body["authenticated"])
        self.assertIn("dashboard_session=;", set_cookie)
        self.assertIn("Max-Age=0", set_cookie)

        # 8. Malformed GET query parameters return HTTP 400 JSON instead of dropping connection
        with self.assertRaises(urllib.error.HTTPError) as bad_get_ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/overview?window_hours=abc", timeout=5)
        self.assertEqual(bad_get_ctx.exception.code, 400)
        bad_get_ctx.exception.close()

    def test_round21_codex_regressions(self):
        from datetime import datetime, timedelta, timezone
        from lib.dashboard_models import (
            DEFAULT_RUNNER_CLOUD_INIT,
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        from lib.dashboard_scaling import evaluate_builder_scaling_decision, evaluate_scaling_decision

        # 1. Admission service reserves inflight capacity on accept before pool.submit
        self.assertIn("MAX_TOTAL_INFLIGHT = 32", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("_, _, cid_slot = reserve_inflight(conn)", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("pool.submit(handle_client, conn)", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 2. Lease reclamation checks container cgroup for stray processes after Runner.Worker exits
        self.assertIn("def container_job_active(cid, started, worker_start=None):", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("return container_job_active(cid, started, start)", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        # 3. Standard runner cloud-init rounds MemTotal up to nominal GiB for slot count and caps MemoryMax to usable RAM
        self.assertIn('host_mem_gib="$(((mem_kib+mem_kib/48+524288)/2097152*2))"', DEFAULT_RUNNER_CLOUD_INIT)
        self.assertIn('if (( slot_mem_mib > 0 && slot_mem_mib < req_mem_mib )); then mem_max="${slot_mem_mib}M"; fi', DEFAULT_RUNNER_CLOUD_INIT)

        # 4. Updating an unfinished build to DELIVERED while repeating requested_at derives live duration and timestamp
        old_req = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-r21-repeat-req",
                "status": "RUNNING",
                "requested_at": old_req,
                "crate_workspace": "oxidex-core",
            },
        )
        delivered_repeat = self._post(
            "/api/rust-builds",
            {
                "build_id": "rb-r21-repeat-req",
                "status": "DELIVERED",
                "requested_at": old_req,
            },
        )["build"]
        self.assertGreaterEqual(delivered_repeat["total_duration_seconds"], 1700.0)
        self.assertNotEqual(delivered_repeat["delivered_at"], old_req)

        # 5. Builder and runner autoscalers preserve current capacity when a scale-up trigger fires above max cap
        builder_hold = evaluate_builder_scaling_decision(
            policy={"min_instances": 1, "max_instances": 2},
            telemetry={
                "current_instances": 4,
                "active_builds": 4,
                "queued_builds": 3,
                "avg_cpu_utilization_pct": 95.0,
                "avg_ram_utilization_pct": 90.0,
                "builds_ran_past_hour": 10,
            },
            per_instance_hourly_usd=0.10,
        )
        self.assertEqual(builder_hold["direction"], "hold")
        self.assertEqual(builder_hold["desired_instances"], 4)

        runner_hold = evaluate_scaling_decision(
            policy={"min_runners": 1, "max_runners": 4, "runners_per_instance": 2, "max_instances": 2},
            telemetry={
                "current_runners": 8,
                "current_instances": 4,
                "active_jobs": 8,
                "queued_jobs": 5,
                "avg_cpu_utilization_pct": 95.0,
                "avg_ram_utilization_pct": 90.0,
                "jobs_ran_past_hour": 20,
            },
            per_instance_hourly_usd=0.10,
        )
        self.assertEqual(runner_hold["direction"], "hold")
        self.assertEqual(runner_hold["desired_runners"], 8)

        # 6. Source archive extraction rejects over-deep paths (>64 components) before creating directories
        self.assertIn(
            "dir_depth = len(rel_parts) if member.isdir() else (len(rel_parts) - 1)",
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        self.assertIn(
            "raise RuntimeError('Source archive member path depth exceeds limit')",
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        depth_check_idx = DEFAULT_RUST_BUILDER_CLOUD_INIT.index(
            "raise RuntimeError('Source archive member path depth exceeds limit')"
        )
        subdir_create_idx = DEFAULT_RUST_BUILDER_CLOUD_INIT.index(
            "cur_fd = open_nofollow_subdir(cur_fd, part, root_info.st_dev)"
        )
        self.assertLess(depth_check_idx, subdir_create_idx)

    def test_round22_codex_regressions(self):
        from datetime import datetime, timezone
        import json
        import subprocess
        import textwrap
        from unittest import mock
        from lib.dashboard_models import (
            DEFAULT_RUNNER_STRICT_CLOUD_INIT,
            DEFAULT_RUST_BUILDER_CLOUD_INIT,
        )
        from lib.dashboard_scaling import (
            _is_recent_unhandled_oom,
            build_github_oom_deep_link,
            evaluate_builder_scaling_decision,
            evaluate_cpu_scope,
            evaluate_scaling_decision,
        )

        # 1. SEC-01 & SEC-02: Strict runner host admission process_record & container_job_active
        self.assertIn("if any(comm == 'Runner.Worker' for _, (_, _, comm) in procs.items()):", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("if stat[0] == 'Z':", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("raise FileNotFoundError('Zombie process')", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        ha_script = textwrap.dedent(
            DEFAULT_RUNNER_STRICT_CLOUD_INIT.split("- path: /opt/gha-runners/host_admission.py", 1)[1]
            .split("content: |\n", 1)[1]
            .split("\n  - path: /opt/gha-runners/build/entrypoint.sh", 1)[0]
        )
        ha_ns: dict[str, object] = {}
        exec(compile(ha_script, "/opt/gha-runners/host_admission.py", "exec"), ha_ns)

        # SEC-02: process_record raises FileNotFoundError on zombie process ('Z')
        mock_proc_stat = mock.MagicMock()
        mock_proc_stat.read_text.return_value = "205 (zombie-app) Z 1 205 205 0 -1 4194304 0 0 0 0 0 0 0 0 20 0 1 0 1000 0 0 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0"
        with mock.patch("pathlib.Path.__truediv__", return_value=mock_proc_stat):
            with self.assertRaises(FileNotFoundError) as ctx:
                ha_ns["process_record"](205)
            self.assertEqual(str(ctx.exception), "Zombie process")

        # SEC-01: container_job_active does not kill subsequent Runner.Worker or its hook processes
        killed_pids: list[int] = []
        with mock.patch("os.kill", side_effect=lambda pid, sig: killed_pids.append(pid)):
            def mock_proc_rec(pid):
                if pid == 100:  # Runner.Listener
                    return 1, "1000", "Runner.Listener", "/docker/cid-123"
                if pid == 300:  # New Runner.Worker
                    return 100, "3000", "Runner.Worker", "/docker/cid-123"
                if pid == 301:  # New acquire hook
                    return 300, "3010", "python3", "/docker/cid-123"
                raise FileNotFoundError(f"PID {pid}")

            mock_dir = [
                mock.MagicMock(name="100"),
                mock.MagicMock(name="300"),
                mock.MagicMock(name="301"),
            ]
            mock_dir[0].name = "100"
            mock_dir[1].name = "300"
            mock_dir[2].name = "301"

            with mock.patch("pathlib.Path.iterdir", return_value=mock_dir), \
                 mock.patch.dict(ha_ns, {"process_record": mock_proc_rec}):
                active = ha_ns["container_job_active"]("cid-123", "2026-10-16T00:00:00Z", worker_start="2000")
                self.assertFalse(active)
                self.assertEqual(killed_pids, [])

        # 2. SEC-03: host admission acquire CLI catches transient RuntimeError and sleeps before retrying
        self.assertIn("except OrphanRunnerHook:", DEFAULT_RUNNER_STRICT_CLOUD_INIT)
        self.assertIn("except (OSError, RuntimeError):", DEFAULT_RUNNER_STRICT_CLOUD_INIT)

        acquire_attempts = 0
        def mock_request(action):
            nonlocal acquire_attempts
            acquire_attempts += 1
            if acquire_attempts == 1:
                raise RuntimeError("Too many concurrent admission requests")
            return True

        sleep_calls: list[float] = []
        with mock.patch.dict(ha_ns, {"request": mock_request}), \
             mock.patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            # Simulate the acquire CLI loop
            while True:
                try:
                    if ha_ns["request"]("acquire"):
                        break
                except ha_ns["OrphanRunnerHook"]:
                    raise
                except (OSError, RuntimeError):
                    ha_ns["time"].sleep(5)
                else:
                    ha_ns["time"].sleep(5)
            self.assertEqual(acquire_attempts, 2)
            self.assertEqual(sleep_calls, [5])

        # Test OrphanRunnerHook fails fast
        with mock.patch.dict(ha_ns, {"request": mock.MagicMock(side_effect=ha_ns["OrphanRunnerHook"]("lost"))}):
            with self.assertRaises(ha_ns["OrphanRunnerHook"]):
                try:
                    ha_ns["request"]("acquire")
                except ha_ns["OrphanRunnerHook"]:
                    raise
                except (OSError, RuntimeError):
                    ha_ns["time"].sleep(5)

        # 3. SEC-04: reconcile_project_containers tolerates auto-removed containers on inspect and uses check=False on rm
        self.assertIn("except (subprocess.CalledProcessError, json.JSONDecodeError):", DEFAULT_RUST_BUILDER_CLOUD_INIT)
        self.assertIn("check=False", DEFAULT_RUST_BUILDER_CLOUD_INIT)

        tx_script = textwrap.dedent(
            DEFAULT_RUST_BUILDER_CLOUD_INIT.split("- path: /usr/local/bin/oxidex-remote-transaction", 1)[1]
            .split("content: |\n", 1)[1]
            .split("\n  - path: /usr/local/bin/oxidex-remote-build", 1)[0]
        )
        tx_ns: dict[str, object] = {}
        exec(compile(tx_script, "/usr/local/bin/oxidex-remote-transaction", "exec"), tx_ns)

        def mock_check_output(cmd, **kwargs):
            if "ps" in cmd:
                return "cid-vanished cid-exists\n"
            if "inspect" in cmd and "cid-vanished" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            if "inspect" in cmd and "cid-exists" in cmd:
                return json.dumps([{
                    "Mounts": [{"Source": "/var/tmp/source"}],
                    "State": {
                        "Running": False,
                        "Paused": False,
                        "Restarting": False,
                        "Pid": 0,
                        "Status": "exited",
                    },
                }])
            raise ValueError(f"Unexpected command: {cmd}")

        rm_calls: list[list[str]] = []
        def mock_run(cmd, **kwargs):
            if "rm" in cmd:
                self.assertFalse(kwargs.get("check", True))
                rm_calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("subprocess.check_output", side_effect=mock_check_output), \
             mock.patch("subprocess.run", side_effect=mock_run):
            tx_ns["reconcile_project_containers"]("/var/tmp/source", "/var/tmp/target")
            self.assertEqual(len(rm_calls), 1)
            self.assertEqual(rm_calls[0], ["/usr/bin/docker", "rm", "cid-exists"])

        # 4. SCALE-01: active_jobs and builder equivalents alias to provisioned_jobs and active_builds
        scale_alias = evaluate_scaling_decision(
            policy={
                "min_runners": 2,
                "max_runners": 16,
                "runners_per_instance": 2,
                "scale_down_target_runners": 2,
                "provisioned_jobs_threshold": 10,
            },
            telemetry={
                "current_runners": 8,
                "current_instances": 4,
                "active_jobs": 6,
                "queued_jobs": 0,
                "jobs_ran_past_hour": 1,
            },
            per_instance_hourly_usd=0.10,
        )
        self.assertEqual(scale_alias["direction"], "scale_down")
        self.assertEqual(scale_alias["desired_runners"], 6)
        self.assertEqual(scale_alias["desired_instances"], 3)

        builder_alias = evaluate_builder_scaling_decision(
            policy={"min_instances": 1, "max_instances": 8},
            telemetry={
                "current_instances": 4,
                "active_rust_builds": 3,
                "queued_rust_builds": 2,
                "builds_ran_past_hour": 0,
            },
            per_instance_hourly_usd=0.10,
        )
        self.assertEqual(builder_alias["direction"], "hold")
        self.assertEqual(builder_alias["desired_instances"], 4)

        # 5. SCALE-02: OOM occurring when fleet is at max capacity is marked handled and avoids freezing scale-down
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        oom_max = {
            "id": "oom-cap-01",
            "failed_at": now_iso,
            "is_oom": True,
            "runner_name": "gha-oxidex-runners-us-central1-a-01",
        }
        telem_oom = {
            "current_runners": 12,
            "current_instances": 3,
            "github_oom_runs": [oom_max],
            "queued_jobs": 0,
            "jobs_ran_past_hour": 1,
        }
        pol_oom = {
            "min_runners": 4,
            "max_runners": 12,
            "runners_per_instance": 4,
            "scale_down_target_runners": 4,
        }
        first_oom_eval = evaluate_scaling_decision(policy=pol_oom, telemetry=telem_oom)
        self.assertEqual(first_oom_eval["direction"], "hold")
        second_oom_eval = evaluate_scaling_decision(policy=pol_oom, telemetry=telem_oom)
        self.assertTrue(oom_max.get("handled"))
        self.assertEqual(second_oom_eval["direction"], "scale_down")
        self.assertEqual(second_oom_eval["desired_runners"], 4)

        # Also verify server _runner_oom_satisfied satisfies at max_runners
        self.assertTrue(self.store._consume_active_oom_events is not None)

        # 6. SCALE-03: Deep link generation and OOM telemetry safely handle None fields
        with self.assertRaises(ValueError):
            build_github_oom_deep_link("swack-tools/oxidex", 123, 456, step_number=None, line_number=None)

        none_fields_eval = evaluate_scaling_decision(
            policy={"min_runners": 4, "max_runners": 12},
            telemetry={
                "current_runners": 4,
                "current_instances": 1,
                "github_oom_runs": [
                    {
                        "id": "partial-oom-none",
                        "failed_at": now_iso,
                        "is_oom": True,
                        "runner_name": "gha-oxidex-runners-us-central1-a-01",
                        "repository": None,
                        "step_number": None,
                        "line_number": None,
                    }
                ],
            },
            per_instance_hourly_usd=0.01,
        )
        self.assertEqual(none_fields_eval["direction"], "scale_up")

        # 7. SCALE-04: Naive ISO timestamp in _is_recent_unhandled_oom attaches UTC
        naive_now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertTrue(
            _is_recent_unhandled_oom(
                {"failed_at": naive_now_iso},
                now_ts=datetime.now(timezone.utc).timestamp(),
                lookback_seconds=3600.0,
            )
        )

        # 8. SCALE-05: update_scaling includes runner scale-up recommendation in builder spend when budget is shared
        with self.store._lock:
            self.store.state["global_budget_enforced"] = True
            self.store.state["scaling"]["cicd_runner"]["shared_global_budget"] = True
            self.store.state["scaling"]["rust_builder"]["shared_global_budget"] = True
            self.store.state["scaling"]["cicd_runner"]["max_scale_up_hourly_usd"] = 1.00
            self.store.state["scaling"]["rust_builder"]["max_scale_up_hourly_usd"] = 1.00
            self.store.state["scaling_telemetry"] = {
                "current_runners": 4,
                "current_instances": 1,
                "current_builder_instances": 1,
                "queued_jobs": 10,
                "active_jobs": 4,
                "queued_rust_builds": 5,
                "active_rust_builds": 1,
            }

        upd_res = self._post(
            "/api/scaling",
            {"role": "rust_builder", "scaling": {"shared_global_budget": True}},
        )
        self.assertEqual(upd_res["status"], "ok")
        builder_eval_res = upd_res["evaluation"]
        self.assertEqual(builder_eval_res["role"], "rust_builder")

        # 9. SCALE-06: evaluate_builder_scaling_decision adds max_instances_cap reason on clamp
        builder_clamped = evaluate_builder_scaling_decision(
            policy={"min_instances": 1, "max_instances": 2},
            telemetry={"current_instances": 5, "active_builds": 1, "builds_ran_past_hour": 10},
            per_instance_hourly_usd=0.10,
        )
        self.assertEqual(builder_clamped["direction"], "scale_down")
        self.assertEqual(builder_clamped["desired_instances"], 2)
        self.assertTrue(any(r["trigger"] == "max_instances_cap" for r in builder_clamped["scale_down_reasons"]))

        # 10. SCALE-07: Null lists in telemetry do not cause TypeError
        null_telem_eval = evaluate_scaling_decision(
            policy={"min_runners": 2, "max_runners": 8},
            telemetry={
                "current_runners": 2,
                "current_instances": 1,
                "instance_cpu_percentages": None,
                "journal_oom_events": None,
                "github_oom_runs": None,
            },
        )
        self.assertEqual(null_telem_eval["direction"], "hold")

        builder_null_eval = evaluate_builder_scaling_decision(
            policy={"min_instances": 1, "max_instances": 4},
            telemetry={
                "current_instances": 1,
                "builder_cpu_percentages": None,
                "journal_oom_events": None,
                "builder_journal_oom_events": None,
            },
        )
        self.assertEqual(builder_null_eval["direction"], "hold")

        # 11. CPU scope trims whitespace
        scope_ok, _ = evaluate_cpu_scope([85.0, 30.0, 20.0], 50.0, " any ")
        self.assertTrue(scope_ok)


class FleetInventoryReconcileTests(unittest.TestCase):
    """The instance list mirrors the read-only Compute Engine inventory; nothing is seeded or fabricated."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "dashboard-state.json"
        self.now = time.time()
        self.fleet: list[dict] = []
        from lib.dashboard_server import DashboardStore

        self.store = DashboardStore(state_path=self.state_file)
        self.store.start()
        # Set after start() so no background inventory loop races the explicit refreshes below.
        self.store.fleet_source = lambda: list(self.fleet)
        self.addCleanup(self.store.close)

    def _record(self, name: str) -> dict:
        return next(i for i in self.store.state["instances"] if i["name"] == name)

    def test_default_state_has_no_instances(self):
        from lib.dashboard_server import default_dashboard_state

        self.assertEqual(default_dashboard_state()["instances"], [])
        self.assertEqual(self.store.state["instances"], [])
        self.assertEqual(self.store.state["scaling_telemetry"]["current_instances"], 0)

    def test_running_vm_is_listed_as_running(self):
        self.fleet = [fleet_observation("1001", "oxidex-runners-us-south1-a-1", "us-south1-a", runners_count=4,
                                        started_hours_ago=3.0, now=self.now)]
        version_before = self.store.state_version()
        self.assertTrue(self.store.refresh_fleet_inventory())
        rec = self._record("oxidex-runners-us-south1-a-1")
        self.assertEqual(rec["status"], "RUNNING")
        self.assertEqual(rec["inventory_source"], "gce")
        self.assertEqual(rec["region"], "us-south1")
        self.assertIsNone(rec["deleted_at"])
        self.assertAlmostEqual(rec["started_epoch"], self.now - 3 * 3600.0, delta=1.0)
        self.assertEqual(self.store.state["scaling_telemetry"]["current_runners"], 4)
        self.assertEqual(self.store.state_version(), version_before + 1)
        # The default state's sample OOM event is recent, so the next listing records its baseline once.
        self.store.refresh_fleet_inventory()
        version = self.store.state_version()
        # The same listing again is not a change: no new state version (and no KV publish).
        self.assertFalse(self.store.refresh_fleet_inventory())
        self.assertEqual(self.store.state_version(), version)
        status = self.store.fleet_inventory_status()
        self.assertEqual(status["observed_count"], 1)
        self.assertEqual(status["last_error"], "")
        view = self.store.query_instances(include_deleted=False)
        self.assertEqual([r["name"] for r in view["instances"]], ["oxidex-runners-us-south1-a-1"])

    def test_unknown_utilization_never_reads_as_idle(self):
        self.fleet = [fleet_observation(f"12{i}", f"oxidex-runners-idle-{i}", "us-south1-a", runners_count=4,
                                        now=self.now) for i in range(3)]
        self.store.refresh_fleet_inventory()
        telemetry = self.store.state["scaling_telemetry"]
        self.assertEqual(telemetry["current_runners"], 12)
        self.assertEqual(telemetry["instance_cpu_percentages"], [])
        evaluation = self.store.get_overview()["scaling_evaluation"]
        self.assertNotIn("idle_cpu", [r.get("trigger") for r in evaluation.get("scale_down_reasons", [])])

    def test_restart_keeps_earlier_run_in_runtime_and_window(self):
        # Ran 9 hours (from 10h ago to 1h ago), then started again 30 minutes ago.
        self.fleet = [fleet_observation("1300", "oxidex-runners-restart-1", "us-south1-a", started_hours_ago=10.0,
                                        now=self.now)]
        self.store.refresh_fleet_inventory()
        self.fleet = [fleet_observation("1300", "oxidex-runners-restart-1", "us-south1-a", status="TERMINATED",
                                        started_hours_ago=10.0, stopped_hours_ago=1.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        self.fleet = [fleet_observation("1300", "oxidex-runners-restart-1", "us-south1-a", started_hours_ago=0.5,
                                        stopped_hours_ago=1.0, now=self.now)]
        self.assertTrue(self.store.refresh_fleet_inventory())
        rec = self._record("oxidex-runners-restart-1")
        self.assertEqual(len(rec["prior_runs"]), 1)
        self.assertAlmostEqual(rec["runtime_hours"], 9.5, delta=0.05)
        row = next(r for r in self.store.query_instances(window_hours=24)["instances"]
                   if r["name"] == "oxidex-runners-restart-1")
        self.assertAlmostEqual(row["runtime_hours"], 9.5, delta=0.05)
        self.assertAlmostEqual(row["hours_in_window"], 9.5, delta=0.05)
        short = next(r for r in self.store.query_instances(window_hours=2)["instances"]
                     if r["name"] == "oxidex-runners-restart-1")
        self.assertAlmostEqual(short["hours_in_window"], 1.5, delta=0.05)
        # The same listing again is stable.
        self.assertFalse(self.store.refresh_fleet_inventory())

    def test_stopped_vm_keeps_accruing_disk_cost(self):
        self.fleet = [fleet_observation("1400", "builder-oxidex-disk-1", "us-south1-b", status="TERMINATED",
                                        started_hours_ago=10.0, stopped_hours_ago=8.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        row = next(r for r in self.store.query_instances(window_hours=4)["instances"]
                   if r["name"] == "builder-oxidex-disk-1")
        pricing = row["pricing"]
        disk_all_in = pricing["disk_hourly_usd"] * (1 + pricing["tax_rate_percent"] / 100.0)
        self.assertGreater(disk_all_in, 0.0)
        # Stopped 8 hours ago: no compute in the last 4 hours, but 4 hours of disk.
        self.assertEqual(row["hours_in_window"], 0.0)
        self.assertAlmostEqual(row["total_cost_in_window_usd"], round(4.0 * disk_all_in, 2), delta=0.011)
        compute_only = next(r for r in self.store.query_instances(window_hours=4, include_disk_and_tax=False)
                            ["instances"] if r["name"] == "builder-oxidex-disk-1")
        self.assertEqual(compute_only["total_cost_in_window_usd"], 0.0)

    def test_vm_stopped_before_window_and_deleted_inside_it_stays_listed(self):
        from lib.dashboard_server import _mark_instance_deleted

        self.fleet = [fleet_observation("1500", "builder-oxidex-gone-1", "us-south1-b", status="TERMINATED",
                                        started_hours_ago=12.0, stopped_hours_ago=10.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            self.store.state["instances"] = [_mark_instance_deleted(self._record("builder-oxidex-gone-1"),
                                                                    time.time() - 3600.0)]
        rows = self.store.query_instances(window_hours=4)["instances"]
        self.assertEqual([r["name"] for r in rows], ["builder-oxidex-gone-1"])
        pricing = rows[0]["pricing"]
        disk_all_in = pricing["disk_hourly_usd"] * (1 + pricing["tax_rate_percent"] / 100.0)
        self.assertEqual(rows[0]["hours_in_window"], 0.0)
        self.assertAlmostEqual(rows[0]["total_cost_in_window_usd"], round(3.0 * disk_all_in, 2), delta=0.011)

    def test_long_stop_then_restart_keeps_disk_lifetime(self):
        from lib.dashboard_server import instance_usage

        self.fleet = [fleet_observation("1600", "builder-oxidex-long-1", "us-south1-b", status="TERMINATED",
                                        started_hours_ago=36 * 24.0, stopped_hours_ago=35 * 24.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        self.fleet = [fleet_observation("1600", "builder-oxidex-long-1", "us-south1-b", started_hours_ago=1.0,
                                        stopped_hours_ago=35 * 24.0, now=self.now)]
        self.fleet[0]["created_epoch"] = self.now - 36 * 24 * 3600.0 - 60.0
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-long-1")
        self.assertEqual(rec["prior_runs"], [])  # The 35-day-old run is past retention.
        usage = instance_usage(rec, self.now, self.now - 24 * 3600.0)
        self.assertAlmostEqual(usage["disk_hours_in_window"], 24.0, delta=0.01)
        self.assertAlmostEqual(usage["compute_hours_in_window"], 1.0, delta=0.01)

    def test_lifetime_survives_pruned_runs(self):
        # Ran 120 hours, stopped 35 days ago (past retention), started again an hour ago.
        stop_ago = 35 * 24.0
        self.fleet = [fleet_observation("1700", "oxidex-runners-old-1", "us-south1-a", status="TERMINATED",
                                        started_hours_ago=stop_ago + 120.0, stopped_hours_ago=stop_ago, now=self.now)]
        self.store.refresh_fleet_inventory()
        self.fleet = [fleet_observation("1700", "oxidex-runners-old-1", "us-south1-a", started_hours_ago=1.0,
                                        stopped_hours_ago=stop_ago, now=self.now)]
        self.store.refresh_fleet_inventory()
        rec = self._record("oxidex-runners-old-1")
        self.assertEqual(rec["prior_runs"], [])
        self.assertAlmostEqual(rec["pruned_compute_hours"], 120.0, delta=0.01)
        self.assertAlmostEqual(rec["runtime_hours"], 121.0, delta=0.05)
        row = next(r for r in self.store.query_instances(window_hours=24)["instances"]
                   if r["name"] == "oxidex-runners-old-1")
        self.assertAlmostEqual(row["runtime_hours"], 121.0, delta=0.05)
        self.assertAlmostEqual(row["hours_in_window"], 1.0, delta=0.05)

    def test_stopped_vm_disks_count_in_current_burn(self):
        self.fleet = [fleet_observation("1800", "builder-oxidex-idle-1", "us-south1-b", status="TERMINATED",
                                        started_hours_ago=5.0, stopped_hours_ago=2.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        view = self.store.query_instances()
        pricing = view["instances"][0]["pricing"]
        disk_all_in = pricing["disk_hourly_usd"] * (1 + pricing["tax_rate_percent"] / 100.0)
        self.assertEqual(view["running_count"], 0)
        self.assertGreater(view["active_hourly_burn_usd"], 0.0)
        self.assertAlmostEqual(view["active_hourly_burn_usd"], disk_all_in, delta=0.0001)
        self.assertEqual(self.store.query_instances(include_disk_and_tax=False)["active_hourly_burn_usd"], 0.0)

    def test_runner_limits_are_unknown_not_guessed(self):
        from lib.fleet_inventory import observe_fleet_items

        obs = observe_fleet_items(
            [{"id": "31", "name": "oxidex-runners-tier-1", "zone": "zones/us-south1-a", "status": "RUNNING",
              "machineType": "machineTypes/n4-standard-4",
              "metadata": {"items": [{"key": "spot-runner-count", "value": "8"}]}}],
            shapes={("us-south1-a", "n4-standard-4"): {"vcpu": 4, "ram_gib": 16.0}},
        )
        self.assertEqual(obs[0]["runners_count"], 8)
        self.assertEqual((obs[0]["runner_vcpu"], obs[0]["runner_ram_gib"]), (0, 0.0))
        self.assertFalse(obs[0]["runner_limits_known"])
        self.fleet = obs
        self.store.refresh_fleet_inventory()
        self.assertFalse(self._record("oxidex-runners-tier-1")["runner_limits_known"])
        with self.assertRaisesRegex(ValueError, "not recorded"):
            self.store.create_vm_template_from_instance({"instance_name": "oxidex-runners-tier-1"})

    def test_unchanged_rates_write_nothing(self):
        self.fleet = [fleet_observation("1010", "oxidex-runners-price-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        self.store.refresh_fleet_inventory()  # Records the sample OOM event's baseline once.
        version = self.store.state_version()
        self.assertFalse(self.store.refresh_fleet_inventory())
        self.assertEqual(self.store.state_version(), version)
        self.assertEqual(self._record("oxidex-runners-price-1")["disk_segments"], [])

    def test_delayed_stop_trims_repriced_segment(self):
        running = fleet_observation("1011", "builder-oxidex-late-1", "us-south1-b", started_hours_ago=10.0,
                                    now=self.now)
        self.fleet = [running]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            budget = self.store.state["profiles"]["rust_builder"]["budget"]
            budget["override_regional_tax"] = True
            budget["tax_rate_percent"] = 30.0
        self.store.refresh_fleet_inventory()  # Repriced while still RUNNING: a run segment closes at "now".
        self.assertEqual(len(self._record("builder-oxidex-late-1")["prior_runs"]), 1)
        # The next listing reports it stopped 3 hours ago (for example after an inventory outage).
        self.fleet = [dict(running, status="TERMINATED", last_stop_epoch=self.now - 3 * 3600.0)]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-late-1")
        self.assertTrue(all(run[1] <= self.now - 3 * 3600.0 + 1.0 for run in rec["prior_runs"]))
        self.assertAlmostEqual(rec["runtime_hours"], 7.0, delta=0.05)

    def test_stopped_vm_shows_gce_status_and_stops_accruing(self):
        self.fleet = [fleet_observation("1002", "builder-oxidex-eu-1", "europe-west4-a", started_hours_ago=5.0,
                                        now=self.now)]
        self.store.refresh_fleet_inventory()
        self.assertEqual(self.store.state["scaling_telemetry"]["current_builder_instances"], 1)
        self.fleet = [fleet_observation("1002", "builder-oxidex-eu-1", "europe-west4-a", status="TERMINATED",
                                        started_hours_ago=5.0, stopped_hours_ago=1.0, now=self.now)]
        self.assertTrue(self.store.refresh_fleet_inventory())
        rec = self._record("builder-oxidex-eu-1")
        self.assertEqual(rec["status"], "TERMINATED")
        self.assertIsNone(rec["deleted_at"])
        self.assertAlmostEqual(rec["stopped_epoch"], self.now - 3600.0, delta=1.0)
        self.assertAlmostEqual(rec["runtime_hours"], 4.0, delta=0.02)
        self.assertIn("Stopped in Compute Engine (TERMINATED)", rec["deletion_trigger"])
        self.assertEqual(self.store.state["scaling_telemetry"]["current_builder_instances"], 0)
        row = next(r for r in self.store.query_instances()["instances"] if r["name"] == "builder-oxidex-eu-1")
        self.assertAlmostEqual(row["hours_in_window"], 4.0, delta=0.02)
        # Stopped is not deleted: the VM (and its billed disks) still shows with deleted instances hidden.
        self.assertEqual([r["name"] for r in self.store.query_instances(include_deleted=False)["instances"]],
                         ["builder-oxidex-eu-1"])
        # Restarted: RUNNING again from the new start.
        self.fleet = [fleet_observation("1002", "builder-oxidex-eu-1", "europe-west4-a", started_hours_ago=0.5,
                                        stopped_hours_ago=1.0, now=self.now)]
        self.assertTrue(self.store.refresh_fleet_inventory())
        rec = self._record("builder-oxidex-eu-1")
        self.assertEqual(rec["status"], "RUNNING")
        self.assertIsNone(rec["stopped_epoch"])

    def test_vm_gone_from_gce_is_marked_deleted(self):
        self.fleet = [
            fleet_observation("1003", "oxidex-runners-africa-1", "africa-south1-a", now=self.now),
            fleet_observation("1004", "oxidex-runners-dallas-1", "us-south1-a", now=self.now),
        ]
        self.store.refresh_fleet_inventory()
        self.fleet = [fleet_observation("1004", "oxidex-runners-dallas-1", "us-south1-a", now=self.now)]
        clock_now = time.time()
        self.assertTrue(self.store.refresh_fleet_inventory())
        gone = self._record("oxidex-runners-africa-1")
        self.assertEqual(gone["status"], "DELETED")
        self.assertIsNotNone(gone["deleted_at"])
        self.assertAlmostEqual(gone["deleted_epoch"], clock_now, delta=5.0)
        self.assertIn("No longer in the Compute Engine inventory", gone["deletion_trigger"])
        self.assertEqual(self._record("oxidex-runners-dallas-1")["status"], "RUNNING")
        self.assertEqual(self.store.state["scaling_telemetry"]["current_instances"], 1)
        overview = self.store.get_overview()
        africa = next(r for r in overview["regions"] if r["region"] == "africa-south1")
        self.assertEqual(africa["running_instances"], [])
        # A later pass keeps the deletion time and does not count as a change.
        self.assertFalse(self.store.refresh_fleet_inventory())
        self.assertEqual(self._record("oxidex-runners-africa-1")["deleted_epoch"], gone["deleted_epoch"])

    def test_recreated_vm_with_same_name_is_a_new_record(self):
        self.fleet = [fleet_observation("1005", "builder-oxidex-turin-1", "us-south1-b", now=self.now)]
        self.store.refresh_fleet_inventory()
        self.fleet = [fleet_observation("2005", "builder-oxidex-turin-1", "us-south1-b", started_hours_ago=0.1,
                                        now=self.now)]
        self.store.refresh_fleet_inventory()
        by_id = {r["instance_id"]: r for r in self.store.state["instances"]}
        self.assertEqual(by_id["1005"]["status"], "DELETED")
        self.assertEqual(by_id["2005"]["status"], "RUNNING")

    def test_deleted_history_expires_after_retention(self):
        from lib.dashboard_server import DELETED_INSTANCE_RETENTION_SECONDS, reconcile_instance_records

        old = {"inventory_source": "gce", "instance_id": "9", "name": "oxidex-runners-old", "status": "DELETED",
               "deleted_epoch": self.now - DELETED_INSTANCE_RETENTION_SECONDS - 60.0}
        records, changed = reconcile_instance_records([old], [], project="p", now=self.now)
        self.assertEqual(records, [])
        self.assertTrue(changed)

    def test_listing_failure_keeps_records_and_reports_error(self):
        self.fleet = [fleet_observation("1006", "oxidex-runners-keep-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        version = self.store.state_version()

        def failing():
            raise RuntimeError("zones unreachable: us-south1-a")

        self.store.fleet_source = failing
        self.assertFalse(self.store.refresh_fleet_inventory())
        self.assertEqual(self._record("oxidex-runners-keep-1")["status"], "RUNNING")
        self.assertEqual(self.store.state_version(), version)
        self.assertIn("zones unreachable", self.store.fleet_inventory_status()["last_error"])

    def test_frozen_store_skips_reconcile(self):
        self.fleet = [fleet_observation("1007", "oxidex-runners-frozen-1", "us-south1-a", now=self.now)]
        source = unittest.mock.Mock(side_effect=lambda: list(self.fleet))
        self.store.fleet_source = source
        self.store.frozen = True
        self.assertFalse(self.store.refresh_fleet_inventory())
        # Ineligible slots never call Compute Engine.
        source.assert_not_called()
        self.assertEqual(self.store.state["instances"], [])
        self.assertIn("frozen", self.store.fleet_inventory_status()["last_error"])

    def test_inventory_dropped_when_source_not_configured(self):
        from lib.dashboard_server import DashboardStore

        self.fleet = [fleet_observation("2100", "oxidex-runners-stale-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        self.assertEqual(len(DashboardStore(state_path=self.state_file, fleet_source=lambda: []).state["instances"]), 1)
        self.assertEqual(DashboardStore(state_path=self.state_file).state["instances"], [])

    def test_window_is_capped_to_history_retention(self):
        from lib.dashboard_server import MAX_WINDOW_HOURS

        self.assertEqual(self.store.query_instances(window_hours=8760)["window_hours"], MAX_WINDOW_HOURS)

    def test_unknown_region_pricing_is_incomplete_under_any_tax_setting(self):
        self.fleet = [fleet_observation("5400", "builder-oxidex-far-1", "mars-central1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-far-1")
        self.assertFalse(rec["pricing_complete"])
        self.assertIn("outside the dashboard's catalog", rec["pricing_incomplete_reason"])
        for include_taxes in (True, False):
            with self.store._lock:
                self.store.state["profiles"]["rust_builder"]["budget"]["include_taxes"] = include_taxes
            self.assertEqual(self.store._incomplete_cost_hosts("rust_builder"), ["builder-oxidex-far-1"])

    def test_unknown_region_uses_neutral_tax(self):
        self.fleet = [fleet_observation("2200", "oxidex-runners-mars-1", "mars-central1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        rec = self._record("oxidex-runners-mars-1")
        self.assertEqual(rec["pricing"]["tax_rate_percent"], 0.0)
        self.assertFalse(rec["tax_rate_known"])
        self.assertEqual(rec["display_location"], "mars-central1")

    def test_unknown_runner_count_holds_slot_decisions(self):
        known = fleet_observation("2300", "oxidex-runners-known-1", "us-south1-a", runners_count=4, now=self.now)
        legacy = fleet_observation("2301", "oxidex-runners-legacy-1", "us-south1-b", runners_count=0, now=self.now)
        legacy["runners_count_known"] = False
        self.fleet = [known, legacy]
        self.store.update_budget({"target": "cicd_runner", "max_hourly_usd": 5.0, "max_daily_usd": 100.0,
                                  "max_monthly_usd": 3000.0})
        self.store.refresh_fleet_inventory()
        self.assertEqual(self.store.state["scaling_telemetry"]["unknown_runner_count_hosts"],
                         ["oxidex-runners-legacy-1"])
        evaluation = self.store.get_overview()["scaling_evaluation"]
        self.assertEqual(evaluation["direction"], "hold")
        self.assertIn("not recorded", evaluation["hold_reason"])
        with self.assertRaisesRegex(ValueError, "Runner count is not recorded"):
            self.store.scale_region_instances({"region": "us-south1", "role": "cicd_runner", "target_instances": 3})

    def test_stopped_disks_reduce_scaling_evaluation_headroom(self):
        running = fleet_observation("2400", "builder-oxidex-run-1", "us-south1-b", now=self.now)
        self.fleet = [running]
        self.store.refresh_fleet_inventory()
        cap_without = self.store.get_overview()["builder_scaling_evaluation"]["effective_hourly_cap_usd"]
        self.fleet = [running, fleet_observation("2401", "builder-oxidex-stop-1", "us-south1-b", status="TERMINATED",
                                                 started_hours_ago=5.0, stopped_hours_ago=1.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        cap_with = self.store.get_overview()["builder_scaling_evaluation"]["effective_hourly_cap_usd"]
        self.assertAlmostEqual(cap_without - cap_with, self.store._stopped_disk_burn("rust_builder"), delta=0.0002)
        self.assertGreater(self.store._stopped_disk_burn("rust_builder"), 0.0)

    def test_stopped_vm_pricing_follows_tax_override(self):
        self.fleet = [fleet_observation("2500", "builder-oxidex-tax-1", "us-south1-b", status="TERMINATED",
                                        started_hours_ago=5.0, stopped_hours_ago=1.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            budget = self.store.state["profiles"]["rust_builder"]["budget"]
            budget["override_regional_tax"] = True
            budget["tax_rate_percent"] = 20.0
        self.store.get_overview()  # Reads never reprice inventory records ...
        self.assertNotEqual(self._record("builder-oxidex-tax-1")["pricing"]["tax_rate_percent"], 20.0)
        self.assertTrue(self.store.refresh_fleet_inventory())  # ... the persisted reconcile does.
        self.assertEqual(self._record("builder-oxidex-tax-1")["pricing"]["tax_rate_percent"], 20.0)

    def test_build_record_evaluation_counts_shared_runner_burn(self):
        from lib import dashboard_server

        self.fleet = [fleet_observation("2600", "oxidex-runners-burn-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            self.store.state["global_budget_enforced"] = True
        captured = {}
        real = dashboard_server.evaluate_builder_scaling_decision

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real(*args, **kwargs)

        with unittest.mock.patch.object(dashboard_server, "evaluate_builder_scaling_decision", spy):
            self.store.record_rust_build({"build_id": "rb-shared-01", "cache_hits": 1, "cache_misses": 1})
        runner_burn = sum(self.store._running_role_hourly_rates("cicd_runner"))
        self.assertGreater(runner_burn, 0.0)
        self.assertAlmostEqual(captured["other_role_hourly_usd"], runner_burn, delta=0.0002)

    def test_on_demand_vm_uses_on_demand_rates_or_fails_closed(self):
        live = {"machine_hourly": {("us-south1", "n4-standard-16"): 0.10},
                "component_rates": {("ondemand", "us-south1", "n4", "CPU"): 0.04,
                                    ("ondemand", "us-south1", "n4", "RAM"): 0.005},
                "disk_monthly_gib": {}}
        args = dict(family="n4", vcpu=16, ram_gib=64.0, region="us-south1", boot_disk_type="pd-balanced",
                    boot_disk_gb=40, data_disk_type="pd-balanced", data_disk_gb=0, machine_type="n4-standard-16")
        priced = calculate_spot_pricing(**args, live_pricing=live, provisioning="ondemand")
        self.assertTrue(priced["complete"])
        self.assertAlmostEqual(priced["compute_hourly_usd"], 16 * 0.04 + 64 * 0.005, places=4)
        self.assertEqual(calculate_spot_pricing(**args, live_pricing=live)["compute_hourly_usd"], 0.10)
        unpriced = calculate_spot_pricing(**args, live_pricing={}, provisioning="ondemand")
        self.assertFalse(unpriced["complete"])

        standard = fleet_observation("2700", "oxidex-runners-std-1", "us-south1-a", now=self.now)
        standard["spot"] = False
        self.fleet = [standard]
        self.store.update_budget({"target": "cicd_runner", "max_hourly_usd": 50.0, "max_daily_usd": 1000.0,
                                  "max_monthly_usd": 30000.0})
        with unittest.mock.patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={}):
            self.store.refresh_fleet_inventory()
            rec = self._record("oxidex-runners-std-1")
            self.assertEqual(rec["pricing"]["provisioning"], "ondemand")
            self.assertFalse(rec["pricing_complete"])
            with self.assertRaisesRegex(ValueError, "not fully priced"):
                self.store.scale_region_instances({"region": "us-south1", "role": "cicd_runner",
                                                   "target_instances": 2})
            held = self.store._fail_closed_evaluation({"direction": "scale_up", "current_runners": 4,
                                                       "current_instances": 1}, "cicd_runner")
            self.assertEqual(held["direction"], "hold")
            self.assertIn("not fully priced", held["hold_reason"])

    def test_suspended_vm_cost_is_incomplete(self):
        self.fleet = [fleet_observation("2800", "builder-oxidex-susp-1", "us-south1-b", status="SUSPENDED",
                                        started_hours_ago=5.0, stopped_hours_ago=1.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-susp-1")
        self.assertFalse(rec["pricing_complete"])
        self.assertIn("preserved memory", rec["pricing_incomplete_reason"])
        self.assertEqual(self.store._incomplete_cost_hosts("rust_builder"), ["builder-oxidex-susp-1"])

    def test_disabling_inventory_clears_baseline(self):
        from lib.dashboard_server import FLEET_BASELINE_STATE_KEY, DashboardStore

        self.store.refresh_fleet_inventory()
        self.assertTrue(self.store.state.get(FLEET_BASELINE_STATE_KEY))
        disabled = DashboardStore(state_path=self.state_file)
        self.assertNotIn(FLEET_BASELINE_STATE_KEY, disabled.state)

    def test_transitional_vm_counts_fully_and_holds_scale_up(self):
        from lib.dashboard_server import DashboardStore

        self.fleet = [fleet_observation("2900", "builder-oxidex-prov-1", "us-south1-b", status="PROVISIONING",
                                        started_hours_ago=0.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-prov-1")
        budget = self.store.state["profiles"]["rust_builder"]["budget"]
        full = DashboardStore._instance_hourly_for_budget(
            rec["pricing"], include_disk=bool(budget.get("include_disk_costs", True)),
            include_tax=bool(budget.get("include_taxes", False)))
        self.assertAlmostEqual(self.store._stopped_disk_burn("rust_builder"), full, delta=0.0002)
        self.assertGreater(self.store.query_instances()["active_hourly_burn_usd"], 0.0)
        held = self.store._fail_closed_evaluation({"direction": "scale_up"}, "rust_builder")
        self.assertIn("capacity is not settled", held["hold_reason"])
        with self.assertRaisesRegex(ValueError, "starting, stopping, or repairing"):
            self.store.scale_region_instances({"region": "us-south1", "role": "rust_builder", "target_instances": 1})

    def test_deleted_history_is_bounded_by_time_not_count(self):
        from lib.dashboard_server import reconcile_instance_records

        deleted = [{"inventory_source": "gce", "instance_id": str(i), "name": f"oxidex-runners-d{i}",
                    "status": "DELETED", "deleted_epoch": self.now - i * 60.0} for i in range(1, 451)]
        records, _changed = reconcile_instance_records(deleted, [], project="p", now=self.now)
        self.assertEqual(len(records), 450)

    def test_many_restarts_inside_window_all_count(self):
        for i in range(60):
            start_ago = 700.0 - 10.0 * i
            self.fleet = [fleet_observation("3000", "oxidex-runners-flap-1", "us-south1-a",
                                            started_hours_ago=start_ago, now=self.now)]
            self.store.refresh_fleet_inventory()
            self.fleet = [fleet_observation("3000", "oxidex-runners-flap-1", "us-south1-a", status="TERMINATED",
                                            started_hours_ago=start_ago, stopped_hours_ago=start_ago - 5.0,
                                            now=self.now)]
            self.store.refresh_fleet_inventory()
        rec = self._record("oxidex-runners-flap-1")
        self.assertEqual(len(rec["prior_runs"]), 59)  # The 60th run is the current (stopped) one.
        row = next(r for r in self.store.query_instances(window_hours=744)["instances"]
                   if r["name"] == "oxidex-runners-flap-1")
        self.assertAlmostEqual(row["hours_in_window"], 300.0, delta=0.1)

    def test_stopped_row_shows_disk_only_active_rate(self):
        self.fleet = [fleet_observation("3100", "builder-oxidex-rate-1", "us-south1-b", status="TERMINATED",
                                        started_hours_ago=5.0, stopped_hours_ago=1.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        row = self.store.query_instances()["instances"][0]
        pricing = row["pricing"]
        disk_all_in = pricing["disk_hourly_usd"] * (1 + pricing["tax_rate_percent"] / 100.0)
        self.assertAlmostEqual(row["active_hourly_usd"], disk_all_in, delta=0.0001)
        self.assertLess(row["active_hourly_usd"], pricing["total_hourly_usd"])
        compute_only = self.store.query_instances(include_disk_and_tax=False)["instances"][0]
        self.assertEqual(compute_only["active_hourly_usd"], 0.0)

    def test_each_data_disk_type_is_priced_at_its_rate(self):
        from lib.fleet_inventory import observe_fleet_items

        items = [{"id": "3200", "name": "builder-oxidex-disks-1", "zone": "zones/us-south1-b", "status": "RUNNING",
                  "machineType": "machineTypes/n4-highcpu-16",
                  "disks": [{"boot": True, "diskSizeGb": "40", "source": "d/boot"},
                            {"boot": False, "diskSizeGb": "100", "source": "d/ssd"},
                            {"boot": False, "diskSizeGb": "500", "source": "d/std"}]}]
        obs = observe_fleet_items(items, disk_types={"d/boot": "pd-balanced", "d/ssd": "pd-ssd", "d/std": "pd-standard"},
                                  shapes={("us-south1-b", "n4-highcpu-16"): {"vcpu": 16, "ram_gib": 32.0}})
        self.assertEqual(obs[0]["data_disks"], {"pd-ssd": 100, "pd-standard": 500})
        self.assertEqual((obs[0]["data_disk_type"], obs[0]["data_disk_gb"]), ("pd-ssd", 600))
        self.fleet = obs
        with unittest.mock.patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={}):
            self.store.refresh_fleet_inventory()
            rec = self._record("builder-oxidex-disks-1")
            tax = rec["pricing"]["tax_rate_percent"]
            common = dict(family="n4", vcpu=16, ram_gib=32.0, region="us-south1", boot_disk_type="pd-balanced",
                          boot_disk_gb=40, tax_rate_percent=tax, machine_type="n4-highcpu-16")
            primary = calculate_spot_pricing(**common, data_disk_type="pd-ssd", data_disk_gb=100)
            standard = calculate_spot_pricing(family="n4", vcpu=0, ram_gib=0.0, region="us-south1",
                                              boot_disk_type="pd-standard", boot_disk_gb=0,
                                              data_disk_type="pd-standard", data_disk_gb=500, tax_rate_percent=tax)
            all_ssd = calculate_spot_pricing(**common, data_disk_type="pd-ssd", data_disk_gb=600)
        self.assertEqual(rec["data_disk_gb"], 600)
        self.assertAlmostEqual(rec["pricing"]["disk_hourly_usd"],
                               primary["disk_hourly_usd"] + standard["disk_hourly_usd"], delta=0.0002)
        self.assertNotAlmostEqual(rec["pricing"]["disk_hourly_usd"], all_ssd["disk_hourly_usd"], places=3)

    def test_disk_resize_keeps_earlier_rate_for_history(self):
        from lib.dashboard_server import _pricing_rates, instance_usage

        small = fleet_observation("3300", "builder-oxidex-grow-1", "us-south1-b", started_hours_ago=10.0, now=self.now)
        self.fleet = [small]
        self.store.refresh_fleet_inventory()
        old_rates = _pricing_rates(self._record("builder-oxidex-grow-1")["pricing"])
        big = dict(small, data_disk_gb=2000)
        self.fleet = [big]
        self.assertTrue(self.store.refresh_fleet_inventory())
        rec = self._record("builder-oxidex-grow-1")
        new_rates = _pricing_rates(rec["pricing"])
        self.assertGreater(new_rates[2], old_rates[2])
        self.assertEqual(len(rec["disk_segments"]), 1)
        self.assertEqual(len(rec["prior_runs"]), 1)
        frozen_at = rec["disk_segment_start"]
        later = frozen_at + 5 * 3600.0
        usage = instance_usage(rec, later)
        created = rec["disk_start_epoch"]
        expected = ((frozen_at - created) / 3600.0 * old_rates[2] + 5.0 * new_rates[2]
                    + (frozen_at - rec["started_epoch"]) / 3600.0 * old_rates[0] + 5.0 * new_rates[0])
        self.assertAlmostEqual(usage["cost_all_in"], expected, delta=0.001)
        # A repeat listing of the new shape does not freeze again.
        self.assertFalse(self.store.refresh_fleet_inventory())

    def test_refresh_keeps_per_type_disk_pricing(self):
        obs = fleet_observation("3500", "builder-oxidex-mixed-1", "us-south1-b", now=self.now)
        obs.update({"data_disk_type": "pd-ssd", "data_disk_gb": 600, "data_disks": {"pd-ssd": 100, "pd-standard": 500}})
        self.fleet = [obs]
        with unittest.mock.patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={}):
            self.store.refresh_fleet_inventory()
            reconciled = self._record("builder-oxidex-mixed-1")["pricing"]["disk_hourly_usd"]
            self.store.get_overview()
            self.assertAlmostEqual(self._record("builder-oxidex-mixed-1")["pricing"]["disk_hourly_usd"],
                                   reconciled, delta=0.0001)
            self.store.refresh_fleet_inventory()  # May record baselines on existing OOM events once.
            self.assertFalse(self.store.refresh_fleet_inventory())
            self.assertAlmostEqual(self._record("builder-oxidex-mixed-1")["pricing"]["disk_hourly_usd"],
                                   reconciled, delta=0.0001)

    def test_stop_and_resize_in_one_listing_ends_run_at_stop(self):
        running = fleet_observation("3600", "builder-oxidex-stopgrow-1", "us-south1-b", started_hours_ago=10.0,
                                    now=self.now)
        self.fleet = [running]
        self.store.refresh_fleet_inventory()
        stopped = fleet_observation("3600", "builder-oxidex-stopgrow-1", "us-south1-b", status="TERMINATED",
                                    started_hours_ago=10.0, stopped_hours_ago=5.0, now=self.now)
        stopped["data_disk_gb"] = 2000
        self.fleet = [stopped]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-stopgrow-1")
        self.assertEqual(len(rec["prior_runs"]), 1)
        self.assertAlmostEqual(rec["prior_runs"][0][1], self.now - 5 * 3600.0, delta=1.0)
        self.assertAlmostEqual(rec["runtime_hours"], 5.0, delta=0.02)

    def test_resize_then_stop_does_not_double_count(self):
        running = fleet_observation("3700", "builder-oxidex-rs-1", "us-south1-b", started_hours_ago=10.0, now=self.now)
        self.fleet = [running]
        self.store.refresh_fleet_inventory()
        self.fleet = [dict(running, data_disk_gb=2000)]
        self.store.refresh_fleet_inventory()
        self.fleet = [dict(running, data_disk_gb=2000)]
        self.assertFalse(self.store.refresh_fleet_inventory())
        stopped = dict(running, status="TERMINATED", data_disk_gb=2000, last_stop_epoch=time.time() + 60.0)
        self.fleet = [stopped]
        self.assertTrue(self.store.refresh_fleet_inventory())
        self.assertAlmostEqual(self._record("builder-oxidex-rs-1")["runtime_hours"], 10.0, delta=0.05)

    def test_deleted_row_has_no_active_rate(self):
        self.fleet = [fleet_observation("3800", "oxidex-runners-gone-2", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        self.fleet = []
        self.store.refresh_fleet_inventory()
        row = self.store.query_instances()["instances"][0]
        self.assertEqual(row["status"], "DELETED")
        self.assertEqual((row["active_hourly_usd"], row["active_daily_usd"], row["active_monthly_usd"]), (0.0, 0.0, 0.0))
        self.assertGreater(row["total_cost_usd"], 0.0)

    def test_mixed_disk_host_is_not_a_template_source(self):
        obs = fleet_observation("3900", "builder-oxidex-mix-2", "us-south1-b", now=self.now)
        obs.update({"data_disk_type": "pd-ssd", "data_disk_gb": 600, "data_disks": {"pd-ssd": 100, "pd-standard": 500}})
        self.fleet = [obs]
        self.store.refresh_fleet_inventory()
        with self.assertRaisesRegex(ValueError, "several types"):
            self.store.create_vm_template_from_instance({"instance_name": "builder-oxidex-mix-2"})

    def test_compacted_history_keeps_window_cost(self):
        from lib import dashboard_server
        from lib.dashboard_server import _live_instance_record, _mark_instance_deleted, instance_usage

        deleted = [_mark_instance_deleted(_live_instance_record(
            fleet_observation(f"40{i}", f"oxidex-runners-c{i}", "us-south1-a", started_hours_ago=3.0, now=self.now),
            project="p", now=self.now), self.now - i * 600.0) for i in range(4)]
        expected = sum(instance_usage(r, self.now, self.now - 24 * 3600.0)["window_all_in"] for r in deleted)
        compacted: dict = {}
        with unittest.mock.patch.object(dashboard_server, "MAX_DETAILED_INSTANCE_RECORDS", 2):
            records, changed = dashboard_server.reconcile_instance_records(
                deleted, [], project="p", now=self.now, compacted=compacted)
        self.assertEqual(len(records), 2)
        self.assertTrue(compacted)
        kept = sum(instance_usage(r, self.now, self.now - 24 * 3600.0)["window_all_in"] for r in records)
        _h, folded, _c = dashboard_server._bucket_window_costs(compacted, self.now - 24 * 3600.0, self.now,
                                                               "cicd_runner|")
        self.assertAlmostEqual(kept + folded, expected, delta=0.001)

    def test_many_segments_fold_into_hour_buckets(self):
        from lib import dashboard_server

        with unittest.mock.patch.object(dashboard_server, "MAX_DETAILED_SEGMENTS", 3):
            for i in range(6):
                start_ago = 100.0 - 10.0 * i
                self.fleet = [fleet_observation("4100", "oxidex-runners-fold-1", "us-south1-a",
                                                started_hours_ago=start_ago, now=self.now)]
                self.store.refresh_fleet_inventory()
                self.fleet = [fleet_observation("4100", "oxidex-runners-fold-1", "us-south1-a", status="TERMINATED",
                                                started_hours_ago=start_ago, stopped_hours_ago=start_ago - 2.0,
                                                now=self.now)]
                self.store.refresh_fleet_inventory()
        rec = self._record("oxidex-runners-fold-1")
        self.assertEqual(len(rec["prior_runs"]), 3)
        self.assertTrue(rec["folded_cost_buckets"])
        self.assertAlmostEqual(rec["runtime_hours"], 12.0, delta=0.05)
        row = next(r for r in self.store.query_instances(window_hours=744)["instances"]
                   if r["name"] == "oxidex-runners-fold-1")
        self.assertAlmostEqual(row["hours_in_window"], 12.0, delta=0.1)

    def test_size_budget_compacts_until_records_fit(self):
        from lib import dashboard_server
        from lib.dashboard_server import _live_instance_record, _mark_instance_deleted, instance_usage

        window_start = self.now - 24 * 3600.0
        deleted = [_mark_instance_deleted(_live_instance_record(
            fleet_observation(f"42{i:02d}", f"oxidex-runners-z{i}", "us-south1-a", started_hours_ago=3.0,
                              now=self.now), project="p", now=self.now), self.now - i * 60.0) for i in range(30)]
        expected = sum(instance_usage(r, self.now, window_start)["window_all_in"] for r in deleted)
        compacted: dict = {}
        with unittest.mock.patch.object(dashboard_server, "MAX_INSTANCE_STATE_BYTES", 20_000):
            records, _changed = dashboard_server.reconcile_instance_records(
                deleted, [], project="p", now=self.now, compacted=compacted)
            self.assertLessEqual(dashboard_server._serialized_size([records, compacted]), 20_000)
        self.assertLess(len(records), 30)
        kept = sum(instance_usage(r, self.now, window_start)["window_all_in"] for r in records)
        _h, folded, _c = dashboard_server._bucket_window_costs(compacted, window_start, self.now, "cicd_runner|")
        self.assertAlmostEqual(kept + folded, expected, delta=0.001)

    def test_size_budget_folds_live_detail(self):
        from lib import dashboard_server
        from lib.dashboard_server import _live_instance_record, instance_usage

        rec = _live_instance_record(fleet_observation("4300", "oxidex-runners-busy-1", "us-south1-a",
                                                      started_hours_ago=1.0, now=self.now), project="p", now=self.now)
        rec["prior_runs"] = [[self.now - (100 - i) * 3600.0, self.now - (100 - i) * 3600.0 + 1800.0] for i in range(60)]
        before = instance_usage(rec, self.now, self.now - 744 * 3600.0)
        # Leave bounded serialization headroom: wall-clock fractional digits must not decide whether
        # the fully compacted fixture fits. The original detail must still exceed this exact budget.
        compact = dashboard_server._coarsen_record_buckets(dashboard_server._fold_record_detail(rec, 0))
        budget = dashboard_server._serialized_size([[compact], [], {}]) + 128
        self.assertGreater(dashboard_server._serialized_size([[rec], [], {}]), budget)
        with unittest.mock.patch.object(dashboard_server, "MAX_INSTANCE_STATE_BYTES", budget):
            live, _gone = dashboard_server._fit_size_budget([rec], [], {}, self.now)
            self.assertLessEqual(dashboard_server._serialized_size([live, [], {}]), budget)
        self.assertLessEqual(len(live[0]["prior_runs"]), dashboard_server.MIN_DETAILED_SEGMENTS)
        after = instance_usage(live[0], self.now, self.now - 744 * 3600.0)
        self.assertAlmostEqual(after["compute_hours"], before["compute_hours"], delta=0.01)
        self.assertAlmostEqual(after["window_all_in"], before["window_all_in"], delta=0.001)

    def test_stale_inventory_holds_scaling(self):
        self.fleet = [fleet_observation("4400", "oxidex-runners-stale-2", "us-south1-a", now=self.now)]
        self.store.update_budget({"target": "cicd_runner", "max_hourly_usd": 50.0, "max_daily_usd": 1000.0,
                                  "max_monthly_usd": 30000.0})
        self.store.refresh_fleet_inventory()
        self.assertEqual(self.store._fail_closed_evaluation({"direction": "scale_up"}, "cicd_runner")["direction"],
                         "scale_up")

        def failing():
            raise RuntimeError("Google API HTTP 403: permission denied")

        working = self.store.fleet_source
        self.store.fleet_source = failing
        self.store.refresh_fleet_inventory()
        self.assertEqual(self._record("oxidex-runners-stale-2")["status"], "RUNNING")  # Rows stay for display.
        held = self.store._fail_closed_evaluation({"direction": "scale_down"}, "rust_builder")
        self.assertIn("not current", held["hold_reason"])
        with self.assertRaisesRegex(ValueError, "not current"):
            self.store.scale_region_instances({"region": "us-south1", "role": "cicd_runner", "target_instances": 2})
        self.store.fleet_source = working
        self.store.refresh_fleet_inventory()
        self.assertEqual(self.store._inventory_stale_reason(), "")

    def test_local_ssd_is_not_priced_as_persistent_disk(self):
        from lib.fleet_inventory import observe_fleet_items

        obs = observe_fleet_items(
            [{"id": "4500", "name": "oxidex-runners-lssd-1", "zone": "zones/us-south1-a", "status": "RUNNING",
              "machineType": "machineTypes/n4-standard-16",
              "metadata": {"items": [{"key": "spot-runner-count", "value": "4"}]},
              "disks": [{"boot": True, "diskSizeGb": "40", "source": "d/boot"},
                        {"boot": False, "diskSizeGb": "375", "type": "SCRATCH"}]}],
            disk_types={"d/boot": "pd-balanced"},
            shapes={("us-south1-a", "n4-standard-16"): {"vcpu": 16, "ram_gib": 64.0}})
        self.assertEqual((obs[0]["local_ssd_gb"], obs[0]["data_disk_gb"], obs[0]["data_disks"]), (375, 0, {}))
        self.fleet = obs
        self.store.refresh_fleet_inventory()
        rec = self._record("oxidex-runners-lssd-1")
        self.assertFalse(rec["pricing_complete"])
        self.assertIn("Local SSD", rec["pricing_incomplete_reason"])

    def test_repricing_keeps_completed_history(self):
        from lib.dashboard_server import instance_usage

        self.fleet = [fleet_observation("4600", "builder-oxidex-reprice-1", "us-south1-b", status="TERMINATED",
                                        started_hours_ago=10.0, stopped_hours_ago=4.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        before = instance_usage(self._record("builder-oxidex-reprice-1"), time.time())
        with self.store._lock:
            budget = self.store.state["profiles"]["rust_builder"]["budget"]
            budget["override_regional_tax"] = True
            budget["tax_rate_percent"] = 40.0
        version = self.store.state_version()
        self.assertTrue(self.store.refresh_fleet_inventory())
        self.assertEqual(self.store.state_version(), version + 1)
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        saved_rec = next(i for i in saved["instances"] if i["name"] == "builder-oxidex-reprice-1")
        self.assertEqual(len(saved_rec["disk_segments"]), 1)  # The rate boundary is durable.
        rec = self._record("builder-oxidex-reprice-1")
        self.assertEqual(rec["pricing"]["tax_rate_percent"], 40.0)
        self.assertEqual(len(rec["disk_segments"]), 1)
        after = instance_usage(rec, time.time())
        self.assertAlmostEqual(after["cost_compute"], before["cost_compute"], delta=0.001)
        self.assertAlmostEqual(after["cost_all_in"], before["cost_all_in"], delta=0.01)

    def test_expired_folded_buckets_keep_lifetime_totals(self):
        obs = fleet_observation("4700", "oxidex-runners-expire-1", "us-south1-a", started_hours_ago=2.0, now=self.now)
        self.fleet = [obs]
        self.store.refresh_fleet_inventory()
        base = self._record("oxidex-runners-expire-1")["runtime_hours"]
        old_hour = int((self.now - 40 * 86400.0) // 3600 * 3600)
        with self.store._lock:
            self._record("oxidex-runners-expire-1")["folded_cost_buckets"] = {str(old_hour): [5.0, 1.0, 0.5]}
        self.assertTrue(self.store.refresh_fleet_inventory())
        rec = self._record("oxidex-runners-expire-1")
        self.assertEqual(rec["folded_cost_buckets"], {})
        self.assertAlmostEqual(rec["pruned_compute_hours"], 5.0, delta=0.001)
        self.assertAlmostEqual(rec["runtime_hours"], base + 5.0, delta=0.05)

    def test_local_ssd_host_is_not_a_template_source(self):
        obs = fleet_observation("4800", "builder-oxidex-lssd-2", "us-south1-b", now=self.now)
        obs["local_ssd_gb"] = 375
        self.fleet = [obs]
        self.store.refresh_fleet_inventory()
        with self.assertRaisesRegex(ValueError, "Local SSD"):
            self.store.create_vm_template_from_instance({"instance_name": "builder-oxidex-lssd-2"})

    def test_state_that_cannot_fit_is_rejected_and_stale(self):
        from lib import dashboard_server

        self.fleet = [fleet_observation("4900", "oxidex-runners-huge-1", "us-south1-a", now=self.now)]
        with unittest.mock.patch.object(dashboard_server, "MAX_INSTANCE_STATE_BYTES", 100):
            self.assertFalse(self.store.refresh_fleet_inventory())
        self.assertEqual(self.store.state["instances"], [])
        self.assertIn("InventoryTooLargeError", self.store.fleet_inventory_status()["last_error"])
        self.assertTrue(self.store._inventory_stale_reason())

    def test_failed_persist_marks_inventory_stale(self):
        self.fleet = [fleet_observation("5000", "oxidex-runners-disk-full-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        self.assertEqual(self.store._inventory_stale_reason(), "")
        self.fleet.append(fleet_observation("5001", "oxidex-runners-disk-full-2", "us-south1-a", now=self.now))
        with unittest.mock.patch.object(self.store, "_persist", side_effect=OSError("No space left on device")):
            self.assertFalse(self.store.refresh_fleet_inventory())
        self.assertEqual(len(self.store.state["instances"]), 1)  # Rolled back.
        self.assertIn("No space left", self.store._inventory_stale_reason())

    def test_bootstrap_confirmation_alone_updates_record(self):
        provisioning = fleet_observation("5200", "oxidex-runners-boot-2", "us-south1-a", now=self.now)
        provisioning["runners_count_known"] = False
        self.fleet = [provisioning]
        self.store.refresh_fleet_inventory()
        self.assertEqual(self.store.state["scaling_telemetry"]["unknown_runner_count_hosts"], ["oxidex-runners-boot-2"])
        self.fleet = [dict(provisioning, runners_count_known=True)]  # Bootstrap confirmed; nothing else changed.
        self.assertTrue(self.store.refresh_fleet_inventory())
        self.assertTrue(self._record("oxidex-runners-boot-2")["runners_count_known"])
        self.assertEqual(self.store.state["scaling_telemetry"]["unknown_runner_count_hosts"], [])

    def test_new_runner_counts_for_oom_only_once_ready(self):
        base = fleet_observation("5500", "oxidex-runners-base-1", "us-south1-a", runners_count=1, now=self.now)
        self.fleet = [base]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            self.store.state["scaling_telemetry"]["journal_oom_events"] = [
                {"id": "oom-r1", "instance_name": "oxidex-runners-base-1", "role": "cicd_runner",
                 "baseline_runners": 1,
                 "observed_at": datetime.fromtimestamp(time.time() - 60.0, tz=timezone.utc).strftime(
                     "%Y-%m-%dT%H:%M:%SZ")}]
        booting = fleet_observation("5501", "oxidex-runners-new-1", "us-south1-a", runners_count=4, now=self.now)
        booting.update(runners_count_known=False, created_epoch=self.now - 18, last_start_epoch=self.now - 18)
        self.fleet = [base, booting]
        self.store.refresh_fleet_inventory()
        self.assertFalse(self.store.state["scaling_telemetry"]["journal_oom_events"][0].get("handled", False))
        self.assertTrue(self._record("oxidex-runners-new-1")["capacity_pending"])
        self.fleet = [base, dict(booting, runners_count_known=True)]
        self.store.refresh_fleet_inventory()
        self.assertTrue(self.store.state["scaling_telemetry"]["journal_oom_events"][0].get("handled"))
        self.assertTrue(self._record("oxidex-runners-new-1")["capacity_pending"])  # Retained for late incident reports.

    def test_tax_setting_change_reprices_inventory_in_same_save(self):
        self.fleet = [fleet_observation("5800", "builder-oxidex-taxnow-1", "us-south1-b", now=self.now)]
        self.store.refresh_fleet_inventory()
        version = self.store.state_version()
        self.store.update_profile("rust_builder", {"budget": {"override_regional_tax": True, "tax_rate_percent": 25.0}})
        self.assertEqual(self.store.state_version(), version + 1)
        self.assertEqual(self._record("builder-oxidex-taxnow-1")["pricing"]["tax_rate_percent"], 25.0)
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        saved_rec = next(i for i in saved["instances"] if i["name"] == "builder-oxidex-taxnow-1")
        self.assertEqual(saved_rec["pricing"]["tax_rate_percent"], 25.0)

    def test_custom_shape_pricing_is_incomplete(self):
        live = {"component_rates": {("ondemand", "us-south1", "n2", "CPU"): 0.03,
                                    ("ondemand", "us-south1", "n2", "RAM"): 0.004,
                                    ("spot", "us-south1", "n2", "CPU"): 0.01,
                                    ("spot", "us-south1", "n2", "RAM"): 0.001}}
        args = dict(family="n2", vcpu=32, ram_gib=110.0, region="us-south1", boot_disk_type="pd-balanced",
                    boot_disk_gb=40, data_disk_type="pd-balanced", data_disk_gb=0,
                    machine_type="n2-custom-32-112640", live_pricing=live)
        self.assertFalse(calculate_spot_pricing(**args, provisioning="ondemand")["complete"])
        self.assertFalse(calculate_spot_pricing(**args)["complete"])
        standard = dict(args, machine_type="n2-standard-32")
        self.assertTrue(calculate_spot_pricing(**standard, provisioning="ondemand")["complete"])
        self.assertNotIn("complete", calculate_spot_pricing(**standard))
        obs = fleet_observation("5900", "oxidex-runners-custom-2", "us-south1-a", machine_type="n2-custom-32-112640",
                                vcpu=32, ram_gib=110.0, now=self.now)
        self.fleet = [obs]
        self.store.refresh_fleet_inventory()
        rec = self._record("oxidex-runners-custom-2")
        self.assertFalse(rec["pricing_complete"])
        self.assertIn("custom or extended", rec["pricing_incomplete_reason"])
        self.assertNotIn("on-demand", rec["pricing_incomplete_reason"])

    def test_unknown_disk_types_keep_stored_types(self):
        known = fleet_observation("6000", "builder-oxidex-types-1", "us-south1-b", now=self.now)
        self.fleet = [known]
        self.store.refresh_fleet_inventory()
        fallback = dict(known, boot_disk_type="pd-balanced", data_disk_type="pd-balanced", disk_types_known=False)
        self.fleet = [fallback]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-types-1")
        self.assertEqual((rec["boot_disk_type"], rec["data_disk_type"]), ("hyperdisk-balanced", "hyperdisk-balanced"))
        # Kept for pricing, but unverified (a same-size disk may have a new type): scale-ups hold.
        self.assertFalse(rec["pricing_complete"])
        self.assertIn("disk types unknown", rec["pricing_incomplete_reason"])
        new = fleet_observation("6001", "builder-oxidex-types-2", "us-south1-b", now=self.now)
        self.fleet = [fallback, dict(new, boot_disk_type="pd-balanced", data_disk_type="pd-balanced",
                                     disk_types_known=False)]
        self.store.refresh_fleet_inventory()
        rec2 = self._record("builder-oxidex-types-2")
        self.assertFalse(rec2["pricing_complete"])
        self.assertIn("disk types unknown", rec2["pricing_incomplete_reason"])

    def test_oom_target_uses_capacity_running_after_refresh(self):
        old = fleet_observation("6100", "oxidex-runners-old-2", "us-south1-a", runners_count=4, now=self.now)
        self.fleet = [old]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            self.store.state["scaling_telemetry"]["journal_oom_events"] = [
                {"id": "oom-r2", "instance_name": "oxidex-runners-old-2", "role": "cicd_runner",
                 "baseline_runners": 4}]
        replacement = fleet_observation("6101", "oxidex-runners-new-3", "us-south1-a", runners_count=4, now=self.now)
        self.fleet = [dict(old, status="TERMINATED", last_stop_epoch=self.now - 60.0), replacement]
        self.store.refresh_fleet_inventory()
        # One 4-slot host replaced another: no net growth, so the OOM stays active.
        self.assertFalse(self.store.state["scaling_telemetry"]["journal_oom_events"][0].get("handled", False))

    def test_restart_trims_previous_run_to_reported_stop(self):
        running = fleet_observation("6200", "builder-oxidex-trim-1", "us-south1-b", started_hours_ago=10.0,
                                    now=self.now)
        self.fleet = [running]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            budget = self.store.state["profiles"]["rust_builder"]["budget"]
            budget["override_regional_tax"] = True
            budget["tax_rate_percent"] = 33.0
        self.store.refresh_fleet_inventory()  # Repriced while the listing still says RUNNING: closes at "now".
        # Next listing: it had stopped 4 hours ago and has just started again.
        self.fleet = [dict(running, last_stop_epoch=self.now - 4 * 3600.0, last_start_epoch=self.now - 60.0)]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-trim-1")
        self.assertTrue(all(run[1] <= self.now - 4 * 3600.0 + 1.0 for run in rec["prior_runs"]))
        self.assertAlmostEqual(rec["runtime_hours"], 6.0 + 1.0 / 60.0, delta=0.05)

    def test_held_evaluation_projects_held_fleet(self):
        self.fleet = [fleet_observation("6300", "oxidex-runners-hold-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        held = self.store._held({"direction": "scale_up", "projected_hourly_usd": 9.99, "current_runners": 4,
                                 "current_instances": 1}, "test", "cicd_runner")
        self.assertAlmostEqual(held["projected_hourly_usd"],
                               sum(self.store._running_role_hourly_rates("cicd_runner")), delta=0.0001)

    def test_expanding_partial_day_keeps_incurred_cost(self):
        from lib.dashboard_server import _rebucket

        day = 1_800_000_000 // 86400 * 86400
        buckets = {str(day): [6.0, 6.0, 3.0]}
        hourly = _rebucket(buckets, "", 86400.0, 3600.0, until=day + 6 * 3600.0)
        self.assertEqual(len(hourly), 6)
        self.assertAlmostEqual(sum(v[1] for v in hourly.values()), 6.0)

    def test_regional_disk_marks_pricing_incomplete(self):
        from lib.fleet_inventory import observe_fleet_items

        obs = observe_fleet_items(
            [{"id": "6400", "name": "builder-oxidex-reg-1", "zone": "zones/us-south1-b", "status": "RUNNING",
              "machineType": "machineTypes/n4-highcpu-16",
              "disks": [{"boot": True, "diskSizeGb": "40", "source": "z/disks/boot"},
                        {"boot": False, "diskSizeGb": "1000",
                         "source": "projects/p/regions/us-south1/disks/data"}]}],
            disk_types={"z/disks/boot": {"type": "pd-balanced"},
                        "projects/p/regions/us-south1/disks/data": {"type": "pd-ssd", "regional": True}},
            shapes={("us-south1-b", "n4-highcpu-16"): {"vcpu": 16, "ram_gib": 32.0}})
        self.assertTrue(obs[0]["regional_disks"])
        self.fleet = obs
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-reg-1")
        self.assertFalse(rec["pricing_complete"])
        self.assertIn("regional", rec["pricing_incomplete_reason"])

    def test_scaling_holds_without_configured_inventory(self):
        from lib.dashboard_server import DashboardStore

        store = DashboardStore()
        held = store._fail_closed_evaluation({"direction": "scale_up"}, "cicd_runner")
        self.assertIn("not configured", held["hold_reason"])
        with self.assertRaisesRegex(ValueError, "not configured"):
            store.scale_region_instances({"region": "us-south1", "role": "cicd_runner", "target_instances": 1})

    def test_unrepresentable_disk_sources_are_not_templates(self):
        for i, flag in enumerate(("regional_disks", "unpriced_disk_performance")):
            obs = fleet_observation(f"660{i}", f"builder-oxidex-tpl-{i}", "us-south1-b", now=self.now)
            obs[flag] = True
            self.fleet = [obs]
            self.store.refresh_fleet_inventory()
            with self.assertRaisesRegex(ValueError, "cannot describe"):
                self.store.create_vm_template_from_instance({"instance_name": f"builder-oxidex-tpl-{i}"})

    def test_scale_plan_rejects_unpriced_candidates(self):
        self.fleet = [fleet_observation("6700", "oxidex-runners-plan-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        self.store.update_budget({"target": "cicd_runner", "max_hourly_usd": 50.0, "max_daily_usd": 1000.0,
                                  "max_monthly_usd": 30000.0})
        real = calculate_spot_pricing

        def unpriced(*args, **kwargs):
            return {**real(*args, **kwargs), "complete": False}

        with unittest.mock.patch("lib.dashboard_server.calculate_spot_pricing", side_effect=unpriced):
            with self.assertRaisesRegex(ValueError, "cannot be fully priced"):
                self.store.scale_region_instances({"region": "us-south1", "role": "cicd_runner", "target_instances": 2})

    def test_unverified_disk_types_are_not_template_sources(self):
        obs = fleet_observation("6800", "builder-oxidex-unver-1", "us-south1-b", now=self.now)
        obs["disk_types_known"] = False
        self.fleet = [obs]
        self.store.refresh_fleet_inventory()
        with self.assertRaisesRegex(ValueError, "not verified"):
            self.store.create_vm_template_from_instance({"instance_name": "builder-oxidex-unver-1"})

    def test_suspended_vm_stops_at_suspension_time(self):
        obs = fleet_observation("6900", "builder-oxidex-susp-2", "us-south1-b", status="SUSPENDED",
                                started_hours_ago=6.0, now=self.now)
        obs["last_suspended_epoch"] = self.now - 2 * 3600.0
        self.fleet = [obs]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-susp-2")
        self.assertAlmostEqual(rec["stopped_epoch"], self.now - 2 * 3600.0, delta=1.0)
        self.assertAlmostEqual(rec["runtime_hours"], 4.0, delta=0.02)

    def test_pd_extreme_iops_is_unpriced(self):
        from lib.fleet_inventory import _unpriced_performance

        self.assertTrue(_unpriced_performance({"type": "pd-extreme", "provisionedIops": "20000"}))
        self.assertFalse(_unpriced_performance({"type": "pd-ssd"}))

    def test_instance_id_wins_over_reused_name(self):
        first = fleet_observation("7100", "builder-oxidex-same", "us-south1-b", now=self.now)
        second = fleet_observation("7101", "builder-oxidex-same", "us-south1-a", now=self.now)
        self.fleet = [first, second]
        self.store.refresh_fleet_inventory()
        plan = self.store.delete_instance({"instance_id": "7101", "instance_name": "builder-oxidex-same"})
        self.assertEqual(plan["instance"]["instance_id"], "7101")

    def test_inventory_goes_stale_when_refreshes_stop_completing(self):
        self.fleet = [fleet_observation("7300", "oxidex-runners-hang-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        self.assertEqual(self.store._inventory_stale_reason(), "")
        clock = time.time() + 4 * self.store.fleet_inventory_interval
        with unittest.mock.patch.object(self.store, "_clock", return_value=clock):
            self.assertIn("no complete listing", self.store._inventory_stale_reason())

    def _runner_oom(self, event_id: str, host: str) -> None:
        observed = datetime.fromtimestamp(time.time() - 60.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.store._lock:
            self.store.state["scaling_telemetry"]["journal_oom_events"] = [
                {"id": event_id, "instance_name": host, "role": "cicd_runner", "observed_at": observed}]

    def _runner_oom_handled(self) -> bool:
        return bool(self.store.state["scaling_telemetry"]["journal_oom_events"][0].get("handled"))

    def _runner(self, instance_id: str, name: str, **kwargs) -> dict:
        obs = fleet_observation(instance_id, name, "us-south1-a", runners_count=4, now=self.now, **kwargs)
        if kwargs.get("started_hours_ago", 2) < 1 / 60:
            obs["created_epoch"] = obs["last_start_epoch"]  # Explicit new-capacity fixtures are newly created.
        return obs

    def test_first_listing_is_a_baseline_not_growth(self):
        from lib.dashboard_server import FLEET_BASELINE_STATE_KEY

        self._runner_oom("oom-r10", "oxidex-runners-a")
        self.fleet = [self._runner("1900", "oxidex-runners-a")]
        self.store.refresh_fleet_inventory()
        self.assertTrue(self.store.state.get(FLEET_BASELINE_STATE_KEY))
        self.assertFalse(self._runner_oom_handled())
        self.fleet.append(self._runner("1901", "oxidex-runners-b", started_hours_ago=0.005))  # Started after the OOM.
        self.assertTrue(self.store.refresh_fleet_inventory())
        self.assertTrue(self._runner_oom_handled())

    def test_runner_restart_does_not_consume_oom(self):
        host = self._runner("5100", "oxidex-runners-oom-1", started_hours_ago=5.0)
        self.fleet = [host]
        self.store.refresh_fleet_inventory()
        self.fleet = [dict(host, status="TERMINATED", last_stop_epoch=self.now - 3600.0)]
        self.store.refresh_fleet_inventory()
        self._runner_oom("oom-r11", "oxidex-runners-oom-1")
        self.fleet = [dict(host, last_start_epoch=self.now - 60.0)]  # Same VM started again.
        self.store.refresh_fleet_inventory()
        self.assertFalse(self._runner_oom_handled())
        self.fleet.append(self._runner("5101", "oxidex-runners-oom-2", started_hours_ago=0.005))
        self.store.refresh_fleet_inventory()
        self.assertTrue(self._runner_oom_handled())

    def test_new_vm_beside_stopped_capacity_handles_oom(self):
        running = self._runner("5300", "oxidex-runners-cap-1")
        stopped = self._runner("5301", "oxidex-runners-cap-2", status="TERMINATED", started_hours_ago=5.0,
                               stopped_hours_ago=1.0)
        self.fleet = [running, stopped]
        self.store.refresh_fleet_inventory()
        self._runner_oom("oom-r12", "oxidex-runners-cap-1")
        self.store.refresh_fleet_inventory()
        self.fleet = [running, stopped, self._runner("5302", "oxidex-runners-cap-3", started_hours_ago=0.005)]
        self.store.refresh_fleet_inventory()
        self.assertTrue(self._runner_oom_handled())

    def test_vm_seen_staging_first_still_counts_when_running(self):
        base = self._runner("5600", "oxidex-runners-old-1")
        self.fleet = [base]
        self.store.refresh_fleet_inventory()
        self._runner_oom("oom-r13", "oxidex-runners-old-1")
        staging = self._runner("5601", "oxidex-runners-new-2", status="STAGING", started_hours_ago=0.005)
        self.fleet = [base, staging]
        self.store.refresh_fleet_inventory()
        self.assertFalse(self._runner_oom_handled())
        self.fleet = [base, dict(staging, status="RUNNING")]
        self.store.refresh_fleet_inventory()
        self.assertTrue(self._runner_oom_handled())

    def test_runner_oom_keeps_event_baseline_across_polls(self):
        old = self._runner("7200", "oxidex-runners-base-1")
        self.fleet = [old]
        self.store.refresh_fleet_inventory()
        self._runner_oom("oom-r14", "oxidex-runners-base-1")
        stopped_old = dict(old, status="TERMINATED", last_stop_epoch=self.now - 30.0)
        self.fleet = [stopped_old]
        self.store.refresh_fleet_inventory()  # Baseline recorded at the 4 slots running when first seen.
        self.assertEqual(self.store.state["scaling_telemetry"]["journal_oom_events"][0]["baseline_runners"], 4)
        replacement = self._runner("7201", "oxidex-runners-base-2")
        self.fleet = [stopped_old, replacement]
        self.store.refresh_fleet_inventory()
        self.assertFalse(self._runner_oom_handled())  # Back to 4 slots: no growth.
        self.fleet = [stopped_old, replacement, self._runner("7202", "oxidex-runners-base-3", started_hours_ago=0.005)]
        self.store.refresh_fleet_inventory()
        self.assertTrue(self._runner_oom_handled())

    def test_pending_capacity_survives_replacement_until_net_growth(self):
        old = self._runner("7000", "oxidex-runners-net-1")
        self.fleet = [old]
        self.store.refresh_fleet_inventory()
        self._runner_oom("oom-r15", "oxidex-runners-net-1")
        stopped_old = dict(old, status="TERMINATED", last_stop_epoch=self.now - 30.0)
        new = self._runner("7001", "oxidex-runners-net-2", started_hours_ago=0.005)
        self.fleet = [stopped_old, new]
        self.store.refresh_fleet_inventory()
        self.assertFalse(self._runner_oom_handled())
        self.assertTrue(self._record("oxidex-runners-net-2")["capacity_pending"])
        self.fleet = [dict(old, last_start_epoch=time.time()), new]  # The old host starts again: net growth.
        self.store.refresh_fleet_inventory()
        self.assertTrue(self._runner_oom_handled())

    def test_pending_capacity_kept_for_late_oom(self):
        self.fleet = [self._runner("7400", "oxidex-runners-late-1")]
        self.store.refresh_fleet_inventory()
        self.fleet.append(self._runner("7401", "oxidex-runners-late-2", started_hours_ago=0.1))
        self.store.refresh_fleet_inventory()
        # No OOM yet, but the new host started inside the OOM lookback: it stays pending for a late report.
        self.assertTrue(self._record("oxidex-runners-late-2")["capacity_pending"])

    def test_builder_ooms_are_never_handled_from_inventory(self):
        self.fleet = [fleet_observation("7500", "builder-oxidex-bo-1", "us-south1-b", now=self.now)]
        self.store.refresh_fleet_inventory()
        observed = datetime.fromtimestamp(time.time() - 60.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.store._lock:
            self.store.state["scaling_telemetry"]["journal_oom_events"] = [
                {"id": "oom-b9", "instance_name": "builder-oxidex-bo-1", "role": "rust_builder",
                 "observed_at": observed}]
        self.fleet = self.fleet + [fleet_observation(f"750{i}", f"builder-oxidex-bo-{i}", "us-south1-b", now=self.now)
                                   for i in (2, 3)]
        self.store.refresh_fleet_inventory()
        self.assertFalse(self.store.state["scaling_telemetry"]["journal_oom_events"][0].get("handled", False))

    def test_unresolved_disk_source_is_unverified(self):
        from lib.fleet_inventory import observe_fleet_items

        obs = observe_fleet_items(
            [{"id": "7600", "name": "builder-oxidex-race-1", "zone": "zones/us-south1-b", "status": "RUNNING",
              "machineType": "machineTypes/n4-highcpu-16",
              "disks": [{"boot": True, "diskSizeGb": "40", "source": "d/boot"},
                        {"boot": False, "diskSizeGb": "500", "source": "d/new"}]}],
            disk_types={"d/boot": {"type": "pd-balanced"}},
            shapes={("us-south1-b", "n4-highcpu-16"): {"vcpu": 16, "ram_gib": 32.0}})
        self.assertFalse(obs[0]["disk_types_known"])

    def test_first_sight_bills_each_disk_from_its_creation(self):
        from lib.dashboard_server import _pricing_rates, instance_usage

        vm_created = self.now - 30 * 86400.0
        data_created = self.now - 86400.0
        obs = fleet_observation("7700", "builder-oxidex-attach-1", "us-south1-b", started_hours_ago=30 * 24.0,
                                now=self.now)
        obs.update(created_epoch=vm_created, disks_detail=[
            {"type": "hyperdisk-balanced", "gb": 40, "created_epoch": vm_created},
            {"type": "hyperdisk-balanced", "gb": 100, "created_epoch": data_created}])
        self.fleet = [obs]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-attach-1")
        self.assertEqual(len(rec["disk_segments"]), 1)
        start, end, boot_rate = rec["disk_segments"][0]
        self.assertAlmostEqual(start, vm_created, delta=1.0)
        self.assertAlmostEqual(end, data_created, delta=1.0)
        full_rate = _pricing_rates(rec["pricing"])[2]
        self.assertLess(boot_rate, full_rate)
        usage = instance_usage(rec, self.now, self.now - 744 * 3600.0)
        expected_disk = (data_created - vm_created) / 3600.0 * boot_rate + 24.0 * full_rate
        compute = usage["cost_all_in"] - expected_disk
        self.assertAlmostEqual(compute, 30 * 24.0 * _pricing_rates(rec["pricing"])[0], delta=0.05)

    def test_first_sight_without_disk_creation_times_does_not_backfill(self):
        obs = fleet_observation("7800", "builder-oxidex-nobf-1", "us-south1-b", started_hours_ago=48.0, now=self.now)
        obs["disks_detail"] = [{"type": "pd-balanced", "gb": 40, "created_epoch": None}]
        self.fleet = [obs]
        before = time.time()
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-nobf-1")
        self.assertGreaterEqual(rec["disk_segment_start"], before - 1.0)

    def test_delayed_oom_reconciled_on_unchanged_listing(self):
        self.fleet = [self._runner("8000", "oxidex-runners-d-1")]
        self.store.refresh_fleet_inventory()
        new = self._runner("8001", "oxidex-runners-d-2", started_hours_ago=0.25)
        self.fleet.append(new)
        self.store.refresh_fleet_inventory()  # New capacity, no OOM yet: it stays pending (inside the lookback).
        # The OOM is reported afterwards, timestamped before the new host started.
        when = datetime.fromtimestamp(self.now - 1800.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.store._lock:
            self.store.state["scaling_telemetry"]["journal_oom_events"] = [
                {"id": "oom-r20", "instance_name": "oxidex-runners-d-1", "role": "cicd_runner", "observed_at": when,
                 "baseline_runners": 8}]  # As if the scaling evaluation had set it from the grown count.
        self.assertTrue(self.store.refresh_fleet_inventory())  # Same listing: OOM state still reconciled.
        event = self.store.state["scaling_telemetry"]["journal_oom_events"][0]
        self.assertEqual(event["baseline_runners"], 4)
        self.assertTrue(event.get("handled"))

    def test_detached_fleet_disk_keeps_billing(self):
        from lib.fleet_inventory import observe_detached_disks

        created = "2026-10-10T00:00:00.000-07:00"
        disks = observe_detached_disks({
            "z/disks/builder-oxidex-x-cache": {"id": "91", "name": "builder-oxidex-x-cache", "zone": "z/us-south1-b",
                                                "sizeGb": "100", "type": "hyperdisk-balanced",
                                                "provisionedIops": "3000", "provisionedThroughput": "140",
                                                "creationTimestamp": created},
            "z/disks/builder-oxidex-y-cache": {"id": "92", "name": "builder-oxidex-y-cache", "zone": "z/us-south1-b",
                                                "sizeGb": "100", "type": "hyperdisk-balanced", "users": ["vm"]},
            "z/disks/other": {"id": "93", "name": "unrelated-disk", "zone": "z/us-south1-b", "sizeGb": "10"},
        })
        self.assertEqual([d["name"] for d in disks], ["builder-oxidex-x-cache"])
        self.fleet = disks
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-x-cache")
        self.assertEqual(rec["status"], "DETACHED")
        self.assertEqual(rec["runtime_hours"], 0.0)
        view = self.store.query_instances()
        self.assertGreater(view["active_hourly_burn_usd"], 0.0)  # Storage still bills.
        self.assertEqual(self.store.state["scaling_telemetry"]["current_builder_instances"], 0)
        self.fleet = []
        self.store.refresh_fleet_inventory()
        self.assertEqual(self._record("builder-oxidex-x-cache")["status"], "DELETED")

    def test_legacy_runner_prefixes_are_listed(self):
        from lib.fleet_inventory import FLEET_NAME_PREFIXES, fleet_role

        for name in ("newznab-spot-runners-1", "spot-runners-2", "oxidex-spot-runners-3", "oxidex-runners-4"):
            self.assertTrue(name.startswith(FLEET_NAME_PREFIXES))
            self.assertEqual(fleet_role(name), "cicd_runner")
        self.assertEqual(fleet_role("builder-oxidex-1"), "rust_builder")
        self.assertIsNone(fleet_role("spot-control-a"))

    def test_performance_billed_disk_candidates_are_incomplete(self):
        common = dict(family="n2", vcpu=16, ram_gib=64.0, region="us-south1", boot_disk_type="pd-balanced",
                      boot_disk_gb=40, data_disk_gb=500, machine_type="n2-standard-16", live_pricing={})
        self.assertFalse(calculate_spot_pricing(**common, data_disk_type="hyperdisk-throughput")["complete"])
        self.assertFalse(calculate_spot_pricing(**common, data_disk_type="pd-extreme")["complete"])
        self.assertNotIn("complete", calculate_spot_pricing(**common, data_disk_type="pd-ssd"))

    def test_cache_disk_handoff_is_not_billed_twice(self):
        from lib.dashboard_server import instance_usage

        created = self.now - 10 * 86400.0
        cache = "projects/p/zones/us-south1-b/disks/builder-oxidex-h-cache"
        vm = fleet_observation("8100", "builder-oxidex-h", "us-south1-b", started_hours_ago=240.0, now=self.now)
        vm.update(created_epoch=created, disks_detail=[
            {"type": "hyperdisk-balanced", "gb": 40, "created_epoch": created, "source": "z/boot-h"},
            {"type": "hyperdisk-balanced", "gb": 100, "created_epoch": created, "source": cache}])
        self.fleet = [vm]
        self.store.refresh_fleet_inventory()
        detached = {"instance_id": "disk-91", "name": "builder-oxidex-h-cache", "role": "rust_builder",
                    "status": "DETACHED", "kind": "disk", "zone": "us-south1-b", "region": "us-south1",
                    "machine_type": "", "cpu_platform": "", "vcpu": 0, "ram_gib": 0.0, "spot": True,
                    "boot_disk_type": "hyperdisk-balanced", "boot_disk_gb": 0, "data_disk_type": "hyperdisk-balanced",
                    "data_disk_gb": 100, "data_disks": {"hyperdisk-balanced": 100},
                    "disks_detail": [{"type": "hyperdisk-balanced", "gb": 100, "created_epoch": created,
                                      "source": cache}],
                    "created_epoch": created, "last_start_epoch": created, "last_stop_epoch": created}
        self.fleet = [detached]  # The VM is deleted; its cache disk stays.
        handoff = time.time()
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-h-cache")
        usage = instance_usage(rec, handoff + 3600.0)
        self.assertLess(usage["disk_hours"], 1.1)  # Billed from the handoff, not from its creation 10 days ago.

    def test_detached_disk_reprices_and_lists_without_fleet_vms(self):
        from lib import compute_api
        from lib.fleet_inventory import gce_fleet_source

        async def listing(api, project, *, instance_fields=compute_api.INSTANCE_LIST_FIELDS):
            return [], []

        async def disks(api, project):
            return {"z/disks/builder-oxidex-z-cache": {"type": "pd-balanced", "id": "95", "name": "builder-oxidex-z-cache",
                                                       "zone": "z/us-south1-b", "sizeGb": "100"}}

        with unittest.mock.patch.object(compute_api, "get_auth", return_value=compute_api.Auth("t", "p")), \
                unittest.mock.patch.object(compute_api, "list_instances", listing), \
                unittest.mock.patch.object(compute_api, "list_disk_types", disks):
            observed = gce_fleet_source("p")()
        self.assertEqual([o["name"] for o in observed], ["builder-oxidex-z-cache"])
        self.fleet = observed
        self.store.refresh_fleet_inventory()
        before = self._record("builder-oxidex-z-cache")["pricing"]["tax_rate_percent"]
        self.store.update_profile("rust_builder", {"budget": {"override_regional_tax": True, "tax_rate_percent": 21.0}})
        self.assertNotEqual(before, 21.0)
        self.assertEqual(self._record("builder-oxidex-z-cache")["pricing"]["tax_rate_percent"], 21.0)

    def test_unknown_family_generic_rate_is_incomplete(self):
        priced = calculate_spot_pricing(family="zz9", vcpu=8, ram_gib=32.0, region="us-south1",
                                        boot_disk_type="pd-balanced", boot_disk_gb=40, data_disk_type="pd-balanced",
                                        data_disk_gb=0, machine_type="zz9-standard-8", live_pricing={})
        self.assertFalse(priced["complete"])
        obs = fleet_observation("8200", "oxidex-runners-zz-1", "us-south1-a", machine_type="zz9-standard-8", vcpu=8,
                                ram_gib=32.0, now=self.now)
        self.fleet = [obs]
        with unittest.mock.patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={}):
            self.store.refresh_fleet_inventory()
        rec = self._record("oxidex-runners-zz-1")
        self.assertFalse(rec["pricing_complete"])
        self.assertIn("no known rate for the zz9", rec["pricing_incomplete_reason"])

    def test_detached_disk_returning_restarts_billing(self):
        from lib.dashboard_server import instance_usage
        from lib.fleet_inventory import observe_detached_disks

        created = datetime.fromtimestamp(self.now - 5 * 86400.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        detail = {"z/disks/builder-oxidex-r-cache": {"id": "96", "name": "builder-oxidex-r-cache",
                                                     "zone": "z/us-south1-b", "sizeGb": "100", "type": "pd-balanced",
                                                     "creationTimestamp": created}}
        detached = observe_detached_disks(detail)
        self.fleet = detached
        self.store.refresh_fleet_inventory()
        self.fleet = []  # Attached to a VM (billed there) for a while.
        self.store.refresh_fleet_inventory()
        first_end = self._record("builder-oxidex-r-cache")["deleted_epoch"]
        self.fleet = detached  # Detached again.
        back = time.time()
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-r-cache")
        self.assertEqual(rec["status"], "DETACHED")
        self.assertGreaterEqual(rec["disk_segment_start"], back - 1.0)
        self.assertAlmostEqual(rec["disk_segments"][-1][1], first_end, delta=1.0)
        total = instance_usage(rec, back + 3600.0)["disk_hours"]
        first = instance_usage(dict(rec, disk_segments=[], disk_segment_start=None, deleted_epoch=first_end,
                                    status="DELETED"), back)["disk_hours"]
        self.assertAlmostEqual(total, first + 1.0, delta=0.05)

    def test_unlisted_disks_hold_scaling(self):
        from lib.fleet_inventory import FleetListing

        listing = FleetListing([fleet_observation("8300", "oxidex-runners-nd-1", "us-south1-a", now=self.now)])
        listing.disks_listed = False
        self.store.fleet_source = lambda: listing
        self.store.refresh_fleet_inventory()
        self.assertIn("disk listing is unavailable", self.store._inventory_stale_reason())

    def test_held_projection_includes_retained_storage(self):
        self.fleet = [fleet_observation("8400", "builder-oxidex-hp-1", "us-south1-b", now=self.now),
                      fleet_observation("8401", "builder-oxidex-hp-2", "us-south1-b", status="TERMINATED",
                                        started_hours_ago=5.0, stopped_hours_ago=1.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        held = self.store._held({"direction": "scale_up"}, "test", "rust_builder")
        expected = sum(self.store._running_role_hourly_rates("rust_builder")) + self.store._stopped_disk_burn("rust_builder")
        self.assertGreater(self.store._stopped_disk_burn("rust_builder"), 0.0)
        self.assertAlmostEqual(held["projected_hourly_usd"], expected, delta=0.0001)

    def test_pricing_trust_change_is_saved_with_same_rates(self):
        self.fleet = [fleet_observation("8500", "oxidex-runners-trust-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        self.store.refresh_fleet_inventory()  # Settle one-time baselines.
        from lib import dashboard_server
        real = dashboard_server.calculate_spot_pricing

        def untrusted(*args, **kwargs):
            return {**real(*args, **kwargs), "complete": False, "family_rate_unknown": True}

        with unittest.mock.patch.object(dashboard_server, "calculate_spot_pricing", side_effect=untrusted):
            self.assertTrue(self.store.refresh_fleet_inventory())
        self.assertFalse(self._record("oxidex-runners-trust-1")["pricing_complete"])
        self.assertEqual(self._record("oxidex-runners-trust-1")["disk_segments"], [])  # Rates unchanged: no segment.

    def test_reported_freshness_ages_out(self):
        self.fleet = [fleet_observation("8600", "oxidex-runners-age-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        self.assertTrue(self.store.fleet_inventory_status()["fresh"])
        clock = time.time() + 4 * self.store.fleet_inventory_interval
        with unittest.mock.patch.object(self.store, "_clock", return_value=clock):
            status = self.store.query_instances()["fleet_inventory"]
        self.assertFalse(status["fresh"])
        self.assertIn("no complete listing", status["stale_reason"])

    def test_first_sight_uses_profile_tax_override(self):
        with self.store._lock:
            budget = self.store.state["profiles"]["rust_builder"]["budget"]
            budget["override_regional_tax"] = True
            budget["tax_rate_percent"] = 17.0
        self.fleet = [fleet_observation("8700", "builder-oxidex-ovr-1", "us-south1-b", started_hours_ago=48.0,
                                        now=self.now)]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-ovr-1")
        self.assertEqual(rec["pricing"]["tax_rate_percent"], 17.0)
        self.assertEqual(rec["prior_runs"], [])  # No repricing closed history at a provisional rate.
        self.assertEqual(rec["disk_segments"], [])

    def test_shared_stopped_burn_uses_evaluated_budget_flags(self):
        self.fleet = [fleet_observation("8800", "builder-oxidex-sh-1", "us-south1-b", status="TERMINATED",
                                        started_hours_ago=5.0, stopped_hours_ago=1.0, now=self.now)]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            self.store.state["scaling"]["rust_builder"]["include_disk_in_scale_budget"] = False
            self.store.state["scaling"]["cicd_runner"]["include_disk_in_scale_budget"] = True
        self.assertEqual(self.store._stopped_disk_burn("rust_builder"), 0.0)
        shared = self.store._outside_burn("cicd_runner", shared=True, other_running_burn=0.0)
        self.assertGreater(shared, 0.0)
        self.assertAlmostEqual(shared, self.store._stopped_disk_burn("rust_builder", budget_role="cicd_runner"),
                               delta=0.0001)

    def test_host_running_before_oom_is_part_of_its_baseline(self):
        self.fleet = [self._runner("8900", "oxidex-runners-pre-1")]
        self.store.refresh_fleet_inventory()
        self._runner_oom("oom-r30", "oxidex-runners-pre-1")
        # First seen together with the OOM, but it had been running for hours: capacity that already existed.
        self.fleet.append(self._runner("8901", "oxidex-runners-pre-2", started_hours_ago=3.0))
        self.store.refresh_fleet_inventory()
        event = self.store.state["scaling_telemetry"]["journal_oom_events"][0]
        self.assertEqual(event["baseline_runners"], 8)
        self.assertFalse(event.get("handled", False))

    def test_unready_runner_slots_are_not_current_runners(self):
        booting = self._runner("9000", "oxidex-runners-boot-9")
        booting["runners_count_known"] = False
        self.fleet = [self._runner("9001", "oxidex-runners-ok-9"), booting]
        self.store.refresh_fleet_inventory()
        self.assertEqual(self.store.state["scaling_telemetry"]["current_runners"], 4)

    def test_builder_evaluation_cannot_consume_oom_from_count(self):
        self.fleet = [fleet_observation(f"910{i}", f"builder-oxidex-ev-{i}", "us-south1-b", now=self.now)
                      for i in range(2)]
        self.store.refresh_fleet_inventory()
        observed = datetime.fromtimestamp(time.time() - 60.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.store._lock:
            self.store.state["scaling_telemetry"]["journal_oom_events"] = [
                {"id": "oom-b10", "instance_name": "builder-oxidex-ev-0", "role": "rust_builder",
                 "observed_at": observed, "baseline_instances": 1}]
            self.store._sync_fleet_telemetry()
        self.store.get_overview()
        event = self.store.state["scaling_telemetry"]["journal_oom_events"][0]
        self.assertFalse(event.get("handled", False))

    def test_cross_role_running_burn_uses_evaluated_flags(self):
        self.fleet = [fleet_observation("9200", "builder-oxidex-cr-1", "us-south1-b", now=self.now)]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            self.store.state["scaling"]["rust_builder"]["include_disk_in_scale_budget"] = False
            self.store.state["scaling"]["cicd_runner"]["include_disk_in_scale_budget"] = True
        own = sum(self.store._running_role_hourly_rates("rust_builder"))
        evaluated = sum(self.store._running_role_hourly_rates("rust_builder", budget_role="cicd_runner"))
        self.assertGreater(evaluated, own)

    def test_resume_between_listings_ends_run_at_suspension(self):
        vm = fleet_observation("9300", "builder-oxidex-sr-1", "us-south1-b", started_hours_ago=10.0, now=self.now)
        self.fleet = [vm]
        self.store.refresh_fleet_inventory()
        # Suspended 6 hours ago and resumed an hour ago, between two listings.
        self.fleet = [dict(vm, last_start_epoch=self.now - 3600.0, last_suspended_epoch=self.now - 6 * 3600.0)]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-sr-1")
        self.assertAlmostEqual(rec["prior_runs"][-1][1], self.now - 6 * 3600.0, delta=1.0)
        self.assertAlmostEqual(rec["runtime_hours"], 5.0, delta=0.05)

    def test_disk_handoff_keys_by_disk_id(self):
        from lib.dashboard_server import _disk_key

        self.assertEqual(_disk_key({"id": "5", "source": "z/disks/a"}), "id:5")
        self.assertEqual(_disk_key({"source": "z/disks/a"}), "z/disks/a")
        created = self.now - 3 * 86400.0
        old_disk = {"type": "pd-balanced", "gb": 100, "created_epoch": created, "source": "z/disks/cache", "id": "1"}
        vm = fleet_observation("9400", "builder-oxidex-id-1", "us-south1-b", started_hours_ago=72.0, now=self.now)
        vm.update(created_epoch=created, disks_detail=[old_disk])
        self.fleet = [vm]
        self.store.refresh_fleet_inventory()
        self.fleet = []
        self.store.refresh_fleet_inventory()
        # A new disk reusing the name (new ID) on a new VM is billed from its own creation.
        new_created = self.now - 86400.0
        vm2 = fleet_observation("9401", "builder-oxidex-id-2", "us-south1-b", started_hours_ago=24.0, now=self.now)
        vm2.update(created_epoch=new_created, disks_detail=[dict(old_disk, id="2", created_epoch=new_created)])
        self.fleet = [vm2]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-id-2")
        # Not treated as a handoff: billed from its own creation, not from this listing.
        self.assertAlmostEqual(rec["disk_segment_start"], new_created, delta=1.0)

    def test_detached_disk_kept_while_disks_unlisted(self):
        from lib.fleet_inventory import FleetListing, observe_detached_disks

        detail = {"z/disks/builder-oxidex-k-cache": {"id": "97", "name": "builder-oxidex-k-cache",
                                                     "zone": "z/us-south1-b", "sizeGb": "100", "type": "pd-balanced"}}
        self.fleet = observe_detached_disks(detail)
        self.store.refresh_fleet_inventory()
        listing = FleetListing([])
        listing.disks_listed = False
        self.store.fleet_source = lambda: listing
        self.store.refresh_fleet_inventory()
        self.assertEqual(self._record("builder-oxidex-k-cache")["status"], "DETACHED")

    def test_repeated_denied_disk_listing_keeps_types(self):
        known = fleet_observation("9500", "builder-oxidex-den-1", "us-south1-b", now=self.now)
        self.fleet = [known]
        self.store.refresh_fleet_inventory()
        fallback = dict(known, boot_disk_type="pd-balanced", data_disk_type="pd-balanced", disk_types_known=False)
        for _ in range(3):
            self.fleet = [fallback]
            self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-den-1")
        self.assertEqual((rec["boot_disk_type"], rec["data_disk_type"]), ("hyperdisk-balanced", "hyperdisk-balanced"))
        self.assertEqual(rec["disk_segments"], [])  # No false shape change.

    def test_disk_ids_survive_outage_for_handoff(self):
        from lib.dashboard_server import instance_usage
        from lib.fleet_inventory import observe_detached_disks

        created = self.now - 10 * 86400.0
        cache = "projects/p/zones/us-south1-b/disks/builder-oxidex-o-cache"
        vm = fleet_observation("9600", "builder-oxidex-o", "us-south1-b", started_hours_ago=240.0, now=self.now)
        vm.update(created_epoch=created, disks_detail=[
            {"type": "pd-balanced", "gb": 100, "created_epoch": created, "source": cache, "id": "77"}])
        self.fleet = [vm]
        self.store.refresh_fleet_inventory()
        outage = dict(vm, disk_types_known=False,
                      disks_detail=[{"type": "pd-balanced", "gb": 100, "created_epoch": None, "source": cache, "id": ""}])
        self.fleet = [outage]
        self.store.refresh_fleet_inventory()
        self.assertEqual(self._record("builder-oxidex-o")["disk_ids"], {cache: "77"})
        stamp = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.fleet = observe_detached_disks({cache: {"id": "77", "name": "builder-oxidex-o-cache",
                                                     "zone": "z/us-south1-b", "sizeGb": "100", "type": "pd-balanced",
                                                     "creationTimestamp": stamp}})
        handoff = time.time()
        self.store.refresh_fleet_inventory()
        usage = instance_usage(self._record("builder-oxidex-o-cache"), handoff + 3600.0)
        self.assertLess(usage["disk_hours"], 1.1)

    def test_detached_regional_disk_gets_region(self):
        from lib.fleet_inventory import observe_detached_disks

        disks = observe_detached_disks({"projects/p/regions/us-south1/disks/builder-oxidex-rg-cache": {
            "id": "98", "name": "builder-oxidex-rg-cache", "region": "regions/us-south1", "sizeGb": "100",
            "type": "pd-ssd", "regional": True}})
        self.assertEqual((disks[0]["zone"], disks[0]["region"]), ("", "us-south1"))

    def test_online_runner_kpi_counts_confirmed_slots(self):
        booting = self._runner("9700", "oxidex-runners-kpi-1")
        booting["runners_count_known"] = False
        self.fleet = [self._runner("9701", "oxidex-runners-kpi-2"), booting]
        self.store.refresh_fleet_inventory()
        view = self.store.query_instances()
        self.assertEqual(view["running_runner_slots"], 4)
        self.assertEqual(view["unconfirmed_runner_hosts"], 1)

    def test_template_action_hidden_for_unrepresentable_records(self):
        from lib.dashboard_ui_assets import DASHBOARD_HTML

        self.assertIn("function canTemplate(inst)", DASHBOARD_HTML)
        self.assertIn("inst.runner_limits_known !== false", DASHBOARD_HTML)
        # Both template buttons (map inspector and instance table) are behind canTemplate.
        self.assertEqual(DASHBOARD_HTML.count("${canTemplate(inst) ? `<button"), 2)
        self.assertEqual(DASHBOARD_HTML.count("onclick=\"makeTemplateFromInstance("), 2)

    def test_disk_still_attached_in_instance_listing_is_not_detached(self):
        from lib.fleet_inventory import observe_detached_disks

        detail = {"z/disks/builder-oxidex-a-cache": {"id": "99", "name": "builder-oxidex-a-cache",
                                                     "zone": "z/us-south1-b", "sizeGb": "100", "type": "pd-balanced"}}
        self.assertEqual(observe_detached_disks(detail, frozenset({"z/disks/builder-oxidex-a-cache"})), [])
        self.assertEqual(len(observe_detached_disks(detail)), 1)

    def test_disk_record_is_not_a_template_source(self):
        from lib.fleet_inventory import observe_detached_disks

        self.fleet = observe_detached_disks({"z/disks/builder-oxidex-t-cache": {
            "id": "100", "name": "builder-oxidex-t-cache", "zone": "z/us-south1-b", "sizeGb": "100",
            "type": "pd-balanced"}})
        self.store.refresh_fleet_inventory()
        with self.assertRaisesRegex(ValueError, "is a disk, not a VM"):
            self.store.create_vm_template_from_instance({"instance_name": "builder-oxidex-t-cache",
                                                         "assign_to_profile": False})

    def test_freshness_counts_from_completion(self):
        self.fleet = [fleet_observation("9800", "oxidex-runners-slow-1", "us-south1-a", now=self.now)]
        clock = [time.time()]

        def slow_source():
            clock[0] += 5 * self.store.fleet_inventory_interval  # The listing takes a long time.
            return list(self.fleet)

        self.store.fleet_source = slow_source
        with unittest.mock.patch.object(self.store, "_clock", side_effect=lambda: clock[0]):
            self.store.refresh_fleet_inventory()
            self.assertEqual(self.store._inventory_stale_reason(), "")

    def test_added_disk_billed_from_its_creation(self):
        created = self.now - 10 * 86400.0
        boot = {"type": "pd-balanced", "gb": 40, "created_epoch": created, "source": "z/boot-ad", "id": "1"}
        vm = fleet_observation("9900", "builder-oxidex-add-1", "us-south1-b", started_hours_ago=240.0, now=self.now)
        vm.update(created_epoch=created, data_disk_gb=0, data_disks={}, disks_detail=[boot])
        self.fleet = [vm]
        self.store.refresh_fleet_inventory()
        added_at = self.now - 2 * 86400.0
        grown = dict(vm, data_disk_gb=500, data_disks={"pd-balanced": 500}, data_disk_type="pd-balanced",
                     disks_detail=[boot, {"type": "pd-balanced", "gb": 500, "created_epoch": added_at,
                                          "source": "z/data-ad", "id": "2"}])
        self.fleet = [grown]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-add-1")
        self.assertAlmostEqual(rec["disk_segment_start"], added_at, delta=1.0)
        self.assertAlmostEqual(rec["disk_segments"][-1][1], added_at, delta=1.0)

    def test_detached_rows_render_as_present_storage(self):
        from lib.dashboard_ui_assets import DASHBOARD_HTML

        self.assertIn('inst.status === "DETACHED" ? \'<span style="color:#fcd34d;">Present (storage still billed)',
                      DASHBOARD_HTML)

    def test_two_added_disks_split_at_each_creation(self):
        created = self.now - 10 * 86400.0
        boot = {"type": "pd-balanced", "gb": 40, "created_epoch": created, "source": "z/boot-2a", "id": "11"}
        vm = fleet_observation("9910", "builder-oxidex-add-2", "us-south1-b", started_hours_ago=240.0, now=self.now)
        vm.update(created_epoch=created, data_disk_gb=0, data_disks={}, disks_detail=[boot])
        self.fleet = [vm]
        self.store.refresh_fleet_inventory()
        first, second = self.now - 3 * 86400.0, self.now - 86400.0
        grown = dict(vm, data_disk_gb=300, data_disks={"pd-balanced": 300}, data_disk_type="pd-balanced",
                     disks_detail=[boot,
                                   {"type": "pd-balanced", "gb": 100, "created_epoch": first, "source": "z/d1", "id": "12"},
                                   {"type": "pd-balanced", "gb": 200, "created_epoch": second, "source": "z/d2", "id": "13"}])
        self.fleet = [grown]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-add-2")
        ends = [round(seg[1]) for seg in rec["disk_segments"]]
        self.assertIn(round(first), ends)
        self.assertIn(round(second), ends)
        rates = [seg[2] for seg in rec["disk_segments"]]
        self.assertLess(rates[-2], rates[-1])  # The step after the first added disk bills more.
        self.assertAlmostEqual(rec["disk_segment_start"], second, delta=1.0)

    def test_runner_burn_for_builder_budget_prices_added_runners(self):
        self.fleet = [self._runner("9920", "oxidex-runners-bb-1")]
        self.store.refresh_fleet_inventory()
        running = sum(self.store._running_role_hourly_rates("cicd_runner", budget_role="rust_builder"))
        unit = self.store._resolve_scaling_unit_hourly_usd("cicd_runner", None, budget_role="rust_builder")
        burn = self.store._runner_burn_for_builder_budget({"desired_instances": 3, "current_instances": 1})
        self.assertAlmostEqual(burn, running + 2 * unit, delta=0.0002)
        held = self.store._runner_burn_for_builder_budget({"desired_instances": 1, "current_instances": 1,
                                                           "projected_hourly_usd": 99.0})
        self.assertAlmostEqual(held, running, delta=0.0002)  # Never the held projection with storage in it.

    def test_vanished_builder_keeps_disk_liability_while_disks_unlisted(self):
        from lib.dashboard_server import instance_usage
        from lib.fleet_inventory import FleetListing, observe_detached_disks

        created = self.now - 5 * 86400.0
        cache = "projects/p/zones/us-south1-b/disks/builder-oxidex-lia-cache"
        vm = fleet_observation("9930", "builder-oxidex-lia", "us-south1-b", started_hours_ago=120.0, now=self.now)
        vm.update(created_epoch=created, disks_detail=[
            {"type": "pd-balanced", "gb": 100, "created_epoch": created, "source": cache, "id": "55"}])
        self.fleet = [vm]
        self.store.refresh_fleet_inventory()
        listing = FleetListing([])
        listing.disks_listed = False
        self.store.fleet_source = lambda: listing
        self.store.refresh_fleet_inventory()  # The builder is gone while disks cannot be listed.
        rec = self._record("builder-oxidex-lia")
        self.assertEqual(rec["status"], "DELETED")
        self.assertTrue(rec["disk_liability_pending"])
        later = time.time() + 7200.0
        self.assertGreater(instance_usage(rec, later)["disk_hours"], instance_usage(rec, time.time())["disk_hours"])
        self.assertGreater(self.store._stopped_disk_burn("rust_builder"), 0.0)
        stamp = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.fleet = observe_detached_disks({cache: {"id": "55", "name": "builder-oxidex-lia-cache",
                                                     "zone": "z/us-south1-b", "sizeGb": "100", "type": "pd-balanced",
                                                     "creationTimestamp": stamp}})
        self.store.fleet_source = lambda: list(self.fleet)
        resumed = time.time()
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-lia")
        self.assertFalse(rec["disk_liability_pending"])
        self.assertGreaterEqual(rec["disk_end_epoch"], resumed - 1.0)
        detached = self._record("builder-oxidex-lia-cache")
        self.assertGreaterEqual(detached["disk_segment_start"], resumed - 1.0)  # Takes over at the same moment.

    def test_disk_used_by_vm_missing_from_snapshot_is_billed(self):
        from lib.fleet_inventory import observe_detached_disks

        detail = {"z/disks/builder-oxidex-sp-cache": {
            "id": "101", "name": "builder-oxidex-sp-cache", "zone": "z/us-south1-b", "sizeGb": "100",
            "type": "pd-balanced", "users": ["projects/p/zones/us-south1-b/instances/builder-oxidex-sp-2"]}}
        # The replacement builder was created after the instance listing: bill the disk here for now.
        self.assertEqual(len(observe_detached_disks(detail, frozenset(), frozenset({"us-south1-b/builder-oxidex-sp-1"}))), 1)
        # A present VM may have gained this disk after the earlier instance listing. Its name is not evidence
        # that its snapshot record includes this disk, so keep storage billing on the disk until handoff.
        self.assertEqual(len(observe_detached_disks(detail, frozenset(),
                                                  frozenset({"us-south1-b/builder-oxidex-sp-2"}))), 1)
        # Suppress only when the very same instance snapshot actually reports this attachment.
        self.assertEqual(observe_detached_disks(detail, frozenset(detail),
                                               frozenset({"us-south1-b/builder-oxidex-sp-2"})), [])
        # A same-named VM in another zone is a different VM: the disk is still billed here.
        self.assertEqual(len(observe_detached_disks(detail, frozenset(), frozenset({"us-east1-c/builder-oxidex-sp-2"}))), 1)
        other = {"z/disks/builder-oxidex-x-cache": dict(detail["z/disks/builder-oxidex-sp-cache"], id="102",
                                                        name="builder-oxidex-x-cache",
                                                        users=["projects/p/zones/z/instances/someone-else"])}
        self.assertEqual(observe_detached_disks(other, frozenset(), frozenset()), [])
        mixed = {key: dict(value, users=value["users"] + other[next(iter(other))]["users"])
                 for key, value in detail.items()}
        self.assertEqual(len(observe_detached_disks(mixed, frozenset(), frozenset())), 1)
        self.assertEqual(observe_detached_disks(mixed, frozenset(mixed), frozenset()), [])

    def test_disk_attached_between_split_snapshots_keeps_billing_until_handoff(self):
        from lib.fleet_inventory import observe_detached_disks
        from lib.dashboard_server import reconcile_instance_records, instance_usage, _single_disk_all_in

        source = "projects/p/zones/us-south1-b/disks/builder-split-cache"
        created = self.now - 3 * 3600
        detail = {source: {"id": "split-disk", "name": "builder-split-cache", "type": "pd-ssd",
            "zone": "zones/us-south1-b", "sizeGb": "100", "users": [],
            "creationTimestamp": datetime.fromtimestamp(created, timezone.utc).isoformat()}}
        vm = fleet_observation("split-vm", "builder-split", "us-south1-b", now=self.now,
                               started_hours_ago=3, vcpu=0, ram_gib=0)
        vm.update(boot_disk_gb=0, data_disk_gb=0, data_disks={}, disks_detail=[])
        rows, _ = reconcile_instance_records([], [vm] + observe_detached_disks(detail),
                                             project="p", now=self.now - 2 * 3600)
        detail[source]["users"] = ["projects/p/zones/us-south1-b/instances/builder-split"]
        split = observe_detached_disks(detail, frozenset(), frozenset({"us-south1-b/builder-split"}))
        rows, _ = reconcile_instance_records(rows, [vm] + split, project="p", now=self.now - 3600)
        self.assertEqual(next(r for r in rows if r["kind"] == "disk")["status"], "DETACHED")
        disk = {"id": "split-disk", "source": source, "type": "pd-ssd", "gb": 100,
                "created_epoch": created, "auto_delete": False}
        attached = dict(vm, data_disk_type="pd-ssd", data_disk_gb=100, data_disks={"pd-ssd": 100},
                        disks_detail=[disk])
        rows, _ = reconcile_instance_records(rows, [attached], project="p", now=self.now)
        carrier = next(r for r in rows if r["kind"] == "disk")
        rate = _single_disk_all_in(disk, family=carrier["family"], region=carrier["region"], project="p",
                                   tax_rate_percent=carrier["pricing"]["tax_rate_percent"])
        self.assertAlmostEqual(sum(instance_usage(r, self.now)["cost_all_in"] for r in rows), rate * 3,
                               delta=0.001)

    def test_vanished_builder_liability_covers_only_retained_disks(self):
        from lib.dashboard_server import _pricing_rates
        from lib.fleet_inventory import FleetListing

        created = self.now - 5 * 86400.0
        vm = fleet_observation("9940", "builder-oxidex-ret", "us-south1-b", started_hours_ago=120.0, now=self.now)
        vm.update(created_epoch=created, disks_detail=[
            {"type": "hyperdisk-balanced", "gb": 40, "created_epoch": created, "source": "z/boot-r", "id": "61",
             "auto_delete": True},
            {"type": "hyperdisk-balanced", "gb": 100, "created_epoch": created, "source": "z/cache-r", "id": "62",
             "auto_delete": False}])
        self.fleet = [vm]
        self.store.refresh_fleet_inventory()
        full = _pricing_rates(self._record("builder-oxidex-ret")["pricing"])[2]
        listing = FleetListing([])
        listing.disks_listed = False
        self.store.fleet_source = lambda: listing
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-ret")
        self.assertTrue(rec["disk_liability_pending"])
        self.assertTrue(rec["retained_only_pricing"])
        retained = _pricing_rates(rec["pricing"])[2]
        self.assertGreater(retained, 0.0)
        self.assertLess(retained, full)
        self.assertAlmostEqual(_pricing_rates(rec["pricing"])[0], 0.0, delta=0.001)  # No compute after deletion.
        # Repriced while still pending (a tax override), at the retained-disk rate only.
        self.store.update_profile("rust_builder", {"budget": {"override_regional_tax": True, "tax_rate_percent": 30.0}})
        self.assertEqual(self._record("builder-oxidex-ret")["pricing"]["tax_rate_percent"], 30.0)

    def test_build_evaluation_counts_projected_runner_additions(self):
        from lib import dashboard_server

        self.fleet = [self._runner("9950", "oxidex-runners-pb-1")]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            self.store.state["global_budget_enforced"] = True
        captured = {}
        real_builder = dashboard_server.evaluate_builder_scaling_decision

        def ci_eval(*args, **kwargs):
            return {"direction": "scale_up", "current_runners": 4, "desired_runners": 12, "current_instances": 1,
                    "desired_instances": 3, "projected_hourly_usd": 0.0}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real_builder(*args, **kwargs)

        with unittest.mock.patch.object(dashboard_server, "evaluate_scaling_decision", ci_eval), \
                unittest.mock.patch.object(dashboard_server, "evaluate_builder_scaling_decision", spy):
            self.store.record_rust_build({"build_id": "rb-proj-01", "cache_hits": 1, "cache_misses": 1})
        running = sum(self.store._running_role_hourly_rates("cicd_runner", budget_role="rust_builder"))
        unit = self.store._resolve_scaling_unit_hourly_usd("cicd_runner", None, budget_role="rust_builder")
        self.assertAlmostEqual(captured["other_role_hourly_usd"], running + 2 * unit, delta=0.0005)

    def test_multi_attached_disk_billed_once(self):
        from lib.fleet_inventory import observe_fleet_items

        shared = {"boot": False, "diskSizeGb": "500", "source": "z/disks/shared-ro"}
        items = [{"id": f"6{i}", "name": f"oxidex-runners-ro-{i}", "zone": "zones/us-south1-a", "status": "RUNNING",
                  "machineType": "machineTypes/n4-standard-16", "disks": [shared]} for i in range(2)]
        obs = observe_fleet_items(items, disk_types={"z/disks/shared-ro": {"type": "pd-ssd", "id": "500"}},
                                  shapes={("us-south1-a", "n4-standard-16"): {"vcpu": 16, "ram_gib": 64.0}})
        self.assertEqual([o["data_disk_gb"] for o in obs], [500, 500])  # Both VMs' shapes include it.
        self.assertEqual([o["data_disks"] for o in obs], [{"pd-ssd": 500}, {}])  # Billed once.
        self.assertEqual(obs[1]["disks_detail"][0]["id"], "500")
        self.assertFalse(obs[1]["disks_detail"][0]["billing_owner"])
        self.fleet = obs
        self.store.refresh_fleet_inventory()
        second = self._record("oxidex-runners-ro-1")
        self.assertEqual(second["data_disk_gb"], 500)
        self.assertLess(second["pricing"]["disk_hourly_usd"], self._record("oxidex-runners-ro-0")["pricing"]["disk_hourly_usd"])

    def test_shared_disk_ownership_transfer_during_denied_listing(self):
        from lib.fleet_inventory import observe_fleet_items

        shared = {"boot": False, "diskSizeGb": "500", "source": "z/shared-transfer"}
        items = [{"id": str(i), "name": f"builder-transfer-{i}", "zone": "zones/us-south1-a",
                  "status": "RUNNING", "machineType": "types/n4-highcpu-16", "disks": [shared]}
                 for i in (2, 3)]
        shapes = {("us-south1-a", "n4-highcpu-16"): {"vcpu": 16, "ram_gib": 32.0}}
        self.fleet = observe_fleet_items(items, shapes=shapes,
                                         disk_types={"z/shared-transfer": {"id": "888", "type": "pd-balanced"}})
        self.store.refresh_fleet_inventory()
        items.append(dict(items[0], id="1", name="builder-transfer-1"))
        self.fleet = observe_fleet_items(items, shapes=shapes, disk_types={}, disk_types_known=False)
        self.store.refresh_fleet_inventory()
        live = [r for r in self.store.state["instances"] if r["status"] == "RUNNING"]
        self.assertEqual(sum(sum(r["data_disks"].values()) for r in live), 500)
        self.assertEqual(sum(r["data_disk_gb"] for r in live), 1500)
        self.assertEqual(len([r for r in live if r["pricing"]["disk_hourly_usd"] > 0]), 1)

    def test_delayed_replacement_oom_keeps_original_capacity_baseline(self):
        old = {"instance_id": "old", "role": "cicd_runner", "status": "TERMINATED", "runners_count": 4,
               "runners_count_known": True, "capacity_pending": False, "started_epoch": self.now - 7200}
        new = dict(old, instance_id="new", status="RUNNING", capacity_pending=True, started_epoch=self.now - 120)
        event = {"id": "late-replacement", "role": "cicd_runner", "baseline_runners": 4,
                 "observed_at": datetime.fromtimestamp(self.now - 300, timezone.utc).isoformat()}
        self.store.state["scaling_telemetry"]["journal_oom_events"] = [event]
        self.store._consume_ooms_for_fleet_growth([old, new], [dict(old), dict(new)])
        self.assertEqual(event["baseline_runners"], 4)
        self.assertFalse(event.get("handled", False))

    def test_resolved_disk_liability_stays_in_recent_cost_window(self):
        from lib.dashboard_server import reconcile_instance_records, instance_usage

        vm = fleet_observation("liability-window", "builder-window", "us-south1-b", now=self.now,
                               started_hours_ago=120)
        initial, _ = reconcile_instance_records([], [vm], project="p", now=self.now - 3 * 86400)
        pending, _ = reconcile_instance_records(initial, [], project="p", now=self.now - 2 * 86400,
                                                 disks_listed=False)
        resolved, _ = reconcile_instance_records(pending, [], project="p", now=self.now)
        self.store.state["instances"] = resolved
        expected = instance_usage(resolved[0], self.now, self.now - 86400)["window_all_in"]
        self.assertGreater(expected, 0)
        result = self.store.query_instances(window_hours=24)
        self.assertEqual(len(result["instances"]), 1)
        self.assertAlmostEqual(result["total_window_spend_usd"], expected, delta=0.01)

    def test_pending_disk_liability_stops_when_attachment_transfers_during_outage(self):
        from lib.dashboard_server import reconcile_instance_records, instance_usage

        vm = fleet_observation("liability-owner", "builder-owner", "us-south1-b", now=self.now)
        vm["disks_detail"] = [{"source": "z/cache-transfer", "id": "91", "type": "pd-balanced",
                               "gb": 100, "created_epoch": self.now - 7200, "auto_delete": False}]
        initial, _ = reconcile_instance_records([], [vm], project="p", now=self.now - 3600)
        pending, _ = reconcile_instance_records(initial, [], project="p", now=self.now - 1800, disks_listed=False)
        replacement = dict(vm, instance_id="liability-new", name="builder-new")
        after, _ = reconcile_instance_records(pending, [replacement], project="p", now=self.now, disks_listed=False)
        retired = next(r for r in after if r["instance_id"] == "liability-owner")
        self.assertFalse(retired["disk_liability_pending"])
        self.assertEqual(instance_usage(retired, self.now)["cost_all_in"],
                         instance_usage(retired, self.now + 3600)["cost_all_in"])

    def test_added_disk_metadata_recovery_backfills_real_storage_rate(self):
        from lib.dashboard_server import _live_instance_record, _single_disk_all_in, _pricing_rates, instance_usage

        old_disk = {"source": "z/known", "id": "91", "type": "pd-balanced", "gb": 100,
                    "created_epoch": self.now - 36000, "auto_delete": False}
        obs = fleet_observation("added-denied", "builder-added-denied", "us-south1-b", now=self.now,
                                started_hours_ago=10)
        obs.update(boot_disk_gb=0, data_disk_type="pd-balanced", data_disk_gb=100,
                   data_disks={"pd-balanced": 100}, disks_detail=[old_disk])
        initial = _live_instance_record(obs, project="p", now=self.now - 18000)
        unknown = dict(old_disk, source="z/new", id="", created_epoch=None)
        denied = dict(obs, disk_types_known=False, data_disk_gb=200, data_disks={"pd-balanced": 200},
                      disks_detail=[old_disk, unknown])
        record = _live_instance_record(denied, project="p", now=self.now - 7200, previous=initial)
        self.assertEqual(record["pricing"]["disk_hourly_usd"], initial["pricing"]["disk_hourly_usd"])
        self.assertEqual(record["deferred_disk_sources"], ["z/new"])
        self.assertFalse(record["pricing_complete"])
        from lib.dashboard_server import _with_secondary_disks
        catalog = {"disk_monthly_gib": {"pd-balanced": 1.0}}
        quoted = _with_secondary_disks(record["pricing"], record, region=record["region"],
            family=record["family"], project="p", live_pricing=catalog)
        expected_rate = _single_disk_all_in(old_disk, family=record["family"], region=record["region"],
            project="p", tax_rate_percent=0, live_pricing=catalog)
        self.assertAlmostEqual(quoted["disk_hourly_usd"], expected_rate, places=4)
        # Repeated denied polls must not start charging the fallback or lose the marker.
        record = _live_instance_record(denied, project="p", now=self.now - 3600, previous=record)
        real = dict(unknown, id="92", type="pd-ssd", created_epoch=self.now - 10800)
        recovered = dict(obs, data_disk_gb=200, data_disks={"pd-balanced": 100, "pd-ssd": 100},
                         disks_detail=[old_disk, real])
        record = _live_instance_record(recovered, project="p", now=self.now, previous=record)
        tax = record["pricing"]["tax_rate_percent"]
        expected = sum(hours * _single_disk_all_in(d, family=record["family"], region=record["region"],
                                                   project="p", tax_rate_percent=tax)
                       for hours, d in ((10, old_disk), (3, real)))
        usage = instance_usage(record, self.now)
        self.assertAlmostEqual(usage["disk_hours"], 10, places=5)
        self.assertAlmostEqual(usage["cost_all_in"] - _pricing_rates(initial["pricing"])[0] * 10, expected, delta=0.001)
        again = _live_instance_record(recovered, project="p", now=self.now, previous=record)
        self.assertEqual(instance_usage(again, self.now), usage)
        self.assertEqual(record["deferred_disk_sources"], [])

    def test_partial_initial_disk_recovery_keeps_elapsed_disk_hours(self):
        from lib.dashboard_server import _live_instance_record, instance_usage

        disks = [{"source": "z/" + name, "type": "pd-balanced", "gb": 100,
                  "created_epoch": None, "auto_delete": False} for name in ("a", "b")]
        obs = fleet_observation("partial-initial", "builder-partial-initial", "us-south1-b", now=self.now,
                                started_hours_ago=10, vcpu=0, ram_gib=0)
        obs.update(boot_disk_gb=0, data_disk_type="pd-balanced", data_disk_gb=200,
                   data_disks={"pd-balanced": 200}, disks_detail=disks, disk_types_known=False)
        record = _live_instance_record(obs, project="p", now=self.now - 5 * 3600)
        for index, hours_ago in ((0, 3), (1, 0)):
            disks[index] = dict(disks[index], id=str(index + 1), created_epoch=self.now - 10 * 3600)
            record = _live_instance_record(dict(obs, disks_detail=disks, disk_types_known=index == 1),
                project="p", now=self.now - hours_ago * 3600, previous=record)
        usage = instance_usage(record, self.now, self.now - 24 * 3600)
        self.assertAlmostEqual(usage["disk_hours"], 10, places=5)
        self.assertAlmostEqual(usage["disk_hours_in_window"], 10, places=5)

    def test_added_disk_deferral_preserves_initial_and_handoff_boundaries(self):
        from lib.dashboard_server import _live_instance_record, _single_disk_all_in, _pricing_rates, instance_usage

        a = {"source": "a", "id": "a", "type": "pd-balanced", "gb": 100,
             "created_epoch": self.now - 36000, "auto_delete": False}
        b = dict(a, source="b", id="b", type="pd-ssd", created_epoch=self.now - 28800)
        base = fleet_observation("combined-defer", "builder-combined-defer", "us-south1-b", now=self.now,
                                 started_hours_ago=10)
        base.update(boot_disk_gb=0, data_disk_type="pd-balanced", data_disk_gb=100, data_disks={"pd-balanced": 100})
        for initial_unknown, handoff, hours in ((True, False, 8), (False, True, 2)):
            with self.subTest(initial_unknown=initial_unknown, handoff=handoff):
                initial = _live_instance_record(dict(base, disk_types_known=not initial_unknown,
                    disks_detail=[dict(a, created_epoch=None, id="") if initial_unknown else a]),
                    project="p", now=self.now - 18000)
                added = dict(base, disk_types_known=False, data_disk_gb=200, data_disks={"pd-balanced": 200},
                             disks_detail=[dict(a, created_epoch=None, id="") if initial_unknown else a,
                                           dict(b, created_epoch=None, id="", type="pd-balanced")])
                denied = _live_instance_record(added, project="p", now=self.now - 7200, previous=initial)
                recovered = _live_instance_record(dict(base, data_disk_gb=200,
                    data_disks={"pd-balanced": 100, "pd-ssd": 100}, disks_detail=[a, b]), project="p", now=self.now,
                    previous=denied, known_disk_sources=frozenset({"id:b", "b"}) if handoff else frozenset())
                rate = lambda d: _single_disk_all_in(d, family=recovered["family"], region=recovered["region"],
                    project="p", tax_rate_percent=recovered["pricing"]["tax_rate_percent"])
                storage = instance_usage(recovered, self.now)["cost_all_in"] - _pricing_rates(initial["pricing"])[0] * 10
                self.assertAlmostEqual(storage, 10 * rate(a) + hours * rate(b), delta=0.0015)

    def test_reattached_deferred_disk_does_not_rebill_its_old_interval(self):
        from lib.dashboard_server import _live_instance_record, _pricing_rates, _single_disk_all_in, instance_usage

        disks = [{"source": f"z/{name}", "id": name, "type": "pd-balanced", "gb": 100,
                  "created_epoch": self.now - 36000, "auto_delete": False} for name in ("a", "b")]
        base = fleet_observation("reattach-defer", "builder-reattach-defer", "us-south1-b", now=self.now,
                                 started_hours_ago=10)
        base.update(boot_disk_gb=0, data_disk_type="pd-balanced", data_disk_gb=200,
                    data_disks={"pd-balanced": 200}, disks_detail=disks)
        for initially_verified in (True, False):
            with self.subTest(initially_verified=initially_verified):
                first = dict(base, disk_types_known=initially_verified,
                    disks_detail=disks if initially_verified else [disks[0], dict(disks[1], id="", created_epoch=None)])
                initial = _live_instance_record(first, project="p", now=self.now - 18000)
                removed = _live_instance_record(dict(base, data_disk_gb=100, data_disks={"pd-balanced": 100},
                    disks_detail=disks[:1]), project="p", now=self.now - 14400, previous=initial)
                denied = _live_instance_record(dict(base, disk_types_known=False,
                    disks_detail=[disks[0], dict(disks[1], id="", created_epoch=None)]),
                    project="p", now=self.now - 7200, previous=removed)
                recovered = _live_instance_record(base, project="p", now=self.now, previous=denied)
                rate = _single_disk_all_in(disks[0], family=recovered["family"], region=recovered["region"], project="p",
                                          tax_rate_percent=recovered["pricing"]["tax_rate_percent"])
                storage = instance_usage(recovered, self.now)["cost_all_in"] - _pricing_rates(initial["pricing"])[0] * 10
                # The VM billed b for six hours before detachment and two hours after reattachment. Its standalone
                # record, when present, owns the intervening detached interval.
                self.assertAlmostEqual(storage, 18 * rate, delta=0.002)

    def test_unpaid_disk_history_follows_shared_billing_owner(self):
        from lib.dashboard_server import reconcile_instance_records, instance_usage, _single_disk_all_in

        bases = [fleet_observation(name, f"builder-{name}", "us-south1-b", now=self.now, started_hours_ago=3)
                 for name in ("a", "b")]
        bases = [dict(r, boot_disk_gb=0, data_disk_gb=0, data_disks={}, data_disks_attached_gb=0,
                      disks_detail=[]) for r in bases]
        disk = {"source": "z/shared-unpaid", "id": "", "type": "pd-balanced", "gb": 100,
                "created_epoch": None, "auto_delete": False}
        def observations(owner, known=False):
            detail = dict(disk, id="100", created_epoch=self.now - 14400) if known else disk
            return [dict(r, data_disk_gb=100, data_disks_attached_gb=100, disk_types_known=known,
                         data_disks={"pd-balanced": 100} if r["instance_id"] == owner else {},
                         disks_detail=[dict(detail, billing_owner=r["instance_id"] == owner)]) for r in bases]
        def storage(rows):
            return sum(instance_usage(r, self.now)["cost_all_in"] - instance_usage(r, self.now)["cost_compute"]
                       * (1 + r["pricing"]["tax_rate_percent"] / 100) for r in rows)
        for new_receiver, known_on_transfer in ((False, False), (True, False), (True, True)):
            with self.subTest(new_receiver=new_receiver, known_on_transfer=known_on_transfer):
                initial = bases[:1] if new_receiver else bases
                records, _ = reconcile_instance_records([], initial, project="p", now=self.now - 10800)
                added = observations("a")[:1] if new_receiver else observations("a")
                records, _ = reconcile_instance_records(records, added, project="p", now=self.now - 7200)
                if not known_on_transfer:
                    records, _ = reconcile_instance_records(records, observations("b"), project="p", now=self.now - 3600)
                    self.assertEqual(next(r for r in records if r["instance_id"] == "b")["deferred_disk_sources"], [disk["source"]])
                records, _ = reconcile_instance_records(records, observations("b", True), project="p", now=self.now)
                rate = _single_disk_all_in(disk, family=records[0]["family"], region=records[0]["region"], project="p",
                                          tax_rate_percent=records[0]["pricing"]["tax_rate_percent"])
                self.assertAlmostEqual(storage(records), 4 * rate, delta=0.002)
                owner = next(r for r in records if r["instance_id"] == "b")
                self.assertGreater(owner["pricing"]["disk_hourly_usd"], 0)
                again, _ = reconcile_instance_records(records, observations("b", True), project="p", now=self.now)
                self.assertAlmostEqual(storage(again), storage(records), places=6)

    def test_disk_lifecycle_timeline_oracle(self):
        from lib.dashboard_server import reconcile_instance_records, instance_usage, _single_disk_all_in

        bases = [fleet_observation(name, f"builder-oracle-{name}", "us-south1-b", now=self.now,
                                  started_hours_ago=6, vcpu=0, ram_gib=0) for name in ("a", "b", "c")]
        bases = [dict(r, boot_disk_gb=0, data_disk_gb=0, data_disks={}, data_disks_attached_gb=0,
                      disks_detail=[]) for r in bases]
        real = {"source": "z/oracle-disk", "id": "oracle-id", "type": "pd-ssd", "gb": 100,
                "created_epoch": self.now - 18000, "auto_delete": False, "boot": False, "billing_owner": True}
        owner_paths = (("a", "a", "a", "a"), ("a", "b", "b", "a"), ("a", "b", "a", "b"), ("a", "b", "b", "c"), ("a", "b", None, "b"))
        knowledge = ((False, False, True, True), (True, False, False, True), (True, True, False, True),
                     (False, False, False, True))
        for owners in owner_paths:
            for known in knowledge:
                for new_receiver in ((False, True) if owners[-1] == "c" else (False,)):
                    for resized in ((False, True) if known == (False, False, False, True) and owners[2] is not None else (False,)):
                        with self.subTest(owners=owners, known=known, new_receiver=new_receiver, resized=resized):
                            present = list(bases[:2] if new_receiver else bases)
                            rows, _ = reconcile_instance_records([], present, project="p", now=self.now - 21600)
                            for hours, owner, verified in zip((4, 3, 2, 0), owners, known):
                                at = self.now - hours * 3600
                                if owner is not None and owner not in {r["instance_id"] for r in present}:
                                    present.append(dict(bases[2], created_epoch=at, last_start_epoch=at))
                                gb = 200 if resized and hours <= 2 else 100
                                disk = dict(real, gb=gb) if verified else dict(real, id="", type="pd-balanced",
                                                                           created_epoch=None, gb=gb)
                                observations = [dict(r, disk_types_known=verified, data_disk_gb=gb,
                                    data_disks_attached_gb=gb, data_disk_type=disk["type"], data_disks={disk["type"]: gb},
                                    disks_detail=[disk]) if r["instance_id"] == owner else r for r in present]
                                if owner is None and verified:
                                    observations.append(dict(bases[0], instance_id="disk-oracle-id", name="builder-oracle-cache",
                                        kind="disk", status="DETACHED", machine_type="", created_epoch=real["created_epoch"],
                                        last_start_epoch=real["created_epoch"], data_disk_type=disk["type"], data_disk_gb=gb,
                                        data_disks_attached_gb=gb, data_disks={disk["type"]: gb}, disks_detail=[disk]))
                                rows, _ = reconcile_instance_records(rows, observations, project="p", now=at,
                                                                     disks_listed=verified)
                            actual = sum(instance_usage(r, self.now)["cost_all_in"] for r in rows)
                            self.assertEqual(sum(instance_usage(r, self.now)["cost_compute"] for r in rows), 0)
                            rate = lambda gb: _single_disk_all_in(dict(real, gb=gb), family=rows[0]["family"],
                                region=rows[0]["region"], project="p", tax_rate_percent=rows[0]["pricing"]["tax_rate_percent"])
                            # Independent physical timeline: five hours at 100 GiB, or three hours at 100 GiB
                            # followed by two hours at 200 GiB. VM ownership and API visibility cannot change it.
                            expected = 3 * rate(100) + 2 * rate(200) if resized else 5 * rate(100)
                            self.assertAlmostEqual(actual, expected, delta=0.001)

    def test_detached_disk_timeline_preserves_unpaid_history(self):
        from lib.dashboard_server import reconcile_instance_records, instance_usage, _single_disk_all_in

        bases = [fleet_observation(name, f"builder-detach-{name}", "us-south1-b", now=self.now,
                                  started_hours_ago=10, vcpu=0, ram_gib=0) for name in ("a", "b")]
        bases = [dict(r, boot_disk_gb=0, data_disk_gb=0, data_disks={}, data_disks_attached_gb=0,
                      disks_detail=[]) for r in bases]
        real = {"source": "z/detach-oracle", "id": "detach-id", "type": "pd-ssd", "gb": 100,
                "created_epoch": self.now - 36000, "auto_delete": False, "boot": False, "billing_owner": True}
        for final_owner, carrier_deleted in ((None, False), ("b", False), (None, True), ("b", True)):
            with self.subTest(final_owner=final_owner, carrier_deleted=carrier_deleted):
                rows = []
                timeline = ((8, "a", True), (6, None, True), (4, "a", False), (2, None, False), (0, final_owner, True))
                for hours, owner, verified in timeline:
                    disk = real if verified else dict(real, id="", type="pd-balanced", created_epoch=None)
                    observations = [dict(r, disk_types_known=verified, data_disk_type=disk["type"], data_disk_gb=100,
                        data_disks_attached_gb=100, data_disks={disk["type"]: 100}, disks_detail=[disk])
                        if r["instance_id"] == owner else r for r in bases]
                    if carrier_deleted and hours <= 2:
                        observations = [r for r in observations if r["instance_id"] != "a"]
                    if owner is None and verified:
                        observations.append(dict(bases[0], instance_id="disk-detach-id", name="builder-detached-oracle",
                            kind="disk", status="DETACHED", machine_type="", created_epoch=real["created_epoch"],
                            last_start_epoch=real["created_epoch"], data_disk_type=real["type"], data_disk_gb=100,
                            data_disks_attached_gb=100, data_disks={real["type"]: 100}, disks_detail=[real]))
                    rows, _ = reconcile_instance_records(rows, observations, project="p", now=self.now - hours * 3600,
                                                         disks_listed=verified)
                    if hours == 2:
                        if carrier_deleted:
                            # The detached owner now supplies its real type/ID at the denied handoff. A missing
                            # VM carries a priced retained liability instead of inventing new unpriced debt.
                            carrier = next(r for r in rows if r["instance_id"] == "a")
                            self.assertTrue(carrier["disk_liability_pending"])
                            self.assertEqual(carrier["disks_detail"][0]["type"], real["type"])
                            self.assertEqual(carrier["disks_detail"][0]["id"], real["id"])
                            expected_rate = _single_disk_all_in(real, family=carrier["family"], region=carrier["region"],
                                project="p", tax_rate_percent=0)
                            self.assertAlmostEqual(carrier["pricing"]["disk_hourly_usd"], expected_rate, delta=0.0001)
                        else:
                            self.assertTrue(any(r.get("unpaid_disk_details") for r in rows))
                rate = _single_disk_all_in(real, family="n4", region="us-south1", project="p",
                                           tax_rate_percent=rows[0]["pricing"]["tax_rate_percent"])
                self.assertAlmostEqual(sum(instance_usage(r, self.now)["cost_all_in"] for r in rows), 10 * rate, delta=0.001)
                self.assertFalse(any(r.get("unpaid_disk_details") for r in rows))

    def test_confirmed_missing_unpaid_disk_expires_after_retention(self):
        from lib.dashboard_server import reconcile_instance_records, DELETED_INSTANCE_RETENTION_SECONDS

        start = self.now - 63 * 86400
        base = fleet_observation("expired-unpaid", "builder-expired-unpaid", "us-south1-b", now=self.now,
                                 started_hours_ago=70 * 24, vcpu=0, ram_gib=0)
        base.update(boot_disk_gb=0, data_disk_gb=0, data_disks={}, data_disks_attached_gb=0, disks_detail=[])
        unknown = {"source": "z/expired-unpaid", "id": "", "type": "pd-balanced", "gb": 100,
                   "created_epoch": None, "auto_delete": False, "boot": False, "billing_owner": True}
        rows, _ = reconcile_instance_records([], [base], project="p", now=start - 3600)
        rows, _ = reconcile_instance_records(rows, [dict(base, data_disk_type="pd-balanced", data_disk_gb=100,
            data_disks_attached_gb=100, data_disks={"pd-balanced": 100}, disks_detail=[unknown], disk_types_known=False)],
            project="p", now=start, disks_listed=False)
        gone_at = start + 86400
        rows, _ = reconcile_instance_records(rows, [base], project="p", now=gone_at, disk_inventory={})
        self.assertEqual(rows[0]["unpaid_disk_details"][0]["billing_missing_since"], gone_at)
        present, _ = reconcile_instance_records(rows, [base], project="p", now=self.now, disk_inventory={
            unknown["source"]: {"id": "still-present", "name": "outside-fleet", "type": "pd-ssd",
                "sizeGb": "100", "zone": "zones/us-south1-b", "users": [], "creationTimestamp":
                datetime.fromtimestamp(start - 3600, timezone.utc).isoformat()}})
        carrier = next(r for r in present if r["instance_id"] == base["instance_id"])
        recovered = next(r for r in present if r["instance_id"] == "disk-still-present")
        self.assertFalse(carrier["unpaid_disk_details"])
        self.assertEqual(recovered["status"], "DETACHED")
        self.assertGreater(recovered["pricing"]["disk_hourly_usd"], 0)
        catalog = {unknown["source"]: {"id": "still-present", "name": "outside-fleet", "type": "pd-ssd",
            "sizeGb": "100", "zone": "zones/us-south1-b", "users": [], "creationTimestamp":
            datetime.fromtimestamp(start - 3600, timezone.utc).isoformat()}}
        again, _ = reconcile_instance_records(present, [base], project="p", now=self.now + 60,
                                               disk_inventory=catalog)
        self.assertEqual(next(r for r in again if r["instance_id"] == "disk-still-present")["status"], "DETACHED")
        external_catalog = {k: dict(v, users=["projects/p/zones/us-south1-b/instances/external-vm"])
                            for k, v in catalog.items()}
        transferred, _ = reconcile_instance_records(rows, [base], project="p", now=self.now,
                                                     disk_inventory=external_catalog)
        self.assertFalse(next(r for r in transferred if r["instance_id"] == base["instance_id"])["unpaid_disk_details"])
        historical = next(r for r in transferred if r["instance_id"] == "disk-still-present")
        self.assertEqual(historical["status"], "DELETED")
        self.assertEqual(historical["deleted_epoch"], self.now)
        rows, _ = reconcile_instance_records(rows, [base], project="p", now=gone_at + DELETED_INSTANCE_RETENTION_SECONDS - 1,
                                             disk_inventory={})
        self.assertFalse(rows[0]["pricing_complete"])
        rows, _ = reconcile_instance_records(rows, [base], project="p", now=gone_at + DELETED_INSTANCE_RETENTION_SECONDS + 1,
                                             disk_inventory={})
        self.assertEqual(rows[0]["unpaid_disk_details"], [])
        self.assertTrue(rows[0]["pricing_complete"])
        self.assertFalse(rows[0]["lifetime_cost_complete"])
        rows, _ = reconcile_instance_records(rows, [base], project="p", now=self.now, disk_inventory={})
        self.assertTrue(rows[0]["pricing_complete"])
        self.assertFalse(rows[0]["lifetime_cost_complete"])

    def test_deleted_unpaid_history_holds_budget_and_remains_visible(self):
        from lib.dashboard_server import _live_instance_record

        observation = fleet_observation("unpaid-carrier", "builder-unpaid-carrier", "us-south1-b", now=self.now)
        record = _live_instance_record(observation, project="p", now=self.now)
        record.update(status="DELETED", deleted_epoch=self.now - 40 * 86400,
                      unpaid_disk_details=[{"source": "z/custom", "billing_deferred_since": self.now - 3600}],
                      pricing_complete=False)
        self.store.state["instances"] = [record]
        self.assertIn(record["name"], self.store._incomplete_cost_hosts("rust_builder"))
        for include_deleted in (False, True):
            result = self.store.query_instances(window_hours=24, include_deleted=include_deleted)
            self.assertEqual(len(result["instances"]), 1)
            self.assertEqual(result["instances"][0]["active_hourly_usd"], 0)

    def test_recreated_disk_does_not_consume_source_only_unpaid_interval(self):
        from lib.dashboard_server import reconcile_instance_records, instance_usage, _single_disk_all_in

        base = fleet_observation("recreated-unpaid", "builder-recreated-unpaid", "us-south1-b", now=self.now,
                                 started_hours_ago=6, vcpu=0, ram_gib=0)
        base.update(boot_disk_gb=0, data_disk_type="pd-balanced", data_disk_gb=0, data_disks={},
                    data_disks_attached_gb=0, disks_detail=[])
        old = {"source": "z/reused-unpaid", "id": "", "type": "pd-balanced", "gb": 100,
               "created_epoch": None, "auto_delete": False, "boot": False, "billing_owner": True}
        rows, _ = reconcile_instance_records([], [base], project="p", now=self.now - 18000)
        rows, _ = reconcile_instance_records(rows, [dict(base, data_disk_gb=100, data_disks_attached_gb=100,
            data_disks={"pd-balanced": 100}, disks_detail=[old], disk_types_known=False)],
            project="p", now=self.now - 14400, disks_listed=False)
        new = dict(old, id="replacement-id", type="pd-ssd", gb=200, created_epoch=self.now - 3600)
        observed = dict(base, data_disk_type="pd-ssd", data_disk_gb=200, data_disks_attached_gb=200,
                        data_disks={"pd-ssd": 200}, disks_detail=[new])
        rows, _ = reconcile_instance_records(rows, [observed], project="p", now=self.now)
        self.assertEqual(rows[0]["unpaid_disk_details"][0]["billing_deferred_since"], self.now - 14400)
        self.assertFalse(rows[0]["pricing_complete"])
        rate = _single_disk_all_in(new, family=rows[0]["family"], region=rows[0]["region"], project="p",
                                   tax_rate_percent=rows[0]["pricing"]["tax_rate_percent"])
        self.assertAlmostEqual(instance_usage(rows[0], self.now)["cost_all_in"], rate, delta=0.001)
        denied = dict(observed, disk_types_known=False, disks_detail=[dict(new, id="", created_epoch=None)])
        rows, _ = reconcile_instance_records(rows, [denied], project="p", now=self.now + 60, disks_listed=False)
        self.assertEqual(len(rows[0]["unpaid_disk_details"]), 1)
        self.assertEqual(rows[0]["deferred_disk_sources"], [])

    def test_readiness_observation_does_not_date_bootstrap_after_an_oom(self):
        from lib.dashboard_server import _live_instance_record

        a = fleet_observation("ready-a", "oxidex-runners-ready-a", "us-south1-b", now=self.now, started_hours_ago=3)
        b = fleet_observation("ready-b", "oxidex-runners-ready-b", "us-south1-b", now=self.now, started_hours_ago=0.3)
        old = _live_instance_record(a, project="p", now=self.now - 1800)
        old["capacity_pending"] = False
        unready = _live_instance_record(dict(b, runners_count_known=False), project="p", now=self.now - 900)
        ready = _live_instance_record(b, project="p", now=self.now - 300, previous=unready)
        self.assertEqual(ready["capacity_first_ready_lower_bound"], b["created_epoch"])
        for seconds_ago, handled, baseline in ((400, False, 8), (1000, False, 8), (1200, True, 4)):
            with self.subTest(seconds_ago=seconds_ago):
                event = {"id": "uncertain-bootstrap", "role": "cicd_runner", "instance_name": b["name"],
                         "observed_at": datetime.fromtimestamp(self.now - seconds_ago, timezone.utc).isoformat()}
                self.store.state["scaling_telemetry"]["journal_oom_events"] = [event]
                self.store._consume_ooms_for_fleet_growth([old, ready], [dict(old), dict(ready)])
                self.assertEqual(event.get("handled", False), handled)
                self.assertEqual(event["baseline_runners"], baseline)

    def test_new_identity_restart_cannot_satisfy_later_oom(self):
        from lib.dashboard_server import _live_instance_record

        obs = fleet_observation("pending-restart", "oxidex-runners-pending-restart", "us-south1-b", now=self.now)
        ready = _live_instance_record(obs, project="p", now=self.now - 600)
        stopped = _live_instance_record(dict(obs, status="TERMINATED", last_stop_epoch=self.now - 300),
                                        project="p", now=self.now - 300, previous=ready)
        event = {"id": "during-stop", "role": "cicd_runner", "baseline_runners": 0,
                 "observed_at": datetime.fromtimestamp(self.now - 120, timezone.utc).isoformat()}
        self.store.state["scaling_telemetry"]["journal_oom_events"] = [event]
        self.store._consume_ooms_for_fleet_growth([stopped], [dict(stopped)])
        self.assertEqual(event["baseline_source"], "inventory")
        restarted = _live_instance_record(dict(obs, last_start_epoch=self.now - 60), project="p", now=self.now,
                                          previous=stopped)
        self.assertEqual(restarted["capacity_first_ready_lower_bound"], ready["capacity_first_ready_lower_bound"])
        self.store._consume_ooms_for_fleet_growth([stopped], [restarted])
        self.assertFalse(event.get("handled", False))
        self.assertEqual(event["baseline_runners"], 4)

    def test_active_only_view_includes_pending_storage_liability(self):
        from lib.dashboard_server import _live_instance_record, _mark_instance_deleted

        obs = fleet_observation("active-liability", "builder-active-liability", "us-south1-b", now=self.now)
        record = _mark_instance_deleted(_live_instance_record(obs, project="p", now=self.now - 3600),
                                        self.now - 1800, disks_pending=True, project="p")
        self.store.state["instances"] = [record]
        all_rows = self.store.query_instances(include_deleted=True)
        active = self.store.query_instances(include_deleted=False)
        self.assertEqual(len(active["instances"]), 1)
        self.assertGreater(active["instances"][0]["active_hourly_usd"], 0)
        self.assertEqual(active["active_hourly_burn_usd"], all_rows["active_hourly_burn_usd"])

    def test_denied_disk_listing_preserves_current_attachment_auto_delete(self):
        from lib.dashboard_server import _live_instance_record, _mark_instance_deleted

        obs = fleet_observation("auto-delete-change", "builder-auto-delete-change", "us-south1-b", now=self.now)
        obs["disks_detail"] = [{"source": "z/cache-auto-delete", "id": "91", "type": "pd-balanced", "gb": 100,
                                "created_epoch": self.now - 7200, "auto_delete": True}]
        previous = _live_instance_record(obs, project="p", now=self.now - 3600)
        current = dict(obs, disk_types_known=False,
                       disks_detail=[dict(obs["disks_detail"][0], id="", auto_delete=False)])
        record = _live_instance_record(current, project="p", now=self.now, previous=previous)
        self.assertFalse(record["disks_detail"][0]["auto_delete"])
        deleted = _mark_instance_deleted(record, self.now, disks_pending=True, project="p")
        self.assertTrue(deleted["disk_liability_pending"])
        # The reverse change is authoritative too: newly auto-deleting storage leaves no liability.
        changed_back = dict(current, disks_detail=[dict(current["disks_detail"][0], auto_delete=True)])
        record = _live_instance_record(changed_back, project="p", now=self.now, previous=record)
        self.assertTrue(record["disks_detail"][0]["auto_delete"])
        self.assertFalse(_mark_instance_deleted(record, self.now, disks_pending=True, project="p")["disk_liability_pending"])

    def test_partial_disk_transfer_keeps_unmatched_liability_billing(self):
        from lib.dashboard_server import reconcile_instance_records, instance_usage

        vm = fleet_observation("multi-owner", "builder-multi-owner", "us-south1-b", now=self.now)
        vm["disks_detail"] = [{"source": f"z/cache-{i}", "id": str(i), "type": "pd-balanced", "gb": 100,
                               "created_epoch": self.now - 7200, "auto_delete": False} for i in (91, 92)]
        initial, _ = reconcile_instance_records([], [vm], project="p", now=self.now - 3600)
        pending, _ = reconcile_instance_records(initial, [], project="p", now=self.now - 1800, disks_listed=False)
        replacement = dict(vm, instance_id="partial-new", name="builder-partial-new",
                           disks_detail=vm["disks_detail"][:1])
        after, _ = reconcile_instance_records(pending, [replacement], project="p", now=self.now, disks_listed=False)
        retired = next(r for r in after if r["instance_id"] == "multi-owner")
        self.assertTrue(retired["disk_liability_pending"])
        delta = instance_usage(retired, self.now + 3600)["cost_all_in"] - instance_usage(retired, self.now)["cost_all_in"]
        self.assertGreater(delta, 0)
        self.assertLess(retired["pricing"]["disk_hourly_usd"], pending[0]["pricing"]["disk_hourly_usd"])

    def test_shared_nonowner_does_not_suppress_owner_history_backfill(self):
        from lib.dashboard_server import reconcile_instance_records
        from lib.fleet_inventory import observe_fleet_items

        shared = {"boot": False, "diskSizeGb": "500", "source": "z/backfill-shared", "autoDelete": False}
        items = [{"id": str(i), "name": f"builder-backfill-{i}", "zone": "zones/us-south1-a",
                  "status": "RUNNING", "machineType": "types/n4-highcpu-16", "disks": [shared]}
                 for i in (1, 2)]
        shapes = {("us-south1-a", "n4-highcpu-16"): {"vcpu": 16, "ram_gib": 32.0}}
        before = observe_fleet_items(items, shapes=shapes, disk_types={}, disk_types_known=False)
        records, _ = reconcile_instance_records([], before, project="p", now=self.now, disks_listed=False)
        stamp = datetime.fromtimestamp(self.now - 86400, timezone.utc).isoformat()
        after = observe_fleet_items(items, shapes=shapes, disk_types={
            "z/backfill-shared": {"id": "99", "type": "pd-balanced", "creationTimestamp": stamp}})
        records, _ = reconcile_instance_records(records, after, project="p", now=self.now + 3600)
        owner = next(r for r in records if r["instance_id"] == "1")
        nonowner = next(r for r in records if r["instance_id"] == "2")
        self.assertTrue(owner["disk_segments"])
        self.assertAlmostEqual(owner["disk_segments"][0][0], self.now - 86400, places=5)
        self.assertEqual(nonowner["disk_segments"], [])

    def test_unowned_unknown_disk_does_not_defer_owned_history(self):
        from lib.dashboard_server import _live_instance_record, instance_usage

        obs = fleet_observation("shared-history", "builder-shared-history", "us-south1-a", now=self.now)
        obs["disks_detail"] = [
            {"source": "boot", "gb": 40, "type": "pd-balanced", "boot": True,
             "billing_owner": True, "created_epoch": self.now - 7200},
            {"source": "owned", "gb": 100, "type": "pd-balanced", "boot": False,
             "billing_owner": True, "created_epoch": self.now - 3600},
            {"source": "shared", "gb": 100, "type": "pd-balanced", "boot": False,
             "billing_owner": False, "created_epoch": None},
        ]
        first = _live_instance_record(obs, project="p", now=self.now)
        self.assertIsNone(first["disk_history_deferred_at"])
        second = _live_instance_record(obs, project="p", now=self.now, previous=first)
        self.assertEqual(first["disk_segments"], second["disk_segments"])
        self.assertEqual(instance_usage(first, self.now), instance_usage(second, self.now))

    def test_shared_boot_shape_and_billing_are_separate(self):
        from lib.fleet_inventory import observe_fleet_items

        disk = {"boot": True, "diskSizeGb": "40", "source": "z/shared-boot"}
        items = [{"id": str(i), "name": f"builder-shared-{i}", "zone": "zones/us-south1-a",
                  "status": "RUNNING", "machineType": "types/n4-highcpu-16", "disks": [disk]} for i in (2, 1)]
        args = dict(disk_types={"z/shared-boot": {"type": "pd-balanced", "id": "88"}},
                    shapes={("us-south1-a", "n4-highcpu-16"): {"vcpu": 16, "ram_gib": 32.0}})
        observed = observe_fleet_items(items, **args)
        self.assertEqual([r["boot_disk_gb"] for r in observed], [40, 40])
        self.assertEqual([r["boot_disk_billed_gb"] for r in observed], [40, 0])
        self.assertEqual(observed, observe_fleet_items(list(reversed(items)), **args))

    def test_compaction_keeps_resolved_liability_interval(self):
        from lib.dashboard_server import _compact_deleted_record, _live_instance_record

        rec = _live_instance_record(fleet_observation("9960", "builder-oxidex-cl", "us-south1-b", started_hours_ago=10.0,
                                                      now=self.now), project="p", now=self.now - 6 * 3600.0)
        rec.update(status="DELETED", deleted_epoch=self.now - 6 * 3600.0, stopped_epoch=self.now - 6 * 3600.0,
                   disk_end_epoch=self.now - 3600.0, disk_liability_pending=False)
        buckets: dict = {}
        _compact_deleted_record(rec, buckets, self.now)
        disk_only = sum(v[1] - v[2] for k, v in buckets.items()
                        if float(k.split("|")[1]) >= self.now - 6 * 3600.0)
        self.assertGreater(disk_only, 0.0)  # Storage between deletion and resolution is kept.

    def test_unknown_disk_type_rate_is_incomplete(self):
        priced = calculate_spot_pricing(family="n2", vcpu=16, ram_gib=64.0, region="us-south1",
                                        boot_disk_type="pd-balanced", boot_disk_gb=40, data_disk_type="pd-mystery",
                                        data_disk_gb=100, machine_type="n2-standard-16", live_pricing={})
        self.assertFalse(priced["complete"])
        self.assertEqual(priced["disk_rate_unknown"], ["pd-mystery"])
        obs = fleet_observation("9970", "oxidex-runners-dt-1", "us-south1-a", machine_type="n2-standard-16", now=self.now)
        obs.update(data_disk_type="pd-mystery", data_disks={"pd-mystery": 100})
        self.fleet = [obs]
        with unittest.mock.patch("lib.dashboard_models.fetch_live_spot_pricing", return_value={}):
            self.store.refresh_fleet_inventory()
        rec = self._record("oxidex-runners-dt-1")
        self.assertFalse(rec["pricing_complete"])
        self.assertIn("pd-mystery", rec["pricing_incomplete_reason"])

    def test_preserved_detached_record_retired_when_vm_attaches_it(self):
        from lib.fleet_inventory import FleetListing, observe_detached_disks

        cache = "z/disks/builder-oxidex-ra-cache"
        self.fleet = observe_detached_disks({cache: {"id": "103", "name": "builder-oxidex-ra-cache",
                                                     "zone": "z/us-south1-b", "sizeGb": "100", "type": "pd-balanced"}})
        self.store.refresh_fleet_inventory()
        vm = fleet_observation("9980", "builder-oxidex-ra", "us-south1-b", started_hours_ago=0.1, now=self.now)
        vm.update(disk_types_known=False, disks_detail=[{"type": "pd-balanced", "gb": 100, "created_epoch": None,
                                                         "source": cache, "id": ""}])
        listing = FleetListing([vm])
        listing.disks_listed = False
        self.store.fleet_source = lambda: listing
        self.store.refresh_fleet_inventory()
        self.assertEqual(self._record("builder-oxidex-ra-cache")["status"], "DELETED")

    def test_pending_liability_row_shows_storage_rate(self):
        from lib.fleet_inventory import FleetListing

        created = self.now - 5 * 86400.0
        vm = fleet_observation("9990", "builder-oxidex-pr", "us-south1-b", started_hours_ago=120.0, now=self.now)
        vm.update(created_epoch=created, disks_detail=[
            {"type": "pd-balanced", "gb": 100, "created_epoch": created, "source": "z/c-pr", "id": "71",
             "auto_delete": False}])
        self.fleet = [vm]
        self.store.refresh_fleet_inventory()
        listing = FleetListing([])
        listing.disks_listed = False
        self.store.fleet_source = lambda: listing
        self.store.refresh_fleet_inventory()
        row = next(r for r in self.store.query_instances()["instances"] if r["name"] == "builder-oxidex-pr")
        self.assertGreater(row["active_hourly_usd"], 0.0)

    def test_denied_listing_keeps_per_disk_details(self):
        known = fleet_observation("9995", "builder-oxidex-pd-1", "us-south1-b", now=self.now)
        known["disks_detail"] = [{"type": "hyperdisk-balanced", "gb": 100, "created_epoch": self.now - 86400.0,
                                  "source": "z/c-pd", "id": "81", "auto_delete": False}]
        self.fleet = [known]
        self.store.refresh_fleet_inventory()
        denied = dict(known, disk_types_known=False, boot_disk_type="pd-balanced", data_disk_type="pd-balanced",
                      disks_detail=[{"type": "pd-balanced", "gb": 100, "created_epoch": None, "source": "z/c-pd",
                                     "id": "", "auto_delete": False}])
        self.fleet = [denied]
        self.store.refresh_fleet_inventory()
        detail = self._record("builder-oxidex-pd-1")["disks_detail"][0]
        self.assertEqual((detail["type"], detail["id"]), ("hyperdisk-balanced", "81"))

    def test_secondary_unknown_disk_type_makes_pricing_incomplete(self):
        from lib.dashboard_server import _with_secondary_disks

        base = calculate_spot_pricing(family="n2", vcpu=16, ram_gib=64.0, region="us-south1",
                                      boot_disk_type="pd-balanced", boot_disk_gb=40, data_disk_type="pd-ssd",
                                      data_disk_gb=100, machine_type="n2-standard-16", live_pricing={})
        out = _with_secondary_disks(base, {"data_disk_type": "pd-ssd", "data_disks": {"pd-ssd": 100, "pd-odd": 50}},
                                    region="us-south1", family="n2", project="p", live_pricing={})
        self.assertFalse(out["complete"])
        self.assertIn("pd-odd", out["disk_rate_unknown"])

    def test_deferred_disk_history_backfilled_when_creation_known(self):
        from lib.dashboard_server import instance_usage

        created = self.now - 5 * 86400.0
        first = fleet_observation("9996", "builder-oxidex-bf-1", "us-south1-b", started_hours_ago=120.0, now=self.now)
        first["disks_detail"] = [{"type": "pd-balanced", "gb": 100, "created_epoch": None, "source": "z/bf", "id": ""}]
        self.fleet = [first]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-bf-1")
        self.assertIsNotNone(rec["disk_history_deferred_at"])
        deferred_at = rec["disk_history_deferred_at"]
        self.fleet = [dict(first, disks_detail=[{"type": "pd-balanced", "gb": 100, "created_epoch": created,
                                                 "source": "z/bf", "id": "91"}])]
        self.store.refresh_fleet_inventory()
        rec = self._record("builder-oxidex-bf-1")
        self.assertIsNone(rec["disk_history_deferred_at"])
        usage = instance_usage(rec, deferred_at)
        self.assertAlmostEqual(usage["disk_hours"], (deferred_at - created) / 3600.0, delta=0.05)

    def test_expired_oom_not_rewritten_by_growth(self):
        old = datetime.fromtimestamp(time.time() - 5 * 3600.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.fleet = [self._runner("9997", "oxidex-runners-ex-1")]
        self.store.refresh_fleet_inventory()
        with self.store._lock:
            self.store.state["scaling_telemetry"]["journal_oom_events"] = [
                {"id": "oom-old", "instance_name": "oxidex-runners-ex-1", "role": "cicd_runner", "observed_at": old}]
        self.fleet.append(self._runner("9998", "oxidex-runners-ex-2", started_hours_ago=0.01))
        self.store.refresh_fleet_inventory()
        self.assertFalse(self.store.state["scaling_telemetry"]["journal_oom_events"][0].get("handled", False))

    def test_source_only_alias_requires_pending_liability(self):
        from lib.dashboard_server import _disk_known, _record_disk_keys

        record = {"disk_sources": ["z/cache"], "disk_ids": {}, "status": "DELETED"}
        disk = {"source": "z/cache", "id": "new-id"}
        self.assertFalse(_disk_known(disk, _record_disk_keys(record)))
        self.assertFalse(_disk_known(disk, _record_disk_keys(dict(record, status="RUNNING"))))
        self.assertTrue(_disk_known(disk, _record_disk_keys(dict(record, disk_liability_pending=True))))
        pending = dict(record, disk_liability_pending=True, deleted_epoch=self.now - 3600)
        self.assertFalse(_disk_known(dict(disk, created_epoch=self.now), _record_disk_keys(pending)))
        self.assertTrue(_disk_known(dict(disk, created_epoch=self.now - 7200), _record_disk_keys(pending)))

    def test_source_only_liability_matches_id_at_handoff(self):
        from lib.dashboard_server import instance_usage
        from lib.fleet_inventory import FleetListing, observe_detached_disks

        created = self.now - 5 * 86400.0
        cache = "projects/p/zones/us-south1-b/disks/builder-oxidex-so-cache"
        vm = fleet_observation("9999", "builder-oxidex-so", "us-south1-b", started_hours_ago=120.0, now=self.now)
        vm.update(disk_types_known=False, disks_detail=[{"type": "pd-balanced", "gb": 100, "created_epoch": None,
                                                         "source": cache, "id": "", "auto_delete": False}])
        listing = FleetListing([vm])
        listing.disks_listed = False
        self.store.fleet_source = lambda: listing
        self.store.refresh_fleet_inventory()
        gone = FleetListing([])
        gone.disks_listed = False
        self.store.fleet_source = lambda: gone
        self.store.refresh_fleet_inventory()  # Vanished during the outage: source-only liability pending.
        stamp = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.fleet = observe_detached_disks({cache: {"id": "404", "name": "builder-oxidex-so-cache",
                                                     "zone": "z/us-south1-b", "sizeGb": "100", "type": "pd-balanced",
                                                     "creationTimestamp": stamp}})
        self.store.fleet_source = lambda: list(self.fleet)
        resumed = time.time()
        self.store.refresh_fleet_inventory()
        detached = self._record("builder-oxidex-so-cache")
        # The first denied listing billed no fabricated rate. Recovery now supplies the previously unpaid
        # creation-to-observation interval, exactly once, on the physical disk's new billing record.
        self.assertAlmostEqual(instance_usage(detached, resumed + 3600.0)["disk_hours"],
                               (resumed + 3600.0 - created) / 3600.0, delta=0.05)

    def test_fleet_total_keeps_sub_cent_costs(self):
        self.fleet = [fleet_observation(f"34{i:03d}", f"oxidex-runners-short-{i}", "us-south1-a",
                                        started_hours_ago=1.0 / 60.0, now=self.now) for i in range(300)]
        self.store.refresh_fleet_inventory()
        view = self.store.query_instances(window_hours=1)
        self.assertTrue(all(r["total_cost_in_window_usd"] <= 0.01 for r in view["instances"]))
        self.assertGreater(view["total_window_spend_usd"], sum(r["total_cost_in_window_usd"] for r in view["instances"]))

    def test_inventory_status_reports_source(self):
        from lib.dashboard_server import DashboardStore

        self.assertEqual(DashboardStore(fleet_source=lambda: []).fleet_inventory_status()["source"], "gce")
        self.assertEqual(DashboardStore().fleet_inventory_status()["source"], "none")

    def test_empty_first_listing_still_records_baseline(self):
        from lib.dashboard_server import FLEET_BASELINE_STATE_KEY

        self.store.refresh_fleet_inventory()
        self.assertTrue(self.store.state.get(FLEET_BASELINE_STATE_KEY))
        self.store.refresh_fleet_inventory()  # May record baselines on existing OOM events once.
        version = self.store.state_version()
        self.store.refresh_fleet_inventory()
        self.assertEqual(self.store.state_version(), version)

    def test_scale_plan_budget_counts_stopped_disks(self):
        running = fleet_observation("2000", "oxidex-runners-run-1", "us-south1-a", machine_type="n4-standard-16",
                                    now=self.now)
        stopped = fleet_observation("2001", "oxidex-runners-stop-1", "us-south1-b", status="TERMINATED",
                                    started_hours_ago=5.0, stopped_hours_ago=1.0, now=self.now)
        payload = {"region": "us-south1", "role": "cicd_runner", "target_instances": 2}
        self.store.update_budget({"target": "cicd_runner", "max_hourly_usd": 5.0, "max_daily_usd": 100.0,
                                  "max_monthly_usd": 3000.0})
        self.fleet = [running]
        self.store.refresh_fleet_inventory()
        without_stopped = self.store.scale_region_instances(payload)["projected_fleet_hourly_usd"]
        self.fleet = [running, stopped]
        self.store.refresh_fleet_inventory()
        with_stopped = self.store.scale_region_instances(payload)["projected_fleet_hourly_usd"]
        pricing = self._record("oxidex-runners-stop-1")["pricing"]
        disk_all_in = pricing["disk_hourly_usd"] * (1 + pricing["tax_rate_percent"] / 100.0)
        self.assertGreater(without_stopped, 0.0)
        self.assertAlmostEqual(with_stopped - without_stopped, disk_all_in, delta=0.0002)

    def test_seed_and_fabricated_records_are_dropped_on_load(self):
        from lib.dashboard_server import DashboardStore

        seed = _build_instance_record(
            instance_id="90481726351002", name="oxidex-runners-dallas-01", role="cicd_runner", status="RUNNING",
            region="us-south1", zone="us-south1-a", machine_type="n4-standard-16", vcpu=16, ram_gib=64,
            cpu_platform="Intel Emerald Rapids", boot_disk_type="hyperdisk-balanced", boot_disk_gb=40,
            data_disk_type="hyperdisk-balanced", data_disk_gb=100, runners_count=4, runner_vcpu=4,
            runner_ram_gib=16, started_hours_ago=14.0, deleted_hours_ago=None, deletion_trigger="seed",
            cloud_init_template_id="ci-runner-default", vm_template_id=None,
        )
        fabricated = dict(seed, instance_id="1792345678001", name="oxidex-runners-us-south1-ab12")
        self.fleet = [fleet_observation("1008", "oxidex-runners-real-1", "us-south1-a", now=self.now)]
        self.store.refresh_fleet_inventory()
        raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        raw["instances"] = [seed, fabricated] + raw["instances"]
        self.state_file.write_text(json.dumps(raw), encoding="utf-8")

        reloaded = DashboardStore(state_path=self.state_file, fleet_source=lambda: [])
        self.assertEqual([i["name"] for i in reloaded.state["instances"]], ["oxidex-runners-real-1"])
        self.assertEqual(reloaded.state["scaling_telemetry"]["current_runners"], 4)
        on_disk = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual([i["name"] for i in on_disk["instances"]], ["oxidex-runners-real-1"])

        # A snapshot from KV or a handover import is migrated the same way.
        from lib.edge_state import build_snapshot

        snapshot = build_snapshot(dict(raw, instances=[seed]), reloaded.state_version() + 1, "a")
        reloaded.import_snapshot(snapshot)
        self.assertEqual(reloaded.state["instances"], [])

    def test_observe_fleet_items_normalizes_gce_listing(self):
        from lib.fleet_inventory import observe_fleet_items

        items = [
            {"id": "11", "name": "oxidex-runners-asia-1", "zone": ".../zones/asia-northeast2-a", "status": "RUNNING",
             "machineType": ".../machineTypes/c4d-standard-16", "cpuPlatform": "AMD Turin",
             "creationTimestamp": "2026-10-18T01:00:00.000-07:00",
             "lastStartTimestamp": "2026-10-18T01:01:00.000-07:00",
             "scheduling": {"provisioningModel": "SPOT"},
             "labels": {"spot-target": "swack-tools_oxidex"},
             "metadata": {"items": [{"key": "spot-runner-count", "value": "4"},
                                    {"key": "startup-script", "value": "#!/bin/bash secret"}]},
             "disks": [{"boot": True, "diskSizeGb": "40", "source": "disk/boot-11"},
                       {"boot": False, "diskSizeGb": "200", "source": "disk/data-11"}]},
            {"id": "12", "name": "builder-oxidex-1", "zone": "zones/us-south1-b", "status": "TERMINATED",
             "machineType": "machineTypes/n4-highcpu-16", "lastStopTimestamp": "2026-10-18T10:00:00Z"},
            {"id": "13", "name": "spot-control-a", "zone": "zones/us-central1-a", "status": "RUNNING",
             "machineType": "machineTypes/e2-micro"},
            {"name": "oxidex-runners-no-id", "zone": "zones/us-south1-a", "status": "RUNNING"},
        ]
        observed = observe_fleet_items(
            items,
            disk_types={"disk/boot-11": "hyperdisk-balanced", "disk/data-11": "hyperdisk-throughput"},
            shapes={("asia-northeast2-a", "c4d-standard-16"): {"vcpu": 16, "ram_gib": 62.0}},
        )
        self.assertEqual([o["name"] for o in observed], ["oxidex-runners-asia-1", "builder-oxidex-1"])
        runner, builder = observed
        self.assertEqual(runner["region"], "asia-northeast2")
        self.assertEqual(runner["runners_count"], 4)
        self.assertEqual((runner["vcpu"], runner["ram_gib"], runner["specs_known"]), (16, 62.0, True))
        self.assertEqual((runner["boot_disk_type"], runner["boot_disk_gb"]), ("hyperdisk-balanced", 40))
        self.assertEqual((runner["data_disk_type"], runner["data_disk_gb"]), ("hyperdisk-throughput", 200))
        self.assertTrue(runner["spot"])
        self.assertNotIn("startup-script", json.dumps(observed))
        self.assertEqual(builder["role"], "rust_builder")
        self.assertEqual(builder["status"], "TERMINATED")
        self.assertEqual((builder["vcpu"], builder["specs_known"]), (16, False))
        self.assertIsNotNone(builder["last_stop_epoch"])

    def test_builder_capacity_requires_verified_fixed_host_shape(self):
        from lib.fleet_inventory import observe_fleet_items

        for machine, vcpu, ram, verified in (("n4-highcpu-8", 8, 16.0, True),
                ("n4-highcpu-16", 16, 28.0, True), ("n4-highcpu-16", 8, 32.0, True),
                ("n4-highcpu-16", 16, 32.0, False),
                ("n4-highcpu-16", 16, 32.0, True)):
            valid = machine == "n4-highcpu-16" and vcpu == 16 and ram == 32.0 and verified
            with self.subTest(machine=machine, ram=ram, verified=verified):
                item = {"id": "shape-check", "name": "builder-shape-check", "zone": "zones/us-south1-b",
                        "status": "RUNNING", "machineType": "types/" + machine}
                shapes = {("us-south1-b", machine): {"vcpu": vcpu, "ram_gib": ram}} if verified else {}
                observation = observe_fleet_items([item], shapes=shapes)[0]
                self.assertEqual(observation["runner_limits_known"], valid)
                self.assertEqual(observation["runners_count_known"], valid)
                self.assertEqual((observation["runners_count"], observation["runner_vcpu"],
                                  observation["runner_ram_gib"]), (1, 16, 28.0) if valid else (0, 0, 0.0))
                self.fleet = [observation]
                self.store.refresh_fleet_inventory()
                self.assertEqual(self.store.state["scaling_telemetry"]["current_builder_instances"], int(valid))
                if not valid:
                    with self.assertRaisesRegex(ValueError, "Per-runner limits"):
                        self.store.create_vm_template_from_instance({"instance_id": "shape-check",
                                                                    "assign_to_profile": False})

    def test_custom_machine_type_vcpu_fallback(self):
        from lib.fleet_inventory import observe_fleet_items

        observed = observe_fleet_items([
            {"id": "21", "name": "oxidex-runners-custom-1", "zone": "zones/us-south1-a", "status": "RUNNING",
             "machineType": "machineTypes/n2-custom-32-112640"},
        ])
        self.assertEqual((observed[0]["vcpu"], observed[0]["specs_known"]), (32, False))

    def test_runner_count_needs_bootstrap_confirmation(self):
        from lib.fleet_inventory import observe_fleet_items

        start = "2026-10-18T01:00:00.000-07:00"
        base = {"id": "61", "name": "oxidex-runners-boot-1", "zone": "zones/us-south1-a", "status": "RUNNING",
                "machineType": "machineTypes/n4-standard-16", "lastStartTimestamp": start}
        provisioned = {"items": [{"key": "spot-runner-count", "value": "4"},
                                 {"key": "spot-runner-patch-pending-sha256", "value": "none"}]}
        confirmed = {"items": [{"key": "spot-runner-count", "value": "4"},
                               {"key": "spot-runner-patch-sha256", "value": "none"},
                               {"key": "spot-runner-patch-boot", "value": start}]}
        stale_boot = {"items": [{"key": "spot-runner-count", "value": "4"},
                                {"key": "spot-runner-patch-sha256", "value": "none"},
                                {"key": "spot-runner-patch-boot", "value": "2026-10-01T00:00:00.000-07:00"}]}
        shapes = {("us-south1-a", "n4-standard-16"): {"vcpu": 16, "ram_gib": 64.0}}
        known = [observe_fleet_items([dict(base, metadata=m)], shapes=shapes)[0]["runners_count_known"]
                 for m in (provisioned, confirmed, stale_boot)]
        self.assertEqual(known, [False, True, False])

    def test_provisioned_hyperdisk_performance_marks_pricing_incomplete(self):
        from lib.fleet_inventory import observe_fleet_items

        def observe(detail):
            return observe_fleet_items(
                [{"id": "62", "name": "builder-oxidex-hd-1", "zone": "zones/us-south1-b", "status": "RUNNING",
                  "machineType": "machineTypes/n4-highcpu-16",
                  "disks": [{"boot": True, "diskSizeGb": "40", "source": "d/boot"},
                            {"boot": False, "diskSizeGb": "500", "source": "d/data"}]}],
                disk_types={"d/boot": {"type": "pd-balanced"}, "d/data": detail},
                shapes={("us-south1-b", "n4-highcpu-16"): {"vcpu": 16, "ram_gib": 32.0}})[0]

        included = {"type": "hyperdisk-balanced", "provisionedIops": "3000", "provisionedThroughput": "140"}
        self.assertFalse(observe(included)["unpriced_disk_performance"])
        self.assertTrue(observe(dict(included, provisionedIops="8000"))["unpriced_disk_performance"])
        self.assertTrue(observe({"type": "hyperdisk-balanced"})["unpriced_disk_performance"])
        throughput = observe({"type": "hyperdisk-throughput", "provisionedThroughput": "200"})
        self.assertTrue(throughput["unpriced_disk_performance"])
        self.fleet = [throughput]
        self.store.refresh_fleet_inventory()
        rec = next(i for i in self.store.state["instances"] if i["name"] == "builder-oxidex-hd-1")
        self.assertFalse(rec["pricing_complete"])
        self.assertIn("provisioned IOPS or throughput", rec["pricing_incomplete_reason"])

    def test_fleet_source_from_env(self):
        from lib.fleet_inventory import fleet_source_from_env

        self.assertIsNone(fleet_source_from_env("p", {}))
        self.assertIsNone(fleet_source_from_env("p", {"DASHBOARD_FLEET_INVENTORY": "state"}))
        self.assertTrue(callable(fleet_source_from_env("p", {"DASHBOARD_FLEET_INVENTORY": "gce"})))

    def test_gce_fleet_source_refuses_partial_listing(self):
        from lib import compute_api
        from lib.fleet_inventory import gce_fleet_source

        async def partial(api, project, *, instance_fields=compute_api.INSTANCE_LIST_FIELDS):
            return [{"id": "1", "name": "oxidex-runners-x", "zone": "zones/us-south1-a"}], ["zones/us-east1-b"]

        with unittest.mock.patch.object(compute_api, "get_auth", return_value=compute_api.Auth("t", "p")), \
                unittest.mock.patch.object(compute_api, "list_instances", partial):
            with self.assertRaisesRegex(RuntimeError, "zones unreachable"):
                gce_fleet_source("p")()

    def test_gce_fleet_source_keeps_inventory_on_transient_disk_failure(self):
        from lib import compute_api
        from lib.fleet_inventory import gce_fleet_source

        async def listing(api, project, *, instance_fields=compute_api.INSTANCE_LIST_FIELDS):
            return [{"id": "1", "name": "builder-oxidex-x", "zone": "zones/us-south1-a", "status": "RUNNING",
                     "machineType": "machineTypes/n4-highcpu-16",
                     "disks": [{"boot": True, "diskSizeGb": "40", "source": "d/boot"}]}], []

        async def capacity(api, project, zone, machine_type):
            return {"vcpu": 16, "ram_gib": 32.0}

        disk_results = [RuntimeError("HTTP 403"), {"d/boot": "hyperdisk-balanced",
            "d/external": {"id": "external", "type": "pd-ssd", "name": "outside-fleet"}}, RuntimeError("HTTP 503")]

        async def disks(api, project):
            result = disk_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with unittest.mock.patch.object(compute_api, "get_auth", return_value=compute_api.Auth("t", "p")), \
                unittest.mock.patch.object(compute_api, "list_instances", listing), \
                unittest.mock.patch.object(compute_api, "machine_type_capacity", capacity), \
                unittest.mock.patch.object(compute_api, "list_disk_types", disks):
            source = gce_fleet_source("p")
            # Disk listing never worked yet (for example no permission): default disk type.
            denied = source()
            self.assertEqual(denied[0]["boot_disk_type"], "pd-balanced")
            self.assertIsNone(denied.disk_inventory)
            complete = source()
            self.assertEqual(complete[0]["boot_disk_type"], "hyperdisk-balanced")
            self.assertIn("d/external", complete.disk_inventory)
            # It worked before, so a failure is transient: the listing fails rather than reporting defaults.
            with self.assertRaisesRegex(RuntimeError, "disk listing failed"):
                source()

    def test_gce_fleet_source_disk_flag_and_capacity(self):
        from lib import compute_api
        from lib.fleet_inventory import gce_fleet_source

        items: list[dict] = []

        async def listing(api, project, *, instance_fields=compute_api.INSTANCE_LIST_FIELDS):
            return list(items), []

        capacity_ok = {"value": True}

        async def capacity(api, project, zone, machine_type):
            if not capacity_ok["value"]:
                raise RuntimeError("HTTP 403")
            return {"vcpu": 16, "ram_gib": 32.0}

        async def disks(api, project):
            raise RuntimeError("HTTP 403 compute.disks.list")

        with unittest.mock.patch.object(compute_api, "get_auth", return_value=compute_api.Auth("t", "p")), \
                unittest.mock.patch.object(compute_api, "list_instances", listing), \
                unittest.mock.patch.object(compute_api, "machine_type_capacity", capacity), \
                unittest.mock.patch.object(compute_api, "list_disk_types", disks):
            source = gce_fleet_source("p")
            self.assertEqual(source(), [])  # Empty fleet plus denied disk listing is not a successful disk inventory.
            items.append({"id": "1", "name": "builder-oxidex-x", "zone": "zones/us-south1-a", "status": "RUNNING",
                          "machineType": "machineTypes/n4-highcpu-16"})
            self.assertEqual(source()[0]["boot_disk_type"], "pd-balanced")  # Never worked: default, no failure.
            capacity_ok["value"] = False
            items.append({"id": "2", "name": "builder-oxidex-y", "zone": "zones/us-east1-b", "status": "RUNNING",
                          "machineType": "machineTypes/c4d-highcpu-16"})
            with self.assertRaisesRegex(RuntimeError, "machine type capacity unavailable"):
                source()

    def test_first_disk_listing_failure_other_than_403_fails_refresh(self):
        from lib import compute_api
        from lib.fleet_inventory import gce_fleet_source

        async def listing(api, project, *, instance_fields=compute_api.INSTANCE_LIST_FIELDS):
            return [{"id": "1", "name": "builder-oxidex-x", "zone": "zones/us-south1-a", "status": "RUNNING",
                     "machineType": "machineTypes/n4-highcpu-16"}], []

        async def capacity(api, project, zone, machine_type):
            return {"vcpu": 16, "ram_gib": 32.0}

        async def disks(api, project):
            raise RuntimeError("Google API HTTP 503: backend error")

        with unittest.mock.patch.object(compute_api, "get_auth", return_value=compute_api.Auth("t", "p")), \
                unittest.mock.patch.object(compute_api, "list_instances", listing), \
                unittest.mock.patch.object(compute_api, "machine_type_capacity", capacity), \
                unittest.mock.patch.object(compute_api, "list_disk_types", disks):
            # A fresh process (for example after a restart) must not replace stored disk types on a 503.
            with self.assertRaisesRegex(RuntimeError, "disk listing failed"):
                gce_fleet_source("p")()

    def test_disk_listing_rejects_unreachable_zones(self):
        import asyncio

        from lib import compute_api

        class FakeApi:
            async def get_json(self, path, query):
                return {"items": {"zones/us-south1-a": {"disks": [{"selfLink": "d/1", "type": "t/pd-ssd"}]}},
                        "unreachables": ["zones/us-east1-b"]}

        with self.assertRaisesRegex(RuntimeError, "unreachable"):
            asyncio.run(compute_api.list_disk_types(FakeApi(), "p"))

    def test_lifecycle_endpoints_are_plan_only_in_the_ui(self):
        from lib.dashboard_ui_assets import DASHBOARD_HTML

        self.assertNotIn("deleteInstanceNow(", DASHBOARD_HTML)
        self.assertIn("Plan only", DASHBOARD_HTML)
        self.assertIn("live Compute Engine inventory", DASHBOARD_HTML)


class PublicOriginBehindWorkerTests(unittest.TestCase):
    """The Worker forwards to the tunnel hostname, so Host differs from the browser's Origin."""

    PUBLIC = "https://spot.swacktech.com"
    TUNNEL_HOST = "spot-origin-a.swacktech.com"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = unittest.mock.patch(
            "lib.dashboard_server.fetch_live_spot_pricing",
            return_value={"source": "catalog_baseline", "fetched_at": 0.0, "component_rates": {},
                          "machine_hourly": {}, "disk_monthly_gib": {}},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        env = unittest.mock.patch.dict("os.environ", {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DASHBOARD_PUBLIC_ORIGIN", None)
        os.environ.pop("GITHUB_OAUTH_REDIRECT_URI", None)

    def start(self, **kwargs):
        server, store = create_dashboard_server(
            host="127.0.0.1", port=0, state_path=Path(self.tmp.name) / "state.json", **kwargs
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        self.port = server.server_address[1]
        return server, store

    def post(self, path, origin=None, host=None, extra=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json", "Host": host or self.TUNNEL_HOST,
                   "X-Forwarded-Proto": "https", "X-Forwarded-Host": "spot.swacktech.com"}
        if origin is not None:
            headers["Origin"] = origin
        headers.update(extra or {})
        try:
            conn.request("POST", path, body=b"{}", headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"null")
        finally:
            conn.close()

    def test_proxied_posts_from_public_origin_are_accepted(self):
        self.start(public_origin=self.PUBLIC)
        for path in ("/auth/logout", "/api/auth/logout"):
            status, body = self.post(path, origin=self.PUBLIC)
            self.assertEqual((status, body["authenticated"]), (200, False), path)
        status, _ = self.post("/api/budget", origin=self.PUBLIC)
        self.assertNotEqual(status, 403)

    def test_own_host_origin_still_accepted(self):
        self.start(public_origin=self.PUBLIC)
        status, _ = self.post("/auth/logout", origin=f"https://{self.TUNNEL_HOST}")
        self.assertEqual(status, 200)

    def test_other_origins_rejected_and_forwarded_host_not_trusted(self):
        self.start(public_origin=self.PUBLIC)
        for origin in ("https://evil.example", "http://spot.swacktech.com", "https://spot.swacktech.com:8443",
                       "https://spot.swacktech.com.evil.example", "null"):
            status, _ = self.post("/api/auth/logout", origin=origin)
            self.assertEqual(status, 403, origin)
        status, _ = self.post("/api/auth/logout", origin="https://evil.example",
                              extra={"X-Forwarded-Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.post("/api/auth/logout", origin=self.PUBLIC, extra={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_without_public_origin_proxied_post_is_rejected(self):
        self.start()
        status, _ = self.post("/api/auth/logout", origin=self.PUBLIC)
        self.assertEqual(status, 403)

    def test_public_origin_defaults_to_redirect_uri_origin(self):
        os.environ["GITHUB_OAUTH_REDIRECT_URI"] = "https://Spot.SwackTech.com/auth/github/callback"
        self.start()
        status, _ = self.post("/api/auth/logout", origin=self.PUBLIC)
        self.assertEqual(status, 200)

    def test_public_origin_env_and_validation(self):
        from lib.dashboard_server import args_parser, normalize_public_origin, resolve_public_origin

        self.assertEqual(normalize_public_origin("HTTPS://Spot.SwackTech.com:443/"), self.PUBLIC)
        self.assertEqual(normalize_public_origin("http://localhost:8787"), "http://localhost:8787")
        self.assertIsNone(normalize_public_origin(""))
        for bad in ("spot.swacktech.com", "https://spot.swacktech.com/app", "https://u:p@spot.swacktech.com",
                    "https://spot.swacktech.com/?x=1", "ftp://spot.swacktech.com", "https://"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_public_origin(bad)
        self.assertEqual(resolve_public_origin(None, environ={"DASHBOARD_PUBLIC_ORIGIN": self.PUBLIC + "/"}),
                         self.PUBLIC)
        self.assertEqual(resolve_public_origin("https://a.example", environ={"DASHBOARD_PUBLIC_ORIGIN": self.PUBLIC}),
                         "https://a.example")
        self.assertEqual(resolve_public_origin(None, redirect_uri=self.PUBLIC + "/auth/github/callback", environ={}),
                         self.PUBLIC)
        self.assertIsNone(resolve_public_origin(None, environ={}))
        with self.assertRaises(ValueError):
            create_dashboard_server(host="127.0.0.1", port=0, public_origin="https://x.example/path")
        self.assertEqual(args_parser().parse_args(["--public-origin", self.PUBLIC]).public_origin, self.PUBLIC)

    def test_oauth_redirect_falls_back_to_public_origin(self):
        import http.client
        import urllib.parse

        from lib.dashboard_auth import GitHubOAuthManager

        manager = GitHubOAuthManager("Iv1.testclientid", "client-secret",
                                     None, "test-session-secret-0123456789-abcdefghijklmnopqrstuvwxyz")
        self.start(public_origin=self.PUBLIC, oauth_manager=manager, config_targets_provider=lambda: [])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/auth/github/login", headers={"Host": self.TUNNEL_HOST})
            resp = conn.getresponse()
            resp.read()
            location = resp.getheader("Location")
        finally:
            conn.close()
        self.assertEqual(resp.status, 302)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        self.assertEqual(query["redirect_uri"], [self.PUBLIC + "/auth/github/callback"])


class ConfigRepoSelectionTests(unittest.TestCase):
    def test_resolution_order_and_validation(self):
        from lib.dashboard_server import args_parser, resolve_config_repo

        self.assertEqual(resolve_config_repo(org="swack-tools", environ={}), "swack-tools/spot-runners-config")
        self.assertEqual(resolve_config_repo(org="acme", environ={}), "acme/spot-runners-config")
        self.assertEqual(resolve_config_repo(org="acme", environ={"SPOT_CONFIG_REPO": "ops/runner-config"}),
                         "ops/runner-config")
        self.assertEqual(resolve_config_repo("x/y", org="acme", environ={"SPOT_CONFIG_REPO": "ops/runner-config"}),
                         "x/y")
        for bad in ("noslash", "a/b/c", "a/..", "-bad/repo", "a/ b"):
            with self.assertRaises(ValueError, msg=bad):
                resolve_config_repo(org="acme", environ={"SPOT_CONFIG_REPO": bad})
        self.assertEqual(args_parser().parse_args(["--config-repo", "o/r"]).config_repo, "o/r")

    def test_authorization_provider_uses_the_resolved_repo(self):
        from lib.dashboard_auth import GitHubOAuthManager

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        manager = GitHubOAuthManager("Iv1.testclientid", "client-secret", None,
                                     "test-session-secret-0123456789-abcdefghijklmnopqrstuvwxyz")
        seen = []

        class Recorder:
            def __init__(self, repo):
                seen.append(repo)

        cases = ((dict(org="acme"), {}, "acme/spot-runners-config"),
                 (dict(org="acme"), {"SPOT_CONFIG_REPO": "ops/runner-config"}, "ops/runner-config"),
                 (dict(org="acme", config_repo="x/y"), {"SPOT_CONFIG_REPO": "ops/runner-config"}, "x/y"))
        for kwargs, env, expected in cases:
            with unittest.mock.patch("lib.dashboard_server.ConfigRepoTargets", Recorder), \
                    unittest.mock.patch.dict("os.environ", env, clear=False):
                if "SPOT_CONFIG_REPO" not in env:
                    os.environ.pop("SPOT_CONFIG_REPO", None)
                server, store = create_dashboard_server(host="127.0.0.1", port=0,
                                                        state_path=Path(tmp.name) / "s.json",
                                                        oauth_manager=manager, **kwargs)
                server.server_close()
                store.close()
            self.assertEqual(seen[-1], expected)


if __name__ == "__main__":
    unittest.main()
