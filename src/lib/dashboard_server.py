"""HTTP REST API server and persistent state controller for the Spot Fleet &
Rust Remote Builder Web UI Dashboard.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

from .dashboard_models import (
    DEFAULT_RUNNER_CLOUD_INIT,
    DEFAULT_RUNNER_STRICT_CLOUD_INIT,
    DEFAULT_RUST_BUILDER_CLOUD_INIT,
    REGIONS_CATALOG,
    calculate_spot_pricing,
    compute_cpu_benchmark_score,
    disk_category,
    format_gcloud_ssh_command,
    measure_region_latencies,
    normalize_budget,
    parse_cloud_init,
    qualify_instances,
)
from .dashboard_scaling import (
    build_github_oom_deep_link,
    classify_github_ci_failure,
    default_scaling_config,
    evaluate_builder_scaling_decision,
    evaluate_scaling_decision,
    parse_github_oom_deep_link,
)
from .rust_build_telemetry import (
    UNFINISHED_BUILD_STATUSES,
    _finite_float,
    _finite_int,
    _iso_to_epoch,
    classify_ip_and_network,
    enrich_rust_build_record,
)


def _iso_hours_ago(hours_ago: float, base_epoch: float | None = None) -> str:
    now_ts = base_epoch if base_epoch is not None else time.time()
    dt = datetime.fromtimestamp(now_ts - hours_ago * 3600.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_seconds_ago(seconds_ago: float, base_epoch: float | None = None) -> str:
    now_ts = base_epoch if base_epoch is not None else time.time()
    target_ts = now_ts - seconds_ago
    dt = datetime.fromtimestamp(target_ts, tz=timezone.utc)
    if float(seconds_ago).is_integer():
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_instance_record(
    *,
    instance_id: str,
    name: str,
    role: str,
    status: str,
    region: str,
    zone: str,
    machine_type: str,
    vcpu: int,
    ram_gib: float,
    cpu_platform: str,
    boot_disk_type: str,
    boot_disk_gb: int,
    data_disk_type: str,
    data_disk_gb: int,
    runners_count: int,
    runner_vcpu: int,
    runner_ram_gib: float,
    started_hours_ago: float,
    deleted_hours_ago: float | None,
    deletion_trigger: str,
    cloud_init_template_id: str,
    vm_template_id: str | None,
    cpu_utilization_percent: float = 42.0,
    ram_utilization_percent: float = 54.0,
    project: str = "homelab-424523",
    now_epoch: float | None = None,
) -> dict[str, Any]:
    ref_now = now_epoch if now_epoch is not None else time.time()
    family = machine_type.split("-", 1)[0]
    reg_info = next((r for r in REGIONS_CATALOG if r["region"] == region), REGIONS_CATALOG[0])
    tax_rate = float(reg_info.get("tax_rate_default", 8.25))
    pricing = calculate_spot_pricing(
        family=family,
        vcpu=vcpu,
        ram_gib=ram_gib,
        region=region,
        boot_disk_type=boot_disk_type,
        boot_disk_gb=boot_disk_gb,
        data_disk_type=data_disk_type,
        data_disk_gb=data_disk_gb,
        tax_rate_percent=tax_rate,
    )
    benchmark = compute_cpu_benchmark_score(cpu_platform, vcpu, machine_type)
    started_epoch = ref_now - started_hours_ago * 3600.0
    deleted_epoch = (ref_now - deleted_hours_ago * 3600.0) if deleted_hours_ago is not None else None
    runtime_hours = round(
        ((deleted_epoch or ref_now) - started_epoch) / 3600.0, 2
    )
    total_cost_with_disk_tax = round(runtime_hours * pricing["total_hourly_usd"], 2)
    total_cost_compute_only = round(runtime_hours * pricing["compute_hourly_usd"], 2)

    return {
        "instance_id": instance_id,
        "name": name,
        "role": role,
        "status": status,
        "region": region,
        "zone": zone,
        "display_location": reg_info["display_name"],
        "continent": reg_info["continent"],
        "lat": reg_info["lat"],
        "lon": reg_info["lon"],
        "map_x": reg_info["map_x"],
        "map_y": reg_info["map_y"],
        "machine_type": machine_type,
        "family": family,
        "vcpu": vcpu,
        "ram_gib": ram_gib,
        "cpu_platform": cpu_platform,
        "benchmark_score": benchmark,
        "boot_disk_type": boot_disk_type,
        "boot_disk_gb": boot_disk_gb,
        "data_disk_type": data_disk_type,
        "data_disk_gb": data_disk_gb,
        "disk_category": disk_category(data_disk_type),
        "runners_count": runners_count,
        "runner_vcpu": runner_vcpu,
        "runner_ram_gib": runner_ram_gib,
        "workload_summary": (
            f"Currently running {runners_count} GitHub runners, each with {runner_vcpu} vCPU and {runner_ram_gib:g} GB of RAM"
            if role == "cicd_runner"
            else f"Dedicated Rust 1.99 Builder ({runner_vcpu} vCPU / {runner_ram_gib:g} GB container cap)"
        ),
        "pricing": pricing,
        "started_at": _iso_hours_ago(started_hours_ago, ref_now),
        "started_epoch": started_epoch,
        "deleted_at": _iso_hours_ago(deleted_hours_ago, ref_now) if deleted_hours_ago is not None else None,
        "deleted_epoch": deleted_epoch,
        "runtime_hours": runtime_hours,
        "total_cost_usd": total_cost_with_disk_tax,
        "total_cost_compute_only_usd": total_cost_compute_only,
        "deletion_trigger": deletion_trigger,
        "cloud_init_template_id": cloud_init_template_id,
        "vm_template_id": vm_template_id,
        "cpu_utilization_percent": cpu_utilization_percent,
        "ram_utilization_percent": ram_utilization_percent,
        "ssh_command": format_gcloud_ssh_command(name, zone, project),
    }


def default_dashboard_state(project: str = "homelab-424523", org: str = "swack-tools") -> dict[str, Any]:
    """Construct the initial operational state for the Web UI Dashboard."""
    ref_now = time.time()
    regions_state = {r["region"]: True for r in REGIONS_CATALOG}

    cloud_init_templates = [
        {
            "id": "ci-runner-default",
            "name": "Default GitHub Actions Runner (Cloud-Config)",
            "role": "cicd_runner",
            "description": "Standard Ubuntu/Debian Docker + GitHub Actions self-hosted runner cloud-init configuration.",
            "updated_at": _iso_hours_ago(2.0, ref_now),
            "content": DEFAULT_RUNNER_CLOUD_INIT,
        },
        {
            "id": "ci-runner-strict-pool",
            "name": "Strict Pool Heavy32 Runner (Rust 1.99)",
            "role": "cicd_runner",
            "description": "Strict 8-vCPU / 32-GiB isolated runner pool with NVMe Hyperdisk and preinstalled Rust 1.99.",
            "updated_at": _iso_hours_ago(6.0, ref_now),
            "content": DEFAULT_RUNNER_STRICT_CLOUD_INIT,
        },
        {
            "id": "ci-rust-builder-199",
            "name": "Dedicated Rust 1.99 Remote Builder",
            "role": "rust_builder",
            "description": "Zero-runner dedicated Rust 1.99 builder with approved SSH uploader and persistent Cargo/target cache.",
            "updated_at": _iso_hours_ago(4.0, ref_now),
            "content": DEFAULT_RUST_BUILDER_CLOUD_INIT,
        },
    ]

    vm_templates = [
        {
            "id": "tpl-runner-n4-std16",
            "name": "Runner N4-Standard-16 (Hyperdisk NVMe)",
            "role": "cicd_runner",
            "machine_type": "n4-standard-16",
            "vcpu": 16,
            "ram_gib": 64,
            "cpu_platform": "Intel Emerald Rapids",
            "benchmark_score": compute_cpu_benchmark_score("Intel Emerald Rapids", 16, "n4-standard-16"),
            "disk_category": "hyperdrive",
            "boot_disk_type": "hyperdisk-balanced",
            "boot_disk_gb": 40,
            "data_disk_type": "hyperdisk-balanced",
            "data_disk_gb": 100,
            "runners_count": 4,
            "runner_vcpu": 4,
            "runner_ram_gib": 16,
            "cloud_init_template_id": "ci-runner-default",
            "source_instance": "oxidex-runners-dallas-01",
            "created_at": _iso_hours_ago(12.0, ref_now),
        },
        {
            "id": "tpl-runner-za-n2-16",
            "name": "South Africa N2-Standard-16 Runner Template",
            "role": "cicd_runner",
            "machine_type": "n2-standard-16",
            "vcpu": 16,
            "ram_gib": 64,
            "cpu_platform": "Intel Ice Lake",
            "benchmark_score": compute_cpu_benchmark_score("Intel Ice Lake", 16, "n2-standard-16"),
            "disk_category": "normal",
            "boot_disk_type": "pd-balanced",
            "boot_disk_gb": 40,
            "data_disk_type": "pd-ssd",
            "data_disk_gb": 100,
            "runners_count": 4,
            "runner_vcpu": 4,
            "runner_ram_gib": 8,
            "cloud_init_template_id": "ci-runner-default",
            "source_instance": "oxidex-runners-za-jnb-01",
            "created_at": _iso_hours_ago(18.0, ref_now),
        },
        {
            "id": "tpl-builder-c4d-16",
            "name": "Dedicated Rust Builder C4D-HighCPU-16 (AMD Turin)",
            "role": "rust_builder",
            "machine_type": "c4d-highcpu-16",
            "vcpu": 16,
            "ram_gib": 32,
            "cpu_platform": "AMD Turin",
            "benchmark_score": compute_cpu_benchmark_score("AMD Turin", 16, "c4d-highcpu-16"),
            "disk_category": "hyperdrive",
            "boot_disk_type": "hyperdisk-balanced",
            "boot_disk_gb": 40,
            "data_disk_type": "hyperdisk-balanced",
            "data_disk_gb": 100,
            "runners_count": 1,
            "runner_vcpu": 16,
            "runner_ram_gib": 28,
            "cloud_init_template_id": "ci-rust-builder-199",
            "source_instance": "builder-oxidex-turin-01",
            "created_at": _iso_hours_ago(24.0, ref_now),
        },
    ]

    profiles = {
        "cicd_runner": {
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
            "disk_category": "hyperdrive",
            "disk_type": "hyperdisk-balanced",
            "boot_disk_gb": 40,
            "data_disk_gb": 100,
            "use_vm_template": True,
            "vm_template_id": "tpl-runner-n4-std16",
            "cloud_init_template_id": "ci-runner-default",
            "budget": {
                "active_period": "day",
                "max_hourly_usd": 5.00,
                "max_daily_usd": 10.00,
                "max_monthly_usd": 50.00,
                "include_disk_costs": True,
                "include_taxes": True,
                "tax_rate_percent": 8.25,
            },
        },
        "rust_builder": {
            "role": "rust_builder",
            "min_runners": 1,
            "min_instances": 1,
            "max_instances": 4,
            "runner_vcpu": 16,
            "runner_ram_gib": 28,
            "host_overhead_gib": 4,
            "min_instance_vcpu": 16,
            "min_instance_ram_gib": 32,
            "min_benchmark_score": 1400,
            "disk_category": "hyperdrive",
            "disk_type": "hyperdisk-balanced",
            "boot_disk_gb": 40,
            "data_disk_gb": 100,
            "use_vm_template": True,
            "vm_template_id": "tpl-builder-c4d-16",
            "cloud_init_template_id": "ci-rust-builder-199",
            "budget": {
                "active_period": "day",
                "max_hourly_usd": 3.50,
                "max_daily_usd": 12.00,
                "max_monthly_usd": 80.00,
                "include_disk_costs": True,
                "include_taxes": True,
                "tax_rate_percent": 8.25,
            },
        },
    }

    instances = [
        # Active instance in South Africa (matches the user's exact world map example)
        _build_instance_record(
            instance_id="90481726351001",
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
            instance_id="90481726351002",
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
            instance_id="90481726351003",
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
            instance_id="90481726351004",
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
            instance_id="90481726351005",
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
            instance_id="80481726350901",
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
            instance_id="80481726350902",
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
            instance_id="80481726350903",
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
            instance_id="80481726350904",
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

    scaling_telemetry = {
        "current_runners": 10,
        "jobs_ran_past_hour": 14,
        "provisioned_jobs": 7,
        "queued_jobs": 3,
        "instance_cpu_percentages": [64.0, 72.0, 58.0],
        "journal_oom_events": [
            {
                "id": "oom-journal-01",
                "instance_name": "oxidex-runners-dallas-01",
                "zone": "us-south1-a",
                "observed_at": _iso_seconds_ago(420, ref_now),
                "pid": 18492,
                "process": "rustc",
                "constraint": "CONSTRAINT_MEMCG",
                "journal_line": (
                    "kernel: oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),"
                    "cpuset=docker-8f4a21.scope,mems_allowed=0,oom_memcg=/system.slice/docker-8f4a21.scope,"
                    "task_memcg=/system.slice/docker-8f4a21.scope,task=rustc,pid=18492,uid=1001"
                ),
            },
        ],
        "github_oom_runs": [
            {
                "id": "gh-oom-37192272062",
                "repository": "swack-tools/oxidex",
                "workflow_name": "CI",
                "run_id": 37192272062,
                "job_id": 111407847874,
                "job_name": "Verify Generated Tables / catalog join",
                "runner_name": "gha-oxidex-runners-asianortheast2-a-1791098074-2",
                "step_number": 8,
                "line_number": 106,
                "failed_at": "2026-10-04T09:39:54Z",
                "error_excerpt": (
                    "line 14: 15780 Killed python3 tools/exiftool-tables — "
                    "##[error]Process completed with exit code 137."
                ),
                "failure_classification": "OOM",
                "is_oom": True,
                "is_self_hosted_runner": True,
                "badge_text": "OOM Killed (Exit 137 · Self-Hosted Spot Runner)",
                "deep_link": build_github_oom_deep_link(
                    "swack-tools/oxidex", 37192272062, 111407847874, 8, 106
                ),
            },
            {
                "id": "gh-oom-37192994059",
                "repository": "swack-tools/oxidex",
                "workflow_name": "CI",
                "run_id": 37192994059,
                "job_id": 111409747367,
                "job_name": "Verify Generated Tables / tools 5/8",
                "runner_name": "gha-oxidex-runners-asianortheast2-a-1791098074-5",
                "step_number": 7,
                "line_number": 77,
                "failed_at": "2026-10-04T10:01:39Z",
                "error_excerpt": (
                    "test_committed_rules_and_ledger_equal_fresh_native_generation ... "
                    "##[error]Process completed with exit code 137."
                ),
                "failure_classification": "OOM",
                "is_oom": True,
                "is_self_hosted_runner": True,
                "badge_text": "OOM Killed (Exit 137 · Self-Hosted Spot Runner)",
                "deep_link": build_github_oom_deep_link(
                    "swack-tools/oxidex", 37192994059, 111409747367, 7, 77
                ),
            },
            {
                "id": "gh-timeout-38596411473",
                "repository": "Appz4Fun/nzbget",
                "workflow_name": "docs",
                "run_id": 38596411473,
                "job_id": 115964282184,
                "job_name": "deploy (Deploy to GitHub Pages)",
                "runner_name": "GitHub Actions 1000014245",
                "step_number": 2,
                "line_number": 50,
                "failed_at": "2026-10-14T16:25:26Z",
                "error_excerpt": "2026-10-14T16:25:26.2656514Z ##[error]Timeout reached, aborting! (Canceling Pages deployment)",
                "failure_classification": "TIMEOUT",
                "is_oom": False,
                "is_self_hosted_runner": False,
                "badge_text": "Timeout · GitHub-Hosted Runner (Not Ours — Ignored)",
                "deep_link": build_github_oom_deep_link(
                    "Appz4Fun/nzbget", 38596411473, 115964282184, 2, 50
                ),
            },
        ],
    }

    raw_rust_builds = [
        {
            "build_id": "rb-20261015-001",
            "project_name": "oxidex",
            "profile": "release",
            "target_triple": "x86_64-unknown-linux-gnu",
            "commit_sha": "af7c14f8cb2c",
            "status": "DELIVERED",
            "requested_at": _iso_seconds_ago(920, ref_now),
            "finished_at": _iso_seconds_ago(818, ref_now),
            "delivered_at": _iso_seconds_ago(806, ref_now),
            "cache_hits": 412,
            "cache_misses": 18,
            "total_crates": 430,
            "instance_name": "builder-oxidex-turin-01",
            "zone": "us-south1-b",
            "machine_type": "c4d-highcpu-16",
            "vcpu": 16,
            "cpu_platform": "AMD Turin",
            "origin_ip": "185.159.157.42",
            "origin_hostname": "node-ch-14.protonvpn.net",
            "peak_ram_gib": 21.8,
            "container_ram_cap_gib": 28.0,
            "cpu_time_seconds": 1184.5,
        },
        {
            "build_id": "rb-20261015-002",
            "project_name": "oxidex-cli",
            "profile": "release",
            "target_triple": "x86_64-unknown-linux-musl",
            "commit_sha": "ce4af62b32cc",
            "status": "DELIVERED",
            "requested_at": _iso_seconds_ago(2450, ref_now),
            "finished_at": _iso_seconds_ago(2290, ref_now),
            "delivered_at": _iso_seconds_ago(2274, ref_now),
            "cache_hits": 295,
            "cache_misses": 94,
            "total_crates": 389,
            "instance_name": "builder-oxidex-eu-01",
            "zone": "europe-west4-a",
            "machine_type": "n4-highcpu-16",
            "vcpu": 16,
            "cpu_platform": "Intel Emerald Rapids",
            "origin_ip": "185.220.101.34",
            "origin_hostname": "tor-exit-101.zwiebelfreunde.de",
            "peak_ram_gib": 24.6,
            "container_ram_cap_gib": 28.0,
            "cpu_time_seconds": 1892.0,
        },
        {
            "build_id": "rb-20261015-003",
            "project_name": "oxidex",
            "profile": "debug",
            "target_triple": "x86_64-unknown-linux-gnu",
            "commit_sha": "f20f9f9b780f",
            "status": "DELIVERED",
            "requested_at": _iso_seconds_ago(4800, ref_now),
            "finished_at": _iso_seconds_ago(4736, ref_now),
            "delivered_at": _iso_seconds_ago(4728, ref_now),
            "cache_hits": 424,
            "cache_misses": 6,
            "total_crates": 430,
            "instance_name": "builder-oxidex-turin-01",
            "zone": "us-south1-b",
            "machine_type": "c4d-highcpu-16",
            "vcpu": 16,
            "cpu_platform": "AMD Turin",
            "origin_ip": "73.162.44.19",
            "origin_hostname": "m4air-workstation.local",
            "peak_ram_gib": 14.2,
            "container_ram_cap_gib": 28.0,
            "cpu_time_seconds": 542.0,
        },
        {
            "build_id": "rb-20261015-004",
            "project_name": "nzbget-rust-ffi",
            "profile": "release",
            "target_triple": "aarch64-apple-darwin",
            "commit_sha": "e3c84b8190aa",
            "status": "DELIVERED",
            "requested_at": _iso_seconds_ago(7600, ref_now),
            "finished_at": _iso_seconds_ago(7455, ref_now),
            "delivered_at": _iso_seconds_ago(7441, ref_now),
            "cache_hits": 318,
            "cache_misses": 42,
            "total_crates": 360,
            "instance_name": "builder-oxidex-eu-01",
            "zone": "europe-west4-a",
            "machine_type": "n4-highcpu-16",
            "vcpu": 16,
            "cpu_platform": "Intel Emerald Rapids",
            "origin_ip": "193.138.218.77",
            "origin_hostname": "se-got-wg-004.mullvad.net",
            "peak_ram_gib": 19.7,
            "container_ram_cap_gib": 28.0,
            "cpu_time_seconds": 1510.4,
        },
        {
            "build_id": "rb-20261015-005",
            "project_name": "oxidex-bench",
            "profile": "release",
            "target_triple": "x86_64-unknown-linux-gnu",
            "commit_sha": "3317b1b902ef",
            "status": "DELIVERED",
            "requested_at": _iso_seconds_ago(11200, ref_now),
            "finished_at": _iso_seconds_ago(11010, ref_now),
            "delivered_at": _iso_seconds_ago(10996, ref_now),
            "cache_hits": 260,
            "cache_misses": 170,
            "total_crates": 430,
            "instance_name": "builder-oxidex-turin-01",
            "zone": "us-south1-b",
            "machine_type": "c4d-highcpu-16",
            "vcpu": 16,
            "cpu_platform": "AMD Turin",
            "origin_ip": "24.28.119.82",
            "origin_hostname": "dev-macbook-pro.tx.rr.com",
            "peak_ram_gib": 26.1,
            "container_ram_cap_gib": 28.0,
            "cpu_time_seconds": 2480.0,
        },
    ]

    rust_builds = [enrich_rust_build_record(row, project=project) for row in raw_rust_builds]

    return {
        "schema": "spot-web-dashboard-v1",
        "project": project,
        "org": org,
        "updated_at": _iso_seconds_ago(0, ref_now),
        "global_budget": {
            "active_period": "day",
            "max_hourly_usd": 5.00,
            "max_daily_usd": 10.00,
            "max_monthly_usd": 50.00,
            "include_disk_costs": True,
            "include_taxes": True,
            "tax_rate_percent": 8.25,
        },
        "profiles": profiles,
        "scaling": default_scaling_config(),
        "scaling_telemetry": scaling_telemetry,
        "regions_enabled": regions_state,
        "region_latencies_ms": {r["region"]: r["latency_ms"] for r in REGIONS_CATALOG},
        "cloud_init_templates": cloud_init_templates,
        "vm_templates": vm_templates,
        "instances": instances,
        "rust_builds": rust_builds,
    }


class DashboardStore:
    """Thread-safe controller store backing the Web UI Dashboard API."""

    def __init__(
        self,
        state_path: Path | None = None,
        *,
        project: str = "homelab-424523",
        org: str = "swack-tools",
    ) -> None:
        self._lock = threading.RLock()
        self.state_path = state_path
        self.project = project
        self.org = org
        if state_path and state_path.exists():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                base = default_dashboard_state(project=project, org=org)
                base.update(loaded)
                self.state = base
                self._normalize_project_derived_fields()
                self._normalize_github_ci_runs()
                self._sync_fleet_telemetry()
                self._persist()
            except (OSError, json.JSONDecodeError):
                self.state = default_dashboard_state(project=project, org=org)
                self._sync_fleet_telemetry()
        else:
            self.state = default_dashboard_state(project=project, org=org)
            self._sync_fleet_telemetry()
            self._persist()

    def _normalize_project_derived_fields(self) -> None:
        """Ensure project/org metadata and gcloud SSH commands match the active store configuration."""
        self.state["project"] = self.project
        self.state["org"] = self.org
        for inst in self.state.get("instances", []):
            name = str(inst.get("name", ""))
            zone = str(inst.get("zone", "us-south1-a"))
            if name:
                inst["ssh_command"] = format_gcloud_ssh_command(name, zone, self.project)
        for build in self.state.get("rust_builds", []):
            inst_name = str(build.get("instance_name", ""))
            zone = str(build.get("zone", "us-south1-b"))
            if inst_name:
                build["ssh_command"] = format_gcloud_ssh_command(inst_name, zone, self.project)

    def _resolve_template_runners_per_instance(self) -> int:
        prof = self.state.get("profiles", {}).get("cicd_runner", {})
        if prof.get("use_vm_template") and prof.get("vm_template_id"):
            tpl = next(
                (
                    t
                    for t in self.state.get("vm_templates", [])
                    if t.get("id") == prof.get("vm_template_id")
                    and t.get("role", "cicd_runner") == "cicd_runner"
                ),
                None,
            )
            if tpl and int(tpl.get("runners_count", 0)) > 0:
                return int(tpl["runners_count"])
        inst_vcpu = int(prof.get("min_instance_vcpu", 16))
        inst_ram = float(prof.get("min_instance_ram_gib", 64))
        r_vcpu = max(1, int(prof.get("runner_vcpu", 4)))
        r_ram = max(0.5, float(prof.get("runner_ram_gib", 16)))
        overhead = max(0.0, float(prof.get("host_overhead_gib", 0)))
        usable_ram = max(r_ram, inst_ram - overhead)
        return max(1, min(inst_vcpu // r_vcpu, int(usable_ram // r_ram)))

    def _sync_template_density(self) -> None:
        tpl_runners = self._resolve_template_runners_per_instance()
        telem = self.state.setdefault("scaling_telemetry", {})
        telem["template_runners_per_instance"] = tpl_runners
        self.state.get("scaling", {}).get("cicd_runner", {})["runners_per_instance"] = tpl_runners

    def _sync_rust_build_telemetry(self, now_ts: float | None = None) -> None:
        """Recompute Rust builder load counters (builds_ran_past_hour, active_builds, queued_builds) from build records."""
        ref_ts = float(now_ts) if now_ts is not None else time.time()
        builds = self.state.get("rust_builds", [])
        builds_past_hour = 0
        active_builds = 0
        queued_builds = 0
        for b in builds:
            status = str(b.get("status", "DELIVERED")).strip().upper()
            if status in ("QUEUED", "PENDING", "WAITING"):
                queued_builds += 1
            elif status in ("RUNNING", "COMPILING", "ACTIVE", "IN_PROGRESS", "BUILDING"):
                active_builds += 1
            ts_str = b.get("delivered_at") or b.get("finished_at") or b.get("requested_at")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(str(ts_str).strip().replace("Z", "+00:00"))
                    age_sec = ref_ts - dt.timestamp()
                    if 0.0 <= age_sec <= 3600.0:
                        builds_past_hour += 1
                except ValueError:
                    # Safe to ignore unparseable timestamps in legacy or partial build records.
                    continue
        telem = self.state.setdefault("scaling_telemetry", {})
        telem["builds_ran_past_hour"] = builds_past_hour
        telem["active_builds"] = active_builds
        telem["queued_builds"] = queued_builds

    def _validate_vm_template_compatibility(
        self,
        vm_tpl: dict[str, Any] | None,
        profile: dict[str, Any],
        role: str,
    ) -> int:
        """Validate that a VM template satisfies the workload profile's role, vCPU, RAM, benchmark, disk, and unit density."""
        if vm_tpl is None or vm_tpl.get("role", role) != role:
            tpl_id = profile.get("vm_template_id") if vm_tpl is None else vm_tpl.get("id")
            raise ValueError(f"VM template '{tpl_id}' does not belong to role '{role}'")
        tpl_name = str(vm_tpl.get("name") or vm_tpl.get("id") or "template")
        tpl_vcpu = int(vm_tpl.get("vcpu", 0))
        tpl_ram = float(vm_tpl.get("ram_gib", 0.0))
        unit_vcpu = max(1, int(profile.get("runner_vcpu", 4 if role == "cicd_runner" else 16)))
        unit_ram = max(1.0, float(profile.get("runner_ram_gib", 16.0 if role == "cicd_runner" else 28.0)))
        overhead = max(0.0, float(profile.get("host_overhead_gib", 0.0)))
        min_inst_vcpu = max(unit_vcpu, int(profile.get("min_instance_vcpu", unit_vcpu)))
        min_inst_ram = max(unit_ram + overhead, float(profile.get("min_instance_ram_gib", unit_ram + overhead)))
        if tpl_vcpu < min_inst_vcpu or tpl_ram < min_inst_ram:
            raise ValueError(
                f"VM template '{tpl_name}' ({tpl_vcpu} vCPU / {tpl_ram} GiB) does not satisfy "
                f"profile sizing requirements ({min_inst_vcpu} vCPU / {min_inst_ram} GiB)"
            )
        min_bench = max(0, int(profile.get("min_benchmark_score", 0)))
        tpl_bench = int(vm_tpl.get("benchmark_score", 0))
        if tpl_bench < min_bench:
            raise ValueError(
                f"VM template '{tpl_name}' benchmark ({tpl_bench}) is below profile minimum ({min_bench})"
            )
        disk_mode = str(profile.get("disk_category", "any")).lower()
        tpl_boot_disk = str(vm_tpl.get("boot_disk_type", "")).strip()
        if tpl_boot_disk == "hyperdisk-throughput":
            raise ValueError(
                f"VM template '{tpl_name}' boot disk type 'hyperdisk-throughput' is not supported as a boot disk"
            )
        tpl_disk_cat = str(
            vm_tpl.get("disk_category") or disk_category(str(vm_tpl.get("data_disk_type", "pd-ssd")))
        ).lower()
        if disk_mode in ("hyperdrive", "normal") and tpl_disk_cat != disk_mode:
            raise ValueError(
                f"VM template '{tpl_name}' disk category '{tpl_disk_cat}' does not satisfy profile '{disk_mode}'"
            )
        req_disk_gb = int(profile.get("data_disk_gb", 0))
        if int(vm_tpl.get("data_disk_gb", 0)) < req_disk_gb:
            raise ValueError(
                f"VM template '{tpl_name}' data disk ({vm_tpl.get('data_disk_gb')} GB) is smaller than profile minimum ({req_disk_gb} GB)"
            )
        if role == "cicd_runner":
            units = min(tpl_vcpu // unit_vcpu, int(max(0.0, tpl_ram - overhead) // unit_ram))
            min_runners = max(1, int(profile.get("min_runners", 4)))
            max_instances = max(1, int(profile.get("max_instances", 12)))
            if units < 1 or ((min_runners + units - 1) // units) > max_instances:
                raise ValueError(
                    f"VM template '{tpl_name}' ({units} runners/VM) cannot satisfy min_runners ({min_runners}) within max_instances ({max_instances})"
                )
            return units
        return 1

    def _sync_fleet_telemetry(self) -> None:
        """Synchronize scaling telemetry counters and CPU percentages with active fleet instances."""
        running_runners = [
            inst
            for inst in self.state.get("instances", [])
            if inst.get("status") == "RUNNING" and inst.get("role") == "cicd_runner"
        ]
        running_builders = [
            inst
            for inst in self.state.get("instances", [])
            if inst.get("status") == "RUNNING" and inst.get("role") == "rust_builder"
        ]
        telem = self.state.setdefault("scaling_telemetry", {})
        telem["current_runners"] = sum(int(inst.get("runners_count", 0)) for inst in running_runners)
        telem["current_instances"] = len(running_runners)
        telem["instance_cpu_percentages"] = [
            float(inst.get("cpu_utilization_percent", 0.0)) for inst in running_runners
        ]
        telem["current_builder_instances"] = len(running_builders)
        telem["builder_cpu_percentages"] = [
            float(inst.get("cpu_utilization_percent", 0.0)) for inst in running_builders
        ]
        raw_journal_ooms = telem.get("journal_oom_events", [])
        if isinstance(raw_journal_ooms, list):
            telem["builder_journal_oom_events"] = [
                evt
                for evt in raw_journal_ooms
                if isinstance(evt, dict)
                and (
                    evt.get("role") == "rust_builder"
                    or str(evt.get("instance_name", "")).startswith("builder-")
                )
            ]
        self._sync_rust_build_telemetry()
        self._sync_template_density()

    @staticmethod
    def _instance_hourly_for_budget(
        pricing: dict[str, Any],
        *,
        include_disk: bool,
        include_tax: bool,
        fallback_hourly: float = 0.16,
    ) -> float:
        """Compute hourly USD for a single instance under the configured disk and tax flags."""
        compute_hr = float(pricing.get("compute_hourly_usd", fallback_hourly))
        if not include_disk:
            if include_tax:
                tax_pct = float(pricing.get("tax_rate_percent", 0.0))
                return round(compute_hr * (1.0 + tax_pct / 100.0), 4)
            return compute_hr
        if include_tax:
            return float(pricing.get("total_hourly_usd", fallback_hourly))
        return round(
            compute_hr + float(pricing.get("disk_hourly_usd", 0.0)),
            4,
        )

    def _resolve_scaling_unit_hourly_usd(
        self,
        role: str,
        qual: dict[str, Any] | None = None,
    ) -> float:
        """Resolve per-instance hourly USD for scaling evaluation matching active template or density."""
        prof = self.state.get("profiles", {}).get(role, {})
        pol = self.state.get("scaling", {}).get(role, {})
        budget = prof.get("budget", {})
        include_disk = bool(
            pol.get("include_disk_in_scale_budget", budget.get("include_disk_costs", True))
        )
        include_tax = bool(budget.get("include_taxes", False))
        target_units = (
            max(
                1,
                int(
                    pol.get("runners_per_instance")
                    or self.state.get("scaling_telemetry", {}).get("template_runners_per_instance")
                    or 4
                ),
            )
            if role == "cicd_runner"
            else 1
        )

        def _offer_hourly(offer: dict[str, Any]) -> float:
            pricing = offer.get("pricing", {})
            return self._instance_hourly_for_budget(
                pricing,
                include_disk=include_disk,
                include_tax=include_tax,
                fallback_hourly=float(offer.get("evaluated_hourly_usd", 0.16)),
            )

        if qual is None:
            qual = qualify_instances(
                prof,
                self.state.get("regions_enabled", {}),
                self.project,
                region_latencies_ms=self.state.get("region_latencies_ms"),
            )

        spot_offers = qual.get("spot_price_qualifying", [])
        hw_offers = qual.get("hardware_qualifying", [])

        if prof.get("use_vm_template") and prof.get("vm_template_id"):
            vm_tpl = next(
                (
                    t
                    for t in self.state.get("vm_templates", [])
                    if t.get("id") == prof.get("vm_template_id") and t.get("role", role) == role
                ),
                None,
            )
            if vm_tpl and (
                role != "cicd_runner" or int(vm_tpl.get("runners_count", 0)) == target_units
            ):
                tpl_machine = vm_tpl.get("machine_type")
                matching_spot = [o for o in spot_offers if o.get("machine_type") == tpl_machine]
                if matching_spot:
                    return min(_offer_hourly(o) for o in matching_spot)
                matching_hw = [o for o in hw_offers if o.get("machine_type") == tpl_machine]
                if matching_hw:
                    return min(_offer_hourly(o) for o in matching_hw)
                family = str(tpl_machine or "n2-standard-16").split("-", 1)[0]
                enabled_regs = [
                    r
                    for r in REGIONS_CATALOG
                    if self.state.get("regions_enabled", {}).get(r["region"], True)
                    and family in r.get("families", [])
                ] or [REGIONS_CATALOG[0]]
                tpl_rates: list[float] = []
                for reg_info in enabled_regs:
                    pricing = calculate_spot_pricing(
                        family=family,
                        vcpu=int(vm_tpl.get("vcpu", 16)),
                        ram_gib=float(vm_tpl.get("ram_gib", 64)),
                        region=reg_info["region"],
                        boot_disk_type=str(vm_tpl.get("boot_disk_type", "hyperdisk-balanced")),
                        boot_disk_gb=int(vm_tpl.get("boot_disk_gb", 40)),
                        data_disk_type=str(vm_tpl.get("data_disk_type", "hyperdisk-balanced")),
                        data_disk_gb=int(vm_tpl.get("data_disk_gb", 100)),
                        tax_rate_percent=float(reg_info.get("tax_rate_default", 8.25)),
                    )
                    tpl_rates.append(_offer_hourly({"pricing": pricing}))
                if tpl_rates:
                    return min(tpl_rates)

        capable_spot = [
            o for o in spot_offers if int(o.get("units_per_instance", 1)) >= target_units
        ]
        if capable_spot:
            return min(_offer_hourly(o) for o in capable_spot)

        capable_hw = [
            o for o in hw_offers if int(o.get("units_per_instance", 1)) >= target_units
        ]
        if capable_hw:
            return min(_offer_hourly(o) for o in capable_hw)

        if spot_offers:
            best = spot_offers[0]
            best_units = max(1, int(best.get("units_per_instance", 1)))
            return round(_offer_hourly(best) * (target_units / best_units), 4)

        return 0.16 if role == "cicd_runner" else 0.34

    def _normalize_github_ci_runs(self) -> None:
        runs = self.state.get("scaling_telemetry", {}).get("github_oom_runs", [])
        for r in runs:
            if int(r.get("run_id", 0)) == 38601924810:
                r["id"] = "gh-oom-37192272062"
                r["repository"] = "swack-tools/oxidex"
                r["workflow_name"] = "CI"
                r["run_id"] = 37192272062
                r["job_id"] = 111407847874
                r["job_name"] = "Verify Generated Tables / catalog join"
                r["runner_name"] = "gha-oxidex-runners-asianortheast2-a-1791098074-2"
                r["step_number"] = 8
                r["line_number"] = 106
                r["failed_at"] = "2026-10-04T09:39:54Z"
                r["error_excerpt"] = (
                    "line 14: 15780 Killed python3 tools/exiftool-tables — "
                    "##[error]Process completed with exit code 137."
                )
                r["deep_link"] = build_github_oom_deep_link(
                    "swack-tools/oxidex", 37192272062, 111407847874, 8, 106
                )
            if int(r.get("run_id", 0)) == 38596411473 and int(r.get("job_id", 0)) == 115964282184:
                if "nzbget-core" in str(r.get("error_excerpt", "")):
                    r["id"] = "gh-timeout-38596411473"
                    r["workflow_name"] = "docs"
                    r["job_name"] = "deploy (Deploy to GitHub Pages)"
                    r["runner_name"] = "GitHub Actions 1000014245"
                    r["error_excerpt"] = (
                        "2026-10-14T16:25:26.2656514Z ##[error]Timeout reached, aborting! (Canceling Pages deployment)"
                    )
                    r.pop("is_oom", None)
            cls_info = classify_github_ci_failure(
                str(r.get("error_excerpt", "")),
                runner_name=r.get("runner_name"),
            )
            r["badge_text"] = cls_info["badge_text"]
            r["failure_classification"] = cls_info["failure_classification"]
            r["is_oom"] = cls_info["is_oom"]
            r["is_self_hosted_runner"] = cls_info["is_self_hosted_runner"]

    def _persist(self) -> None:
        new_updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        candidate_state = {**self.state, "updated_at": new_updated_at}
        serialized = json.dumps(candidate_state, indent=2, allow_nan=False)
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    os.chmod(tmp, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(serialized)
                    fh.flush()
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    # Best-effort cleanup of the temporary state file when writing fails.
                    pass
                raise
            tmp.replace(self.state_path)
            try:
                os.chmod(self.state_path, 0o600)
            except OSError:
                # Filesystems that do not support POSIX chmod keep the mode set at creation.
                pass
        self.state["updated_at"] = new_updated_at

    def get_overview(
        self,
        *,
        include_deleted: bool = True,
        window_hours: float = 24.0,
        include_disk_and_tax: bool = True,
        sort_by: str = "total_cost_in_window_usd",
        sort_dir: str = "desc",
        search_filter: str = "",
        role_filter: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            latencies = self.state.get("region_latencies_ms")
            cicd_qual = qualify_instances(
                self.state["profiles"]["cicd_runner"],
                self.state["regions_enabled"],
                self.project,
                region_latencies_ms=latencies,
            )
            builder_qual = qualify_instances(
                self.state["profiles"]["rust_builder"],
                self.state["regions_enabled"],
                self.project,
                region_latencies_ms=latencies,
            )
            scaling_eval = evaluate_scaling_decision(
                self.state["scaling"]["cicd_runner"],
                self.state["scaling_telemetry"],
                per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd(
                    "cicd_runner", cicd_qual
                ),
            )
            builder_scaling_eval = evaluate_builder_scaling_decision(
                self.state["scaling"]["rust_builder"],
                self.state["scaling_telemetry"],
                per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd(
                    "rust_builder", builder_qual
                ),
            )
            instances_view = self.query_instances(
                include_deleted=include_deleted,
                window_hours=window_hours,
                include_disk_and_tax=include_disk_and_tax,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search_filter=search_filter,
                role_filter=role_filter,
            )
            fleet_summary = (
                instances_view
                if (not search_filter and not role_filter)
                else self.query_instances(
                    include_deleted=include_deleted,
                    window_hours=window_hours,
                    include_disk_and_tax=include_disk_and_tax,
                    sort_by=sort_by,
                    sort_dir=sort_dir,
                    search_filter="",
                    role_filter="",
                )
            )
            regions_list = []
            for reg in REGIONS_CATALOG:
                r_code = reg["region"]
                regions_list.append(
                    {
                        **reg,
                        "latency_ms": self.state["region_latencies_ms"].get(r_code, reg["latency_ms"]),
                        "enabled": bool(self.state["regions_enabled"].get(r_code, True)),
                        "running_instances": [
                            inst
                            for inst in self.state["instances"]
                            if inst["region"] == r_code and inst["status"] == "RUNNING"
                        ],
                    }
                )

            enriched_templates = [
                {**tpl, "parsed": parse_cloud_init(tpl["content"])}
                for tpl in self.state["cloud_init_templates"]
            ]

            return {
                "project": self.project,
                "org": self.org,
                "updated_at": self.state["updated_at"],
                "global_budget": self.state["global_budget"],
                "profiles": self.state["profiles"],
                "qualifications": {
                    "cicd_runner": cicd_qual,
                    "rust_builder": builder_qual,
                },
                "scaling": self.state["scaling"],
                "scaling_telemetry": self.state["scaling_telemetry"],
                "scaling_evaluation": scaling_eval,
                "scaling_evaluations": {
                    "cicd_runner": scaling_eval,
                    "rust_builder": builder_scaling_eval,
                },
                "regions": regions_list,
                "cloud_init_templates": enriched_templates,
                "vm_templates": self.state["vm_templates"],
                "instances_view": instances_view,
                "fleet_summary": fleet_summary,
                "rust_builds": self.get_rust_builds_summary(),
            }

    def query_instances(
        self,
        *,
        include_deleted: bool = True,
        window_hours: float = 24.0,
        include_disk_and_tax: bool = True,
        sort_by: str = "total_cost_in_window_usd",
        sort_dir: str = "desc",
        search_filter: str = "",
        role_filter: str = "",
    ) -> dict[str, Any]:
        """Filter, compute window-scoped costs, and sort running & deleted instances."""
        with self._lock:
            now_ts = time.time()
            window_hours = max(0.5, _finite_float(window_hours, "window_hours", min_val=0.0))
            window_start_ts = now_ts - window_hours * 3600.0
            q = (search_filter or "").strip().lower()
            r_filter = (role_filter or "").strip().lower()

            rows: list[dict[str, Any]] = []
            for inst in self.state["instances"]:
                is_deleted = inst["status"] != "RUNNING"
                if is_deleted and not include_deleted:
                    continue
                deleted_ts = inst.get("deleted_epoch")
                started_ts = float(inst.get("started_epoch", now_ts - 3600.0))
                end_ts = float(deleted_ts) if (is_deleted and deleted_ts is not None) else now_ts

                # Exclude deleted instances whose deletion occurred before the selected window
                if is_deleted and end_ts < window_start_ts:
                    continue

                if r_filter and r_filter != "all" and inst["role"].lower() != r_filter:
                    continue

                overlap_start = max(started_ts, window_start_ts)
                overlap_seconds = max(0.0, end_ts - overlap_start)
                hours_in_window = round(overlap_seconds / 3600.0, 2)
                live_runtime_hours = (
                    round(max(0.0, (now_ts - started_ts) / 3600.0), 2)
                    if not is_deleted
                    else float(inst["runtime_hours"])
                )

                pricing = inst["pricing"]
                hourly_rate = (
                    pricing["total_hourly_usd"]
                    if include_disk_and_tax
                    else pricing["compute_hourly_usd"]
                )
                daily_rate = (
                    pricing["total_daily_usd"]
                    if include_disk_and_tax
                    else pricing["compute_daily_usd"]
                )
                monthly_rate = (
                    pricing["total_monthly_usd"]
                    if include_disk_and_tax
                    else pricing["compute_monthly_usd"]
                )
                total_cost_in_window = round(hours_in_window * hourly_rate, 2)
                lifetime_cost = round(live_runtime_hours * hourly_rate, 2)
                live_total_cost_usd = round(live_runtime_hours * pricing["total_hourly_usd"], 2)
                live_total_cost_compute_only_usd = round(
                    live_runtime_hours * pricing["compute_hourly_usd"], 2
                )

                row = {
                    **inst,
                    "runtime_hours": live_runtime_hours,
                    "total_cost_usd": live_total_cost_usd,
                    "total_cost_compute_only_usd": live_total_cost_compute_only_usd,
                    "active_hourly_usd": hourly_rate,
                    "active_daily_usd": daily_rate,
                    "active_monthly_usd": monthly_rate,
                    "hours_in_window": hours_in_window,
                    "total_cost_in_window_usd": total_cost_in_window,
                    "lifetime_cost_selected_mode_usd": lifetime_cost,
                }

                if q:
                    haystack = " ".join(
                        str(row.get(k, ""))
                        for k in (
                            "name",
                            "region",
                            "zone",
                            "display_location",
                            "machine_type",
                            "cpu_platform",
                            "role",
                            "status",
                            "deletion_trigger",
                            "workload_summary",
                        )
                    ).lower()
                    if q not in haystack:
                        continue

                rows.append(row)

            reverse = str(sort_dir).lower() != "asc"

            def _sort_key(item: dict[str, Any]) -> Any:
                val = item.get(sort_by)
                if val is None:
                    return 0 if reverse else ""
                return val

            try:
                rows.sort(key=_sort_key, reverse=reverse)
            except TypeError:
                rows.sort(key=lambda x: str(x.get(sort_by, "")), reverse=reverse)

            running_rows = [r for r in rows if r["status"] == "RUNNING"]
            running_count = len(running_rows)
            deleted_count = sum(1 for r in rows if r["status"] != "RUNNING")
            running_runner_rows = [r for r in running_rows if r.get("role") == "cicd_runner"]
            running_builder_rows = [r for r in running_rows if r.get("role") == "rust_builder"]
            running_runner_slots = sum(int(r.get("runners_count", 0)) for r in running_runner_rows)
            total_window_spend = round(sum(r["total_cost_in_window_usd"] for r in rows), 2)
            active_hourly_burn = round(
                sum(r["active_hourly_usd"] for r in running_rows), 4
            )

            return {
                "window_hours": window_hours,
                "include_deleted": include_deleted,
                "include_disk_and_tax": include_disk_and_tax,
                "sort_by": sort_by,
                "sort_dir": sort_dir,
                "running_count": running_count,
                "deleted_count": deleted_count,
                "running_runner_vms": len(running_runner_rows),
                "running_runner_slots": running_runner_slots,
                "running_builder_vms": len(running_builder_rows),
                "total_window_spend_usd": total_window_spend,
                "active_hourly_burn_usd": active_hourly_burn,
                "active_daily_burn_usd": round(active_hourly_burn * 24.0, 2),
                "active_monthly_burn_usd": round(active_hourly_burn * 24.0 * (365.0 / 12.0), 2),
                "instances": rows,
            }

    def update_budget(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update global or workload-specific billing budget on the fly."""
        with self._lock:
            target = str(payload.get("target", "global")).strip()
            if target not in ("global", "cicd_runner", "rust_builder"):
                raise ValueError(f"Unsupported budget target: {target}")
            base_budget = (
                dict(self.state["profiles"][target].get("budget", {}))
                if target in ("cicd_runner", "rust_builder")
                else dict(self.state.get("global_budget", {}))
            )
            normalized = normalize_budget({**base_budget, **payload})
            prev_profiles = copy.deepcopy(self.state["profiles"])
            prev_scaling = copy.deepcopy(self.state["scaling"])
            prev_global = copy.deepcopy(self.state.get("global_budget", {}))
            try:
                if target in ("cicd_runner", "rust_builder"):
                    self.state["profiles"][target]["budget"] = normalized
                    self.state["scaling"][target]["max_scale_up_hourly_usd"] = normalized["max_hourly_usd"]
                    self.state["scaling"][target]["max_scale_up_daily_usd"] = normalized["max_daily_usd"]
                    self.state["scaling"][target]["max_scale_up_monthly_usd"] = normalized["max_monthly_usd"]
                    self.state["scaling"][target]["include_disk_in_scale_budget"] = normalized["include_disk_costs"]
                else:
                    self.state["global_budget"] = normalized
                    for role_key in ("cicd_runner", "rust_builder"):
                        self.state["profiles"][role_key]["budget"] = dict(normalized)
                        self.state["scaling"][role_key]["max_scale_up_hourly_usd"] = normalized["max_hourly_usd"]
                        self.state["scaling"][role_key]["max_scale_up_daily_usd"] = normalized["max_daily_usd"]
                        self.state["scaling"][role_key]["max_scale_up_monthly_usd"] = normalized["max_monthly_usd"]
                        self.state["scaling"][role_key]["include_disk_in_scale_budget"] = normalized["include_disk_costs"]
                self._persist()
            except Exception:
                self.state["profiles"] = prev_profiles
                self.state["scaling"] = prev_scaling
                self.state["global_budget"] = prev_global
                raise
            return {"status": "ok", "target": target, "budget": normalized}

    def update_profile(self, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update CI/CD runner or Rust builder sizing, benchmark, disk, and template settings."""
        if role not in ("cicd_runner", "rust_builder"):
            raise ValueError(f"Unsupported workload role: {role}")
        with self._lock:
            if payload.get("vm_template_id"):
                vm_tpl = next(
                    (
                        t
                        for t in self.state.get("vm_templates", [])
                        if t.get("id") == payload["vm_template_id"]
                    ),
                    None,
                )
                if vm_tpl is None or vm_tpl.get("role", role) != role:
                    raise ValueError(
                        f"VM template '{payload['vm_template_id']}' does not belong to role '{role}'"
                    )
            if payload.get("cloud_init_template_id"):
                ci_tpl = next(
                    (
                        t
                        for t in self.state.get("cloud_init_templates", [])
                        if t.get("id") == payload["cloud_init_template_id"]
                    ),
                    None,
                )
                if ci_tpl is None or ci_tpl.get("role", role) != role:
                    raise ValueError(
                        f"Cloud-init template '{payload['cloud_init_template_id']}' does not belong to role '{role}'"
                    )
            current = self.state["profiles"][role]
            candidate: dict[str, Any] = {
                **current,
                "budget": dict(current.get("budget", {})),
            }
            int_positive_keys = (
                "min_runners",
                "min_instances",
                "max_instances",
                "runner_vcpu",
                "min_instance_vcpu",
                "min_benchmark_score",
                "boot_disk_gb",
                "data_disk_gb",
            )
            float_positive_keys = (
                "runner_ram_gib",
                "min_instance_ram_gib",
            )
            for key in int_positive_keys:
                if key in payload:
                    val_int = _finite_int(payload[key], key, min_val=1)
                    candidate[key] = val_int
            for key in float_positive_keys:
                if key in payload:
                    val_float = _finite_float(payload[key], key, min_val=None)
                    if val_float <= 0:
                        raise ValueError(f"{key} must be > 0")
                    candidate[key] = val_float
            if "host_overhead_gib" in payload:
                overhead_val = _finite_float(
                    payload["host_overhead_gib"], "host_overhead_gib", min_val=0.0
                )
                candidate["host_overhead_gib"] = overhead_val
            if "disk_category" in payload:
                d_cat = str(payload["disk_category"]).strip().lower()
                if d_cat not in ("any", "hyperdrive", "normal"):
                    raise ValueError(f"Unsupported disk_category: {payload['disk_category']}")
                candidate["disk_category"] = d_cat
            if "disk_type" in payload:
                candidate["disk_type"] = str(payload["disk_type"]).strip()
            if "use_vm_template" in payload:
                candidate["use_vm_template"] = bool(payload["use_vm_template"])
            if "vm_template_id" in payload:
                candidate["vm_template_id"] = payload["vm_template_id"]
            if "cloud_init_template_id" in payload:
                candidate["cloud_init_template_id"] = payload["cloud_init_template_id"]
            if int(candidate.get("max_instances", 8)) < int(candidate.get("min_instances", 1)):
                raise ValueError("max_instances must be >= min_instances")

            normalized_budget: dict[str, Any] | None = None
            if "budget" in payload and isinstance(payload["budget"], dict):
                merged_budget_input = {**candidate.get("budget", {}), **payload["budget"]}
                normalized_budget = normalize_budget(merged_budget_input)
                candidate["budget"] = normalized_budget

            active_tpl: dict[str, Any] | None = None
            validated_tpl_units: int | None = None
            if candidate.get("vm_template_id"):
                active_tpl = next(
                    (
                        t
                        for t in self.state.get("vm_templates", [])
                        if t.get("id") == candidate.get("vm_template_id")
                    ),
                    None,
                )
            if candidate.get("use_vm_template") and candidate.get("vm_template_id"):
                validated_tpl_units = self._validate_vm_template_compatibility(active_tpl, candidate, role)

            qual = qualify_instances(
                candidate,
                self.state["regions_enabled"],
                self.project,
                region_latencies_ms=self.state.get("region_latencies_ms"),
            )

            prev_profiles = copy.deepcopy(self.state["profiles"])
            prev_scaling = copy.deepcopy(self.state["scaling"])
            prev_vm_templates = copy.deepcopy(self.state["vm_templates"])
            prev_telemetry = copy.deepcopy(self.state["scaling_telemetry"])
            try:
                self.state["profiles"][role] = candidate
                if active_tpl is not None:
                    if "cloud_init_template_id" in payload and candidate.get("cloud_init_template_id"):
                        active_tpl["cloud_init_template_id"] = candidate["cloud_init_template_id"]
                    if validated_tpl_units is not None:
                        active_tpl["runner_vcpu"] = int(candidate["runner_vcpu"])
                        active_tpl["runner_ram_gib"] = float(candidate["runner_ram_gib"])
                        active_tpl["runners_count"] = validated_tpl_units
                if normalized_budget is not None:
                    self.state["scaling"][role]["max_scale_up_hourly_usd"] = normalized_budget["max_hourly_usd"]
                    self.state["scaling"][role]["max_scale_up_daily_usd"] = normalized_budget["max_daily_usd"]
                    self.state["scaling"][role]["max_scale_up_monthly_usd"] = normalized_budget["max_monthly_usd"]
                    self.state["scaling"][role]["include_disk_in_scale_budget"] = normalized_budget["include_disk_costs"]
                if role == "cicd_runner":
                    self.state["scaling"]["cicd_runner"]["min_runners"] = int(candidate["min_runners"])
                    self.state["scaling"]["cicd_runner"]["runner_vcpu"] = int(candidate["runner_vcpu"])
                    self.state["scaling"]["cicd_runner"]["runner_ram_gib"] = float(candidate["runner_ram_gib"])
                    self._sync_template_density()
                elif role == "rust_builder":
                    self.state["scaling"]["rust_builder"]["min_instances"] = int(candidate["min_instances"])
                    self.state["scaling"]["rust_builder"]["max_instances"] = int(candidate["max_instances"])
                    self.state["scaling"]["rust_builder"]["container_vcpu"] = int(candidate["runner_vcpu"])
                    self.state["scaling"]["rust_builder"]["builder_vcpu"] = int(candidate["runner_vcpu"])
                    self.state["scaling"]["rust_builder"]["container_ram_gib"] = float(candidate["runner_ram_gib"])
                    self.state["scaling"]["rust_builder"]["builder_ram_gib"] = float(candidate["runner_ram_gib"])
                self._persist()
            except Exception:
                self.state["profiles"] = prev_profiles
                self.state["scaling"] = prev_scaling
                self.state["vm_templates"] = prev_vm_templates
                self.state["scaling_telemetry"] = prev_telemetry
                raise
            return {"status": "ok", "profile": candidate, "qualification": qual}

    def update_scaling(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update scaling rules, triggers, hourly thresholds, CPU scope, and telemetry."""
        with self._lock:
            role = str(payload.get("role", "cicd_runner")).strip()
            if role not in ("cicd_runner", "rust_builder"):
                raise ValueError(f"Unsupported workload role: {role}")
            prev_scaling = copy.deepcopy(self.state["scaling"])
            prev_profiles = copy.deepcopy(self.state["profiles"])
            prev_telemetry = copy.deepcopy(self.state["scaling_telemetry"])
            try:
                if "policy" in payload and isinstance(payload["policy"], dict):
                    self.state["scaling"][role].update(payload["policy"])
                    pol_target = self.state["scaling"][role]
                    prof_budget = self.state["profiles"][role].setdefault("budget", {})
                    if "max_scale_up_hourly_usd" in payload["policy"]:
                        prof_budget["max_hourly_usd"] = float(pol_target["max_scale_up_hourly_usd"])
                    if "max_scale_up_daily_usd" in payload["policy"]:
                        prof_budget["max_daily_usd"] = float(pol_target["max_scale_up_daily_usd"])
                    if "max_scale_up_monthly_usd" in payload["policy"]:
                        prof_budget["max_monthly_usd"] = float(pol_target["max_scale_up_monthly_usd"])
                    if "include_disk_in_scale_budget" in payload["policy"]:
                        prof_budget["include_disk_costs"] = bool(pol_target["include_disk_in_scale_budget"])
                    if role == "cicd_runner":
                        pol = self.state["scaling"]["cicd_runner"]
                        self.state["profiles"]["cicd_runner"]["min_runners"] = int(pol.get("min_runners", 4))
                        self.state["profiles"]["cicd_runner"]["runner_vcpu"] = int(pol.get("runner_vcpu", 4))
                        self.state["profiles"]["cicd_runner"]["runner_ram_gib"] = float(pol.get("runner_ram_gib", 16))
                        if "runners_per_instance" not in payload["policy"]:
                            self._sync_template_density()
                    else:
                        pol = self.state["scaling"]["rust_builder"]
                        if "builder_vcpu" in payload["policy"] and "container_vcpu" not in payload["policy"]:
                            pol["container_vcpu"] = int(pol["builder_vcpu"])
                        elif "container_vcpu" in payload["policy"] and "builder_vcpu" not in payload["policy"]:
                            pol["builder_vcpu"] = int(pol["container_vcpu"])
                        if "builder_ram_gib" in payload["policy"] and "container_ram_gib" not in payload["policy"]:
                            pol["container_ram_gib"] = float(pol["builder_ram_gib"])
                        elif "container_ram_gib" in payload["policy"] and "builder_ram_gib" not in payload["policy"]:
                            pol["builder_ram_gib"] = float(pol["container_ram_gib"])
                        self.state["profiles"]["rust_builder"]["min_instances"] = int(pol.get("min_instances", 1))
                        self.state["profiles"]["rust_builder"]["max_instances"] = int(pol.get("max_instances", 4))
                        self.state["profiles"]["rust_builder"]["runner_vcpu"] = int(
                            pol.get("container_vcpu", pol.get("builder_vcpu", 16))
                        )
                        self.state["profiles"]["rust_builder"]["runner_ram_gib"] = float(
                            pol.get("container_ram_gib", pol.get("builder_ram_gib", 28))
                        )
                if "telemetry" in payload and isinstance(payload["telemetry"], dict):
                    if "current_runners" in payload["telemetry"] and "current_instances" not in payload["telemetry"]:
                        self.state["scaling_telemetry"].pop("current_instances", None)
                    self.state["scaling_telemetry"].update(payload["telemetry"])
                    if (
                        "journal_oom_events" in payload["telemetry"]
                        and "builder_journal_oom_events" not in payload["telemetry"]
                    ):
                        raw_journal_ooms = self.state["scaling_telemetry"].get("journal_oom_events", [])
                        if isinstance(raw_journal_ooms, list):
                            self.state["scaling_telemetry"]["builder_journal_oom_events"] = [
                                evt
                                for evt in raw_journal_ooms
                                if isinstance(evt, dict)
                                and (
                                    evt.get("role") == "rust_builder"
                                    or str(evt.get("instance_name", "")).startswith("builder-")
                                )
                            ]
                if "add_github_oom_run" in payload and isinstance(payload["add_github_oom_run"], dict):
                    item = dict(payload["add_github_oom_run"])
                    if "deep_link" in item and not item.get("run_id"):
                        parsed = parse_github_oom_deep_link(item["deep_link"])
                        item = {**item, **parsed, "deep_link": parsed["url"]}
                    else:
                        link = build_github_oom_deep_link(
                            item.get("repository", "Appz4Fun/nzbget"),
                            item.get("run_id", 38596411473),
                            item.get("job_id", 115964282184),
                            item.get("step_number", 2),
                            item.get("line_number", 50),
                        )
                        item["deep_link"] = link
                    is_known_timeout_run = (
                        int(item.get("run_id", 0)) == 38596411473
                        and int(item.get("job_id", 0)) == 115964282184
                    )
                    if not item.get("error_excerpt"):
                        if is_known_timeout_run:
                            item["error_excerpt"] = (
                                "2026-10-14T16:25:26.2656514Z ##[error]Timeout reached, aborting! (Canceling Pages deployment)"
                            )
                        else:
                            item["error_excerpt"] = (
                                "cargo: rustc killed by signal 9 (SIGKILL) — cgroup memory.oom.group=1 (exit code 137)"
                            )
                    if "runner_name" not in item:
                        item["runner_name"] = (
                            "GitHub Actions 1000014245"
                            if is_known_timeout_run
                            else "gha-oxidex-runners-us-south1-a-01"
                        )
                    cls_info = classify_github_ci_failure(
                        str(item["error_excerpt"]),
                        runner_name=item.get("runner_name"),
                    )
                    item["failure_classification"] = cls_info["failure_classification"]
                    item["is_oom"] = cls_info["is_oom"]
                    item["is_self_hosted_runner"] = cls_info["is_self_hosted_runner"]
                    item["badge_text"] = cls_info["badge_text"]
                    item.setdefault("id", f"gh-ci-{uuid.uuid4().hex[:8]}")
                    item.setdefault("failed_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                    self.state["scaling_telemetry"]["github_oom_runs"].insert(0, item)

                cicd_eval = evaluate_scaling_decision(
                    self.state["scaling"]["cicd_runner"],
                    self.state["scaling_telemetry"],
                    per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd("cicd_runner"),
                )
                builder_eval = evaluate_builder_scaling_decision(
                    self.state["scaling"]["rust_builder"],
                    self.state["scaling_telemetry"],
                    per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd("rust_builder"),
                )
            except Exception:
                self.state["scaling"] = prev_scaling
                self.state["profiles"] = prev_profiles
                self.state["scaling_telemetry"] = prev_telemetry
                raise

            self._persist()
            evaluation = builder_eval if role == "rust_builder" else cicd_eval
            return {
                "status": "ok",
                "scaling": self.state["scaling"],
                "scaling_telemetry": self.state["scaling_telemetry"],
                "evaluation": evaluation,
                "evaluations": {
                    "cicd_runner": cicd_eval,
                    "rust_builder": builder_eval,
                },
            }

    def update_regions(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Enable/disable regions individually or by latency/capability filter."""
        with self._lock:
            prev_enabled = copy.deepcopy(self.state["regions_enabled"])
            prev_latencies = copy.deepcopy(self.state["region_latencies_ms"])
            try:
                if "regions_enabled" in payload and isinstance(payload["regions_enabled"], dict):
                    for reg, enabled in payload["regions_enabled"].items():
                        if reg in self.state["regions_enabled"]:
                            self.state["regions_enabled"][reg] = bool(enabled)
                if payload.get("measure_latency"):
                    self.state["region_latencies_ms"] = measure_region_latencies()
                if "deselect_matching" in payload and isinstance(payload["deselect_matching"], dict):
                    criteria = payload["deselect_matching"]
                    max_lat = criteria.get("min_latency_ms_to_disable")
                    max_lat_val = (
                        _finite_float(max_lat, "min_latency_ms_to_disable", min_val=0.0)
                        if max_lat is not None
                        else None
                    )
                    for reg in REGIONS_CATALOG:
                        r_code = reg["region"]
                        lat = self.state["region_latencies_ms"].get(r_code, reg["latency_ms"])
                        if max_lat_val is not None and lat > max_lat_val:
                            self.state["regions_enabled"][r_code] = False
                self._persist()
            except Exception:
                self.state["regions_enabled"] = prev_enabled
                self.state["region_latencies_ms"] = prev_latencies
                raise
            return {
                "status": "ok",
                "regions_enabled": self.state["regions_enabled"],
                "region_latencies_ms": self.state["region_latencies_ms"],
            }

    def save_cloud_init_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save an edited cloud-init template or Save As a new named template."""
        with self._lock:
            mode = str(payload.get("mode", "save")).lower()
            content = str(payload.get("content", "")).strip()
            if not content:
                raise ValueError("Cloud-init content cannot be empty")
            parsed = parse_cloud_init(content)
            if not parsed["valid"]:
                errors_str = "; ".join(parsed.get("errors", [])) or "Invalid cloud-init syntax"
                raise ValueError(f"Invalid cloud-init template: {errors_str}")
            role = str(payload.get("role", "cicd_runner")).strip()
            if role not in ("cicd_runner", "rust_builder"):
                raise ValueError(f"Unsupported workload role: {role}")
            name = str(payload.get("name", "Custom Cloud-Init Template")).strip()
            description = str(payload.get("description", "Operator-customized cloud-init template")).strip()
            template_id = str(payload.get("id", "")).strip()
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            if mode == "save_as" or not template_id:
                new_id = f"ci-{role.split('_')[0]}-{uuid.uuid4().hex[:6]}"
                record = {
                    "id": new_id,
                    "name": name,
                    "role": role,
                    "description": description,
                    "updated_at": now_iso,
                    "content": content,
                }
                self.state["cloud_init_templates"].append(record)
                if payload.get("assign_to_profile", True) and role in self.state["profiles"]:
                    prof = self.state["profiles"][role]
                    prof["cloud_init_template_id"] = new_id
                    if prof.get("vm_template_id"):
                        assigned_vm_tpl = next(
                            (
                                t
                                for t in self.state.get("vm_templates", [])
                                if t.get("id") == prof.get("vm_template_id")
                            ),
                            None,
                        )
                        if assigned_vm_tpl is not None:
                            assigned_vm_tpl["cloud_init_template_id"] = new_id
                self._persist()
                return {"status": "ok", "template": {**record, "parsed": parsed}}

            existing = next(
                (t for t in self.state["cloud_init_templates"] if t["id"] == template_id),
                None,
            )
            if existing is None:
                raise ValueError(f"Cloud-init template '{template_id}' not found")
            old_role = str(existing.get("role", "cicd_runner")).strip()
            target_role = role if "role" in payload else old_role
            existing["role"] = target_role
            existing["content"] = content
            if name:
                existing["name"] = name
            if description:
                existing["description"] = description
            existing["updated_at"] = now_iso
            if old_role != target_role and old_role in self.state["profiles"]:
                fallback_tpl = next(
                    (
                        t["id"]
                        for t in self.state["cloud_init_templates"]
                        if t.get("role") == old_role and t.get("id") != existing["id"]
                    ),
                    None,
                )
                old_prof = self.state["profiles"][old_role]
                if old_prof.get("cloud_init_template_id") == existing["id"]:
                    old_prof["cloud_init_template_id"] = fallback_tpl
                for vm_t in self.state.get("vm_templates", []):
                    if vm_t.get("role") == old_role and vm_t.get("cloud_init_template_id") == existing["id"]:
                        vm_t["cloud_init_template_id"] = fallback_tpl
            if payload.get("assign_to_profile", False) and existing["role"] in self.state["profiles"]:
                prof = self.state["profiles"][existing["role"]]
                prof["cloud_init_template_id"] = existing["id"]
                if prof.get("vm_template_id"):
                    assigned_vm_tpl = next(
                        (
                            t
                            for t in self.state.get("vm_templates", [])
                            if t.get("id") == prof.get("vm_template_id")
                        ),
                        None,
                    )
                    if assigned_vm_tpl is not None:
                        assigned_vm_tpl["cloud_init_template_id"] = existing["id"]
            self._persist()
            return {"status": "ok", "template": {**existing, "parsed": parsed}}

    def create_vm_template_from_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a reusable VM template out of a current or historical instance."""
        with self._lock:
            instance_id = str(payload.get("instance_id", "")).strip()
            instance_name = str(payload.get("instance_name", "")).strip()
            inst = next(
                (
                    i
                    for i in self.state["instances"]
                    if (instance_id and i["instance_id"] == instance_id)
                    or (instance_name and i["name"] == instance_name)
                ),
                None,
            )
            if inst is None:
                raise ValueError("Source instance not found for VM template creation")

            role = inst["role"]
            prof = self.state["profiles"].get(role)
            tpl_name = str(
                payload.get("template_name")
                or f"Template from {inst['name']} ({inst['machine_type']})"
            ).strip()
            new_tpl = {
                "id": f"tpl-{uuid.uuid4().hex[:8]}",
                "name": tpl_name,
                "role": role,
                "machine_type": inst["machine_type"],
                "vcpu": inst["vcpu"],
                "ram_gib": inst["ram_gib"],
                "cpu_platform": inst["cpu_platform"],
                "benchmark_score": inst["benchmark_score"],
                "disk_category": inst["disk_category"],
                "boot_disk_type": inst["boot_disk_type"],
                "boot_disk_gb": inst["boot_disk_gb"],
                "data_disk_type": inst["data_disk_type"],
                "data_disk_gb": inst["data_disk_gb"],
                "runners_count": inst["runners_count"],
                "runner_vcpu": inst["runner_vcpu"],
                "runner_ram_gib": inst["runner_ram_gib"],
                "cloud_init_template_id": (
                    prof.get("cloud_init_template_id")
                    if prof and prof.get("cloud_init_template_id")
                    else inst["cloud_init_template_id"]
                ),
                "source_instance": inst["name"],
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            assign_to_profile = bool(payload.get("assign_to_profile", True))
            if assign_to_profile and prof is not None:
                validated_units = self._validate_vm_template_compatibility(new_tpl, prof, role)
                new_tpl["runner_vcpu"] = int(prof["runner_vcpu"])
                new_tpl["runner_ram_gib"] = float(prof["runner_ram_gib"])
                new_tpl["runners_count"] = validated_units
            self.state["vm_templates"].append(new_tpl)
            if assign_to_profile and prof is not None:
                prof["use_vm_template"] = True
                prof["vm_template_id"] = new_tpl["id"]
                if role == "cicd_runner":
                    self._sync_template_density()
            self._persist()
            return {"status": "ok", "vm_template": new_tpl}

    def delete_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a running instance on demand and record its deletion trigger and final cost."""
        with self._lock:
            instance_id = str(payload.get("instance_id", "")).strip()
            instance_name = str(payload.get("instance_name", "")).strip()
            reason = str(
                payload.get("reason")
                or "Manual operator deletion from Web UI Dashboard"
            ).strip()
            inst = next(
                (
                    i
                    for i in self.state["instances"]
                    if (instance_id and i["instance_id"] == instance_id)
                    or (instance_name and i["name"] == instance_name)
                ),
                None,
            )
            if inst is None:
                raise ValueError("Instance not found")
            if inst.get("status") != "RUNNING":
                return {"status": "already_deleted", "instance": inst}
            now_ts = time.time()
            inst["status"] = "DELETED"
            inst["deleted_epoch"] = now_ts
            inst["deleted_at"] = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            inst["runtime_hours"] = round(max(0.1, (now_ts - float(inst["started_epoch"])) / 3600.0), 2)
            inst["total_cost_usd"] = round(inst["runtime_hours"] * inst["pricing"]["total_hourly_usd"], 2)
            inst["total_cost_compute_only_usd"] = round(
                inst["runtime_hours"] * inst["pricing"]["compute_hourly_usd"], 2
            )
            inst["deletion_trigger"] = reason
            inst["cpu_utilization_percent"] = 0.0
            inst["ram_utilization_percent"] = 0.0
            self._sync_fleet_telemetry()
            self._persist()
            return {"status": "ok", "instance": inst}

    def scale_region_instances(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Scale the number of instances in a specific region/location on demand."""
        with self._lock:
            region = str(payload.get("region", "africa-south1")).strip()
            raw_target = _finite_int(payload.get("target_instances", 1), "target_instances", min_val=0)
            target_count = raw_target
            role = str(payload.get("role", "cicd_runner")).strip()
            if role not in ("cicd_runner", "rust_builder"):
                raise ValueError(f"Unsupported workload role: {role}")
            reg_info = next((r for r in REGIONS_CATALOG if r["region"] == region), None)
            if reg_info is None:
                raise ValueError(f"Unknown region: {region}")
            prof = self.state["profiles"].get(role, self.state["profiles"]["cicd_runner"])
            pol = self.state.get("scaling", {}).get(role, {})
            prof_max_instances = int(prof.get("max_instances", 20))
            if role == "rust_builder" and pol.get("max_instances") is not None:
                prof_max_instances = min(prof_max_instances, int(pol["max_instances"]))
            max_allowed = min(20, max(1, prof_max_instances))
            running_in_region = [
                i
                for i in self.state["instances"]
                if i["region"] == region and i["status"] == "RUNNING" and i["role"] == role
            ]
            running_other_regions = sum(
                1
                for i in self.state["instances"]
                if i["region"] != region and i["status"] == "RUNNING" and i["role"] == role
            )
            current_count = len(running_in_region)
            projected_total = running_other_regions + target_count
            if target_count > 20 or (
                target_count > current_count
                and (target_count > max_allowed or projected_total > max_allowed)
            ):
                raise ValueError(
                    f"target_instances ({target_count}) in {region} (total fleet: {projected_total}) "
                    f"exceeds maximum allowed instances ({max_allowed}) for role '{role}'"
                )
            now_ts = time.time()

            if target_count < current_count:
                to_remove = running_in_region[target_count:]
                for inst in to_remove:
                    inst["status"] = "DELETED"
                    inst["deleted_epoch"] = now_ts
                    inst["deleted_at"] = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    inst["runtime_hours"] = round(
                        max(0.1, (now_ts - float(inst["started_epoch"])) / 3600.0), 2
                    )
                    inst["total_cost_usd"] = round(
                        inst["runtime_hours"] * inst["pricing"]["total_hourly_usd"], 2
                    )
                    inst["total_cost_compute_only_usd"] = round(
                        inst["runtime_hours"] * inst["pricing"]["compute_hourly_usd"], 2
                    )
                    inst["deletion_trigger"] = (
                        f"On-demand regional scale-down in {region} ({current_count} -> {target_count} instances)"
                    )
                    inst["cpu_utilization_percent"] = 0.0
                    inst["ram_utilization_percent"] = 0.0
            elif target_count > current_count:
                if not bool(self.state["regions_enabled"].get(region, True)):
                    raise ValueError(f"Cannot scale up in disabled region '{region}'")
                vm_tpl = None
                if prof.get("use_vm_template") and prof.get("vm_template_id"):
                    vm_tpl = next(
                        (
                            t
                            for t in self.state.get("vm_templates", [])
                            if t.get("id") == prof.get("vm_template_id")
                            and t.get("role", role) == role
                        ),
                        None,
                    )
                    if vm_tpl is None:
                        raise ValueError(
                            f"Assigned VM template '{prof.get('vm_template_id')}' not found for role '{role}'"
                        )
                    self._validate_vm_template_compatibility(vm_tpl, prof, role)
                    tpl_machine_type = str(vm_tpl.get("machine_type", ""))
                    tpl_family = tpl_machine_type.split("-", 1)[0]
                    if tpl_family not in reg_info.get("families", []):
                        raise ValueError(
                            f"VM template '{vm_tpl.get('name', vm_tpl['id'])}' machine family '{tpl_family}' "
                            f"({tpl_machine_type}) is not available in region '{region}' "
                            f"(supported: {', '.join(reg_info.get('families', []))})"
                        )
                    tpl_cpu = str(vm_tpl.get("cpu_platform", ""))
                    if tpl_cpu and tpl_cpu not in reg_info.get("cpu_platforms", []):
                        raise ValueError(
                            f"VM template '{vm_tpl.get('name', vm_tpl['id'])}' CPU platform '{tpl_cpu}' "
                            f"is not available in region '{region}' "
                            f"(supported: {', '.join(reg_info.get('cpu_platforms', []))})"
                        )
                    tpl_boot_disk = str(vm_tpl.get("boot_disk_type", ""))
                    if tpl_boot_disk and tpl_boot_disk not in reg_info.get("disk_types", []):
                        raise ValueError(
                            f"VM template '{vm_tpl.get('name', vm_tpl['id'])}' boot disk type '{tpl_boot_disk}' "
                            f"is not available in region '{region}' "
                            f"(supported: {', '.join(reg_info.get('disk_types', []))})"
                        )
                    tpl_data_disk = str(vm_tpl.get("data_disk_type", ""))
                    if tpl_data_disk and tpl_data_disk not in reg_info.get("disk_types", []):
                        raise ValueError(
                            f"VM template '{vm_tpl.get('name', vm_tpl['id'])}' data disk type '{tpl_data_disk}' "
                            f"is not available in region '{region}' "
                            f"(supported: {', '.join(reg_info.get('disk_types', []))})"
                        )
                source_spec: dict[str, Any] | None = vm_tpl
                if source_spec is None:
                    existing_cand = running_in_region[0] if running_in_region else None
                    if existing_cand is not None:
                        try:
                            self._validate_vm_template_compatibility(existing_cand, prof, role)
                            if (
                                int(existing_cand.get("runner_vcpu", 0)) == int(prof["runner_vcpu"])
                                and float(existing_cand.get("runner_ram_gib", 0.0))
                                == float(prof["runner_ram_gib"])
                            ):
                                source_spec = existing_cand
                        except ValueError:
                            # Existing instance no longer matches updated profile requirements; select a qualifying offer below.
                            source_spec = None
                    if source_spec is None:
                        reg_qual = qualify_instances(
                            prof,
                            {r["region"]: (r["region"] == region) for r in REGIONS_CATALOG},
                            self.project,
                            region_latencies_ms=self.state.get("region_latencies_ms"),
                        )
                        best_offer = (
                            reg_qual["spot_price_qualifying"]
                            or reg_qual["hardware_qualifying"]
                            or [None]
                        )[0]
                        if best_offer is None:
                            raise ValueError(
                                f"No machine shape in region '{region}' satisfies the sizing and hardware "
                                f"requirements for role '{role}'"
                            )
                        source_spec = {
                            "machine_type": best_offer["machine_type"],
                            "vcpu": best_offer["vcpu"],
                            "ram_gib": best_offer["ram_gib"],
                            "cpu_platform": best_offer["cpu_platform"],
                            "boot_disk_type": best_offer["boot_disk_type"],
                            "boot_disk_gb": best_offer["boot_disk_gb"],
                            "data_disk_type": best_offer["data_disk_type"],
                            "data_disk_gb": best_offer["data_disk_gb"],
                            "runner_vcpu": best_offer["unit_vcpu"],
                            "runner_ram_gib": best_offer["unit_ram_gib"],
                            "runners_count": best_offer["units_per_instance"],
                            "cloud_init_template_id": prof.get(
                                "cloud_init_template_id", "ci-runner-default"
                            ),
                            "vm_template_id": None,
                        }

                default_ci_id = "ci-runner-default" if role == "cicd_runner" else "ci-rust-builder-199"
                new_records: list[dict[str, Any]] = []
                for idx in range(current_count + 1, target_count + 1):
                    zone = reg_info["zones"][(idx - 1) % len(reg_info["zones"])]
                    machine_type = source_spec["machine_type"]
                    vcpu = int(source_spec["vcpu"])
                    ram_gib = float(source_spec["ram_gib"])
                    cpu_platform = source_spec["cpu_platform"]
                    boot_disk_type = source_spec["boot_disk_type"]
                    boot_disk_gb = int(source_spec.get("boot_disk_gb", prof.get("boot_disk_gb", 40)))
                    data_disk_type = source_spec["data_disk_type"]
                    data_disk_gb = int(source_spec.get("data_disk_gb", prof.get("data_disk_gb", 100)))
                    runner_vcpu = int(source_spec["runner_vcpu"])
                    runner_ram = float(source_spec["runner_ram_gib"])
                    runners_cnt = int(source_spec["runners_count"])
                    cloud_init_id = (
                        prof.get("cloud_init_template_id")
                        or source_spec.get("cloud_init_template_id")
                        or default_ci_id
                    )
                    vm_tpl_id = (
                        vm_tpl["id"]
                        if vm_tpl
                        else source_spec.get("vm_template_id")
                    )
                    prefix = "oxidex-runners" if role == "cicd_runner" else "builder-oxidex"
                    new_name = f"{prefix}-{region}-{uuid.uuid4().hex[:4]}"
                    created = _build_instance_record(
                        instance_id=str(int(now_ts * 1000) + idx),
                        name=new_name,
                        role=role,
                        status="RUNNING",
                        region=region,
                        zone=zone,
                        machine_type=machine_type,
                        vcpu=vcpu,
                        ram_gib=ram_gib,
                        cpu_platform=cpu_platform,
                        boot_disk_type=boot_disk_type,
                        boot_disk_gb=boot_disk_gb,
                        data_disk_type=data_disk_type,
                        data_disk_gb=data_disk_gb,
                        runners_count=runners_cnt,
                        runner_vcpu=runner_vcpu,
                        runner_ram_gib=runner_ram,
                        started_hours_ago=0.05,
                        deleted_hours_ago=None,
                        deletion_trigger=f"Active — Scaled up on demand in {region}",
                        cloud_init_template_id=cloud_init_id,
                        vm_template_id=vm_tpl_id,
                        cpu_utilization_percent=35.0,
                        ram_utilization_percent=40.0,
                        project=self.project,
                        now_epoch=now_ts,
                    )
                    new_records.append(created)

                budget = prof.get("budget", {})
                max_hourly_cap = float(
                    pol.get("max_scale_up_hourly_usd", budget.get("max_hourly_usd", 5.0))
                )
                max_daily_cap = float(
                    pol.get("max_scale_up_daily_usd", budget.get("max_daily_usd", 10.0))
                )
                max_monthly_cap = float(
                    pol.get("max_scale_up_monthly_usd", budget.get("max_monthly_usd", 50.0))
                )
                include_disk = bool(
                    pol.get("include_disk_in_scale_budget", budget.get("include_disk_costs", True))
                )
                include_tax = bool(budget.get("include_taxes", False))
                effective_hourly_cap = min(
                    max_hourly_cap,
                    max_daily_cap / 24.0,
                    max_monthly_cap / (24.0 * 365.0 / 12.0),
                )
                existing_running_role = [
                    i
                    for i in self.state["instances"]
                    if i["status"] == "RUNNING" and i["role"] == role
                ]
                if role == "cicd_runner":
                    projected_runner_slots = sum(
                        int(i.get("runners_count", 1)) for i in existing_running_role + new_records
                    )
                    max_runners_cap = max(
                        int(prof.get("min_runners", 1)),
                        int(pol.get("max_runners", 24)),
                    )
                    if projected_runner_slots > max_runners_cap:
                        raise ValueError(
                            f"Scaling up in {region} to {target_count} instance(s) "
                            f"(projected {projected_runner_slots} runner slots across {projected_total} VMs) "
                            f"exceeds maximum allowed CI/CD runners ({max_runners_cap})"
                        )
                projected_fleet_hourly = round(
                    sum(
                        self._instance_hourly_for_budget(
                            i["pricing"], include_disk=include_disk, include_tax=include_tax
                        )
                        for i in existing_running_role + new_records
                    ),
                    4,
                )
                evaluator_unit_hourly = max(
                    0.0001, float(self._resolve_scaling_unit_hourly_usd(role))
                )
                max_affordable_instances = max(
                    0, int(math.floor(effective_hourly_cap / evaluator_unit_hourly))
                )
                if (
                    projected_fleet_hourly > effective_hourly_cap
                    or projected_total > max_affordable_instances
                ):
                    raise ValueError(
                        f"Scaling up in {region} to {target_count} instance(s) (total fleet: {projected_total}, "
                        f"projected ${projected_fleet_hourly:.4f}/hr) exceeds scale-up budget cap "
                        f"(${max_hourly_cap:.2f}/hr, ${max_daily_cap:.2f}/day, ${max_monthly_cap:.2f}/mo) for role '{role}'"
                    )
                for created in new_records:
                    self.state["instances"].insert(0, created)

            self._sync_fleet_telemetry()
            self._persist()
            updated_running = [
                i
                for i in self.state["instances"]
                if i["region"] == region and i["status"] == "RUNNING" and i["role"] == role
            ]
            return {
                "status": "ok",
                "region": region,
                "role": role,
                "previous_count": current_count,
                "current_count": len(updated_running),
                "running_instances": updated_running,
            }

    def get_rust_builds_summary(self) -> dict[str, Any]:
        """Return enriched Rust builder telemetry records and aggregate stats."""
        with self._lock:
            builds = list(self.state["rust_builds"])
            total_builds = len(builds)
            completed_builds = [
                b
                for b in builds
                if str(b.get("status", "DELIVERED")).strip().upper() not in UNFINISHED_BUILD_STATUSES
            ]
            completed_count = len(completed_builds)
            avg_duration = (
                round(
                    sum(b["total_duration_seconds"] for b in completed_builds) / completed_count,
                    2,
                )
                if completed_count
                else 0.0
            )
            total_hits = sum(b["cache_hits"] for b in completed_builds)
            total_crates = sum(b["total_crates"] for b in completed_builds)
            overall_hit_rate = round((total_hits / total_crates * 100.0) if total_crates else 0.0, 1)
            max_peak_ram = max((b["peak_ram_gib"] for b in completed_builds), default=0.0)
            total_cpu_time = round(sum(b["cpu_time_seconds"] for b in completed_builds), 2)
            total_cpu_points = round(sum(b["cpu_points"] for b in completed_builds), 2)
            vpn_or_tor_count = sum(
                1 for b in builds if b["network_security"]["is_vpn"] or b["network_security"]["is_tor"]
            )
            return {
                "total_builds": total_builds,
                "completed_builds": completed_count,
                "average_duration_seconds": avg_duration,
                "total_cache_hits": total_hits,
                "overall_cache_hit_rate_percent": overall_hit_rate,
                "max_peak_ram_gib": max_peak_ram,
                "total_cpu_time_seconds": total_cpu_time,
                "total_cpu_points": total_cpu_points,
                "vpn_or_tor_count": vpn_or_tor_count,
                "builds": builds,
            }

    def record_rust_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record a new Rust remote builder telemetry event or merge a partial update."""
        with self._lock:
            now_ts = time.time()
            incoming_build_id = str(payload.get("build_id") or "").strip()
            existing_idx = (
                next(
                    (
                        idx
                        for idx, b in enumerate(self.state["rust_builds"])
                        if b.get("build_id") == incoming_build_id
                    ),
                    None,
                )
                if incoming_build_id
                else None
            )
            existing_build = (
                self.state["rust_builds"][existing_idx]
                if existing_idx is not None
                else {}
            )

            status_str = str(payload.get("status", existing_build.get("status", "DELIVERED")))
            is_unfinished = status_str.strip().upper() in UNFINISHED_BUILD_STATUSES
            was_unfinished = (
                str(existing_build.get("status", "QUEUED")).strip().upper() in UNFINISHED_BUILD_STATUSES
                if existing_build
                else True
            )

            raw_duration = (
                payload.get("total_duration_seconds")
                if "total_duration_seconds" in payload
                else (existing_build.get("total_duration_seconds") if not was_unfinished else None)
            )
            raw_delivery = (
                payload.get("delivery_duration_seconds")
                if "delivery_duration_seconds" in payload
                else (existing_build.get("delivery_duration_seconds") if not was_unfinished else None)
            )
            raw_compile = (
                payload.get("compile_duration_seconds")
                if "compile_duration_seconds" in payload
                else (existing_build.get("compile_duration_seconds") if not was_unfinished else None)
            )
            explicit_total = (
                _finite_float(raw_duration, "total_duration_seconds", min_val=0.0)
                if raw_duration is not None
                else None
            )
            explicit_delivery = (
                _finite_float(raw_delivery, "delivery_duration_seconds", min_val=0.0)
                if raw_delivery is not None
                else None
            )
            explicit_compile = (
                _finite_float(raw_compile, "compile_duration_seconds", min_val=0.0)
                if raw_compile is not None
                else None
            )

            if is_unfinished:
                duration = explicit_total if explicit_total is not None else 0.0
                delivery_sec = explicit_delivery if explicit_delivery is not None else 0.0
            else:
                if explicit_total is not None:
                    duration = explicit_total
                    if explicit_delivery is not None:
                        delivery_sec = min(explicit_delivery, duration)
                    elif explicit_compile is not None:
                        delivery_sec = max(0.0, duration - min(explicit_compile, duration))
                    else:
                        delivery_sec = min(8.0, round(duration * 0.2, 2))
                else:
                    delivery_sec = explicit_delivery if explicit_delivery is not None else 8.0
                    c_sec = explicit_compile if explicit_compile is not None else 87.0
                    duration = c_sec + delivery_sec

            has_hits = payload.get("cache_hits") is not None
            has_misses = payload.get("cache_misses") is not None
            has_total = payload.get("total_crates") is not None
            has_existing_crates = bool(existing_build and int(existing_build.get("total_crates", 0)) > 0)
            if has_hits:
                cache_hits = _finite_int(payload["cache_hits"], "cache_hits", min_val=0)
            elif has_total and has_misses:
                cache_hits = max(
                    0,
                    _finite_int(payload["total_crates"], "total_crates", min_val=0)
                    - _finite_int(payload["cache_misses"], "cache_misses", min_val=0),
                )
            elif has_existing_crates:
                cache_hits = _finite_int(existing_build.get("cache_hits", 0), "cache_hits", min_val=0)
            else:
                cache_hits = 0 if is_unfinished else 380

            if has_misses:
                cache_misses = _finite_int(payload["cache_misses"], "cache_misses", min_val=0)
            elif has_total:
                cache_misses = max(
                    0, _finite_int(payload["total_crates"], "total_crates", min_val=0) - cache_hits
                )
            elif has_existing_crates:
                cache_misses = _finite_int(existing_build.get("cache_misses", 0), "cache_misses", min_val=0)
            else:
                cache_misses = 0 if is_unfinished else 25

            if has_total:
                total_crates = _finite_int(payload["total_crates"], "total_crates", min_val=0)
            elif has_existing_crates and not (has_hits or has_misses):
                total_crates = _finite_int(existing_build.get("total_crates", 0), "total_crates", min_val=0)
            else:
                total_crates = cache_hits + cache_misses

            if is_unfinished:
                req_at = (
                    payload.get("requested_at")
                    or existing_build.get("requested_at")
                    or _iso_seconds_ago(0, now_ts)
                )
                fin_at = (
                    payload.get("finished_at")
                    if "finished_at" in payload
                    else existing_build.get("finished_at")
                )
                del_at = (
                    payload.get("delivered_at")
                    if "delivered_at" in payload
                    else existing_build.get("delivered_at")
                )
            elif existing_build.get("requested_at") and not payload.get("requested_at"):
                req_at = str(existing_build["requested_at"])
                req_epoch = _iso_to_epoch(req_at)
                if payload.get("finished_at"):
                    fin_at = str(payload["finished_at"])
                    fin_epoch = _iso_to_epoch(fin_at)
                elif (
                    existing_build.get("finished_at")
                    and "total_duration_seconds" not in payload
                    and "compile_duration_seconds" not in payload
                ):
                    fin_at = str(existing_build["finished_at"])
                    fin_epoch = _iso_to_epoch(fin_at)
                else:
                    fin_epoch = req_epoch + max(0.0, duration - delivery_sec)
                    fin_at = _iso_seconds_ago(0, fin_epoch)
                if payload.get("delivered_at"):
                    del_at = str(payload["delivered_at"])
                elif (
                    existing_build.get("delivered_at")
                    and "total_duration_seconds" not in payload
                    and "delivery_duration_seconds" not in payload
                ):
                    del_at = str(existing_build["delivered_at"])
                else:
                    del_epoch = fin_epoch + delivery_sec
                    del_at = _iso_seconds_ago(0, del_epoch)
            else:
                del_at = payload.get("delivered_at") or _iso_seconds_ago(0, now_ts)
                del_epoch = _iso_to_epoch(str(del_at))
                fin_at = payload.get("finished_at") or _iso_seconds_ago(delivery_sec, del_epoch)
                fin_epoch = _iso_to_epoch(str(fin_at))
                req_at = payload.get("requested_at") or _iso_seconds_ago(
                    max(0.0, duration - delivery_sec), fin_epoch
                )

            existing_peak_ram = existing_build.get("peak_ram_gib")
            default_peak_ram = (
                existing_peak_ram
                if (existing_peak_ram is not None and float(existing_peak_ram) > 0.0)
                else (0.0 if is_unfinished else 20.5)
            )
            existing_cpu_time = existing_build.get("cpu_time_seconds")
            default_cpu_time = (
                existing_cpu_time
                if (existing_cpu_time is not None and float(existing_cpu_time) > 0.0)
                else (0.0 if is_unfinished else 1120.0)
            )

            raw = {
                "build_id": incoming_build_id or f"rb-{uuid.uuid4().hex[:8]}",
                "project_name": payload.get(
                    "project_name", existing_build.get("project_name", "oxidex")
                ),
                "profile": payload.get("profile", existing_build.get("profile", "release")),
                "target_triple": payload.get(
                    "target_triple",
                    existing_build.get("target_triple", "x86_64-unknown-linux-gnu"),
                ),
                "commit_sha": payload.get(
                    "commit_sha", existing_build.get("commit_sha", "af7c14f8cb2c")
                ),
                "status": status_str,
                "requested_at": req_at,
                "finished_at": fin_at,
                "delivered_at": del_at,
                "total_duration_seconds": raw_duration,
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
                "total_crates": total_crates,
                "instance_name": payload.get(
                    "instance_name",
                    existing_build.get("instance_name", "builder-oxidex-turin-01"),
                ),
                "zone": payload.get("zone", existing_build.get("zone", "us-south1-b")),
                "machine_type": payload.get(
                    "machine_type", existing_build.get("machine_type", "c4d-highcpu-16")
                ),
                "vcpu": _finite_int(
                    payload.get("vcpu", existing_build.get("vcpu", 16)), "vcpu", min_val=1
                ),
                "cpu_platform": payload.get(
                    "cpu_platform", existing_build.get("cpu_platform", "AMD Turin")
                ),
                "origin_ip": payload.get(
                    "origin_ip", existing_build.get("origin_ip", "185.159.157.42")
                ),
                "origin_hostname": (
                    payload.get("origin_hostname")
                    if "origin_hostname" in payload
                    else existing_build.get("origin_hostname")
                ),
                "peak_ram_gib": _finite_float(
                    payload.get("peak_ram_gib", default_peak_ram), "peak_ram_gib", min_val=0.0
                ),
                "container_ram_cap_gib": _finite_float(
                    payload.get(
                        "container_ram_cap_gib",
                        existing_build.get("container_ram_cap_gib", 28.0),
                    ),
                    "container_ram_cap_gib",
                    min_val=0.1,
                ),
                "cpu_time_seconds": _finite_float(
                    payload.get("cpu_time_seconds", default_cpu_time),
                    "cpu_time_seconds",
                    min_val=0.0,
                ),
            }
            enriched = enrich_rust_build_record(raw, project=self.project)
            if existing_idx is not None:
                self.state["rust_builds"][existing_idx] = enriched
            else:
                self.state["rust_builds"].insert(0, enriched)
            self._sync_rust_build_telemetry(now_ts)
            self._persist()
            builder_eval = evaluate_builder_scaling_decision(
                self.state["scaling"]["rust_builder"],
                self.state["scaling_telemetry"],
                per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd("rust_builder"),
                now_epoch=now_ts,
            )
            return {
                "status": "ok",
                "build": enriched,
                "builder_scaling_evaluation": builder_eval,
            }


def _is_loopback_host(host: str) -> bool:
    """Return True only if host is localhost or an IPv4/IPv6 loopback address."""
    cleaned = (host or "").strip().lower()
    if not cleaned:
        return False
    if cleaned == "localhost":
        return True
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def make_handler(
    store: DashboardStore,
    *,
    auth_token: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Create the HTTP handler bound to a DashboardStore instance."""
    from .dashboard_ui_assets import render_dashboard_html

    expected_token = str(auth_token).strip() if auth_token else ""

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(
            self,
            data: Any,
            status: int = HTTPStatus.OK,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(data, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html_text: str, status: int = HTTPStatus.OK) -> None:
            body = html_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            """Verify Bearer token or X-Dashboard-Token when authentication is configured."""
            if not expected_token:
                return True
            auth_hdr = (self.headers.get("Authorization") or "").strip()
            candidate = ""
            if auth_hdr.lower().startswith("bearer "):
                candidate = auth_hdr[7:].strip()
            if not candidate:
                candidate = (self.headers.get("X-Dashboard-Token") or "").strip()
            if candidate and hmac.compare_digest(candidate, expected_token):
                return True
            self._send_json(
                {"error": "Unauthorized: valid Bearer token or X-Dashboard-Token required"},
                status=HTTPStatus.UNAUTHORIZED,
                extra_headers={"WWW-Authenticate": 'Bearer realm="Spot Dashboard"'},
            )
            return False

        def _validate_same_origin_json_post(self) -> tuple[bool, HTTPStatus, str]:
            """Verify mutating POST requests carry application/json and are not cross-origin."""
            sec_fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if sec_fetch_site == "cross-site":
                return (
                    False,
                    HTTPStatus.FORBIDDEN,
                    "Cross-site POST requests are forbidden",
                )
            origin = (self.headers.get("Origin") or "").strip()
            if origin:
                if origin.lower() == "null":
                    return (
                        False,
                        HTTPStatus.FORBIDDEN,
                        "Cross-origin POST request from null Origin is forbidden",
                    )
                parsed_origin = urlparse(origin)
                req_host = (self.headers.get("Host") or "").strip().lower()
                origin_host = parsed_origin.netloc.lower()
                if parsed_origin.scheme not in ("http", "https") or (
                    req_host and origin_host != req_host
                ):
                    return (
                        False,
                        HTTPStatus.FORBIDDEN,
                        f"Cross-origin POST request from '{origin}' is forbidden",
                    )
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return (
                    False,
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "Content-Type must be application/json",
                )
            return True, HTTPStatus.OK, ""

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

        def do_GET(self) -> None:  # noqa: N802
            if not self._check_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                self._send_html(render_dashboard_html(project=store.project, org=store.org))
                return

            if path == "/api/health":
                self._send_json({"status": "healthy", "project": store.project, "org": store.org})
                return

            if path == "/api/overview":
                include_deleted = qs.get("include_deleted", ["true"])[0].lower() != "false"
                window_hours = float(qs.get("window_hours", ["24"])[0])
                include_disk_tax = qs.get("include_disk_and_tax", ["true"])[0].lower() != "false"
                sort_by = qs.get("sort_by", ["total_cost_in_window_usd"])[0]
                sort_dir = qs.get("sort_dir", ["desc"])[0]
                search = qs.get("search", [""])[0]
                role_filter = qs.get("role", [""])[0]
                self._send_json(
                    store.get_overview(
                        include_deleted=include_deleted,
                        window_hours=window_hours,
                        include_disk_and_tax=include_disk_tax,
                        sort_by=sort_by,
                        sort_dir=sort_dir,
                        search_filter=search,
                        role_filter=role_filter,
                    )
                )
                return

            if path == "/api/instances":
                include_deleted = qs.get("include_deleted", ["true"])[0].lower() != "false"
                window_hours = float(qs.get("window_hours", ["24"])[0])
                include_disk_tax = qs.get("include_disk_and_tax", ["true"])[0].lower() != "false"
                sort_by = qs.get("sort_by", ["total_cost_in_window_usd"])[0]
                sort_dir = qs.get("sort_dir", ["desc"])[0]
                search = qs.get("search", [""])[0]
                role_filter = qs.get("role", [""])[0]
                self._send_json(
                    store.query_instances(
                        include_deleted=include_deleted,
                        window_hours=window_hours,
                        include_disk_and_tax=include_disk_tax,
                        sort_by=sort_by,
                        sort_dir=sort_dir,
                        search_filter=search,
                        role_filter=role_filter,
                    )
                )
                return

            if path == "/api/rust-builds":
                self._send_json(store.get_rust_builds_summary())
                return

            self._send_json({"error": f"Unknown endpoint: {path}"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            ok_req, err_status, err_msg = self._validate_same_origin_json_post()
            if not ok_req:
                self._send_json({"error": err_msg}, status=err_status)
                return
            try:
                payload = self._read_json()
                if path == "/api/budget":
                    self._send_json(store.update_budget(payload))
                    return
                if path == "/api/profile/cicd_runner":
                    self._send_json(store.update_profile("cicd_runner", payload))
                    return
                if path == "/api/profile/rust_builder":
                    self._send_json(store.update_profile("rust_builder", payload))
                    return
                if path == "/api/qualify":
                    role = str(payload.get("role", "cicd_runner")).strip()
                    if role not in ("cicd_runner", "rust_builder"):
                        raise ValueError(f"Unsupported workload role: {role}")
                    merged = {**store.state["profiles"].get(role, {}), **payload}
                    self._send_json(
                        qualify_instances(
                            merged,
                            store.state["regions_enabled"],
                            store.project,
                            region_latencies_ms=store.state.get("region_latencies_ms"),
                        )
                    )
                    return
                if path == "/api/scaling":
                    self._send_json(store.update_scaling(payload))
                    return
                if path == "/api/regions":
                    self._send_json(store.update_regions(payload))
                    return
                if path == "/api/cloud-init/save":
                    self._send_json(store.save_cloud_init_template(payload))
                    return
                if path == "/api/cloud-init/parse":
                    self._send_json(parse_cloud_init(str(payload.get("content", ""))))
                    return
                if path == "/api/vm-templates/from-instance":
                    self._send_json(store.create_vm_template_from_instance(payload))
                    return
                if path == "/api/instances/delete":
                    self._send_json(store.delete_instance(payload))
                    return
                if path == "/api/instances/scale-region":
                    self._send_json(store.scale_region_instances(payload))
                    return
                if path == "/api/rust-builds":
                    self._send_json(store.record_rust_build(payload))
                    return
                if path == "/api/ip-intel":
                    self._send_json(
                        classify_ip_and_network(
                            str(payload.get("ip_address", "")),
                            payload.get("hostname"),
                        )
                    )
                    return
                self._send_json({"error": f"Unknown endpoint: {path}"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    return DashboardRequestHandler


def create_dashboard_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    state_path: Path | None = None,
    project: str = "homelab-424523",
    org: str = "swack-tools",
    auth_token: str | None = None,
) -> tuple[ThreadingHTTPServer, DashboardStore]:
    """Instantiate the ThreadingHTTPServer and DashboardStore."""
    resolved_token = (
        auth_token
        if auth_token is not None
        else os.environ.get("DASHBOARD_AUTH_TOKEN")
    )
    cleaned_token = str(resolved_token).strip() if resolved_token else ""
    if not _is_loopback_host(host) and not cleaned_token:
        raise ValueError(
            f"Refusing to bind Web UI Dashboard to non-loopback address '{host}' without authentication. "
            "Provide --auth-token or set DASHBOARD_AUTH_TOKEN, or bind to 127.0.0.1 / ::1 / localhost."
        )
    store = DashboardStore(state_path=state_path, project=project, org=org)
    handler = make_handler(store, auth_token=cleaned_token or None)
    server = ThreadingHTTPServer((host, port), handler)
    return server, store


def args_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the Spot GitHub Runners and Rust Remote Builders Web UI Dashboard."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="HTTP port (default: 8787)")
    parser.add_argument("--project", default="homelab-424523", help="GCP project ID")
    parser.add_argument("--org", default="swack-tools", help="GitHub organization")
    parser.add_argument(
        "--auth-token",
        default=None,
        help="Bearer token required for HTTP requests (mandatory for non-loopback --host; or set DASHBOARD_AUTH_TOKEN)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Optional path to persist dashboard state JSON",
    )
    parser.add_argument(
        "--dump-overview",
        action="store_true",
        help="Print the initial JSON overview to stdout and exit without serving",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = args_parser()
    args = parser.parse_args(argv)
    if args.dump_overview:
        store = DashboardStore(state_path=args.state_file, project=args.project, org=args.org)
        print(json.dumps(store.get_overview(), indent=2))
        return 0
    server, _ = create_dashboard_server(
        host=args.host,
        port=args.port,
        state_path=args.state_file,
        project=args.project,
        org=args.org,
        auth_token=args.auth_token,
    )
    actual_host, actual_port = server.server_address[:2]
    print(f"Spot Fleet & Rust Builder Web UI Dashboard listening on http://{actual_host}:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Operator requested graceful shutdown via Ctrl-C; close the listening socket in finally.
        return 0
    finally:
        server.server_close()
    return 0

