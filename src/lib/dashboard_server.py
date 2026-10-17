"""HTTP REST API server and persistent state controller for the Spot Fleet &
Rust Remote Builder Web UI Dashboard.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

from .dashboard_auth import (
    MAX_ADDED_ADMINS,
    MAX_DYNAMIC_CONTROLLED_TARGETS,
    OAUTH_STATE_COOKIE_NAME,
    OWNER_IDENTITIES,
    SESSION_COOKIE_NAME,
    AuthCapacityError,
    ConfigRepoTargets,
    ConfigTargetsError,
    GitHubApiError,
    GitHubOAuthManager,
    GitHubUserLookupError,
    audit_admin_change,
    audit_login,
    normalize_github_login,
    format_cleared_session_cookie,
    format_cleared_state_cookie,
    format_session_cookie,
    format_state_cookie,
    parse_controlled_targets,
    parse_controlled_targets_env,
    parse_cookie_header,
    render_login_gate_html,
    safe_return_path,
    target_list_digest,
    TargetChange,
    ENV_CONTROLLED_TARGETS,
    ENV_DEVICE_FLOW,
    ENV_REDIRECT_URI,
)
from .patch_console import PatchConsole, PatchConsoleError, config_repo_token, executor_from_env, host_source_from_env
from .edge_state import (
    CONTROL_OPS,
    CONTROL_SLOTS,
    DEFAULT_RETRY_AFTER_SECONDS,
    ENV_CONTROL_SECRET,
    KV_ORIGIN_KEY,
    KV_STATE_KEY,
    MAX_KV_VALUE_BYTES,
    CloudflareKv,
    KvConfig,
    KvError,
    OriginRecord,
    build_snapshot,
    parse_snapshot,
    unavailable_payload,
    validate_slot,
    verify_control_token,
)
from .dashboard_models import (
    DEFAULT_RUNNER_CLOUD_INIT,
    DEFAULT_RUNNER_STRICT_CLOUD_INIT,
    DEFAULT_RUST_BUILDER_CLOUD_INIT,
    REGIONS_CATALOG,
    calculate_spot_pricing,
    compute_cpu_benchmark_score,
    disk_category,
    fetch_live_spot_pricing,
    format_gcloud_ssh_command,
    measure_region_latencies,
    normalize_budget,
    parse_cloud_init,
    qualify_instances,
    render_cloud_init_for_profile,
)
from .dashboard_scaling import (
    _assert_finite_json_tree,
    build_github_oom_deep_link,
    classify_github_ci_failure,
    default_scaling_config,
    evaluate_builder_scaling_decision,
    evaluate_scaling_decision,
    parse_github_oom_deep_link,
)
from .rust_build_telemetry import (
    ALLOWED_BUILD_STATUSES,
    SUCCESSFUL_BUILD_STATUSES,
    UNFINISHED_BUILD_STATUSES,
    UNSUCCESSFUL_BUILD_STATUSES,
    _finite_float,
    _finite_int,
    _iso_to_epoch,
    classify_ip_and_network,
    enrich_rust_build_record,
)

MAX_JSON_BODY_BYTES = 262_144
MAX_LOGIN_BODY_BYTES = 4_096
MAX_CONTROL_BODY_BYTES = MAX_KV_VALUE_BYTES + 65_536

IDEMPOTENCY_STATE_KEY = "idempotency"
IDEMPOTENCY_MAX_KEYS = 500
IDEMPOTENCY_TTL_SECONDS = 86_400
IDEMPOTENCY_MAX_BODY_BYTES = 65_536
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")


def request_fingerprint(payload: Any) -> str:
    """SHA-256 of the canonical JSON request body that an Idempotency-Key record is bound to.

    Keys are sorted and separators fixed, so the same body always yields the same fingerprint regardless of key
    order or whitespace on the wire.
    """
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

KV_WRITE_DEBOUNCE_SECONDS = 1.0
KV_WRITE_MAX_DELAY_SECONDS = 5.0
KV_WRITER_RECHECK_SECONDS = 30.0
KV_RETRY_BACKOFF_SECONDS = 5.0
KV_LOOP_INTERVAL_SECONDS = 0.25
HYDRATE_RETRY_DELAYS_SECONDS = (4.0, 5.0)  # 3 KV reads over roughly 10 seconds
# With no local state and KV unreachable, hydration keeps retrying with this backoff (seconds).
HYDRATE_BACKOFF_INITIAL_SECONDS = 5.0
HYDRATE_BACKOFF_MAX_SECONDS = 60.0
KV_STALE_REFUSAL_BACKOFF_SECONDS = 30.0

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# POST endpoints that compute a response without changing dashboard state; they skip idempotency storage.
NON_MUTATING_POST_PATHS = frozenset({"/api/qualify", "/api/cloud-init/parse", "/api/ip-intel",
                                     "/api/patches/validate", "/api/patches/preview"})
# POST endpoints whose side effect is outside dashboard state (a GitHub pull request, live runner hosts): they
# need an Idempotency-Key and run through DashboardStore.run_external.
EXTERNAL_MUTATION_PATHS = frozenset({"/api/patches/save", "/api/patches/hot-patch"})
# Owner-only routes that change the added-administrator list (dashboard state); they need an Idempotency-Key
# and "confirm": true.
ADMIN_MUTATION_PATHS = frozenset({"/api/admins/add", "/api/admins/remove"})
MAX_DASHBOARD_ADMINS_HISTORY = 50
OWNER_REQUIRED_ERROR = "Only the dashboard owner can view or change dashboard administrators"
# The edge Worker sets this header to the browser's IP (from Cloudflare's CF-Connecting-IP) and strips any
# client-supplied X-Spot-* header. It is trusted only from a loopback peer (the local cloudflared), and
# only for rate-limit bucketing.
CLIENT_IP_HEADER = "X-Spot-Client-Ip"
ORIGIN_MARKER_HEADER = "X-Spot-Origin"
ORIGIN_MARKER_VALUE = "dashboard"
FORWARDED_CLIENT_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", CLIENT_IP_HEADER)
MAX_CONTROLLED_TARGETS_HISTORY = 50
SESSION_TOO_LARGE_ERROR = (
    "Controlled target list is too long to fit in a session cookie; use fewer or shorter targets"
)
POLICY_UNAVAILABLE_ERROR = "controlled_targets_unavailable"
# Public browser origin (for example https://spot.swacktech.com) when a proxy such as the Cloudflare
# Worker reaches this server under a different Host (the tunnel hostname). Defaults to the origin of
# GITHUB_OAUTH_REDIRECT_URI.
ENV_PUBLIC_ORIGIN = "DASHBOARD_PUBLIC_ORIGIN"
# Central runner configuration repository whose repos/<owner>/<repo>.yaml files are controlled targets.
ENV_CONFIG_REPO = "SPOT_CONFIG_REPO"
CONFIG_REPO_NAME = "spot-runners-config"
_CONFIG_REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
FREEZE_MARKER_SUFFIX = ".frozen"
# Durable recovery hold: written (0600, fsynced) BEFORE an unreadable state file is moved aside and removed only
# after verified state is restored and persisted. While it exists every start fails closed, even when the
# state path is empty, so a restart in between can never fall back to defaults.
RECOVERY_MARKER_SUFFIX = ".recovery-pending"
# How long a freeze waits for in-flight external changes (pull requests, hot-patches) to finish and persist
# their idempotency records; kept under the control VM manager's 60-second control-call timeout.
FREEZE_EXTERNAL_DRAIN_SECONDS = 45.0
EXTERNAL_PENDING_RETRY_SECONDS = 3

LOG = logging.getLogger("spot_dashboard")


class _PayloadTooLargeError(ValueError):
    """Raised when an incoming HTTP JSON request body exceeds the configured byte limit."""


class DashboardFrozenError(RuntimeError):
    """Raised when a state mutation is attempted while the control VM is frozen for a handover."""


class FreezeBusyError(RuntimeError):
    """A freeze could not start because external changes were still running; the caller retries."""


class ControlledTargetsUnavailableError(RuntimeError):
    """The dynamic controlled-target policy could not be loaded; OAuth access fails closed."""


class ControlledTargetsConflictError(RuntimeError):
    """A pending controlled-target change was based on a list that has since changed."""


class DashboardAdminError(ValueError):
    """An added-administrator change was refused; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = int(status)



def snapshot_state_digest(state: Any) -> str:
    """SHA-256 of a snapshot's state, ignoring ``updated_at`` (a re-save of the same content changes only that)."""
    content = {k: v for k, v in state.items() if k != "updated_at"} if isinstance(state, dict) else state
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False,
                                     default=str).encode("utf-8")).hexdigest()


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
    tax_rate_percent: float | None = None,
) -> dict[str, Any]:
    ref_now = now_epoch if now_epoch is not None else time.time()
    family = machine_type.split("-", 1)[0]
    reg_info = next((r for r in REGIONS_CATALOG if r["region"] == region), REGIONS_CATALOG[0])
    tax_rate = (
        float(tax_rate_percent)
        if tax_rate_percent is not None
        else float(reg_info.get("tax_rate_default", 8.25))
    )
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
        machine_type=machine_type,
        project=project,
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
        "runner_labels": (
            [
                "self-hosted",
                "Linux",
                "X64",
                "spot-runner",
                "rust",
                "docker",
                f"gcp-{region}",
                family,
            ]
            if role == "cicd_runner"
            else []
        ),
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
        "service_account": None if role == "rust_builder" else "default",
        "scopes": [] if role == "rust_builder" else ["https://www.googleapis.com/auth/cloud-platform"],
        "gcloud_auth_flags": ["--no-service-account", "--no-scopes"] if role == "rust_builder" else [],
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
        # Dynamic GitHub OAuth controlled targets (empty by default); see dashboard_auth.
        "controlled_targets": [],
        # Bounded audit history of committed controlled-target changes.
        "controlled_targets_history": [],
        # Administrators the owner added on the Admins page: {login, uid, added_by, added_by_uid, added_at}.
        # They may sign in (matched by uid) and travel with every snapshot, so they survive a cutover.
        "dashboard_admins": [],
        # Bounded audit history of added-administrator changes.
        "dashboard_admins_history": [],
    }


class DashboardStore:
    """Thread-safe controller store backing the Web UI Dashboard API.

    If the state file exists but cannot be read or parsed (including PermissionError and
    IsADirectoryError), it is renamed aside to ``<state>.corrupt-<UTC time>`` (0600, never deleted) and the
    store recovers from the latest verified Cloudflare KV snapshot through the normal hydration path (or a
    control-plane import). Until then it stays unhydrated and fails closed: ``/api/health`` answers 503 with
    ``"status": "state_unavailable"`` and a ``reason``, every other request gets the wait response, and
    nothing is written to the state path. If the file cannot be moved aside, nothing ever replaces it.
    """

    def __init__(
        self,
        state_path: Path | None = None,
        *,
        project: str = "homelab-424523",
        org: str = "swack-tools",
        control_slot: str | None = None,
        kv: CloudflareKv | None = None,
        clock: Any = time.time,
        sleep: Any = time.sleep,
    ) -> None:
        self._lock = threading.RLock()
        self.state_path = state_path
        self.project = project
        self.org = org
        self.control_slot = validate_slot(control_slot) if control_slot else None
        if kv is not None and self.control_slot is None:
            LOG.warning("Cloudflare KV is configured but --control-slot is not; dashboard state stays local only")
        self.kv = kv if self.control_slot is not None else None
        self._clock = clock
        self._sleep = sleep
        self.hydrate_retry_delays: tuple[float, ...] = HYDRATE_RETRY_DELAYS_SECONDS
        # Durability flags: requests (other than health and control) are refused until hydrated.
        self.hydrated = False
        self.frozen = False
        # False after starting from the local file while KV was configured but unreachable: GETs work,
        # mutations get the wait response, and KV is retried until the two copies are reconciled.
        self.reconciled = True
        self._unreconciled_base_version: int | None = None
        self._reconcile_next_at = 0.0
        self._reconcile_delay = HYDRATE_BACKOFF_INITIAL_SECONDS
        self.kv_writer = False
        # True when the last successful read of origin:active named the other slot: a Worker with a cached
        # origin may still send requests here, but changes would never reach the active dashboard.
        self.known_inactive = False
        # Ownership of origin:active as last positively read from KV: "active", "inactive", or "unknown"
        # (never read, or the record is unreadable). With KV configured, mutations need "active".
        self.ownership = "unknown"
        # Why hydration or reconciliation is blocked (reported as the health "reason"); empty when fine.
        self.hydrate_problem = ""
        self.hydration_source = ""
        self._writer_checked_at: float | None = None
        self._kv_dirty = False
        self._kv_dirty_first_at = 0.0
        self._kv_dirty_last_at = 0.0
        self._kv_next_attempt_at = 0.0
        self.kv_last_written_version: int | None = None
        # Set when a published snapshot did not read back as ours: this store is fenced (changes wait).
        self.publish_conflict = ""
        self._conflict_expect: tuple[int, str] = (0, "")
        # Every KV snapshot write (background write-behind and freeze) runs under this lock, in version order.
        self._kv_write_lock = threading.Lock()
        # True once this VM, as the KV writer, has compared its state with the KV snapshot (and adopted a
        # newer one). Reset whenever it is not the writer, so a VM that becomes active by cutover catches up
        # with KV before it accepts a change or publishes.
        self._writer_synced = False
        self._defer: dict[str, Any] | None = None
        # Idempotency-Key -> (method, path, body fingerprint) of external changes that are still running.
        self._external_inflight: dict[str, tuple[str, str, str]] = {}
        # Signalled whenever an external change finishes, so a freeze can wait for in-flight ones to persist.
        self._external_done = threading.Condition(self._lock)
        self.freeze_drain_seconds = FREEZE_EXTERNAL_DRAIN_SECONDS
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        # A freeze survives container and VM restarts: the marker sits next to the state file until an
        # explicit unfreeze or a successful import, so a restarted old VM never accepts writes that the
        # replacement did not import.
        self.freeze_marker_path = (
            state_path.parent / f"{state_path.name}{FREEZE_MARKER_SUFFIX}" if state_path else None
        )
        if self.freeze_marker_path is not None and os.path.lexists(self.freeze_marker_path):
            self.frozen = True
            LOG.warning("Dashboard starts frozen: freeze marker %s is present", self.freeze_marker_path)
        self.loaded_from_file = False
        self.controlled_targets_error = ""
        # Set when the state file was unreadable: the store must recover from a verified snapshot first.
        self.recovery_pending = False
        self.recovery_detail = ""
        self.state_backup_path: Path | None = None
        # True when nothing of the unreadable state is left at the state path, so recovery may write there.
        self._state_path_clear = False
        self.recovery_marker_path = (
            state_path.parent / f"{state_path.name}{RECOVERY_MARKER_SUFFIX}" if state_path else None
        )
        held = self.recovery_marker_path is not None and os.path.lexists(self.recovery_marker_path)
        # Read the file directly: Path.exists() also reports False when stat fails (for example EACCES
        # on a parent directory), which would silently load an empty, fail-open policy. Only a genuinely
        # absent file starts from defaults (or KV).
        raw_state: str | None = None
        read_error: Exception | None = None
        if state_path and not held:
            try:
                raw_state = state_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw_state = None
            except (OSError, ValueError) as exc:
                read_error = exc
        if held:
            # An earlier start found the state unreadable and recovery never finished: stay closed, whatever is
            # (or is not) at the state path now.
            original = self._marker_field("reason")
            self._enter_recovery(
                f"Dashboard state recovery is still pending (marker {self.recovery_marker_path})"
                + (f"; first failure: {original}" if original else "")
                + "; the dynamic controlled-target policy is unavailable until verified state is restored",
                marker_present=True,
            )
        elif raw_state is not None or read_error is not None:
            try:
                if read_error is not None:
                    raise read_error
                loaded = json.loads(raw_state)
                self._apply_loaded_state(loaded)
            except (OSError, ValueError, TypeError) as exc:
                # Fail closed: keep the unreadable file aside and recover only from a verified snapshot.
                self._enter_recovery(
                    f"Dashboard state file {state_path} could not be loaded ({type(exc).__name__}); "
                    "the dynamic controlled-target policy is unavailable until state is recovered",
                    marker_present=False,
                )
            else:
                self.loaded_from_file = True
                try:
                    # Normalization write-back keeps the loaded state_version (not a new change).
                    self._persist(bump=False)
                except OSError:
                    # Preserve loaded and normalized in-memory state when startup write-back fails.
                    pass
        elif self.kv is not None:
            # No local state yet: hydrate from KV first. Writing version-0 defaults here would let a
            # restart before hydration mistake them for real local state.
            self.state = default_dashboard_state(project=project, org=org)
            self._sync_fleet_telemetry()
        else:
            self.state = default_dashboard_state(project=project, org=org)
            self._sync_fleet_telemetry()
            try:
                self._persist(bump=False)
            except OSError:
                # Preserve default in-memory state when initial persistence fails on a read-only path.
                pass

    # -- durability: load, version, hydration, KV write-behind, freeze/import, idempotency --

    def _enter_recovery(self, reason: str, *, marker_present: bool) -> None:
        """Fail closed and prepare recovery: durable hold marker first, then move anything at the state path aside."""
        assert self.state_path is not None and self.recovery_marker_path is not None
        self.controlled_targets_error = reason
        self.recovery_pending = True
        LOG.error("%s", reason)
        self.state = default_dashboard_state(project=self.project, org=self.org)
        self._sync_fleet_telemetry()
        previous_backup = self._marker_backup() if marker_present else None
        if not marker_present:
            try:
                self._write_private_durably(self.recovery_marker_path, {"reason": reason, "since": self._clock()})
            except OSError as exc:
                # Without a durable hold a restart after the rename could start from defaults: leave the file.
                self.recovery_detail = (f"could not record the recovery hold ({exc}), so the unreadable state file "
                                        "is left in place and nothing will replace it; repair it and restart")
                LOG.error("Dashboard state recovery: %s", self.recovery_detail)
                return
        if os.path.lexists(self.state_path):
            self.state_backup_path = self._preserve_unreadable_state()
            if self.state_backup_path is None:
                self.recovery_detail = (
                    "the state file could not be moved aside, so it is left in place and nothing will replace it; "
                    "repair or move it by hand and restart"
                )
                LOG.error("Dashboard state recovery: %s", self.recovery_detail)
                return
            try:
                self._write_private_durably(self.recovery_marker_path, {
                    "reason": (self._marker_field("reason") or reason) if marker_present else reason,
                    "since": self._clock(), "backup": str(self.state_backup_path)})
            except OSError as exc:
                LOG.warning("Could not note the backup path in %s: %s", self.recovery_marker_path, exc)
        else:
            self.state_backup_path = previous_backup
        self._state_path_clear = True
        kept = f"kept the unreadable state as {self.state_backup_path}" if self.state_backup_path else "state moved aside"
        self.recovery_detail = f"{kept}; waiting for a verified KV snapshot"
        LOG.error("Dashboard state recovery: %s", self.recovery_detail)

    def _marker_field(self, name: str) -> str:
        try:
            raw = json.loads(self.recovery_marker_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except (OSError, ValueError):
            return ""
        value = raw.get(name) if isinstance(raw, dict) else None
        return value[:500] if isinstance(value, str) else ""

    def _marker_backup(self) -> Path | None:
        backup = self._marker_field("backup")
        return Path(backup) if backup else None

    def _write_private_durably(self, path: Path, payload: dict[str, Any]) -> None:
        """Atomically write a 0600 JSON file next to the state file and fsync it and its directory. Raises OSError."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError(f"Refusing symlinked path: {path}")
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, allow_nan=False))
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(path)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._fsync_dir(path.parent)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _clear_recovery_marker(self) -> None:
        """Release the hold once verified state is durably on disk. A failure only keeps the hold (fail-closed)."""
        if self.recovery_marker_path is None:
            return
        try:
            self.recovery_marker_path.unlink(missing_ok=True)
            self._fsync_dir(self.recovery_marker_path.parent)
        except OSError as exc:
            LOG.error("Could not remove the recovery hold %s (the next start stays closed and recovers again): %s",
                      self.recovery_marker_path, exc)

    def _preserve_unreadable_state(self) -> Path | None:
        """Rename the unreadable state path to ``<name>.corrupt-<UTC>`` (0600). Returns it, or None on failure.

        A rename keeps the exact bytes (or directory) and never deletes anything; an existing backup name is
        never reused.
        """
        assert self.state_path is not None
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = f"{self.state_path.name}.corrupt-{stamp}"
        target = self.state_path.parent / base
        counter = 1
        while os.path.lexists(target):
            target = self.state_path.parent / f"{base}.{counter}"
            counter += 1
        try:
            os.rename(self.state_path, target)
        except OSError as exc:
            LOG.error("Could not move the unreadable dashboard state file %s aside: %s", self.state_path, exc)
            return None
        try:
            mode = os.lstat(target).st_mode
            if stat.S_ISDIR(mode):
                os.chmod(target, 0o700)
            elif stat.S_ISREG(mode):
                os.chmod(target, 0o600)
        except OSError as exc:
            LOG.warning("Could not restrict permissions on %s: %s", target, exc)
        try:
            fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
        return target

    def _read_kv_snapshot_verified(self) -> tuple[dict[str, Any] | None, str]:
        """Return (verified snapshot or None, outcome) with outcome ok, missing, invalid, or unreachable."""
        assert self.kv is not None
        attempts = len(self.hydrate_retry_delays) + 1
        for attempt in range(attempts):
            if self._stop_event.is_set():
                break
            try:
                raw = self.kv.get(KV_STATE_KEY)
            except KvError as exc:
                LOG.warning("Dashboard KV snapshot read %d/%d failed: %s", attempt + 1, attempts, exc)
                if attempt < attempts - 1:
                    self._sleep(self.hydrate_retry_delays[attempt])
                continue
            return self._classify_kv_snapshot(raw)
        return None, "unreachable"

    def _recover_unreadable_state(self) -> str:
        """Recover from the latest verified KV snapshot after an unreadable state file was moved aside."""
        if not self._state_path_clear:
            return "unavailable"
        if self.kv is None:
            self.recovery_detail = (
                f"kept the unreadable file as {self.state_backup_path}; no Cloudflare KV snapshot source is "
                "configured, so restore the file (or import a snapshot) and restart"
            )
            return "unavailable"
        snapshot, outcome = self._read_kv_snapshot_verified()
        if snapshot is None:
            why = {"missing": "Cloudflare KV holds no dashboard snapshot",
                   "invalid": "the Cloudflare KV snapshot failed verification",
                   "unreachable": "Cloudflare KV is unreachable"}.get(outcome, outcome)
            self.recovery_detail = f"kept the unreadable file as {self.state_backup_path}; {why}; retrying"
            LOG.error("Dashboard state recovery waiting: %s", self.recovery_detail)
            return "unavailable"
        self.refresh_writer()
        with self._lock:
            if not self.recovery_pending:
                return self.hydration_source  # A control-plane import recovered meanwhile.
            try:
                # Durable or nothing: the hold is released only once the recovered state is on disk.
                if not self._adopt_recovered_snapshot(snapshot, require_persist=True):
                    return "unavailable"
            except OSError as exc:
                self.recovery_detail = f"could not write the recovered state ({exc}); retrying"
                LOG.error("Dashboard state recovery: %s", self.recovery_detail)
                return "unavailable"
            self.hydration_source = "kv-recovery"
            self.hydrated = True
            self.reconciled = True
            self._writer_synced = bool(self.kv_writer)
        LOG.warning("Dashboard state recovered from the verified KV snapshot at state_version %d; the unreadable "
                    "file is kept as %s", self.state_version(), self.state_backup_path)
        return "kv-recovery"

    def _adopt_recovered_snapshot(self, snapshot: dict[str, Any], *, require_persist: bool) -> bool:
        """Adopt a verified snapshot in place of the unreadable state (caller holds the lock)."""
        error = self.controlled_targets_error
        self.controlled_targets_error = ""
        try:
            self._adopt_snapshot(snapshot, require_persist=require_persist)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self.controlled_targets_error = error
            self.recovery_detail = (f"kept the unreadable file as {self.state_backup_path}; the KV snapshot failed "
                                    f"validation ({type(exc).__name__}); retrying")
            LOG.error("Dashboard state recovery: %s", self.recovery_detail)
            return False
        except BaseException:
            self.controlled_targets_error = error
            raise
        self.recovery_pending = False
        self._clear_recovery_marker()
        self.recovery_detail = f"recovered from a verified snapshot; the unreadable file is kept as {self.state_backup_path}"
        return True

    def _apply_loaded_state(self, loaded: Any) -> None:
        """Validate and normalize a loaded state dict through the standard load path (atomic on failure)."""
        if not isinstance(loaded, dict):
            raise ValueError("dashboard state must be a JSON object")
        version = loaded.get("state_version", 0)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ValueError("dashboard state_version must be a non-negative integer")
        base = default_dashboard_state(project=self.project, org=self.org)
        for key, default_value in base.items():
            if key in loaded and isinstance(default_value, (list, dict)) and not isinstance(loaded[key], type(default_value)):
                raise ValueError(f"dashboard state field {key!r} must be a JSON {type(default_value).__name__}")
        base.update(copy.deepcopy(loaded))
        previous = getattr(self, "state", None)
        self.state = base
        try:
            self._normalize_project_derived_fields()
            self._normalize_github_ci_runs()
            self._sync_fleet_telemetry()
        except Exception as exc:
            if previous is not None:
                self.state = previous
            raise ValueError(f"invalid dashboard state: {type(exc).__name__}: {exc}") from exc

    def state_version(self) -> int:
        version = self.state.get("state_version", 0)
        return version if isinstance(version, int) and not isinstance(version, bool) and version >= 0 else 0

    def start(self) -> None:
        """Hydrate (in a background thread when KV is configured) and start KV write-behind."""
        if self.kv is None:
            self.hydrate()
            return
        thread = threading.Thread(target=self._background_main, name="dashboard-kv-sync", daemon=True)
        self._threads.append(thread)
        thread.start()

    def close(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=timeout)

    def _background_main(self) -> None:
        delay = HYDRATE_BACKOFF_INITIAL_SECONDS
        while not self._stop_event.is_set():
            try:
                self.hydrate()
            except Exception:  # noqa: BLE001 - keep retrying; serve local state when there is some
                LOG.exception("Dashboard hydration failed")
                if self.loaded_from_file:
                    with self._lock:
                        if not self.hydrated:
                            self._mark_unreconciled()
                            self.hydrated = True
            if self.hydrated:
                break
            # No local state and KV unreachable: stay unhydrated (health 503) rather than serve defaults.
            self._stop_event.wait(delay)
            delay = min(delay * 2, HYDRATE_BACKOFF_MAX_SECONDS)
        while not self._stop_event.is_set():
            try:
                self.kv_tick()
            except Exception:  # noqa: BLE001 - the write-behind loop must keep running
                LOG.exception("Dashboard KV write-behind iteration failed")
            self._stop_event.wait(KV_LOOP_INTERVAL_SECONDS)

    @staticmethod
    def _classify_kv_snapshot(raw: str | None) -> tuple[dict[str, Any] | None, str]:
        """(snapshot, outcome): ``ok``; ``missing`` only when KV positively has no key; else ``invalid``."""
        if raw is None:
            return None, "missing"
        snapshot = parse_snapshot(raw)
        return (snapshot, "ok") if snapshot is not None else (None, "invalid")

    def hydrate(self) -> str:
        """Choose the newer of the local state file and the KV snapshot (tie: local), then mark hydrated.

        Returns the chosen source: ``local``, ``kv``, ``kv-recovery``, or ``default``, or ``unavailable``
        (still unhydrated) when there is no local state and KV is configured but unreachable. Defaults are
        used only when KV is not configured or positively reports that no snapshot exists, and never after
        an unreadable state file: that case recovers only from a verified KV snapshot.
        """
        if self.recovery_pending:
            return self._recover_unreadable_state()
        snapshot: dict[str, Any] | None = None
        reachable = True
        outcome = "missing"
        if self.kv is not None:
            snapshot, outcome = self._read_kv_snapshot_verified()
            reachable = outcome != "unreachable"
            if outcome == "invalid" and not self.loaded_from_file:
                # An existing snapshot that fails verification is not "no snapshot": starting from defaults
                # would drop the saved policy and let later writes replace the real snapshot.
                self.hydrate_problem = ("the Cloudflare KV dashboard snapshot exists but failed verification; "
                                        "refusing to start from defaults; retrying")
                LOG.error("Dashboard stays unhydrated: %s", self.hydrate_problem)
                return "unavailable"
            if not reachable:
                if not self.loaded_from_file:
                    LOG.error("Cloudflare KV unreachable and no local dashboard state; staying unhydrated "
                              "and retrying instead of serving defaults")
                    return "unavailable"
                LOG.warning("Cloudflare KV unreachable during hydration; serving local dashboard state "
                            "read-only until KV is reachable again")
            self.refresh_writer()
        with self._lock:
            if self.hydrated:
                # A control-plane import hydrated the store meanwhile; it is authoritative.
                return self.hydration_source
            if not reachable:
                self._mark_unreconciled()
            source = "local" if self.loaded_from_file else "default"
            kv_unusable = outcome == "invalid"
            if snapshot is not None:
                newer = snapshot["state_version"] > self.state_version()
                if newer or not self.loaded_from_file:
                    try:
                        self._adopt_snapshot(snapshot)
                        source = "kv"
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        if not self.loaded_from_file:
                            self.hydrate_problem = (f"the Cloudflare KV dashboard snapshot failed validation "
                                                    f"({type(exc).__name__}); refusing to start from defaults; retrying")
                            LOG.error("Dashboard stays unhydrated: %s", self.hydrate_problem)
                            return "unavailable"
                        kv_unusable = True
            if kv_unusable:
                # Local state is served read-only: a KV copy that cannot be verified may be newer, so changes wait
                # until reconciliation can compare the two.
                self.hydrate_problem = ("the Cloudflare KV dashboard snapshot failed verification; serving local "
                                        "state read-only until KV holds a verifiable snapshot")
                LOG.error("%s", self.hydrate_problem)
                self._mark_unreconciled()
            if source == "local" and reachable and not kv_unusable and (
                    snapshot is None or snapshot["state_version"] < self.state_version()):
                # The local file is ahead of KV (for example a preemption interrupted write-behind after a
                # successful local save): publish it once this VM is the writer.
                self._mark_kv_dirty()
            self.hydration_source = source
            self.hydrated = True
            if not kv_unusable:
                self.hydrate_problem = ""
            self._writer_synced = bool(reachable and not kv_unusable and self.kv_writer)
        LOG.info("Dashboard hydrated from %s at state_version %d", source, self.state_version())
        return source

    def _adopt_snapshot(self, snapshot: dict[str, Any], *, require_persist: bool = False) -> None:
        """Replace state with a validated snapshot envelope and keep its state_version (no bump).

        With ``require_persist`` a failed local write restores the previous state and raises OSError.
        """
        previous = self.state
        loaded = dict(snapshot["state"])
        loaded["state_version"] = int(snapshot["state_version"])
        self._apply_loaded_state(loaded)
        self.state["state_version"] = int(snapshot["state_version"])
        try:
            self._persist(bump=False)
        except OSError as exc:
            if require_persist:
                self.state = previous
                raise
            LOG.warning("Could not write adopted dashboard snapshot to the local state file: %s", exc)

    # -- reconciliation after a start from local state while KV was unreachable --

    def _mark_unreconciled(self) -> None:
        if self.kv is None:
            return
        self.reconciled = False
        self._unreconciled_base_version = self.state_version()
        self._reconcile_next_at = 0.0
        self._reconcile_delay = HYDRATE_BACKOFF_INITIAL_SECONDS

    def reconcile(self) -> bool:
        """Compare the local start state with KV once KV answers; True when mutations may resume.

        KV at or above the version the store started from wins (it is imported). Only when KV is older
        or empty is the local state published (by the active slot). The comparison uses the starting
        version, so local counter growth while unreconciled never authorizes overwriting KV.
        """
        if self.reconciled or self.kv is None:
            return True
        try:
            raw = self.kv.get(KV_STATE_KEY)
        except KvError as exc:
            LOG.warning("Dashboard still unreconciled; KV unreachable: %s", exc)
            return False
        snapshot, outcome = self._classify_kv_snapshot(raw)
        if outcome == "invalid":
            self.hydrate_problem = ("the Cloudflare KV dashboard snapshot failed verification; serving local state "
                                    "read-only until KV holds a verifiable snapshot")
            LOG.error("Dashboard stays unreconciled: %s", self.hydrate_problem)
            return False
        base = self._unreconciled_base_version or 0
        if snapshot is not None and snapshot["state_version"] >= base:
            with self._lock:
                if self.reconciled:
                    return True
                try:
                    self._adopt_snapshot(snapshot)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    LOG.error("KV snapshot is invalid; dashboard stays unreconciled: %s", exc)
                    return False
                self._finish_reconcile("kv")
            return True
        if self.refresh_writer():
            try:
                written = self._publish_snapshot()
            except KvError as exc:
                LOG.warning("Could not publish local dashboard state during reconciliation: %s", exc)
                return False
            if written is None:
                return False
        with self._lock:
            self._finish_reconcile("local")
        return True

    def _finish_reconcile(self, source: str) -> None:
        self.reconciled = True
        self._unreconciled_base_version = None
        self.hydrate_problem = ""
        LOG.warning("Dashboard reconciled with KV using the %s copy at state_version %d", source,
                    self.state_version())

    def import_snapshot(self, envelope: Any) -> dict[str, Any]:
        """Import a snapshot envelope when its state_version >= local; always marks the store hydrated.

        Only the control plane's handover target receives an import, so a successful import also clears
        any freeze (in memory and the durable marker): this VM is about to become the active slot.
        """
        snapshot = parse_snapshot(envelope)
        if snapshot is None:
            raise ValueError("Invalid dashboard snapshot envelope")
        with self._lock:
            if self.recovery_pending:
                if not self._state_path_clear:
                    raise OSError("the unreadable dashboard state file is still in place; refusing to overwrite it")
                # A verified snapshot from the control plane recovers the store like a KV snapshot would.
                self._adopt_recovered_snapshot(snapshot, require_persist=True)
                if self.recovery_pending:
                    raise ValueError("the imported snapshot failed validation")
                self._mark_kv_dirty()
                imported = True
                self.hydration_source = "import"
                if self.frozen or (self.freeze_marker_path is not None and os.path.lexists(self.freeze_marker_path)):
                    self._clear_freeze_marker()
                    self.frozen = False
                self.hydrated = True
                self.reconciled = True
                self._unreconciled_base_version = None
                return {"state_version": self.state_version(), "imported": imported}
            imported = snapshot["state_version"] >= self.state_version()
            if imported:
                # A failed local write raises OSError (state unchanged, not hydrated): the caller gets a
                # 500 and must not treat the import as done.
                self._adopt_snapshot(snapshot, require_persist=True)
                self._mark_kv_dirty()
            if self.frozen or (self.freeze_marker_path is not None and os.path.lexists(self.freeze_marker_path)):
                self._clear_freeze_marker()
                self.frozen = False
                LOG.info("Import cleared the dashboard freeze")
            self.hydrated = True
            self.reconciled = True
            self._unreconciled_base_version = None
            self.hydration_source = "import" if imported else self.hydration_source
            self.hydrate_problem = ""
            return {"state_version": self.state_version(), "imported": imported}

    def snapshot_envelope(self) -> dict[str, Any]:
        if self.control_slot is None:
            raise ValueError("Snapshots require --control-slot")
        if self.controlled_targets_error:
            # The in-memory state is not the unreadable file's content; publishing it would hand the next
            # VM (through KV or an import) a policy that silently lost its controlled targets.
            raise ValueError(f"Refusing to snapshot dashboard state: {self.controlled_targets_error}")
        with self._lock:
            return build_snapshot(copy.deepcopy(self.state), self.state_version(), self.control_slot,
                                  now=self._clock())

    def refresh_writer(self) -> bool:
        """This VM writes KV iff ``origin:active`` names its slot or is unset. Unchanged on KV errors."""
        if self.kv is None or self.control_slot is None:
            self.kv_writer = False
            return False
        try:
            raw = self.kv.get(KV_ORIGIN_KEY)
        except KvError as exc:
            LOG.warning("Could not read %s from Cloudflare KV: %s", KV_ORIGIN_KEY, exc)
            return self.kv_writer
        return self._apply_origin(raw)

    def _apply_origin(self, raw: str | None) -> bool:
        """Classify a successful ``origin:active`` read: missing key -> this VM may write (bootstrap); a record
        naming this slot -> active; another slot -> inactive; an existing but malformed value -> UNKNOWN (never
        "no owner")."""
        if raw is None:
            writer = True
            self.known_inactive = False
            self.ownership = "active"
        else:
            record = OriginRecord.from_json(raw)
            writer = record is not None and record.slot == self.control_slot
            self.known_inactive = record is not None and record.slot != self.control_slot
            self.ownership = "active" if writer else ("inactive" if self.known_inactive else "unknown")
        if not writer:
            self._writer_synced = False
        self.kv_writer = writer
        self._writer_checked_at = self._clock()
        return writer

    def _confirm_ownership_now(self) -> None:
        """Re-read ``origin:active`` for THIS mutation: a cutover may have moved ownership since any earlier read
        (an unreachable old slot keeps its stale writer flags), so only a successful read naming this slot counts."""
        if self.kv is None:
            return
        try:
            raw = self.kv.get(KV_ORIGIN_KEY)
        except KvError as exc:
            raise DashboardFrozenError(f"Could not confirm ownership of {KV_ORIGIN_KEY}: {exc}") from exc
        self._apply_origin(raw)
        if self.ownership != "active":
            raise DashboardFrozenError(
                "This control VM is not the active slot" if self.known_inactive
                else f"{KV_ORIGIN_KEY} is unreadable, so this control VM cannot confirm it is the active slot")

    def sync_with_kv_as_writer(self) -> bool:
        """When this VM is (or just became) the writer, adopt a newer KV snapshot before going further.

        Returns True when it is safe to accept a change or publish: not the writer, or the writer and
        already compared with KV. Returns False when KV could not be read after becoming the writer.
        """
        if self.kv is None:
            return True
        if self.kv_writer and self._writer_synced:
            return True
        if not self.refresh_writer():
            return True
        if self._writer_synced:
            return True
        try:
            snapshot, outcome = self._classify_kv_snapshot(self.kv.get(KV_STATE_KEY))
        except KvError as exc:
            LOG.warning("Became the KV writer but could not read the KV snapshot: %s", exc)
            return False
        if outcome == "invalid":
            LOG.error("Became the KV writer but the KV snapshot failed verification; refusing changes")
            return False
        with self._lock:
            if snapshot is not None and snapshot["state_version"] > self.state_version():
                try:
                    self._adopt_snapshot(snapshot)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    LOG.error("Became the KV writer but the newer KV snapshot is invalid: %s", exc)
                    return False
                self._kv_dirty = False  # Local state now equals the KV copy.
                LOG.warning("Became the KV writer; adopted newer KV snapshot at state_version %d",
                            snapshot["state_version"])
            self._writer_synced = True
        return True

    def _mark_kv_dirty(self) -> None:
        if self.kv is None:
            return
        now = self._clock()
        if not self._kv_dirty:
            self._kv_dirty_first_at = now
        self._kv_dirty = True
        self._kv_dirty_last_at = now

    def _restore_kv_dirty(self) -> None:
        with self._lock:
            if not self._kv_dirty:
                self._kv_dirty = True
                self._kv_dirty_first_at = self._kv_dirty_last_at = self._clock()

    def _publish_snapshot(self, envelope: dict[str, Any] | None = None) -> int | None:
        """Write a snapshot to KV, serialized with every other KV snapshot write.

        ``envelope`` None captures the current state inside the write lock. Returns the state_version now
        in KV from this writer, or None when the write was refused because it would move KV backwards
        (older than this process's last write, or older than the snapshot already in KV). Raises KvError.
        """
        assert self.kv is not None
        with self._kv_write_lock:
            captured = envelope is None
            if captured:
                with self._lock:
                    if self.frozen:
                        # A freeze (and maybe a handover) happened while this write waited for the lock.
                        return None
                    envelope = self.snapshot_envelope()
                    self._kv_dirty = False
            version = int(envelope["state_version"])
            last = self.kv_last_written_version
            if last is not None and version <= last:
                if version < last:
                    LOG.error("Refusing to write dashboard snapshot state_version %d to KV after already "
                              "writing state_version %d", version, last)
                    return None
                return version  # Already published.
            if envelope.get("writer_slot") != self.control_slot:
                LOG.error("REFUSING to publish a dashboard snapshot that names writer slot %r from slot %s",
                          envelope.get("writer_slot"), self.control_slot)
                return None
            digest = snapshot_state_digest(envelope.get("state") or {})
            try:
                # Under the write lock, immediately before the put: a fresh read must show this VM owns
                # origin:active (a malformed record is unknown ownership), and KV must not hold a newer snapshot
                # or a different one at the same version.
                if not self._apply_origin(self.kv.get(KV_ORIGIN_KEY)):
                    LOG.warning("Skipping dashboard KV write: slot %s does not own origin:active (%s)",
                                self.control_slot, self.ownership)
                    if captured:
                        self._restore_kv_dirty()
                    return None
                raw_current = self.kv.get(KV_STATE_KEY)
                current, outcome = self._classify_kv_snapshot(raw_current)
                if outcome == "invalid":
                    LOG.error("REFUSING to overwrite a KV dashboard snapshot that failed verification; it may "
                              "be newer than this VM's state")
                    if captured:
                        self._restore_kv_dirty()
                    return None
                if current is not None and current["state_version"] > version:
                    LOG.error("REFUSING to overwrite the KV dashboard snapshot at state_version %d (slot %s) with "
                              "local state_version %d; this VM's state is stale",
                              current["state_version"], current.get("writer_slot"), version)
                    if captured:
                        self._restore_kv_dirty()
                    return None
                if current is not None and current["state_version"] == version:
                    if snapshot_state_digest(current["state"]) == digest:
                        self.kv_last_written_version = version  # Idempotent republish: KV already has it.
                        return version
                    # Same version, different content: never overwrite it. From the other slot this is expected right
                    # after a handover import (local normalization differs); KV already holds that history, and
                    # this VM's next change publishes version + 1.
                    log = LOG.error if current.get("writer_slot") == self.control_slot else LOG.warning
                    log("Not overwriting the KV dashboard snapshot at state_version %d (slot %s): this VM holds "
                        "different content at the same version", version, current.get("writer_slot"))
                    return None
                self.kv.put(KV_STATE_KEY, json.dumps(envelope, separators=(",", ":"), allow_nan=False))
                verified = self._verify_published(version, digest)
            except KvError:
                if captured:
                    self._restore_kv_dirty()
                raise
            if not verified:
                return None
            self.kv_last_written_version = version
            return version

    def _verify_published(self, version: int, digest: str) -> bool:
        """Read the snapshot back after a PUT; anything but our slot, version, and content fences this store."""
        try:
            stored, _outcome = self._classify_kv_snapshot(self.kv.get(KV_STATE_KEY))
        except KvError as exc:
            stored = None
            LOG.warning("Could not read back the published dashboard snapshot: %s", exc)
        if (stored is not None and stored.get("writer_slot") == self.control_slot
                and stored["state_version"] == version and snapshot_state_digest(stored["state"]) == digest):
            self.publish_conflict = ""
            return True
        found = (f"slot {stored.get('writer_slot')} state_version {stored['state_version']}" if stored is not None
                 else "an unreadable value")
        self.publish_conflict = (f"after writing state_version {version} from slot {self.control_slot}, KV holds "
                                 f"{found}; changes wait until KV shows this slot's snapshot")
        self._conflict_expect = (version, digest)
        self.kv_writer = False
        self._writer_synced = False
        LOG.error("DASHBOARD KV PUBLISH CONFLICT: %s", self.publish_conflict)
        return False

    def _resolve_publish_conflict(self) -> bool:
        """True once KV shows the snapshot this store wrote (for example after eventual consistency caught up)."""
        if not self.publish_conflict or self.kv is None:
            return True
        version, digest = self._conflict_expect
        try:
            stored, _outcome = self._classify_kv_snapshot(self.kv.get(KV_STATE_KEY))
        except KvError:
            return False
        if (stored is not None and stored.get("writer_slot") == self.control_slot
                and stored["state_version"] == version and snapshot_state_digest(stored["state"]) == digest):
            LOG.warning("Dashboard KV publish conflict resolved: KV shows this slot's state_version %d", version)
            self.publish_conflict = ""
            self.kv_last_written_version = version
            return True
        return False

    def _kv_put_snapshot(self) -> int | None:
        """Background write of the current state. Returns the written state_version (None when refused)."""
        return self._publish_snapshot()

    def kv_tick(self) -> bool:
        """One write-behind iteration. Returns True when a snapshot was written to KV."""
        if self.kv is None:
            return False
        now = self._clock()
        if self.publish_conflict:
            # Fenced after a publish that did not read back as ours: only re-check; never write again until resolved.
            if now >= self._kv_next_attempt_at and not self._resolve_publish_conflict():
                self._kv_next_attempt_at = now + KV_RETRY_BACKOFF_SECONDS
            return False
        if self.hydrated and not self.reconciled:
            if now >= self._reconcile_next_at and not self.reconcile():
                self._reconcile_next_at = now + self._reconcile_delay
                self._reconcile_delay = min(self._reconcile_delay * 2, HYDRATE_BACKOFF_MAX_SECONDS)
            return False
        if self._writer_checked_at is None or now - self._writer_checked_at >= KV_WRITER_RECHECK_SECONDS:
            self.refresh_writer()
        if self.hydrated and self.kv_writer and not self._writer_synced and not self.sync_with_kv_as_writer():
            self._kv_next_attempt_at = now + KV_RETRY_BACKOFF_SECONDS
            return False
        if not (self.hydrated and not self.frozen and self.kv_writer and self._kv_dirty):
            return False
        quiet = now - self._kv_dirty_last_at >= KV_WRITE_DEBOUNCE_SECONDS
        overdue = now - self._kv_dirty_first_at >= KV_WRITE_MAX_DELAY_SECONDS
        if not (quiet or overdue) or now < self._kv_next_attempt_at:
            return False
        try:
            written = self._kv_put_snapshot()
        except KvError as exc:
            LOG.warning("Dashboard KV write-behind failed: %s", exc)
            self._kv_next_attempt_at = now + KV_RETRY_BACKOFF_SECONDS
            return False
        if written is None:
            self._kv_next_attempt_at = now + KV_STALE_REFUSAL_BACKOFF_SECONDS
            return False
        return True

    def _fsync_state_dir(self) -> None:
        assert self.freeze_marker_path is not None
        try:
            fd = os.open(str(self.freeze_marker_path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _write_freeze_marker(self, state_version: int) -> None:
        """Atomically write the 0600 freeze marker next to the state file. Raises OSError."""
        if self.freeze_marker_path is None:
            return
        marker = self.freeze_marker_path
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.is_symlink():
            raise OSError(f"Refusing symlinked freeze marker path: {marker}")
        payload = json.dumps({"frozen": True, "state_version": state_version, "slot": self.control_slot,
                              "frozen_at": self._clock()}, allow_nan=False)
        tmp = marker.parent / f".{marker.name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(marker)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._fsync_state_dir()

    def _clear_freeze_marker(self) -> None:
        """Remove the freeze marker (durably). Raises OSError when it cannot be removed."""
        if self.freeze_marker_path is None or not os.path.lexists(self.freeze_marker_path):
            return
        self.freeze_marker_path.unlink()
        self._fsync_state_dir()

    def freeze(self) -> dict[str, Any]:
        """Stop accepting mutations, persist the freeze marker, and synchronously flush the snapshot to KV.

        Raises OSError (the store stays frozen in memory) when the durable marker cannot be written, so the
        caller reports failure instead of a freeze that a restart would silently undo.
        """
        if self.controlled_targets_error:
            raise ValueError(f"Refusing to freeze for a handover: {self.controlled_targets_error}")
        with self._lock:
            was_frozen = self.frozen
            # Frozen first: no new change (internal or external) can start from here on.
            self.frozen = True
            # An external change that is still running must land its idempotency record in this snapshot, or
            # a retry with the same key on the replacement would repeat the external side effect.
            deadline = time.monotonic() + self.freeze_drain_seconds
            while self._external_inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if not was_frozen:
                        self.frozen = False
                    raise FreezeBusyError(
                        f"{len(self._external_inflight)} external change(s) still running; retry the freeze")
                self._external_done.wait(remaining)
            self._write_freeze_marker(self.state_version())  # On failure the store stays frozen in memory.
            envelope = self.snapshot_envelope()
        flushed = False
        if self.kv is not None and self.refresh_writer():
            try:
                # Waits for any in-flight background write, which can therefore never land after this one.
                written = self._publish_snapshot(envelope)
                flushed = written is not None and written >= int(envelope["state_version"])
                if flushed:
                    with self._lock:
                        if self.state_version() == envelope["state_version"]:
                            self._kv_dirty = False
            except KvError as exc:
                LOG.warning("Dashboard KV flush during freeze failed: %s", exc)
        return {"state_version": envelope["state_version"], "snapshot": envelope, "kv_flushed": flushed}

    def unfreeze(self) -> dict[str, Any]:
        """Accept mutations again. Raises OSError (and stays frozen) when the marker cannot be removed."""
        with self._lock:
            self._clear_freeze_marker()
            self.frozen = False
        return {"frozen": False}

    def health(self) -> dict[str, Any]:
        with self._lock:
            hydrated, frozen, reconciled = bool(self.hydrated), bool(self.frozen), bool(self.reconciled)
            if self.recovery_pending:
                # Not ready (hydrated false, HTTP 503): the control-VM controller treats this VM as unhealthy and
                # replaces it after its grace period; the reason tells the operator what happened.
                return {
                    "status": "state_unavailable",
                    "hydrated": False,
                    "frozen": frozen,
                    "reconciled": reconciled,
                    "active": not self.known_inactive,
                    "ownership": self.ownership,
                    "state_version": self.state_version(),
                    "slot": self.control_slot,
                    "kv_writer": bool(self.kv_writer),
                    "reason": f"{self.controlled_targets_error}; {self.recovery_detail}",
                    "state_backup": str(self.state_backup_path) if self.state_backup_path else None,
                }
            status = "frozen" if frozen else ("ok" if hydrated and reconciled else "starting")
            if self.hydrate_problem and not hydrated:
                status = "state_unavailable"
            extra = {"reason": self.hydrate_problem} if self.hydrate_problem else {}
            if self.publish_conflict and not frozen:
                # Not "ok", so the control-VM controller sees it; changes wait until KV shows this slot's write.
                status = "state_conflict"
                extra = {"reason": self.publish_conflict}
            return {
                **extra,
                "status": status,
                "hydrated": hydrated,
                "frozen": frozen,
                "reconciled": reconciled,
                "active": not self.known_inactive,
                "ownership": self.ownership,
                "state_version": self.state_version(),
                "slot": self.control_slot,
                "kv_writer": bool(self.kv_writer),
            }

    def _prune_idempotency(self, records: dict[str, Any], now: float) -> dict[str, Any]:
        fresh = {
            k: v for k, v in records.items()
            if isinstance(v, dict) and isinstance(v.get("t"), (int, float)) and now - float(v["t"]) < IDEMPOTENCY_TTL_SECONDS
        }
        if len(fresh) > IDEMPOTENCY_MAX_KEYS:
            keep = sorted(fresh, key=lambda k: float(fresh[k]["t"]), reverse=True)[:IDEMPOTENCY_MAX_KEYS]
            fresh = {k: fresh[k] for k in keep}
        return fresh

    def run_request(self, key: str | None, method: str, path: str, fn: Any, *, mutating: bool = True,
                    fingerprint: str = "") -> tuple[int, Any, bool]:
        """Run ``fn() -> (status, body)`` with frozen and idempotency handling. Returns (status, body, replayed).

        With a key, the stored response and the mutation are written in the SAME atomic persist: store
        writes inside ``fn`` are deferred and the state is rolled back when the request fails.
        ``fingerprint`` (see :func:`request_fingerprint`) binds the record to the request body: reusing a key
        with a different method, path, or body gets 422 and never replays the first response over the second
        change. A record saved before fingerprints were stored has no ``f`` and cannot prove the body matched,
        so it also gets 422 instead of a replay (fail closed: no second change is silently dropped).
        """
        if not mutating:
            status, body = fn()
            return status, body, False
        self._mutation_preflight()
        with self._lock:
            self._mutation_locked_checks()
            if not key:
                status, body = fn()
                return status, body, False
            now = self._clock()
            raw_records = self.state.get(IDEMPOTENCY_STATE_KEY)
            records = self._prune_idempotency(raw_records if isinstance(raw_records, dict) else {}, now)
            existing = records.get(key)
            if existing is not None:
                if existing.get("m") != method or existing.get("p") != path or existing.get("f") != fingerprint:
                    return 422, {"error": "Idempotency-Key was already used for a different request"}, False
                return int(existing.get("s", 200)), copy.deepcopy(existing.get("b")), True
            snapshot = copy.deepcopy(self.state)
            self._defer = {"thread": threading.get_ident(), "dirty": False, "after_commit": []}
            defer = self._defer
            try:
                status, body = fn()
            except BaseException:
                self.state = snapshot
                raise
            finally:
                self._defer = None
            if status >= 400:
                self.state = snapshot
            if status >= 500:
                return status, body, False
            records = self._prune_idempotency(
                {**records, key: {"m": method, "p": path, "f": fingerprint, "s": int(status),
                                  "b": self._replay_copy(status, body), "t": now}}, now
            )
            self.state[IDEMPOTENCY_STATE_KEY] = records
            try:
                self._persist()
            except Exception:  # noqa: BLE001 - roll back the mutation and its idempotency record together
                self.state = snapshot
                LOG.exception("Failed to persist dashboard state for an idempotent request")
                return 500, {"error": "Failed to persist dashboard state; retry the request"}, False
            if status < 400:
                for callback in defer["after_commit"]:
                    callback()
            return status, body, False

    def _after_commit(self, callback: Any) -> None:
        """Run ``callback`` once the current change is durable.

        Inside an idempotent request the write is deferred to :meth:`run_request`, so the callback waits for
        that commit and is dropped when the request rolls back. Outside one, the change was already persisted
        synchronously (a failure raised before reaching here), so it runs now.
        """
        defer = self._defer
        if defer is not None and defer.get("thread") == threading.get_ident():
            defer.setdefault("after_commit", []).append(callback)
        else:
            callback()

    def _mutation_preflight(self) -> None:
        """Gates every change shares (outside the lock): fresh ownership, writer sync, active slot."""
        if self.publish_conflict and not self._resolve_publish_conflict():
            raise DashboardFrozenError(f"Dashboard KV publish conflict: {self.publish_conflict}")
        if self.hydrated and not self.frozen:
            self._confirm_ownership_now()
        if self.hydrated and not self.frozen and not self.sync_with_kv_as_writer():
            raise DashboardFrozenError("Dashboard became the active slot but cannot read Cloudflare KV yet")
        if self.known_inactive:
            # sync_with_kv_as_writer re-read origin:active just now (unless KV is unreachable, in which case
            # the last successful read stands).
            raise DashboardFrozenError("This control VM is not the active slot; changes go to the active one")
        if self.kv is not None and self.ownership != "active":
            # Ownership must be positively established: a Worker with a cached origin may route here.
            raise DashboardFrozenError("This control VM has not confirmed that it is the active slot yet")

    def _mutation_locked_checks(self) -> None:
        if not self.hydrated:
            raise DashboardFrozenError("Dashboard state is not hydrated yet")
        if self.frozen:
            raise DashboardFrozenError("Dashboard is frozen for a control VM handover")
        if not self.reconciled:
            raise DashboardFrozenError("Dashboard state is not yet reconciled with Cloudflare KV")

    @staticmethod
    def _replay_copy(status: int, body: Any) -> Any:
        # Store an independent copy: the response may share objects with live state that later requests change.
        try:
            serialized = json.dumps(body, allow_nan=False)
            if len(serialized) > IDEMPOTENCY_MAX_BODY_BYTES:
                return {
                    "status": "ok" if status < 400 else "error",
                    "detail": "The original response was too large to store for replay; reload to see current state.",
                }
            return json.loads(serialized)
        except (TypeError, ValueError):
            return {"status": "ok" if status < 400 else "error"}

    @staticmethod
    def _retry_with_same_key(body: Any) -> bool:
        """True for an outcome the client is told to retry with the same Idempotency-Key (an ambiguous GitHub
        result marked ``retry_same_key``, or a ``pending_retry`` answer); such outcomes are never stored."""
        return isinstance(body, dict) and (body.get("retry_same_key") is True or body.get("pending_retry") is True)

    def run_external(self, key: str, method: str, path: str, fn: Any, *, fingerprint: str = "") -> tuple[int, Any, bool]:
        """Run a change whose side effect is outside dashboard state (GitHub, runner hosts) exactly once per key.

        The same gates as :meth:`run_request` apply (raises DashboardFrozenError), but ``fn`` runs WITHOUT the
        store lock, because it makes slow network calls. ``fingerprint`` identifies the request body.
        - A retry of the same request while the first is still running gets 202 ``{"status": "pending"}`` (the
          browser waits and retries with the same key, for example after the edge Worker timed out).
        - A later retry replays the stored response (``replayed`` True). Final responses below 500 are stored;
          outcomes marked for a same-key retry (``retry_same_key``, ``pending_retry``) are not, so the retry
          runs again and reconciles.
        - The same key with a different method, path, or body gets 422, running or finished.
        """
        if not key:
            raise ValueError("an Idempotency-Key is required")
        mismatch = (422, {"error": "Idempotency-Key was already used for a different request"}, False)
        self._mutation_preflight()
        with self._lock:
            self._mutation_locked_checks()
            now = self._clock()
            raw_records = self.state.get(IDEMPOTENCY_STATE_KEY)
            records = self._prune_idempotency(raw_records if isinstance(raw_records, dict) else {}, now)
            existing = records.get(key)
            if existing is not None:
                if existing.get("m") != method or existing.get("p") != path or existing.get("f", fingerprint) != fingerprint:
                    return mismatch
                return int(existing.get("s", 200)), copy.deepcopy(existing.get("b")), True
            running = self._external_inflight.get(key)
            if running is not None:
                if running != (method, path, fingerprint):
                    return mismatch
                return 202, {"status": "pending", "pending_retry": True,
                             "retry_after_seconds": EXTERNAL_PENDING_RETRY_SECONDS,
                             "message": "Still running; retry with the same Idempotency-Key for the result"}, False
            self._external_inflight[key] = (method, path, fingerprint)
        try:
            status, body = fn()
        except BaseException:
            with self._lock:
                self._external_inflight.pop(key, None)
                self._external_done.notify_all()
            raise
        with self._lock:
            try:
                if status >= 500 or self._retry_with_same_key(body):
                    # Not a final result: a retry with this key must run again (and reconcile), not replay it.
                    return status, body, False
                raw_records = self.state.get(IDEMPOTENCY_STATE_KEY)
                records = self._prune_idempotency(raw_records if isinstance(raw_records, dict) else {}, self._clock())
                records[key] = {"m": method, "p": path, "f": fingerprint, "s": int(status),
                                "b": self._replay_copy(status, body), "t": self._clock()}
                # Recorded in memory first and kept there even if the write fails: a completed external result
                # is never dropped, and the next successful save or a freeze snapshot carries it.
                self.state[IDEMPOTENCY_STATE_KEY] = self._prune_idempotency(records, self._clock())
                try:
                    self._persist()
                except Exception:  # noqa: BLE001 - the side effect already happened; report it
                    LOG.exception("Could not persist the idempotency record for %s %s", method, path)
                    if isinstance(body, dict):
                        body = {**body, "warning": "The result is kept in memory but could not be saved to disk yet."}
                return status, body, False
            finally:
                # Released only after the record is in state, so a waiting freeze snapshots it.
                self._external_inflight.pop(key, None)
                self._external_done.notify_all()

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
        prof = self.state.get("profiles", {}).get("cicd_runner", {})
        if prof:
            r_vcpu = max(1, int(prof.get("runner_vcpu", 4)))
            r_ram = max(0.5, float(prof.get("runner_ram_gib", 16)))
            overhead = max(0.0, float(prof.get("host_overhead_gib", 0)))
            inst_ram = float(prof.get("min_instance_ram_gib", 64))
            active_ci_ids: set[str] = set()
            if prof.get("cloud_init_template_id"):
                active_ci_ids.add(str(prof["cloud_init_template_id"]))
            if prof.get("use_vm_template") and prof.get("vm_template_id"):
                vm_tpl = next(
                    (
                        t
                        for t in self.state.get("vm_templates", [])
                        if t.get("id") == prof.get("vm_template_id")
                        and t.get("role", "cicd_runner") == "cicd_runner"
                    ),
                    None,
                )
                if vm_tpl:
                    inst_ram = float(vm_tpl.get("ram_gib", inst_ram))
                    if vm_tpl.get("cloud_init_template_id"):
                        active_ci_ids.add(str(vm_tpl["cloud_init_template_id"]))
            for ci_tpl in self.state.get("cloud_init_templates", []):
                if (
                    ci_tpl.get("role", "cicd_runner") == "cicd_runner"
                    and ci_tpl.get("id") in active_ci_ids
                ):
                    ci_tpl["content"] = render_cloud_init_for_profile(
                        str(ci_tpl.get("content", "")),
                        role="cicd_runner",
                        runners_count=tpl_runners,
                        runner_vcpu=r_vcpu,
                        runner_ram_gib=r_ram,
                        instance_ram_gib=inst_ram,
                        host_overhead_gib=overhead,
                    )
        b_prof = self.state.get("profiles", {}).get("rust_builder", {})
        if b_prof and b_prof.get("cloud_init_template_id"):
            b_vcpu = max(1, int(b_prof.get("runner_vcpu", 16)))
            b_ram = max(1.0, float(b_prof.get("runner_ram_gib", 28)))
            for ci_tpl in self.state.get("cloud_init_templates", []):
                if (
                    ci_tpl.get("role") == "rust_builder"
                    and ci_tpl.get("id") == b_prof.get("cloud_init_template_id")
                ):
                    ci_tpl["content"] = render_cloud_init_for_profile(
                        str(ci_tpl.get("content", "")),
                        role="rust_builder",
                        runners_count=1,
                        runner_vcpu=b_vcpu,
                        runner_ram_gib=b_ram,
                    )

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

    def _running_role_hourly_rates(self, role: str) -> list[float]:
        """Return per-instance hourly USD rates for all currently RUNNING instances of the given role."""
        prof = self.state.get("profiles", {}).get(role, {})
        pol = self.state.get("scaling", {}).get(role, {})
        budget = prof.get("budget", {})
        include_disk = bool(
            pol.get("include_disk_in_scale_budget", budget.get("include_disk_costs", True))
        )
        include_tax = bool(budget.get("include_taxes", False))
        running = [
            inst
            for inst in self.state.get("instances", [])
            if isinstance(inst, dict)
            and inst.get("status") == "RUNNING"
            and inst.get("role") == role
        ]
        return [
            self._instance_hourly_for_budget(
                inst.get("pricing", {}),
                include_disk=include_disk,
                include_tax=include_tax,
            )
            for inst in running
        ]

    def _sync_fleet_telemetry(self) -> None:
        """Synchronize scaling telemetry counters, CPU percentages, and active fleet hourly rates with running instances."""
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
        telem["running_instance_hourly_rates"] = self._running_role_hourly_rates("cicd_runner")
        telem["current_builder_instances"] = len(running_builders)
        telem["builder_cpu_percentages"] = [
            float(inst.get("cpu_utilization_percent", 0.0)) for inst in running_builders
        ]
        telem["builder_instance_hourly_rates"] = self._running_role_hourly_rates("rust_builder")
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
                override_tax_rate = (
                    float(budget.get("tax_rate_percent", 0.0))
                    if budget.get("override_regional_tax")
                    else None
                )
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
                        tax_rate_percent=(
                            override_tax_rate
                            if override_tax_rate is not None
                            else float(reg_info.get("tax_rate_default", 8.25))
                        ),
                        machine_type=str(tpl_machine or "n2-standard-16"),
                        project=self.project,
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

    def _persist(self, bump: bool = True) -> None:
        """Atomically write state. ``bump`` increments the monotonic ``state_version`` (every real change)."""
        new_updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        defer = self._defer
        if defer is not None and defer.get("thread") == threading.get_ident():
            # Inside an idempotent request: the single atomic write happens when the request completes.
            defer["dirty"] = True
            self.state["updated_at"] = new_updated_at
            return
        if self.state_path and self.controlled_targets_error:
            # Never replace an unreadable state file: writing defaults (or a KV copy) over it would turn a
            # fail-closed access policy into a silently changed one after the next restart.
            raise OSError(f"Refusing to overwrite the unreadable dashboard state file: {self.controlled_targets_error}")
        new_version = self.state_version() + (1 if bump else 0)
        candidate_state = {**self.state, "updated_at": new_updated_at, "state_version": new_version}
        serialized = json.dumps(candidate_state, indent=2, allow_nan=False)
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            if self.state_path.is_symlink():
                raise OSError(f"Refusing symlinked state file path: {self.state_path}")
            legacy_tmp = self.state_path.with_suffix(".tmp")
            if legacy_tmp.is_symlink():
                raise OSError(f"Refusing symlinked temporary state path: {legacy_tmp}")
            tmp = self.state_path.parent / f".{self.state_path.name}.{uuid.uuid4().hex}.tmp"
            open_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            )
            fd = os.open(str(tmp), open_flags, 0o600)
            try:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    os.chmod(tmp, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(serialized)
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    # Best-effort cleanup of the temporary state file when writing fails.
                    pass
                raise
            tmp.replace(self.state_path)
        self.state["updated_at"] = new_updated_at
        self.state["state_version"] = new_version
        if bump:
            self._mark_kv_dirty()

    def _refresh_instances_pricing(self) -> None:
        """Recompute active instance pricing using current live/catalog machine rates while preserving historical rates on deleted instances."""
        running_insts = [
            inst
            for inst in self.state.get("instances", [])
            if isinstance(inst, dict)
            and inst.get("status") == "RUNNING"
            and inst.get("region")
            and inst.get("machine_type")
        ]
        if not running_insts:
            return
        active_regions = sorted({str(inst["region"]) for inst in running_insts})
        active_mtypes = sorted({str(inst["machine_type"]) for inst in running_insts})
        shared_live_pricing = fetch_live_spot_pricing(
            project=self.project,
            regions=active_regions,
            machine_types=active_mtypes,
        )
        for inst in running_insts:
            cur_pricing = inst.get("pricing") if isinstance(inst.get("pricing"), dict) else {}
            inst_role = str(inst.get("role", "cicd_runner"))
            prof_budget = (
                self.state.get("profiles", {})
                .get(inst_role, {})
                .get("budget", {})
            )
            reg_info = next(
                (r for r in REGIONS_CATALOG if r["region"] == str(inst["region"])),
                REGIONS_CATALOG[0],
            )
            tax_rate_pct = (
                float(prof_budget.get("tax_rate_percent", 0.0))
                if isinstance(prof_budget, dict) and prof_budget.get("override_regional_tax")
                else float(
                    reg_info.get(
                        "tax_rate_default",
                        cur_pricing.get("tax_rate_percent", 8.25) or 8.25,
                    )
                )
            )
            inst["pricing"] = calculate_spot_pricing(
                family=str(inst.get("family") or str(inst["machine_type"]).split("-", 1)[0]),
                vcpu=int(inst.get("vcpu", 16)),
                ram_gib=float(inst.get("ram_gib", 64)),
                region=str(inst["region"]),
                boot_disk_type=str(inst.get("boot_disk_type", "hyperdisk-balanced")),
                boot_disk_gb=int(inst.get("boot_disk_gb", 40)),
                data_disk_type=str(inst.get("data_disk_type", "hyperdisk-balanced")),
                data_disk_gb=int(inst.get("data_disk_gb", 100)),
                tax_rate_percent=tax_rate_pct,
                machine_type=str(inst["machine_type"]),
                live_pricing=shared_live_pricing,
                project=self.project,
            )

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
            self._refresh_instances_pricing()
            running_runner_rates = self._running_role_hourly_rates("cicd_runner")
            running_builder_rates = self._running_role_hourly_rates("rust_builder")
            if self.state["scaling_telemetry"].get("current_instances") == len(running_runner_rates):
                self.state["scaling_telemetry"]["running_instance_hourly_rates"] = running_runner_rates
            if self.state["scaling_telemetry"].get("current_builder_instances") == len(running_builder_rates):
                self.state["scaling_telemetry"]["builder_instance_hourly_rates"] = running_builder_rates
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
            ci_shared = bool(
                self.state["scaling"]["cicd_runner"].get("shared_global_budget")
                or self.state.get("global_budget_enforced")
            )
            builder_shared = bool(
                self.state["scaling"]["rust_builder"].get("shared_global_budget")
                or self.state.get("global_budget_enforced")
            )
            scaling_eval = evaluate_scaling_decision(
                self.state["scaling"]["cicd_runner"],
                self.state["scaling_telemetry"],
                per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd(
                    "cicd_runner", cicd_qual
                ),
                max_instances=int(
                    self.state["profiles"]["cicd_runner"].get("max_instances", 8)
                ),
                other_role_hourly_usd=sum(running_builder_rates) if ci_shared else None,
            )
            builder_scaling_eval = evaluate_builder_scaling_decision(
                self.state["scaling"]["rust_builder"],
                self.state["scaling_telemetry"],
                per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd(
                    "rust_builder", builder_qual
                ),
                other_role_hourly_usd=(
                    max(sum(running_runner_rates), float(scaling_eval.get("projected_hourly_usd", 0.0)))
                    if builder_shared
                    else None
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
                "builder_scaling_evaluation": builder_scaling_eval,
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
            self._refresh_instances_pricing()
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
            prev_global_enforced = bool(self.state.get("global_budget_enforced", False))
            try:
                if target in ("cicd_runner", "rust_builder"):
                    self.state["profiles"][target]["budget"] = normalized
                    self.state["scaling"][target]["max_scale_up_hourly_usd"] = normalized["max_hourly_usd"]
                    self.state["scaling"][target]["max_scale_up_daily_usd"] = normalized["max_daily_usd"]
                    self.state["scaling"][target]["max_scale_up_monthly_usd"] = normalized["max_monthly_usd"]
                    self.state["scaling"][target]["include_disk_in_scale_budget"] = normalized["include_disk_costs"]
                    for role_key in ("cicd_runner", "rust_builder"):
                        self.state["scaling"][role_key]["shared_global_budget"] = False
                    self.state["global_budget_enforced"] = False
                else:
                    self.state["global_budget"] = normalized
                    self.state["global_budget_enforced"] = True
                    for role_key in ("cicd_runner", "rust_builder"):
                        prev_role_budget = (
                            self.state["profiles"][role_key].get("budget", {})
                            if isinstance(self.state["profiles"][role_key].get("budget"), dict)
                            else {}
                        )
                        role_budget = dict(normalized)
                        if "override_regional_tax" not in payload and "override_regional_tax" in prev_role_budget:
                            role_budget["override_regional_tax"] = bool(prev_role_budget["override_regional_tax"])
                        if "tax_rate_percent" not in payload and "tax_rate_percent" in prev_role_budget:
                            role_budget["tax_rate_percent"] = float(prev_role_budget["tax_rate_percent"])
                        self.state["profiles"][role_key]["budget"] = role_budget
                        self.state["scaling"][role_key]["max_scale_up_hourly_usd"] = normalized["max_hourly_usd"]
                        self.state["scaling"][role_key]["max_scale_up_daily_usd"] = normalized["max_daily_usd"]
                        self.state["scaling"][role_key]["max_scale_up_monthly_usd"] = normalized["max_monthly_usd"]
                        self.state["scaling"][role_key]["include_disk_in_scale_budget"] = normalized["include_disk_costs"]
                        self.state["scaling"][role_key]["shared_global_budget"] = True
                self._refresh_instances_pricing()
                self._persist()
            except Exception:
                self.state["profiles"] = prev_profiles
                self.state["scaling"] = prev_scaling
                self.state["global_budget"] = prev_global
                self.state["global_budget_enforced"] = prev_global_enforced
                self._refresh_instances_pricing()
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
                    candidate[key] = (
                        float(math.ceil(val_float)) if key == "runner_ram_gib" else val_float
                    )
            if "host_overhead_gib" in payload:
                overhead_val = _finite_float(
                    payload["host_overhead_gib"], "host_overhead_gib", min_val=0.0
                )
                candidate["host_overhead_gib"] = float(math.ceil(overhead_val))
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
            tax_override_updated = False
            if "budget" in payload and isinstance(payload["budget"], dict):
                merged_budget_input = {**candidate.get("budget", {}), **payload["budget"]}
                normalized_budget = normalize_budget(merged_budget_input)
                candidate["budget"] = normalized_budget
            elif "override_regional_tax" in payload or "tax_rate_percent" in payload:
                tax_patch: dict[str, Any] = {}
                if "override_regional_tax" in payload:
                    tax_patch["override_regional_tax"] = payload["override_regional_tax"]
                if "tax_rate_percent" in payload:
                    tax_patch["tax_rate_percent"] = payload["tax_rate_percent"]
                merged_budget_input = {**candidate.get("budget", {}), **tax_patch}
                candidate["budget"] = normalize_budget(merged_budget_input)
                tax_override_updated = True

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
            prev_ci_templates = copy.deepcopy(self.state["cloud_init_templates"])
            prev_telemetry = copy.deepcopy(self.state["scaling_telemetry"])
            prev_global_enforced = bool(self.state.get("global_budget_enforced", False))
            try:
                self.state["profiles"][role] = candidate
                if active_tpl is not None:
                    if "cloud_init_template_id" in payload and candidate.get("cloud_init_template_id"):
                        active_tpl["cloud_init_template_id"] = candidate["cloud_init_template_id"]
                    elif (
                        "vm_template_id" in payload
                        and "cloud_init_template_id" not in payload
                        and active_tpl.get("cloud_init_template_id")
                    ):
                        candidate["cloud_init_template_id"] = active_tpl["cloud_init_template_id"]
                    if validated_tpl_units is not None:
                        active_tpl["runner_vcpu"] = int(candidate["runner_vcpu"])
                        active_tpl["runner_ram_gib"] = float(candidate["runner_ram_gib"])
                        active_tpl["runners_count"] = validated_tpl_units
                if normalized_budget is not None:
                    self.state["scaling"][role]["max_scale_up_hourly_usd"] = normalized_budget["max_hourly_usd"]
                    self.state["scaling"][role]["max_scale_up_daily_usd"] = normalized_budget["max_daily_usd"]
                    self.state["scaling"][role]["max_scale_up_monthly_usd"] = normalized_budget["max_monthly_usd"]
                    self.state["scaling"][role]["include_disk_in_scale_budget"] = normalized_budget["include_disk_costs"]
                    for role_key in ("cicd_runner", "rust_builder"):
                        self.state["scaling"][role_key]["shared_global_budget"] = False
                    self.state["global_budget_enforced"] = False
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
                    self._sync_template_density()
                if normalized_budget is not None or tax_override_updated:
                    self._refresh_instances_pricing()
                self._persist()
            except Exception:
                self.state["profiles"] = prev_profiles
                self.state["scaling"] = prev_scaling
                self.state["vm_templates"] = prev_vm_templates
                self.state["cloud_init_templates"] = prev_ci_templates
                self.state["scaling_telemetry"] = prev_telemetry
                self.state["global_budget_enforced"] = prev_global_enforced
                self._refresh_instances_pricing()
                raise
            return {"status": "ok", "profile": candidate, "qualification": qual}

    def _consume_active_oom_events(
        self,
        *,
        role: str | None = None,
        event_id: str | None = None,
        previous_runners: int | None = None,
        current_runners: int | None = None,
        step_runners: int | None = None,
        max_runners: int | None = None,
    ) -> None:
        """Mark active/unhandled OOM events as handled/acknowledged so a single OOM does not repeatedly trigger scale-ups."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        telem = self.state.setdefault("scaling_telemetry", {})

        def _runner_oom_satisfied(record: dict[str, Any]) -> bool:
            if event_id is not None or current_runners is None:
                return True
            baseline_default = int(previous_runners) if previous_runners is not None else 0
            try:
                baseline_val = int(record.setdefault("baseline_runners", baseline_default))
            except (TypeError, ValueError):
                baseline_val = baseline_default
                record["baseline_runners"] = baseline_val
            step_val = max(1, int(step_runners or 1))
            max_val = int(max_runners) if max_runners is not None else (baseline_val + step_val)
            required_target = min(max_val, baseline_val + step_val)
            return int(current_runners) >= required_target and (int(current_runners) > baseline_val or baseline_val >= max_val)

        if role in (None, "cicd_runner"):
            for run in telem.get("github_oom_runs", []):
                if not isinstance(run, dict):
                    continue
                if event_id and str(run.get("id", "")) != event_id and str(run.get("run_id", "")) != event_id:
                    continue
                if not _runner_oom_satisfied(run):
                    continue
                run["handled"] = True
                run["acknowledged"] = True
                run.setdefault("handled_at", now_iso)
        for evt in telem.get("journal_oom_events", []):
            if not isinstance(evt, dict):
                continue
            if event_id and str(evt.get("id", "")) != event_id:
                continue
            is_builder = (
                str(evt.get("role", "")).strip().lower() == "rust_builder"
                or str(evt.get("instance_name", "")).strip().lower().startswith("builder-")
            )
            if role == "cicd_runner" and is_builder:
                continue
            if role == "rust_builder" and not is_builder:
                continue
            if not is_builder and not _runner_oom_satisfied(evt):
                continue
            evt["handled"] = True
            evt["acknowledged"] = True
            evt.setdefault("handled_at", now_iso)
        if role in (None, "rust_builder"):
            for evt in telem.get("builder_journal_oom_events", []):
                if not isinstance(evt, dict):
                    continue
                if event_id and str(evt.get("id", "")) != event_id:
                    continue
                evt["handled"] = True
                evt["acknowledged"] = True
                evt.setdefault("handled_at", now_iso)

    def update_scaling(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update scaling rules, triggers, hourly thresholds, CPU scope, and telemetry."""
        with self._lock:
            _assert_finite_json_tree(payload, "payload")
            role = str(payload.get("role", "cicd_runner")).strip()
            if role not in ("cicd_runner", "rust_builder"):
                raise ValueError(f"Unsupported workload role: {role}")
            prev_scaling = copy.deepcopy(self.state["scaling"])
            prev_profiles = copy.deepcopy(self.state["profiles"])
            prev_ci_templates = copy.deepcopy(self.state["cloud_init_templates"])
            prev_telemetry = copy.deepcopy(self.state["scaling_telemetry"])
            prev_global_enforced = bool(self.state.get("global_budget_enforced", False))
            try:
                if "policy" in payload and isinstance(payload["policy"], dict):
                    prev_role_sc = prev_scaling.get(role, {})
                    budget_cap_changed = any(
                        k in payload["policy"]
                        and abs(
                            _finite_float(payload["policy"][k], k)
                            - float(prev_role_sc.get(k, 0.0))
                        )
                        > 1e-6
                        for k in (
                            "max_scale_up_hourly_usd",
                            "max_scale_up_daily_usd",
                            "max_scale_up_monthly_usd",
                        )
                    )
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
                    if (
                        payload["policy"].get("shared_global_budget") is False
                        or (
                            budget_cap_changed
                            and "shared_global_budget" not in payload["policy"]
                        )
                    ):
                        for role_key in ("cicd_runner", "rust_builder"):
                            self.state["scaling"][role_key]["shared_global_budget"] = False
                        self.state["global_budget_enforced"] = False
                    if role == "cicd_runner":
                        pol = self.state["scaling"]["cicd_runner"]
                        min_runners_val = _finite_int(pol.get("min_runners", 4), "min_runners", min_val=1)
                        max_runners_val = _finite_int(pol.get("max_runners", 24), "max_runners", min_val=1)
                        if max_runners_val < min_runners_val:
                            raise ValueError("max_runners must be >= min_runners")
                        pol["min_runners"] = min_runners_val
                        pol["max_runners"] = max_runners_val
                        self.state["profiles"]["cicd_runner"]["min_runners"] = min_runners_val
                        if "max_instances" in payload["policy"]:
                            max_inst_val = _finite_int(pol.get("max_instances", 8), "max_instances", min_val=1)
                            pol["max_instances"] = max_inst_val
                            self.state["profiles"]["cicd_runner"]["max_instances"] = max_inst_val
                        self.state["profiles"]["cicd_runner"]["runner_vcpu"] = _finite_int(
                            pol.get("runner_vcpu", 4), "runner_vcpu", min_val=1
                        )
                        runner_ram_ceil = float(
                            math.ceil(_finite_float(pol.get("runner_ram_gib", 16), "runner_ram_gib", min_val=0.1))
                        )
                        pol["runner_ram_gib"] = runner_ram_ceil
                        self.state["profiles"]["cicd_runner"]["runner_ram_gib"] = runner_ram_ceil
                        if "runners_per_instance" not in payload["policy"]:
                            self._sync_template_density()
                    else:
                        pol = self.state["scaling"]["rust_builder"]
                        min_inst_val = _finite_int(pol.get("min_instances", 1), "min_instances", min_val=1)
                        max_inst_val = _finite_int(pol.get("max_instances", 4), "max_instances", min_val=1)
                        if max_inst_val < min_inst_val:
                            raise ValueError("max_instances must be >= min_instances")
                        pol["min_instances"] = min_inst_val
                        pol["max_instances"] = max_inst_val
                        if "builder_vcpu" in payload["policy"] and "container_vcpu" not in payload["policy"]:
                            pol["container_vcpu"] = _finite_int(pol["builder_vcpu"], "builder_vcpu", min_val=1)
                        elif "container_vcpu" in payload["policy"] and "builder_vcpu" not in payload["policy"]:
                            pol["builder_vcpu"] = _finite_int(pol["container_vcpu"], "container_vcpu", min_val=1)
                        if "builder_ram_gib" in payload["policy"] and "container_ram_gib" not in payload["policy"]:
                            pol["container_ram_gib"] = _finite_float(
                                pol["builder_ram_gib"], "builder_ram_gib", min_val=0.1
                            )
                        elif "container_ram_gib" in payload["policy"] and "builder_ram_gib" not in payload["policy"]:
                            pol["builder_ram_gib"] = _finite_float(
                                pol["container_ram_gib"], "container_ram_gib", min_val=0.1
                            )
                        builder_ram_ceil = float(
                            math.ceil(
                                _finite_float(
                                    pol.get("container_ram_gib", pol.get("builder_ram_gib", 28)),
                                    "container_ram_gib",
                                    min_val=0.1,
                                )
                            )
                        )
                        pol["container_ram_gib"] = builder_ram_ceil
                        pol["builder_ram_gib"] = builder_ram_ceil
                        self.state["profiles"]["rust_builder"]["min_instances"] = min_inst_val
                        self.state["profiles"]["rust_builder"]["max_instances"] = max_inst_val
                        self.state["profiles"]["rust_builder"]["runner_vcpu"] = _finite_int(
                            pol.get("container_vcpu", pol.get("builder_vcpu", 16)), "container_vcpu", min_val=1
                        )
                        self.state["profiles"]["rust_builder"]["runner_ram_gib"] = builder_ram_ceil
                        self._sync_template_density()
                telem_payload = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else {}
                if telem_payload:
                    if "current_runners" in telem_payload and "current_instances" not in telem_payload:
                        self.state["scaling_telemetry"].pop("current_instances", None)
                    if (
                        ("current_runners" in telem_payload or "current_instances" in telem_payload)
                        and "running_instance_hourly_rates" not in telem_payload
                    ):
                        self.state["scaling_telemetry"].pop("running_instance_hourly_rates", None)
                    if (
                        "current_builder_instances" in telem_payload
                        and "builder_instance_hourly_rates" not in telem_payload
                    ):
                        self.state["scaling_telemetry"].pop("builder_instance_hourly_rates", None)
                    self.state["scaling_telemetry"].update(telem_payload)
                    if "github_oom_runs" in telem_payload and isinstance(
                        self.state["scaling_telemetry"].get("github_oom_runs"), list
                    ):
                        self._normalize_github_ci_runs()
                    if (
                        "journal_oom_events" in telem_payload
                        and "builder_journal_oom_events" not in telem_payload
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
                if payload.get("acknowledge_all_ooms"):
                    self._consume_active_oom_events(role=role if "role" in payload else None)
                if payload.get("acknowledge_oom_id"):
                    self._consume_active_oom_events(
                        role=None, event_id=str(payload["acknowledge_oom_id"]).strip()
                    )
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
                    item.setdefault(
                        "baseline_runners",
                        int(self.state["scaling_telemetry"].get("current_runners", 0)),
                    )
                    self.state["scaling_telemetry"]["github_oom_runs"].insert(0, item)

                running_runner_rates = self._running_role_hourly_rates("cicd_runner")
                running_builder_rates = self._running_role_hourly_rates("rust_builder")
                if (
                    "running_instance_hourly_rates" not in telem_payload
                    and self.state["scaling_telemetry"].get("current_instances") == len(running_runner_rates)
                ):
                    self.state["scaling_telemetry"]["running_instance_hourly_rates"] = running_runner_rates
                if (
                    "builder_instance_hourly_rates" not in telem_payload
                    and self.state["scaling_telemetry"].get("current_builder_instances") == len(running_builder_rates)
                ):
                    self.state["scaling_telemetry"]["builder_instance_hourly_rates"] = running_builder_rates

                global_budget_shared = bool(self.state.get("global_budget_enforced"))
                telem_runner_rates = self.state["scaling_telemetry"].get("running_instance_hourly_rates")
                telem_builder_rates = self.state["scaling_telemetry"].get("builder_instance_hourly_rates")
                runner_burn_for_builder = round(
                    sum(
                        float(r)
                        for r in (
                            telem_runner_rates
                            if isinstance(telem_runner_rates, list)
                            else running_runner_rates
                        )
                    ),
                    4,
                )
                builder_burn_for_runner = round(
                    sum(
                        float(r)
                        for r in (
                            telem_builder_rates
                            if isinstance(telem_builder_rates, list)
                            else running_builder_rates
                        )
                    ),
                    4,
                )
                cicd_eval = evaluate_scaling_decision(
                    self.state["scaling"]["cicd_runner"],
                    self.state["scaling_telemetry"],
                    per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd("cicd_runner"),
                    max_instances=int(
                        self.state["profiles"]["cicd_runner"].get("max_instances", 8)
                    ),
                    other_role_hourly_usd=(
                        builder_burn_for_runner
                        if (global_budget_shared or self.state["scaling"]["cicd_runner"].get("shared_global_budget"))
                        else 0.0
                    ),
                )
                builder_eval = evaluate_builder_scaling_decision(
                    self.state["scaling"]["rust_builder"],
                    self.state["scaling_telemetry"],
                    per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd("rust_builder"),
                    other_role_hourly_usd=(
                        max(runner_burn_for_builder, float(cicd_eval.get("projected_hourly_usd", 0.0)))
                        if (global_budget_shared or self.state["scaling"]["rust_builder"].get("shared_global_budget"))
                        else 0.0
                    ),
                )
                self._persist()
            except Exception:
                self.state["scaling"] = prev_scaling
                self.state["profiles"] = prev_profiles
                self.state["cloud_init_templates"] = prev_ci_templates
                self.state["scaling_telemetry"] = prev_telemetry
                self.state["global_budget_enforced"] = prev_global_enforced
                raise

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

    def get_controlled_targets(self) -> list[str]:
        """Return the dynamic controlled target names (orgs and owner/repo) stored in dashboard state.

        Callers that make access decisions check ``controlled_targets_error`` first; as defense in depth an
        unloadable policy also yields an unparseable sentinel, so any authorization check that consumes it
        denies access.
        """
        with self._lock:
            if self.controlled_targets_error:
                return ["!unavailable-controlled-targets"]
            raw = self.state.get("controlled_targets", [])
            if not isinstance(raw, list):
                # Fail closed: an unparseable entry makes every OAuth authorization check deny access.
                return ["!invalid-controlled-targets"]
            return [t if isinstance(t, str) else "!invalid-controlled-target" for t in raw]

    def get_controlled_targets_history(self) -> list[dict[str, Any]]:
        with self._lock:
            raw = self.state.get("controlled_targets_history", [])
            return copy.deepcopy([e for e in raw if isinstance(e, dict)]) if isinstance(raw, list) else []

    @staticmethod
    def validate_controlled_targets(targets: Any) -> list[str]:
        """Validate a proposed dynamic target list and return canonical names; raises ValueError."""
        if not isinstance(targets, list):
            raise ValueError("targets must be a JSON array of organization or owner/repo names")
        names = [t.name for t in parse_controlled_targets(targets)]
        if len(names) > MAX_DYNAMIC_CONTROLLED_TARGETS:
            raise ValueError(f"At most {MAX_DYNAMIC_CONTROLLED_TARGETS} dynamic controlled targets are supported")
        return names

    def set_controlled_targets(
        self,
        targets: Any,
        *,
        actor: str = "internal",
        actor_uid: int | None = None,
        method: str = "internal",
        expected_digest: str | None = None,
    ) -> list[str]:
        """Validate, replace, persist, and audit the dynamic controlled target list.

        Raises ValueError on any invalid entry, ControlledTargetsUnavailableError when the stored policy
        could not be loaded, and ControlledTargetsConflictError when ``expected_digest`` (see
        ``target_list_digest``) no longer matches the stored list. Every committed change is logged and
        appended to the bounded ``controlled_targets_history``.
        """
        names = self.validate_controlled_targets(targets)
        with self._lock:
            if self.controlled_targets_error:
                raise ControlledTargetsUnavailableError(self.controlled_targets_error)
            previous_names = self.get_controlled_targets()
            if expected_digest is not None and not hmac.compare_digest(
                expected_digest, target_list_digest(previous_names)
            ):
                raise ControlledTargetsConflictError(
                    "Controlled targets changed after this change was requested; review and submit it again"
                )
            previous = copy.deepcopy(self.state.get("controlled_targets", []))
            previous_history = copy.deepcopy(self.state.get("controlled_targets_history", []))
            now = self._clock()
            entry = {
                "at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "epoch": int(now),
                "actor": str(actor),
                "actor_uid": actor_uid,
                "method": str(method),
                "previous": previous_names,
                "targets": list(names),
            }
            history = previous_history if isinstance(previous_history, list) else []
            self.state["controlled_targets"] = names
            self.state["controlled_targets_history"] = (history + [entry])[-MAX_CONTROLLED_TARGETS_HISTORY:]
            try:
                self._persist()
            except Exception:
                self.state["controlled_targets"] = previous
                self.state["controlled_targets_history"] = previous_history
                raise
            LOG.info(
                "Controlled targets changed by %s (uid=%s, method=%s): %s -> %s",
                entry["actor"], actor_uid, entry["method"], json.dumps(previous_names), json.dumps(names),
            )
            return list(names)

    # -- added administrators (Admins page) --

    @staticmethod
    def _valid_admin_entry(entry: Any) -> dict[str, Any] | None:
        """A well-formed stored administrator record, or None (malformed records never grant access)."""
        if not isinstance(entry, dict):
            return None
        login, uid = entry.get("login"), entry.get("uid")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            return None
        try:
            normalize_github_login(login)
        except ValueError:
            return None
        if any(uid == owner_uid for _, owner_uid in OWNER_IDENTITIES):
            return None  # Owners are deploy-configured; a stored copy never changes how they are treated.
        return entry

    def _admin_entries(self) -> list[dict[str, Any]]:
        raw = self.state.get("dashboard_admins", [])
        entries: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in raw if isinstance(raw, list) else []:
            entry = self._valid_admin_entry(item)
            if entry is not None and entry["uid"] not in seen:
                seen.add(entry["uid"])
                entries.append(entry)
        return entries

    def dashboard_admin_uids(self) -> frozenset[int]:
        """GitHub IDs of the added administrators, for sign-in and per-request session checks.

        Fails closed to owners only: while the state file could not be loaded (or is still being recovered),
        this is empty.
        """
        with self._lock:
            if self.controlled_targets_error or self.recovery_pending:
                return frozenset()
            return frozenset(e["uid"] for e in self._admin_entries())

    def get_dashboard_admins(self) -> list[dict[str, Any]]:
        with self._lock:
            if self.controlled_targets_error or self.recovery_pending:
                return []
            return [
                {"login": e["login"], "uid": e["uid"], "added_by": e.get("added_by"),
                 "added_by_uid": e.get("added_by_uid"), "added_at": e.get("added_at")}
                for e in copy.deepcopy(self._admin_entries())
            ]

    def get_dashboard_admins_history(self) -> list[dict[str, Any]]:
        with self._lock:
            raw = self.state.get("dashboard_admins_history", [])
            return copy.deepcopy([e for e in raw if isinstance(e, dict)]) if isinstance(raw, list) else []

    def _change_dashboard_admins(self, entries: list[dict[str, Any]], history_entry: dict[str, Any]) -> None:
        """Replace the list and append history in one persist (caller holds the lock); rolls back on failure."""
        previous = copy.deepcopy(self.state.get("dashboard_admins", []))
        previous_history = copy.deepcopy(self.state.get("dashboard_admins_history", []))
        history = previous_history if isinstance(previous_history, list) else []
        self.state["dashboard_admins"] = entries
        self.state["dashboard_admins_history"] = (history + [history_entry])[-MAX_DASHBOARD_ADMINS_HISTORY:]
        try:
            self._persist()
        except Exception:
            self.state["dashboard_admins"] = previous
            self.state["dashboard_admins_history"] = previous_history
            raise

    def _admin_change_preflight(self) -> None:
        if self.controlled_targets_error or self.recovery_pending:
            raise ControlledTargetsUnavailableError(self.controlled_targets_error or "dashboard state is being recovered")
        self._mutation_locked_checks()

    def add_dashboard_admin(self, login: str, uid: int, *, actor: str, actor_uid: int | None) -> dict[str, Any]:
        """Add a GitHub user (already resolved to its numeric ID) as an administrator. Raises DashboardAdminError."""
        clean_login = normalize_github_login(login)
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise DashboardAdminError("uid must be a positive integer GitHub user ID")
        with self._lock:
            self._admin_change_preflight()
            if any(uid == owner_uid for _, owner_uid in OWNER_IDENTITIES):
                raise DashboardAdminError(f"'{clean_login}' is a dashboard owner already", 409)
            entries = self._admin_entries()
            for entry in entries:
                if entry["uid"] == uid:
                    raise DashboardAdminError(
                        f"'{clean_login}' is already an administrator (added as '{entry['login']}')", 409)
            if len(entries) >= MAX_ADDED_ADMINS:
                raise DashboardAdminError(f"At most {MAX_ADDED_ADMINS} administrators can be added", 409)
            now = self._clock()
            at = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            record = {"login": clean_login, "uid": uid, "added_by": str(actor), "added_by_uid": actor_uid,
                      "added_at": at}
            self._change_dashboard_admins(
                copy.deepcopy(entries) + [record],
                {"at": at, "epoch": int(now), "action": "added", "login": clean_login, "uid": uid,
                 "actor": str(actor), "actor_uid": actor_uid},
            )
            self._after_commit(lambda: audit_admin_change("added", clean_login, uid, actor, actor_uid))
            return dict(record)

    def remove_dashboard_admin(self, uid: Any, *, actor: str, actor_uid: int | None) -> dict[str, Any]:
        """Remove an added administrator by GitHub ID. Its sessions stop working on their next request."""
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise DashboardAdminError("uid must be a positive integer GitHub user ID")
        with self._lock:
            self._admin_change_preflight()
            if any(uid == owner_uid for _, owner_uid in OWNER_IDENTITIES):
                raise DashboardAdminError("Dashboard owners are configured at deploy time and can't be removed here", 400)
            entries = self._admin_entries()
            removed = next((e for e in entries if e["uid"] == uid), None)
            if removed is None:
                raise DashboardAdminError(f"No added administrator has GitHub ID {uid}", 404)
            now = self._clock()
            at = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._change_dashboard_admins(
                copy.deepcopy([e for e in entries if e["uid"] != uid]),
                {"at": at, "epoch": int(now), "action": "removed", "login": removed["login"], "uid": uid,
                 "actor": str(actor), "actor_uid": actor_uid},
            )
            removed_login = removed["login"]
            self._after_commit(lambda: audit_admin_change("removed", removed_login, uid, actor, actor_uid))
            return {"login": removed["login"], "uid": uid}

    def peek_idempotent(self, key: str, method: str, path: str, fingerprint: str) -> tuple[int, Any, bool] | None:
        """The stored outcome for ``key`` (a replay, or 422 for a different request), or None when unused.

        Lets a route skip slow work (a GitHub lookup) on a retry; :meth:`run_request` checks again atomically.
        """
        with self._lock:
            raw_records = self.state.get(IDEMPOTENCY_STATE_KEY)
            records = self._prune_idempotency(raw_records if isinstance(raw_records, dict) else {}, self._clock())
            existing = records.get(key)
            if existing is None:
                return None
            if existing.get("m") != method or existing.get("p") != path or existing.get("f") != fingerprint:
                return 422, {"error": "Idempotency-Key was already used for a different request"}, False
            return int(existing.get("s", 200)), copy.deepcopy(existing.get("b")), True

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

            prev_ci_templates = copy.deepcopy(self.state["cloud_init_templates"])
            prev_profiles = copy.deepcopy(self.state["profiles"])
            prev_vm_templates = copy.deepcopy(self.state.get("vm_templates", []))
            try:
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
            except Exception:
                self.state["cloud_init_templates"] = prev_ci_templates
                self.state["profiles"] = prev_profiles
                self.state["vm_templates"] = prev_vm_templates
                raise

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
            explicit_ci_id = str(payload.get("cloud_init_template_id", "")).strip()
            if explicit_ci_id:
                ci_tpl = next(
                    (
                        t
                        for t in self.state.get("cloud_init_templates", [])
                        if t.get("id") == explicit_ci_id
                    ),
                    None,
                )
                if ci_tpl is None or ci_tpl.get("role", role) != role:
                    raise ValueError(
                        f"Cloud-init template '{explicit_ci_id}' does not belong to role '{role}'"
                    )
                resolved_ci_id = explicit_ci_id
            else:
                resolved_ci_id = (
                    inst.get("cloud_init_template_id")
                    or (prof.get("cloud_init_template_id") if prof else None)
                )
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
                "cloud_init_template_id": resolved_ci_id,
                "source_instance": inst["name"],
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            assign_to_profile = bool(payload.get("assign_to_profile", True))
            if assign_to_profile and prof is not None:
                validated_units = self._validate_vm_template_compatibility(new_tpl, prof, role)
                new_tpl["runner_vcpu"] = int(prof["runner_vcpu"])
                new_tpl["runner_ram_gib"] = float(prof["runner_ram_gib"])
                new_tpl["runners_count"] = validated_units
            prev_vm_templates = copy.deepcopy(self.state["vm_templates"])
            prev_profiles = copy.deepcopy(self.state["profiles"])
            prev_scaling = copy.deepcopy(self.state["scaling"])
            prev_ci_templates = copy.deepcopy(self.state["cloud_init_templates"])
            try:
                self.state["vm_templates"].append(new_tpl)
                if assign_to_profile and prof is not None:
                    prof["use_vm_template"] = True
                    prof["vm_template_id"] = new_tpl["id"]
                    if new_tpl.get("cloud_init_template_id"):
                        prof["cloud_init_template_id"] = new_tpl["cloud_init_template_id"]
                    self._sync_template_density()
                self._persist()
            except Exception:
                self.state["vm_templates"] = prev_vm_templates
                self.state["profiles"] = prev_profiles
                self.state["scaling"] = prev_scaling
                self.state["cloud_init_templates"] = prev_ci_templates
                raise
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
            prev_instances = copy.deepcopy(self.state["instances"])
            prev_telemetry = copy.deepcopy(self.state["scaling_telemetry"])
            try:
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
            except Exception:
                self.state["instances"] = prev_instances
                self.state["scaling_telemetry"] = prev_telemetry
                raise
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
            prev_instances = copy.deepcopy(self.state["instances"])
            prev_telemetry = copy.deepcopy(self.state["scaling_telemetry"])
            try:
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
                            (vm_tpl.get("cloud_init_template_id") if vm_tpl else None)
                            or prof.get("cloud_init_template_id")
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
                        prof_budget = prof.get("budget", {}) if isinstance(prof.get("budget"), dict) else {}
                        override_tax = (
                            float(prof_budget.get("tax_rate_percent", 0.0))
                            if prof_budget.get("override_regional_tax")
                            else None
                        )
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
                            tax_rate_percent=override_tax,
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
                    previous_runner_slots = sum(
                        int(i.get("runners_count", 1)) for i in existing_running_role
                    )
                    projected_runner_slots = previous_runner_slots
                    max_runners_cap = max(
                        int(prof.get("min_runners", 1)),
                        int(pol.get("max_runners", 24)),
                    )
                    if role == "cicd_runner":
                        projected_runner_slots = sum(
                            int(i.get("runners_count", 1)) for i in existing_running_role + new_records
                        )
                        if projected_runner_slots > max_runners_cap:
                            raise ValueError(
                                f"Scaling up in {region} to {target_count} instance(s) "
                                f"(projected {projected_runner_slots} runner slots across {projected_total} VMs) "
                                f"exceeds maximum allowed CI/CD runners ({max_runners_cap})"
                            )
                    other_role = "rust_builder" if role == "cicd_runner" else "cicd_runner"
                    shared_global = bool(
                        pol.get("shared_global_budget") or self.state.get("global_budget_enforced")
                    )
                    other_running = (
                        [
                            i
                            for i in self.state["instances"]
                            if i["status"] == "RUNNING" and i["role"] == other_role
                        ]
                        if shared_global
                        else []
                    )
                    other_role_hourly = round(
                        sum(
                            self._instance_hourly_for_budget(
                                i["pricing"], include_disk=include_disk, include_tax=include_tax
                            )
                            for i in other_running
                        ),
                        4,
                    )
                    projected_fleet_hourly = round(
                        other_role_hourly
                        + sum(
                            self._instance_hourly_for_budget(
                                i["pricing"], include_disk=include_disk, include_tax=include_tax
                            )
                            for i in existing_running_role + new_records
                        ),
                        4,
                    )
                    if (
                        effective_hourly_cap <= 0.0
                        or projected_fleet_hourly > effective_hourly_cap + 1e-9
                    ):
                        raise ValueError(
                            f"Scaling up in {region} to {target_count} instance(s) (total fleet: {projected_total}, "
                            f"projected ${projected_fleet_hourly:.4f}/hr) exceeds scale-up budget cap "
                            f"(${max_hourly_cap:.2f}/hr, ${max_daily_cap:.2f}/day, ${max_monthly_cap:.2f}/mo) for role '{role}'"
                        )
                    for created in new_records:
                        self.state["instances"].insert(0, created)
                    if role == "cicd_runner":
                        self._consume_active_oom_events(
                            role=role,
                            previous_runners=previous_runner_slots,
                            current_runners=projected_runner_slots,
                            step_runners=max(1, int(pol.get("scale_up_step_runners", 2))),
                            max_runners=max_runners_cap,
                        )
                    else:
                        self._consume_active_oom_events(role=role)

                self._sync_fleet_telemetry()
                self._persist()
            except Exception:
                self.state["instances"] = prev_instances
                self.state["scaling_telemetry"] = prev_telemetry
                raise
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
                if str(b.get("status", "DELIVERED")).strip().upper() in SUCCESSFUL_BUILD_STATUSES
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
            norm_status = status_str.strip().upper()
            if norm_status not in ALLOWED_BUILD_STATUSES:
                raise ValueError(f"Unsupported Rust build status: {status_str!r}")
            is_unfinished = norm_status in UNFINISHED_BUILD_STATUSES
            is_unsuccessful = norm_status in UNSUCCESSFUL_BUILD_STATUSES
            is_non_delivered = is_unfinished or is_unsuccessful
            prev_status_norm = str(existing_build.get("status", "QUEUED")).strip().upper()
            was_non_delivered = (
                prev_status_norm not in SUCCESSFUL_BUILD_STATUSES
                if existing_build
                else True
            )

            raw_duration = (
                payload.get("total_duration_seconds")
                if "total_duration_seconds" in payload
                else (existing_build.get("total_duration_seconds") if not was_non_delivered else None)
            )
            raw_delivery = (
                payload.get("delivery_duration_seconds")
                if "delivery_duration_seconds" in payload
                else (existing_build.get("delivery_duration_seconds") if not was_non_delivered else None)
            )
            raw_compile = (
                payload.get("compile_duration_seconds")
                if "compile_duration_seconds" in payload
                else (existing_build.get("compile_duration_seconds") if not was_non_delivered else None)
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

            req_matches_existing = bool(
                existing_build.get("requested_at")
                and (
                    not payload.get("requested_at")
                    or str(payload.get("requested_at")) == str(existing_build.get("requested_at"))
                    or abs(
                        _iso_to_epoch(str(payload["requested_at"]))
                        - _iso_to_epoch(str(existing_build["requested_at"]))
                    )
                    < 1.0
                    or prev_status_norm in UNFINISHED_BUILD_STATUSES
                )
            )
            if is_non_delivered:
                if explicit_total is not None:
                    duration = explicit_total
                elif explicit_compile is not None:
                    duration = explicit_compile
                elif is_unsuccessful and (payload.get("requested_at") or existing_build.get("requested_at")):
                    req_epoch_existing = _iso_to_epoch(
                        str(payload.get("requested_at") or existing_build["requested_at"])
                    )
                    fin_epoch_target = (
                        _iso_to_epoch(str(payload["finished_at"]))
                        if payload.get("finished_at")
                        else (
                            _iso_to_epoch(str(existing_build["finished_at"]))
                            if existing_build.get("finished_at")
                            else now_ts
                        )
                    )
                    duration = max(0.0, round(fin_epoch_target - req_epoch_existing, 2))
                else:
                    duration = 0.0
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
                elif (
                    was_non_delivered
                    and req_matches_existing
                    and explicit_compile is None
                ):
                    req_epoch_existing = _iso_to_epoch(
                        str(payload.get("requested_at") or existing_build["requested_at"])
                    )
                    if payload.get("delivered_at"):
                        completion_epoch = _iso_to_epoch(str(payload["delivered_at"]))
                    elif payload.get("finished_at") and explicit_delivery is not None:
                        completion_epoch = _iso_to_epoch(str(payload["finished_at"])) + explicit_delivery
                    elif payload.get("finished_at"):
                        completion_epoch = max(_iso_to_epoch(str(payload["finished_at"])), now_ts)
                    else:
                        completion_epoch = now_ts
                    duration = max(1.0, round(completion_epoch - req_epoch_existing, 2))
                    if explicit_delivery is not None:
                        delivery_sec = min(explicit_delivery, duration)
                    elif payload.get("finished_at"):
                        fin_ep = _iso_to_epoch(str(payload["finished_at"]))
                        delivery_sec = min(duration, max(0.0, round(completion_epoch - fin_ep, 2)))
                    else:
                        delivery_sec = min(8.0, round(duration * 0.2, 2))
                else:
                    delivery_sec = explicit_delivery if explicit_delivery is not None else 8.0
                    c_sec = explicit_compile if explicit_compile is not None else 87.0
                    duration = c_sec + delivery_sec

            has_hits = payload.get("cache_hits") is not None
            has_misses = payload.get("cache_misses") is not None
            has_total = payload.get("total_crates") is not None
            has_existing_crates = bool(existing_build)
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
                cache_hits = 0 if is_non_delivered else 380

            if has_misses:
                cache_misses = _finite_int(payload["cache_misses"], "cache_misses", min_val=0)
            elif has_total:
                cache_misses = max(
                    0, _finite_int(payload["total_crates"], "total_crates", min_val=0) - cache_hits
                )
            elif has_existing_crates:
                cache_misses = _finite_int(existing_build.get("cache_misses", 0), "cache_misses", min_val=0)
            else:
                cache_misses = 0 if is_non_delivered else 25

            if has_total:
                total_crates = _finite_int(payload["total_crates"], "total_crates", min_val=0)
            elif has_existing_crates and not (has_hits or has_misses):
                total_crates = _finite_int(existing_build.get("total_crates", 0), "total_crates", min_val=0)
            else:
                total_crates = cache_hits + cache_misses

            if is_non_delivered:
                req_at = (
                    payload.get("requested_at")
                    or existing_build.get("requested_at")
                    or _iso_seconds_ago(duration if is_unsuccessful else 0, now_ts)
                )
                req_epoch = _iso_to_epoch(str(req_at))
                if "finished_at" in payload:
                    fin_at = payload.get("finished_at")
                elif existing_build.get("finished_at"):
                    fin_at = existing_build.get("finished_at")
                elif is_unsuccessful:
                    fin_at = _iso_seconds_ago(0, max(now_ts, req_epoch + max(0.0, duration - delivery_sec)))
                else:
                    fin_at = None
                del_at = (
                    payload.get("delivered_at")
                    if "delivered_at" in payload
                    else existing_build.get("delivered_at")
                )
            elif (
                was_non_delivered
                and req_matches_existing
                and explicit_total is None
                and explicit_compile is None
            ):
                req_at = str(payload.get("requested_at") or existing_build["requested_at"])
                req_epoch = _iso_to_epoch(req_at)
                if payload.get("delivered_at"):
                    del_at = str(payload["delivered_at"])
                    del_epoch = _iso_to_epoch(del_at)
                else:
                    del_epoch = max(now_ts, req_epoch + duration)
                    del_at = _iso_seconds_ago(0, del_epoch)
                if payload.get("finished_at"):
                    fin_at = str(payload["finished_at"])
                else:
                    fin_at = _iso_seconds_ago(0, max(req_epoch, del_epoch - delivery_sec))
            elif (payload.get("requested_at") or existing_build.get("requested_at")) and not payload.get("delivered_at"):
                req_at = str(payload.get("requested_at") or existing_build["requested_at"])
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
                if (
                    existing_build.get("delivered_at")
                    and not payload.get("requested_at")
                    and "total_duration_seconds" not in payload
                    and "delivery_duration_seconds" not in payload
                ):
                    del_at = str(existing_build["delivered_at"])
                else:
                    del_epoch = (
                        (req_epoch + duration)
                        if explicit_total is not None
                        else (fin_epoch + delivery_sec)
                    )
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
                if existing_peak_ram is not None
                else 0.0
            )
            existing_cpu_time = existing_build.get("cpu_time_seconds")
            default_cpu_time = (
                existing_cpu_time
                if existing_cpu_time is not None
                else 0.0
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
                "compile_duration_seconds": raw_compile,
                "delivery_duration_seconds": (
                    delivery_sec
                    if (
                        not is_non_delivered
                        and explicit_total is not None
                        and explicit_delivery is not None
                        and not payload.get("finished_at")
                        and not payload.get("delivered_at")
                    )
                    else raw_delivery
                ),
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
            _assert_finite_json_tree(payload, "payload")
            enriched = enrich_rust_build_record(raw, project=self.project)
            prev_builds = copy.deepcopy(self.state["rust_builds"])
            prev_telemetry = copy.deepcopy(self.state["scaling_telemetry"])
            try:
                if existing_idx is not None:
                    self.state["rust_builds"][existing_idx] = enriched
                else:
                    self.state["rust_builds"].insert(0, enriched)
                self._sync_rust_build_telemetry(now_ts)
                self._persist()
            except Exception:
                self.state["rust_builds"] = prev_builds
                self.state["scaling_telemetry"] = prev_telemetry
                raise
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


def _origin_key(value: str) -> tuple[str, str, int] | None:
    """(scheme, lowercase host, port) of an http(s) origin or URL, or None when it is not one."""
    try:
        parsed = urlparse(str(value or "").strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not host or parsed.username or parsed.password:
        return None
    return scheme, host, port if port is not None else (443 if scheme == "https" else 80)


def normalize_public_origin(value: str | None) -> str | None:
    """Validate a configured public origin (``scheme://host[:port]``, no path, query, or credentials).

    Returns the canonical lowercase origin, None for an empty value, and raises ValueError otherwise.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise ValueError(f"{ENV_PUBLIC_ORIGIN} is not a valid URL: {exc}") from exc
    origin = origin_of_url(text)
    if origin is None or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.params:
        raise ValueError(f"{ENV_PUBLIC_ORIGIN} must be an http(s) origin such as https://spot.example.com")
    return origin


def origin_of_url(url: str | None) -> str | None:
    """Origin (``scheme://host[:port]``) of an absolute http(s) URL, or None."""
    key = _origin_key(url or "")
    if key is None:
        return None
    scheme, host, port = key
    shown_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown_host}" + ("" if port == (443 if scheme == "https" else 80) else f":{port}")


def resolve_public_origin(explicit: str | None = None, *, redirect_uri: str | None = None,
                          environ: Any = None) -> str | None:
    """Public browser origin: the explicit value, else DASHBOARD_PUBLIC_ORIGIN, else the redirect URI's origin."""
    env = os.environ if environ is None else environ
    for candidate in (explicit, env.get(ENV_PUBLIC_ORIGIN)):
        normalized = normalize_public_origin(candidate)
        if normalized:
            return normalized
    for candidate in (redirect_uri, env.get(ENV_REDIRECT_URI)):
        derived = origin_of_url(candidate)
        if derived:
            return derived
    return None


def resolve_config_repo(explicit: str | None = None, *, org: str, environ: Any = None) -> str:
    """Config repo: the explicit value, else SPOT_CONFIG_REPO, else ``<org>/spot-runners-config``."""
    env = os.environ if environ is None else environ
    value = str(explicit or env.get(ENV_CONFIG_REPO) or f"{org}/{CONFIG_REPO_NAME}").strip()
    if not _CONFIG_REPO_RE.fullmatch(value) or value.split("/", 1)[1] in (".", ".."):
        raise ValueError(f"{ENV_CONFIG_REPO} must be owner/repo, got {value!r}")
    return value


_HOST_HEADER_RE = re.compile(r"[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::[0-9]{1,5})?")


def make_handler(
    store: DashboardStore,
    *,
    auth_token: str | None = None,
    require_secure_cookie: bool = False,
    oauth_manager: GitHubOAuthManager | None = None,
    controlled_targets: list[str] | tuple[str, ...] | None = None,
    config_targets_provider: Any = None,
    control_secret: str | None = None,
    public_origin: str | None = None,
    patch_console: PatchConsole | None = None,
    github_token_provider: Any = None,
) -> type[BaseHTTPRequestHandler]:
    """Create the HTTP handler bound to a DashboardStore instance.

    Authentication precedence for protected routes: the legacy ``auth_token`` (an explicit
    ``Authorization``/``X-Dashboard-Token`` header, then the ``dashboard_session`` cookie) is considered
    only when the TCP peer is loopback and no ``Cf-Connecting-Ip``/``Cf-Ray``/``X-Forwarded-For`` header is
    present (a wrong explicit token is rejected with 401); then the GitHub OAuth ``__Host-spot_session``
    cookie, which must be signed, unexpired, issued after ``DASHBOARD_SESSION_NOT_BEFORE``, for the
    owner or a CURRENTLY listed added administrator (re-checked on every request), and verified for every
    controlled target unless the identity is a global administrator (static, stored, and config-repo
    targets; otherwise 403). ``/api/admins`` (owner only) lists and changes the added administrators;
    ``github_token_provider`` (default: ``SPOT_CONFIG_GH_TOKEN`` or ``GH_TOKEN``) returns the token for
    the GitHub user lookup. When the stored controlled-target policy could not be loaded, OAuth sessions
    and sign-ins are refused with HTTP 500 ``controlled_targets_unavailable`` (not 503, which the edge
    Worker would turn into an endless wait). ``/api/health`` is public and ``/api/control/*`` uses
    short-lived control tokens instead.

    Unauthenticated sign-in routes are rate limited per client IP and globally (429 with ``Retry-After``).
    The client IP is the ``X-Spot-Client-Ip`` header from a loopback peer (the edge Worker sets it and
    strips any client copy), else the TCP peer.

    Browser POSTs must come from this server's own Host origin or from ``public_origin`` (the URL users
    open when a proxy such as the Cloudflare Worker forwards to a different Host). ``X-Forwarded-Host``
    is never trusted for this check.
    """
    from .dashboard_ui_assets import render_dashboard_html

    static_targets = [t.name for t in parse_controlled_targets(controlled_targets or [])]
    lookup_token = github_token_provider if github_token_provider is not None else config_repo_token
    control_secret_value = str(control_secret or "").strip()
    public_origin_value = normalize_public_origin(public_origin)
    public_origin_key = _origin_key(public_origin_value) if public_origin_value else None
    auth_required = bool(str(auth_token).strip() if auth_token else "") or oauth_manager is not None
    unauthorized_message = (
        "Unauthorized: sign in with GitHub or provide a valid dashboard token"
        if oauth_manager is not None
        else "Unauthorized: valid Bearer token or X-Dashboard-Token required"
    )

    expected_token = str(auth_token).strip() if auth_token else ""
    expected_session_cookie = (
        hmac.new(
            expected_token.encode("utf-8"),
            b"spot-dashboard-session-v1",
            hashlib.sha256,
        ).hexdigest()
        if expected_token
        else ""
    )

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        timeout = 10.0

        def setup(self) -> None:
            super().setup()
            if isinstance(self.connection, ssl.SSLSocket):
                if self.connection.gettimeout() is None:
                    self.connection.settimeout(self.timeout or 10.0)
                self.connection.do_handshake()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _format_session_cookie(self) -> str:
            forwarded_proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
            is_secure = (
                require_secure_cookie
                or isinstance(getattr(self, "connection", None), ssl.SSLSocket)
                or forwarded_proto == "https"
            )
            secure_attr = "; Secure" if is_secure else ""
            return f"dashboard_session={expected_session_cookie}; HttpOnly; SameSite=Strict; Path=/{secure_attr}"

        def _format_cleared_session_cookie(self) -> str:
            forwarded_proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
            is_secure = (
                require_secure_cookie
                or isinstance(getattr(self, "connection", None), ssl.SSLSocket)
                or forwarded_proto == "https"
            )
            secure_attr = "; Secure" if is_secure else ""
            return f"dashboard_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT{secure_attr}"

        def _send_json(
            self,
            data: Any,
            status: int = HTTPStatus.OK,
            extra_headers: dict[str, str] | None = None,
            cookies: list[str] | None = None,
        ) -> None:
            body = json.dumps(data, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Marks a response the dashboard itself produced, so the edge Worker never mistakes an application
            # 5xx (for example a GitHub permission error) for the control VM being down.
            self.send_header(ORIGIN_MARKER_HEADER, ORIGIN_MARKER_VALUE)
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            for cookie_hdr in cookies or []:
                self.send_header("Set-Cookie", cookie_hdr)
            self.end_headers()
            self.wfile.write(body)

        def _send_html(
            self,
            html_text: str,
            status: int = HTTPStatus.OK,
            extra_headers: dict[str, str] | None = None,
            cookies: list[str] | None = None,
        ) -> None:
            body = html_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Marks a response the dashboard itself produced, so the edge Worker never mistakes an application
            # 5xx (for example a GitHub permission error) for the control VM being down.
            self.send_header(ORIGIN_MARKER_HEADER, ORIGIN_MARKER_VALUE)
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            for cookie_hdr in cookies or []:
                self.send_header("Set-Cookie", cookie_hdr)
            self.end_headers()
            self.wfile.write(body)

        def _send_redirect(self, location: str, cookies: list[str] | None = None) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            # Marks a response the dashboard itself produced, so the edge Worker never mistakes an application
            # 5xx (for example a GitHub permission error) for the control VM being down.
            self.send_header(ORIGIN_MARKER_HEADER, ORIGIN_MARKER_VALUE)
            for cookie_hdr in cookies or []:
                self.send_header("Set-Cookie", cookie_hdr)
            self.end_headers()

        def _header_token_candidate(self) -> str:
            auth_hdr = (self.headers.get("Authorization") or "").strip()
            hdr_token = (self.headers.get("X-Dashboard-Token") or "").strip()
            candidate = ""
            if auth_hdr.lower().startswith("bearer "):
                candidate = auth_hdr[7:].strip()
            elif auth_hdr:
                candidate = auth_hdr
            if not candidate and hdr_token:
                candidate = hdr_token
            return candidate

        def _legacy_token_allowed(self) -> bool:
            """The legacy dashboard token is honoured only for direct loopback clients (never via a proxy)."""
            if not expected_token:
                return False
            peer = str((self.client_address or ("",))[0] or "")
            if peer.startswith("::ffff:"):
                peer = peer[len("::ffff:"):]
            if not _is_loopback_host(peer):
                return False
            return not any(self.headers.get(name) for name in FORWARDED_CLIENT_HEADERS)

        def _config_targets(self) -> list[str]:
            """Config-repo targets (cached by the provider); raises ConfigTargetsError to fail closed."""
            if config_targets_provider is None:
                return []
            return list(config_targets_provider())

        def _effective_controlled_targets(self) -> list[str]:
            """Union of static, stored, and config-repo targets. Raises ConfigTargetsError when unlisted."""
            combined = static_targets + store.get_controlled_targets()
            if oauth_manager is not None:
                combined += self._config_targets()
            return combined

        def _client_ip(self) -> str:
            peer = str((self.client_address or ("",))[0] or "")
            if peer.startswith("::ffff:"):
                peer = peer[len("::ffff:"):]
            forwarded = (self.headers.get(CLIENT_IP_HEADER) or "").strip()
            if forwarded and _is_loopback_host(peer):
                try:
                    return str(ipaddress.ip_address(forwarded))
                except ValueError:
                    pass
            return peer

        def _send_too_many(self, retry_after: float, message: str) -> None:
            seconds = max(1, int(math.ceil(retry_after)))
            self._send_json(
                {"error": message, "retry_after": seconds},
                status=HTTPStatus.TOO_MANY_REQUESTS,
                extra_headers={"Retry-After": str(seconds)},
            )

        def _rate_limited(self, group: str) -> bool:
            """Count this request against a sign-in rate limiter; respond 429 and return True when over."""
            if oauth_manager is None:
                return False
            limiter = oauth_manager.rate_limiters.get(group)
            if limiter is None:
                return False
            retry_after = limiter.check(self._client_ip())
            if retry_after <= 0:
                return False
            self._send_too_many(retry_after, "Too many sign-in requests; try again later")
            return True

        def _policy_unavailable_body(self) -> dict[str, Any]:
            return {
                "status": "unavailable",
                "error": "GitHub sign-in is disabled because the controlled-target policy could not be loaded",
                "code": POLICY_UNAVAILABLE_ERROR,
                "detail": store.controlled_targets_error,
            }

        def _send_policy_unavailable(self) -> None:
            # 500, not 503: the edge Worker turns origin 503s into the control-VM wait response, and waiting
            # does not repair an unreadable state file.
            self._send_json(self._policy_unavailable_body(), status=HTTPStatus.INTERNAL_SERVER_ERROR)

        @staticmethod
        def _added_admins() -> frozenset[int]:
            """The added administrators' GitHub IDs, read from state for THIS request; owners only on any error."""
            try:
                return store.dashboard_admin_uids()
            except Exception:  # noqa: BLE001 - an unreadable list must never grant access
                LOG.exception("Could not read the added administrators; only owners can sign in")
                return frozenset()

        def _evaluate_auth(self) -> tuple[str, dict[str, Any]]:
            """Classify the request as ("ok" | "unauthenticated" | "forbidden" | "unavailable", details)."""
            if not auth_required:
                return "ok", {"method": "none"}
            cookies = parse_cookie_header(self.headers.get("Cookie"))
            if self._legacy_token_allowed():
                candidate = self._header_token_candidate()
                if candidate:
                    if hmac.compare_digest(candidate.encode("utf-8"), expected_token.encode("utf-8")):
                        return "ok", {"method": "token"}
                    return "unauthenticated", {"explicit_token_rejected": True}
                sess_val = cookies.get("dashboard_session", "").strip()
                if sess_val and hmac.compare_digest(
                    sess_val.encode("utf-8"), expected_session_cookie.encode("utf-8")
                ):
                    return "ok", {"method": "dashboard_session"}
            if oauth_manager is not None:
                oauth_cookie = cookies.get(SESSION_COOKIE_NAME, "")
                if oauth_cookie:
                    added_admins = self._added_admins()
                    identity = oauth_manager.check_session(oauth_cookie, [], added_admins)
                    if identity.status == "forbidden" and identity.session is not None:
                        return "forbidden", {
                            "method": "github_oauth",
                            "user": identity.session.user_login,
                            "message": identity.message,
                            "missing_targets": [],
                        }
                    if identity.status == "ok" and identity.session is not None and store.controlled_targets_error:
                        # Fail closed: without a trustworthy target policy no OAuth session is honoured.
                        return "unavailable", {
                            "method": "github_oauth",
                            "user": identity.session.user_login,
                            "uid": identity.session.user_id,
                            "message": self._policy_unavailable_body()["error"],
                        }
                    if identity.status == "ok" and identity.session is not None:
                        try:
                            targets = self._effective_controlled_targets()
                        except ConfigTargetsError as exc:
                            return "forbidden", {
                                "method": "github_oauth",
                                "user": identity.session.user_login,
                                "message": f"Forbidden: {exc}",
                                "missing_targets": [],
                            }
                        check = oauth_manager.check_session(oauth_cookie, targets, added_admins)
                        if check.status == "ok" and check.session is not None:
                            return "ok", {
                                "method": "github_oauth",
                                "user": check.session.user_login,
                                "uid": check.session.user_id,
                            }
                        if check.session is not None:
                            return "forbidden", {
                                "method": "github_oauth",
                                "user": check.session.user_login,
                                "message": check.message,
                                "missing_targets": list(check.missing_targets),
                            }
            return "unauthenticated", {}

        def _send_unavailable(self, extra: dict[str, Any] | None = None) -> None:
            """The contract 503 wait response: the browser waits and retries the same request."""
            payload = unavailable_payload()
            if extra:
                payload.update(extra)
            self._send_json(
                payload,
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                extra_headers={"Retry-After": str(DEFAULT_RETRY_AFTER_SECONDS)},
            )

        def _durability_gate(self, method: str, path: str) -> bool:
            """Refuse requests until hydrated, and mutations while frozen. Returns True when it responded."""
            if path == "/api/health" or path.startswith("/api/control/"):
                return False
            if not store.hydrated:
                self._send_unavailable()
                return True
            if method in MUTATING_METHODS and (store.frozen or not store.reconciled):
                self._send_unavailable()
                return True
            return False

        def _send_health(self) -> None:
            info = store.health()
            if info["hydrated"] and not info["frozen"] and info["reconciled"]:
                self._send_json(info)
            else:
                self._send_unavailable(info)

        def _handle_control(self, method: str, path: str) -> None:
            """``/api/control/<op>``: bearer control token for this op and this VM's slot; no OAuth."""
            self.close_connection = True  # freeze/unfreeze bodies are not read; never reuse the connection
            op = path[len("/api/control/"):]
            if not control_secret_value or store.control_slot is None or op not in CONTROL_OPS:
                self._send_json({"error": f"Unknown endpoint: {path}"}, status=HTTPStatus.NOT_FOUND)
                return
            expected_method = "GET" if op == "snapshot" else "POST"
            if method != expected_method:
                self._send_json({"error": "Method not allowed"}, status=HTTPStatus.METHOD_NOT_ALLOWED,
                                extra_headers={"Allow": expected_method})
                return
            auth_hdr = (self.headers.get("Authorization") or "").strip()
            token = auth_hdr[7:].strip() if auth_hdr.lower().startswith("bearer ") else ""
            if not verify_control_token(control_secret_value, token, op, store.control_slot):
                self._send_json({"error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            try:
                if op == "freeze":
                    result = store.freeze()
                    LOG.info("Control freeze at state_version %s (kv_flushed=%s)", result["state_version"],
                             result["kv_flushed"])
                    self._send_json(result)
                elif op == "unfreeze":
                    LOG.info("Control unfreeze")
                    self._send_json(store.unfreeze())
                elif op == "snapshot":
                    self._send_json(store.snapshot_envelope())
                else:
                    envelope = self._read_json(max_bytes=MAX_CONTROL_BODY_BYTES)
                    result = store.import_snapshot(envelope)
                    LOG.info("Control import: %s", result)
                    self._send_json(result)
            except _PayloadTooLargeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            except FreezeBusyError as exc:
                LOG.warning("Control freeze refused: %s", exc)
                self._send_json({"error": str(exc), "retryable": True}, status=HTTPStatus.CONFLICT)
            except (ValueError, TypeError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except OSError as exc:
                LOG.error("Control %s failed on a local write: %s", op, exc)
                body: dict[str, Any] = {"error": f"Control {op} failed on a local write "
                                                 f"(state file or freeze marker): {exc}"}
                if op == "import":
                    body["imported"] = False
                self._send_json(body, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def _send_forbidden(self, info: dict[str, Any]) -> None:
            self._send_json(
                {
                    "error": info.get("message") or "Forbidden",
                    "user": info.get("user", ""),
                    "missing_targets": info.get("missing_targets", []),
                },
                status=HTTPStatus.FORBIDDEN,
            )

        def _oauth_redirect_uri(self) -> str | None:
            if oauth_manager is None:
                return None
            if oauth_manager.redirect_uri:
                return oauth_manager.redirect_uri
            if public_origin_value:
                return f"{public_origin_value}/auth/github/callback"
            host = (self.headers.get("Host") or "").strip()
            if not _HOST_HEADER_RE.fullmatch(host):
                return None
            forwarded_proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
            is_https = (
                require_secure_cookie
                or isinstance(getattr(self, "connection", None), ssl.SSLSocket)
                or forwarded_proto == "https"
            )
            return f"{'https' if is_https else 'http'}://{host}/auth/github/callback"

        def _login_gate(self, *, message: str = "", user: str = "", status: int = HTTPStatus.OK,
                        cookies: list[str] | None = None) -> None:
            assert oauth_manager is not None
            self._send_html(
                render_login_gate_html(
                    device_flow=oauth_manager.device_flow_enabled,
                    web_flow=oauth_manager.web_flow_enabled,
                    legacy_token=bool(expected_token),
                    message=message,
                    signed_in_as=user,
                ),
                status=status,
                cookies=cookies,
            )

        def _handle_oauth_get(self, path: str, qs: dict[str, list[str]]) -> bool:
            """Serve public OAuth GET routes. Returns True when the request was handled."""
            if path in ("/auth/status", "/api/auth/status"):
                status, info = self._evaluate_auth()
                self._send_json(
                    {
                        "authenticated": status == "ok",
                        "status": status,
                        "method": info.get("method"),
                        "user": info.get("user"),
                        # The browser shows the Admins page only to owners; /api/admins enforces it.
                        "owner": self._is_owner_session(info) if status == "ok" else False,
                        "missing_targets": info.get("missing_targets", []),
                        "oauth": {
                            "enabled": oauth_manager is not None,
                            "device_flow": bool(oauth_manager and oauth_manager.device_flow_enabled),
                            "web_flow": bool(oauth_manager and oauth_manager.web_flow_enabled),
                        },
                        "legacy_token": bool(expected_token),
                    }
                )
                return True
            if path not in ("/auth/github/login", "/auth/github/callback"):
                return False
            if oauth_manager is None or not oauth_manager.web_flow_enabled:
                self._send_json(
                    {"error": "GitHub web login is not configured (set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET)"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return True
            if self._rate_limited("start" if path == "/auth/github/login" else "complete"):
                return True
            redirect_uri = self._oauth_redirect_uri()
            if not redirect_uri:
                self._send_json({"error": "Cannot determine OAuth redirect URI"}, status=HTTPStatus.BAD_REQUEST)
                return True
            if path == "/auth/github/login":
                if store.controlled_targets_error:
                    self._login_gate(message=self._policy_unavailable_body()["error"],
                                     status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return True
                try:
                    state = oauth_manager.create_state(safe_return_path(qs.get("next", ["/"])[0]))
                except AuthCapacityError as exc:
                    self._send_too_many(exc.retry_after, str(exc))
                    return True
                self._send_redirect(
                    oauth_manager.create_authorize_url(redirect_uri, state),
                    cookies=[format_state_cookie(state)],
                )
                return True
            # /auth/github/callback
            clear_state = [format_cleared_state_cookie()]
            if qs.get("error"):
                self._login_gate(message="GitHub authorization was not granted.", status=HTTPStatus.FORBIDDEN,
                                 cookies=clear_state)
                return True
            cookie_state = parse_cookie_header(self.headers.get("Cookie")).get(OAUTH_STATE_COOKIE_NAME, "")
            result = oauth_manager.consume_state(qs.get("state", [""])[0], cookie_state)
            if not result.ok:
                self._login_gate(message="Sign-in request expired or is invalid. Please try again.",
                                 status=HTTPStatus.BAD_REQUEST, cookies=clear_state)
                return True
            try:
                token = oauth_manager.exchange_code_for_token(
                    qs.get("code", [""])[0], redirect_uri, qs.get("state", [""])[0]
                )["access_token"]
            except GitHubApiError:
                audit_login("", None, "denied", "code exchange failed")
                self._login_gate(message="GitHub sign-in failed.", status=HTTPStatus.FORBIDDEN, cookies=clear_state)
                return True
            status, body, cookies = self._finish_oauth_login(token, result.target_change)
            if status != HTTPStatus.OK:
                self._login_gate(message=str(body.get("message") or body.get("error") or "Sign-in failed"),
                                 status=status, cookies=clear_state)
                return True
            self._send_redirect(result.return_to, cookies=clear_state + cookies)
            return True

        def _finish_oauth_login(
            self, access_token: str, change: TargetChange | None
        ) -> tuple[int, dict[str, Any], list[str]]:
            """Authorize a fresh GitHub token, commit a bound target change, and issue a session.

            Without a change, the token must satisfy the current policy (positive cache allowed). With a
            change, the token must belong to the requesting GitHub id and pass a FRESH (uncached) admin
            check on the static, config-repo, current, and proposed targets, so the change can neither
            loosen a target the user no longer administers nor lock the user out. The change commits
            through the store's durability gates (writer, frozen, reconciled) and only if the stored list
            still matches the one it was based on (compare-and-swap).
            """
            assert oauth_manager is not None
            if store.controlled_targets_error:
                return HTTPStatus.INTERNAL_SERVER_ERROR, self._policy_unavailable_body(), []
            try:
                config_targets = self._config_targets()
            except ConfigTargetsError as exc:
                audit_login("", None, "denied", "config repo targets unavailable")
                message = f"Forbidden: {exc}"
                return HTTPStatus.FORBIDDEN, {"status": "forbidden", "error": message, "message": message,
                                              "user": "", "missing_targets": []}, []
            if change is None:
                decision = oauth_manager.authorize(
                    access_token, static_targets + store.get_controlled_targets() + config_targets,
                    added_admins=self._added_admins(),
                )
            else:
                current = store.get_controlled_targets()
                if not hmac.compare_digest(change.base, target_list_digest(current)):
                    return HTTPStatus.CONFLICT, {
                        "status": "conflict",
                        "error": "Controlled targets changed after this change was requested; review and submit it again",
                    }, []
                decision = oauth_manager.authorize(
                    access_token,
                    static_targets + config_targets + current + list(change.targets),
                    use_cache=False,
                    expected_uid=change.uid,
                    added_admins=self._added_admins(),
                )
            if not decision.authorized:
                return HTTPStatus.FORBIDDEN, {
                    "status": "forbidden",
                    "error": decision.message,
                    "message": decision.message,
                    "user": decision.user_login,
                    "missing_targets": decision.missing_targets,
                }, []
            if not oauth_manager.session_fits(decision.verified_targets):
                audit_login(decision.user_login, decision.user_id, "denied", "session cookie would be too large")
                return HTTPStatus.BAD_REQUEST, {"status": "error", "error": SESSION_TOO_LARGE_ERROR}, []
            body: dict[str, Any] = {"status": "authorized", "user": decision.user_login}
            if change is not None:
                def commit() -> tuple[int, Any]:
                    store.set_controlled_targets(
                        list(change.targets),
                        actor=decision.user_login,
                        actor_uid=decision.user_id,
                        method="github_oauth",
                        expected_digest=change.base,
                    )
                    return HTTPStatus.OK, {}

                try:
                    store.run_request(None, "POST", "/api/auth/targets", commit)
                except DashboardFrozenError:
                    return HTTPStatus.CONFLICT, {
                        "status": "not_accepting_changes",
                        "error": "The dashboard is not accepting changes right now (a control VM handover may be "
                        "in progress). The change was not applied; submit it again.",
                    }, []
                except ControlledTargetsConflictError as exc:
                    return HTTPStatus.CONFLICT, {"status": "conflict", "error": str(exc)}, []
                except ControlledTargetsUnavailableError:
                    return HTTPStatus.INTERNAL_SERVER_ERROR, self._policy_unavailable_body(), []
                except ValueError as exc:
                    return HTTPStatus.BAD_REQUEST, {"status": "error", "error": str(exc)}, []
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("Could not save controlled targets")
                    return HTTPStatus.INTERNAL_SERVER_ERROR, {
                        "status": "error",
                        "error": f"Could not save controlled targets ({type(exc).__name__})",
                    }, []
                try:
                    body["target_change"] = {"committed": True, **self._targets_body()}
                except ConfigTargetsError:
                    body["target_change"] = {"committed": True}
            cookies = [format_session_cookie(oauth_manager.issue_session(decision), oauth_manager.session_ttl_seconds)]
            return HTTPStatus.OK, body, cookies

        def _handle_oauth_post(self, path: str) -> bool:
            """Serve public OAuth POST routes (already same-origin/JSON validated). Returns True when handled."""
            if path not in ("/auth/device/code", "/auth/device/token", "/auth/logout"):
                return False
            try:
                payload = self._read_json(max_bytes=MAX_LOGIN_BODY_BYTES)
            except _PayloadTooLargeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return True
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return True
            if path == "/auth/logout":
                cleared = [format_cleared_session_cookie()]
                if expected_token:
                    cleared.insert(0, self._format_cleared_session_cookie())
                self._send_json({"status": "ok", "authenticated": False}, cookies=cleared)
                return True
            if oauth_manager is None or not oauth_manager.device_flow_enabled:
                self._send_json({"error": f"GitHub device login is disabled (set {ENV_DEVICE_FLOW}=1)"},
                                status=HTTPStatus.NOT_FOUND)
                return True
            if path == "/auth/device/code":
                if self._rate_limited("start"):
                    return True
                if store.controlled_targets_error:
                    self._send_policy_unavailable()
                    return True
                try:
                    started = oauth_manager.begin_device_login()
                except AuthCapacityError as exc:
                    self._send_too_many(exc.retry_after, str(exc))
                    return True
                except GitHubApiError:
                    self._send_json({"error": "GitHub device authorization is unavailable"}, status=HTTPStatus.BAD_GATEWAY)
                    return True
                self._send_json(started)
                return True
            # /auth/device/token
            if self._rate_limited("complete"):
                return True
            polled = oauth_manager.poll_device_login(payload.get("device_handle"))
            result = polled.get("status")
            if result == "invalid":
                self._send_json({"status": "invalid", "error": "Invalid or expired device handle"},
                                status=HTTPStatus.BAD_REQUEST)
                return True
            if result in ("pending", "slow_down"):
                body: dict[str, Any] = {"status": result}
                if result == "slow_down":
                    body["interval"] = polled.get("interval", 10)
                self._send_json(body)
                return True
            if result == "expired":
                self._send_json({"status": "expired", "error": "Device code expired"}, status=HTTPStatus.BAD_REQUEST)
                return True
            if result == "denied":
                self._send_json({"status": "denied", "error": "GitHub authorization was denied"},
                                status=HTTPStatus.FORBIDDEN)
                return True
            if result == "error" and polled.get("error") == "github_unavailable":
                # The handshake is kept: tell the browser to keep polling the same code until it expires.
                self._send_json({"status": "retry", "retryable": True, "error": "GitHub is temporarily unreachable"},
                                status=HTTPStatus.SERVICE_UNAVAILABLE, extra_headers={"Retry-After": "10"})
                return True
            if result != "success":
                self._send_json({"status": "error", "error": "GitHub device token request failed"},
                                status=HTTPStatus.BAD_GATEWAY)
                return True
            status, body, cookies = self._finish_oauth_login(
                str(polled.get("access_token", "")), polled.get("target_change")
            )
            self._send_json(body, status=status, cookies=cookies)
            return True

        def _handle_targets_post(self, info: dict[str, Any]) -> None:
            """Request a change of the dynamic controlled-target list (OAuth sessions only).

            Nothing commits here. The response (202 ``reauth_required``) starts a fresh GitHub sign-in (web
            ``authorize_url`` with a state cookie, or a device code) bound to the proposed list; the change
            commits when that sign-in proves admin rights on every static, config-repo, current, and proposed
            target. An unchanged list answers 200 ``unchanged``.
            """
            assert oauth_manager is not None
            try:
                payload = self._read_json(max_bytes=MAX_JSON_BODY_BYTES)
            except _PayloadTooLargeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                names = store.validate_controlled_targets(payload.get("targets"))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if store.controlled_targets_error:
                self._send_policy_unavailable()
                return
            try:
                body = self._targets_body()
                config_targets = self._config_targets()
            except ConfigTargetsError as exc:
                self._send_json({"error": f"Forbidden: {exc}"}, status=HTTPStatus.FORBIDDEN)
                return
            current = store.get_controlled_targets()
            if names == current:
                self._send_json({"status": "unchanged", **body})
                return
            try:
                verify = [t.name for t in parse_controlled_targets(static_targets + config_targets + current + names)]
            except ValueError as exc:
                self._send_json({"error": f"Stored controlled targets are invalid ({exc})"},
                                status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            # The session issued after this change carries every verified target in its cookie.
            if not oauth_manager.session_fits(verify):
                self._send_json({"error": SESSION_TOO_LARGE_ERROR}, status=HTTPStatus.BAD_REQUEST)
                return
            uid, login = info.get("uid"), info.get("user")
            if not isinstance(uid, int) or isinstance(uid, bool) or not isinstance(login, str):
                self._send_json({"error": "Session identity is incomplete; sign in again"},
                                status=HTTPStatus.UNAUTHORIZED)
                return
            default_flow = "web" if oauth_manager.web_flow_enabled else "device"
            flow = str(payload.get("flow") or default_flow).lower()
            if flow not in ("web", "device"):
                self._send_json({"error": "flow must be 'web' or 'device'"}, status=HTTPStatus.BAD_REQUEST)
                return
            if self._rate_limited("start"):
                return
            change = TargetChange(tuple(names), target_list_digest(current), uid, login)
            reply: dict[str, Any] = {
                "status": "reauth_required",
                "message": "Confirm this change by signing in with GitHub again; it is applied only after "
                "GitHub confirms you administer every listed target.",
                "flow": flow,
                "current_targets": current,
                "proposed_targets": names,
                "verify_targets": verify,
            }
            if flow == "web":
                redirect_uri = self._oauth_redirect_uri() if oauth_manager.web_flow_enabled else None
                if not redirect_uri:
                    self._send_json({"error": "GitHub web sign-in is not available"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    state = oauth_manager.create_state(safe_return_path(payload.get("next") or "/"),
                                                       target_change=change)
                except AuthCapacityError as exc:
                    self._send_too_many(exc.retry_after, str(exc))
                    return
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                reply["authorize_url"] = oauth_manager.create_authorize_url(redirect_uri, state)
                self._send_json(reply, status=HTTPStatus.ACCEPTED, cookies=[format_state_cookie(state)])
                return
            if not oauth_manager.device_flow_enabled:
                self._send_json({"error": f"GitHub device login is disabled (set {ENV_DEVICE_FLOW}=1)"},
                                status=HTTPStatus.BAD_REQUEST)
                return
            try:
                reply["device"] = oauth_manager.begin_device_login(target_change=change)
            except AuthCapacityError as exc:
                self._send_too_many(exc.retry_after, str(exc))
                return
            except GitHubApiError:
                self._send_json({"error": "GitHub device authorization is unavailable"}, status=HTTPStatus.BAD_GATEWAY)
                return
            self._send_json(reply, status=HTTPStatus.ACCEPTED)

        def _check_auth(self, *, allow_html_shell: bool = False,
                        evaluated: tuple[str, dict[str, Any]] | None = None) -> bool:
            """Verify credentials for a protected route; respond 401, 403, or 500 (policy unavailable)."""
            status, info = evaluated if evaluated is not None else self._evaluate_auth()
            if status == "ok":
                return True
            if status == "forbidden":
                self._send_forbidden(info)
                return False
            if status == "unavailable":
                self._send_policy_unavailable()
                return False
            if allow_html_shell and not info.get("explicit_token_rejected"):
                return True
            self._send_json(
                {"error": unauthorized_message},
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
                matches_public = (
                    public_origin_key is not None
                    and parsed_origin.path == ""
                    and _origin_key(origin) == public_origin_key
                )
                if parsed_origin.scheme not in ("http", "https") or (
                    req_host and origin_host != req_host and not matches_public
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

        def _read_json(self, *, max_bytes: int = MAX_JSON_BODY_BYTES) -> dict[str, Any]:
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                raise ValueError("Transfer-Encoding is not supported; provide a bounded Content-Length header")
            raw_len = (self.headers.get("Content-Length") or "0").strip()
            try:
                length = int(raw_len)
            except ValueError as exc:
                self.close_connection = True
                raise ValueError("Invalid Content-Length header") from exc
            if length < 0:
                self.close_connection = True
                raise ValueError("Content-Length cannot be negative")
            if length > max_bytes:
                self.close_connection = True
                raise _PayloadTooLargeError(
                    f"Request body of {length} bytes exceeds maximum allowed size of {max_bytes} bytes"
                )
            if length == 0:
                return {}
            try:
                raw_bytes = self.rfile.read(length)
            except (TimeoutError, OSError) as exc:
                self.close_connection = True
                raise ValueError("Request body read timed out") from exc
            if len(raw_bytes) != length:
                self.close_connection = True
                raise ValueError("Truncated request body")
            raw = raw_bytes.decode("utf-8")
            if not raw.strip():
                return {}

            def _reject_non_finite_constant(val: str) -> Any:
                raise ValueError(f"Non-finite JSON constant '{val}' is not allowed")

            parsed = json.loads(raw, parse_constant=_reject_non_finite_constant)
            _assert_finite_json_tree(parsed, "payload")
            if not isinstance(parsed, dict):
                raise ValueError("JSON request body must be an object")
            return parsed

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if self._durability_gate("GET", path):
                return
            if path == "/api/health":
                self._send_health()
                return
            if path.startswith("/api/control/"):
                self._handle_control("GET", path)
                return

            if path in ("/", "/index.html"):
                if oauth_manager is not None:
                    gate_status, gate_info = self._evaluate_auth()
                    if gate_status != "ok":
                        if gate_info.get("explicit_token_rejected"):
                            self._check_auth()
                            return
                        if gate_status in ("forbidden", "unavailable"):
                            self._login_gate(
                                message=str(gate_info.get("message", "")),
                                user=str(gate_info.get("user", "")),
                                status=HTTPStatus.FORBIDDEN
                                if gate_status == "forbidden"
                                else HTTPStatus.INTERNAL_SERVER_ERROR,
                            )
                        else:
                            self._login_gate()
                        return
                elif not self._check_auth(allow_html_shell=True):
                    return
                extra_hdrs: dict[str, str] | None = None
                if self._legacy_token_allowed():
                    auth_hdr = (self.headers.get("Authorization") or "").strip()
                    hdr_token = (self.headers.get("X-Dashboard-Token") or "").strip()
                    supplied = (
                        auth_hdr[7:].strip()
                        if auth_hdr.lower().startswith("bearer ")
                        else hdr_token
                    )
                    if supplied and hmac.compare_digest(supplied, expected_token):
                        extra_hdrs = {
                            "Set-Cookie": self._format_session_cookie()
                        }
                self._send_html(
                    render_dashboard_html(project=store.project, org=store.org),
                    extra_headers=extra_hdrs,
                )
                return

            if self._handle_oauth_get(path, qs):
                return

            auth_status, auth_info = self._evaluate_auth()
            if not self._check_auth(evaluated=(auth_status, auth_info)):
                return

            if path == "/api/auth/targets":
                self._send_targets()
                return

            if path == "/api/admins":
                if self._require_owner(auth_info):
                    self._send_json(self._admins_body())
                return

            if path in ("/api/patches", "/api/patches/manifest"):
                self._patches_get(path, qs)
                return

            try:
                if path == "/api/overview":
                    include_deleted = qs.get("include_deleted", ["true"])[0].lower() != "false"
                    window_hours = _finite_float(
                        qs.get("window_hours", ["24"])[0], "window_hours", min_val=0.01
                    )
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
                    window_hours = _finite_float(
                        qs.get("window_hours", ["24"])[0], "window_hours", min_val=0.01
                    )
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
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._send_json({"error": f"Unknown endpoint: {path}"}, status=HTTPStatus.NOT_FOUND)

        # -- added administrators (/api/admins, owner only) --

        def _is_owner_session(self, auth_info: dict[str, Any]) -> bool:
            return (
                oauth_manager is not None
                and auth_info.get("method") == "github_oauth"
                and oauth_manager.is_owner(auth_info.get("user"), auth_info.get("uid"))
            )

        def _require_owner(self, auth_info: dict[str, Any]) -> bool:
            """Answer 403 unless the request carries a GitHub session of a dashboard owner."""
            if self._is_owner_session(auth_info):
                return True
            self._send_json({"error": OWNER_REQUIRED_ERROR, "code": "owner_required"}, status=HTTPStatus.FORBIDDEN)
            return False

        def _admins_body(self) -> dict[str, Any]:
            assert oauth_manager is not None
            body: dict[str, Any] = {
                "owners": [
                    {"login": login, "uid": uid, "global_admin": oauth_manager.is_global_admin(login, uid)}
                    for login, uid in sorted(OWNER_IDENTITIES)
                ],
                "admins": store.get_dashboard_admins(),
                "history": store.get_dashboard_admins_history()[-20:],
                # False when DASHBOARD_GLOBAL_ADMINS is "none": added administrators then need administrator
                # permission on every controlled target, like the owner.
                "added_admins_exempt": bool(oauth_manager.global_admins),
                "max_admins": MAX_ADDED_ADMINS,
            }
            if store.controlled_targets_error:
                body["error"] = store.controlled_targets_error
            return body

        def _admins_post(self, path: str, payload: dict[str, Any], key: str | None, auth_info: dict[str, Any]) -> None:
            if not self._require_owner(auth_info):
                return
            assert oauth_manager is not None
            if path not in ADMIN_MUTATION_PATHS:
                self._send_json({"error": f"Unknown endpoint: {path}"}, status=HTTPStatus.NOT_FOUND)
                return
            if key is None:
                self._send_json({"error": "This change needs an Idempotency-Key header"}, status=HTTPStatus.BAD_REQUEST)
                return
            if payload.get("confirm") is not True:
                self._send_json({"error": "Changing dashboard administrators needs \"confirm\": true"},
                                status=HTTPStatus.BAD_REQUEST)
                return
            actor, actor_uid = str(auth_info.get("user")), auth_info.get("uid")
            fingerprint = request_fingerprint(payload)
            if path == "/api/admins/add":
                try:
                    login = normalize_github_login(payload.get("login"))
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                stored = store.peek_idempotent(key, "POST", path, fingerprint)
                if stored is not None:
                    status, body, replayed = stored
                    self._send_json(body, status=status, extra_headers={"Idempotent-Replay": "true"} if replayed else None)
                    return
                try:
                    token = str(lookup_token() or "")
                except Exception:  # noqa: BLE001 - no token means an anonymous lookup
                    token = ""
                try:
                    user = oauth_manager.lookup_user(login, token)
                except GitHubUserLookupError as exc:
                    error: dict[str, Any] = {"error": str(exc)}
                    if exc.status == HTTPStatus.FAILED_DEPENDENCY:
                        error["code"] = "upstream_error"
                    if exc.retryable:
                        error["retry_same_key"] = True  # Nothing changed; the same key may try again.
                    self._send_json(error, status=exc.status)
                    return

                def commit() -> tuple[int, Any]:
                    added = store.add_dashboard_admin(user["login"], user["uid"], actor=actor, actor_uid=actor_uid)
                    return HTTPStatus.OK, {"status": "added", "admin": added, **self._admins_body()}
            else:
                uid = payload.get("uid")
                if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
                    self._send_json({"error": "uid must be a positive integer GitHub user ID"},
                                    status=HTTPStatus.BAD_REQUEST)
                    return

                def commit() -> tuple[int, Any]:
                    removed = store.remove_dashboard_admin(uid, actor=actor, actor_uid=actor_uid)
                    return HTTPStatus.OK, {"status": "removed", "admin": removed, **self._admins_body()}

            def run() -> tuple[int, Any]:
                try:
                    return commit()
                except DashboardAdminError as exc:
                    return exc.status, {"error": str(exc)}
                except ControlledTargetsUnavailableError:
                    return HTTPStatus.INTERNAL_SERVER_ERROR, self._policy_unavailable_body()

            try:
                status, body, replayed = store.run_request(key, "POST", path, run, fingerprint=fingerprint)
            except DashboardFrozenError:
                self._send_unavailable()
                return
            self._send_json(body, status=status, extra_headers={"Idempotent-Replay": "true"} if replayed else None)

        def _send_targets(self) -> None:
            try:
                body = self._targets_body()
            except ConfigTargetsError as exc:
                self._send_json({"error": f"Forbidden: {exc}"}, status=HTTPStatus.FORBIDDEN)
                return
            self._send_json(body)

        def _targets_body(self) -> dict[str, Any]:
            config_targets = self._config_targets() if oauth_manager is not None else []
            body: dict[str, Any] = {
                "static_targets": list(static_targets),
                "dynamic_targets": store.get_controlled_targets(),
                "config_targets": config_targets,
                "locked_targets": sorted(set(static_targets + config_targets), key=str.lower),
                "controlled_targets": self._effective_controlled_targets(),
                "history": store.get_controlled_targets_history()[-10:],
            }
            if store.controlled_targets_error:
                body["error"] = store.controlled_targets_error
            return body

        def _dispatch_post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
            """Run one protected POST route and return (status, JSON body) without writing the response."""
            try:
                if path == "/api/budget":
                    return HTTPStatus.OK, store.update_budget(payload)
                if path == "/api/profile/cicd_runner":
                    return HTTPStatus.OK, store.update_profile("cicd_runner", payload)
                if path == "/api/profile/rust_builder":
                    return HTTPStatus.OK, store.update_profile("rust_builder", payload)
                if path == "/api/qualify":
                    role = str(payload.get("role", "cicd_runner")).strip()
                    if role not in ("cicd_runner", "rust_builder"):
                        raise ValueError(f"Unsupported workload role: {role}")
                    merged = {**store.state["profiles"].get(role, {}), **payload}
                    return HTTPStatus.OK, qualify_instances(
                        merged,
                        store.state["regions_enabled"],
                        store.project,
                        region_latencies_ms=store.state.get("region_latencies_ms"),
                    )
                if path == "/api/scaling":
                    return HTTPStatus.OK, store.update_scaling(payload)
                if path == "/api/regions":
                    return HTTPStatus.OK, store.update_regions(payload)
                if path == "/api/cloud-init/save":
                    return HTTPStatus.OK, store.save_cloud_init_template(payload)
                if path == "/api/cloud-init/parse":
                    return HTTPStatus.OK, parse_cloud_init(str(payload.get("content", "")))
                if path == "/api/vm-templates/from-instance":
                    return HTTPStatus.OK, store.create_vm_template_from_instance(payload)
                if path == "/api/instances/delete":
                    return HTTPStatus.OK, store.delete_instance(payload)
                if path == "/api/instances/scale-region":
                    return HTTPStatus.OK, store.scale_region_instances(payload)
                if path == "/api/rust-builds":
                    return HTTPStatus.OK, store.record_rust_build(payload)
                if path == "/api/ip-intel":
                    return HTTPStatus.OK, classify_ip_and_network(
                        str(payload.get("ip_address", "")),
                        payload.get("hostname"),
                    )
                return HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"}
            except ConfigTargetsError as exc:
                return HTTPStatus.FORBIDDEN, {"error": f"Forbidden: {exc}"}
            except _PayloadTooLargeError as exc:
                return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                return HTTPStatus.BAD_REQUEST, {"error": str(exc)}

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if self._durability_gate("POST", path):
                return
            if path.startswith("/api/control/"):
                self._handle_control("POST", path)
                return
            ok_req, err_status, err_msg = self._validate_same_origin_json_post()
            if not ok_req:
                self._send_json({"error": err_msg}, status=err_status)
                return
            if path == "/api/auth/login":
                try:
                    payload = self._read_json(max_bytes=MAX_LOGIN_BODY_BYTES)
                except _PayloadTooLargeError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                token_candidate = str(payload.get("token", "")).strip()
                if not expected_token:
                    if oauth_manager is not None:
                        self._send_json(
                            {"error": "Dashboard token login is not configured; sign in with GitHub"},
                            status=HTTPStatus.UNAUTHORIZED,
                        )
                        return
                    self._send_json({"status": "ok", "authenticated": True})
                    return
                if not self._legacy_token_allowed():
                    audit_login("", None, "denied", "legacy token from a non-loopback or proxied client")
                    self._send_json(
                        {"error": "Dashboard token login is accepted only from a direct local connection"},
                        status=HTTPStatus.UNAUTHORIZED,
                        extra_headers={"WWW-Authenticate": 'Bearer realm="Spot Dashboard"'},
                    )
                    return
                if token_candidate and hmac.compare_digest(
                    token_candidate.encode("utf-8"), expected_token.encode("utf-8")
                ):
                    self._send_json(
                        {"status": "ok", "authenticated": True},
                        extra_headers={
                            "Set-Cookie": self._format_session_cookie()
                        },
                    )
                    return
                self._send_json(
                    {"error": "Unauthorized: invalid dashboard authentication token"},
                    status=HTTPStatus.UNAUTHORIZED,
                    extra_headers={"WWW-Authenticate": 'Bearer realm="Spot Dashboard"'},
                )
                return
            if path == "/api/auth/logout":
                try:
                    self._read_json(max_bytes=MAX_LOGIN_BODY_BYTES)
                except _PayloadTooLargeError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                logout_cookies = [self._format_cleared_session_cookie()]
                if oauth_manager is not None:
                    logout_cookies.append(format_cleared_session_cookie())
                self._send_json({"status": "ok", "authenticated": False}, cookies=logout_cookies)
                return
            if self._handle_oauth_post(path):
                return
            auth_status, auth_info = self._evaluate_auth()
            if not self._check_auth(evaluated=(auth_status, auth_info)):
                return
            if path == "/api/auth/targets":
                if auth_info.get("method") != "github_oauth" or oauth_manager is None:
                    self._send_json(
                        {"error": "Changing controlled targets requires a GitHub OAuth session"},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                # Not an idempotent mutation: it only starts a bound re-verification; the commit happens
                # in the OAuth callback or device poll, through the store's durability gates.
                self._handle_targets_post(auth_info)
                return
            try:
                payload = self._read_json(max_bytes=MAX_JSON_BODY_BYTES)
            except _PayloadTooLargeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            key = (self.headers.get("Idempotency-Key") or "").strip() or None
            if key is not None and not _IDEMPOTENCY_KEY_RE.fullmatch(key):
                self._send_json({"error": "Invalid Idempotency-Key header"}, status=HTTPStatus.BAD_REQUEST)
                return
            if path.startswith("/api/patches/"):
                self._patches_post(path, payload, key)
                return
            if path == "/api/admins" or path.startswith("/api/admins/"):
                self._admins_post(path, payload, key, auth_info)
                return
            try:
                status, body, replayed = store.run_request(
                    key, "POST", path, lambda: self._dispatch_post(path, payload),
                    mutating=path not in NON_MUTATING_POST_PATHS, fingerprint=request_fingerprint(payload),
                )
            except DashboardFrozenError:
                self._send_unavailable()
                return
            self._send_json(body, status=status, extra_headers={"Idempotent-Replay": "true"} if replayed else None)

        # -- runner patch console (/api/patches) --

        def _send_patch_error(self, exc: PatchConsoleError) -> None:
            self._send_json({"error": str(exc), **exc.body}, status=exc.status)

        def _fleet_instances(self) -> list[dict[str, Any]]:
            with store._lock:
                return copy.deepcopy([i for i in store.state.get("instances", []) if isinstance(i, dict)])

        def _patches_get(self, path: str, qs: dict[str, list[str]]) -> None:
            if patch_console is None:
                self._send_json({"error": "The runner patch console is not configured"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                if path == "/api/patches":
                    self._send_json(patch_console.list_manifests())
                else:
                    self._send_json(patch_console.load_manifest(qs.get("path", [""])[0]))
            except PatchConsoleError as exc:
                self._send_patch_error(exc)

        def _patches_post(self, path: str, payload: dict[str, Any], key: str | None) -> None:
            if patch_console is None:
                self._send_json({"error": "The runner patch console is not configured"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                if path == "/api/patches/validate":
                    self._send_json(patch_console.validate(str(payload.get("path") or ""), payload.get("content")))
                    return
                if path == "/api/patches/preview":
                    self._send_json(patch_console.preview(str(payload.get("repository") or ""),
                                                          self._fleet_instances(), payload.get("content")))
                    return
            except PatchConsoleError as exc:
                self._send_patch_error(exc)
                return
            if path not in EXTERNAL_MUTATION_PATHS:
                self._send_json({"error": f"Unknown endpoint: {path}"}, status=HTTPStatus.NOT_FOUND)
                return
            if key is None:
                self._send_json({"error": "This change needs an Idempotency-Key header"}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/patches/hot-patch" and payload.get("confirm") is not True:
                self._send_json({"error": "Hot-patching running hosts needs \"confirm\": true"},
                                status=HTTPStatus.BAD_REQUEST)
                return

            def run() -> tuple[int, Any]:
                try:
                    if path == "/api/patches/save":
                        return HTTPStatus.OK, patch_console.save(
                            str(payload.get("path") or ""), payload.get("content"), payload.get("base_sha"),
                            payload.get("message"), idempotency_key=key)
                    return patch_console.hot_patch(str(payload.get("repository") or ""),
                                                   payload.get("patch_sha256"), self._fleet_instances())
                except PatchConsoleError as exc:
                    return exc.status, {"error": str(exc), **exc.body}

            try:
                status, body, replayed = store.run_external(key, "POST", path, run,
                                                            fingerprint=request_fingerprint(payload))
            except DashboardFrozenError:
                self._send_unavailable()
                return
            headers = {"Idempotent-Replay": "true"} if replayed else {}
            if status == HTTPStatus.ACCEPTED and isinstance(body, dict) and body.get("pending_retry"):
                headers["Retry-After"] = str(body.get("retry_after_seconds", EXTERNAL_PENDING_RETRY_SECONDS))
            self._send_json(body, status=status, extra_headers=headers or None)

        def _reject_method(self, method: str) -> None:
            parsed = urlparse(self.path)
            if self._durability_gate(method, parsed.path):
                return
            self._send_json({"error": "Method not allowed"}, status=HTTPStatus.METHOD_NOT_ALLOWED,
                            extra_headers={"Allow": "GET, POST"})

        def do_PUT(self) -> None:  # noqa: N802
            self._reject_method("PUT")

        def do_PATCH(self) -> None:  # noqa: N802
            self._reject_method("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._reject_method("DELETE")

    return DashboardRequestHandler


class DashboardThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    dashboard_store: DashboardStore | None = None

    def server_close(self) -> None:
        super().server_close()
        if self.dashboard_store is not None:
            self.dashboard_store.close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (TimeoutError, ssl.SSLError, ConnectionError, BrokenPipeError, OSError)):
            return
        super().handle_error(request, client_address)


def create_dashboard_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    state_path: Path | None = None,
    project: str = "homelab-424523",
    org: str = "swack-tools",
    auth_token: str | None = None,
    tls_certfile: str | Path | None = None,
    tls_keyfile: str | Path | None = None,
    allow_insecure_non_loopback: bool | None = None,
    oauth_manager: GitHubOAuthManager | None = None,
    controlled_targets: list[str] | tuple[str, ...] | None = None,
    config_targets_provider: Any = None,
    control_slot: str | None = None,
    control_secret: str | None = None,
    kv: CloudflareKv | None = None,
    public_origin: str | None = None,
    config_repo: str | None = None,
    patch_console: PatchConsole | None = None,
    github_token_provider: Any = None,
) -> tuple[ThreadingHTTPServer, DashboardStore]:
    """Instantiate the ThreadingHTTPServer and DashboardStore, then start hydration.

    ``oauth_manager`` enables GitHub OAuth login (additive to ``auth_token``); ``controlled_targets`` are
    static organization or ``owner/repo`` targets on which OAuth users must be administrators, in
    addition to the dynamic list stored in dashboard state and every repository in the private config
    repo (``config_targets_provider``; defaults to :class:`ConfigRepoTargets` for ``config_repo``, else
    ``SPOT_CONFIG_REPO``, else ``<org>/spot-runners-config``, when OAuth is enabled).
    ``control_slot``/``kv`` enable Cloudflare KV hydration and write-behind; ``control_secret`` enables
    ``/api/control/*``. Nothing is read from Cloudflare unless ``kv`` is passed. ``public_origin``
    (else ``DASHBOARD_PUBLIC_ORIGIN``, else the OAuth redirect URI's origin) is the browser-facing origin
    accepted on POSTs in addition to the request's own Host.
    """
    resolved_token = (
        auth_token
        if auth_token is not None
        else os.environ.get("DASHBOARD_AUTH_TOKEN")
    )
    cleaned_token = str(resolved_token).strip() if resolved_token else ""
    resolved_cert = (
        tls_certfile
        if tls_certfile is not None
        else os.environ.get("DASHBOARD_TLS_CERTFILE")
    )
    resolved_key = (
        tls_keyfile
        if tls_keyfile is not None
        else os.environ.get("DASHBOARD_TLS_KEYFILE")
    )
    cleaned_cert = str(resolved_cert).strip() if resolved_cert else ""
    cleaned_key = str(resolved_key).strip() if resolved_key else ""
    if bool(cleaned_cert) != bool(cleaned_key):
        raise ValueError(
            "Both --tls-certfile and --tls-keyfile (or DASHBOARD_TLS_CERTFILE and DASHBOARD_TLS_KEYFILE) must be provided together."
        )
    has_tls = bool(cleaned_cert and cleaned_key)
    resolved_allow_insecure = (
        bool(allow_insecure_non_loopback)
        if allow_insecure_non_loopback is not None
        else (
            str(os.environ.get("DASHBOARD_ALLOW_INSECURE_NON_LOOPBACK", ""))
            .strip()
            .lower()
            in ("1", "true", "yes", "on")
        )
    )
    if not _is_loopback_host(host):
        if not cleaned_token and oauth_manager is None:
            raise ValueError(
                f"Refusing to bind Web UI Dashboard to non-loopback address '{host}' without authentication. "
                "Provide --auth-token or set DASHBOARD_AUTH_TOKEN, configure GitHub OAuth "
                "(GITHUB_OAUTH_CLIENT_ID and DASHBOARD_SESSION_SECRET), or bind to 127.0.0.1 / ::1 / localhost."
            )
        if not has_tls and not resolved_allow_insecure:
            raise ValueError(
                f"Refusing to bind Web UI Dashboard to non-loopback address '{host}' over plaintext HTTP without TLS. "
                "Provide --tls-certfile and --tls-keyfile (or DASHBOARD_TLS_CERTFILE / DASHBOARD_TLS_KEYFILE), "
                "bind to a loopback address (127.0.0.1 / ::1 / localhost) behind a TLS proxy, or pass "
                "--allow-insecure-non-loopback (DASHBOARD_ALLOW_INSECURE_NON_LOOPBACK=1) if a trusted network proxy terminates TLS."
            )
    static_targets = [t.name for t in parse_controlled_targets(controlled_targets or [])]
    if oauth_manager is not None and not oauth_manager.session_fits(static_targets):
        raise ValueError(SESSION_TOO_LARGE_ERROR.replace("Controlled target list", "Static controlled target list"))
    resolved_public_origin = resolve_public_origin(
        public_origin, redirect_uri=oauth_manager.redirect_uri if oauth_manager is not None else None
    )
    if oauth_manager is not None and config_targets_provider is None:
        config_targets_provider = ConfigRepoTargets(resolve_config_repo(config_repo, org=org))
    store = DashboardStore(state_path=state_path, project=project, org=org, control_slot=control_slot, kv=kv)
    handler = make_handler(
        store,
        auth_token=cleaned_token or None,
        require_secure_cookie=has_tls or not _is_loopback_host(host),
        oauth_manager=oauth_manager,
        controlled_targets=static_targets,
        config_targets_provider=config_targets_provider,
        control_secret=control_secret,
        public_origin=resolved_public_origin,
        patch_console=patch_console if patch_console is not None else PatchConsole(
            resolve_config_repo(config_repo, org=org), executor=executor_from_env(project),
            host_source=host_source_from_env(project)),
        github_token_provider=github_token_provider,
    )
    server = DashboardThreadingHTTPServer((host, port), handler)
    server.dashboard_store = store
    if has_tls:
        try:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_ctx.load_cert_chain(certfile=cleaned_cert, keyfile=cleaned_key)
            server.socket = ssl_ctx.wrap_socket(
                server.socket,
                server_side=True,
                do_handshake_on_connect=False,
            )
        except Exception:
            server.server_close()
            raise
    store.start()
    return server, store


def build_oauth_config(
    *,
    controlled_targets: str | None = None,
    redirect_uri: str | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[GitHubOAuthManager | None, list[str]]:
    """Resolve GitHub OAuth settings from CLI values (preferred) and the environment.

    Returns ``(None, targets)`` when ``GITHUB_OAUTH_CLIENT_ID`` is unset. Raises ValueError when OAuth is
    enabled without an adequate ``DASHBOARD_SESSION_SECRET`` or when any setting is invalid. The owner
    identity is hardcoded (``swackhamer``, ID 619624) and cannot be configured; other administrators are
    added on the dashboard's Admins page and stored in dashboard state.
    """
    env = os.environ if environ is None else environ
    extra: dict[str, Any] = {}
    if redirect_uri:
        extra["redirect_uri"] = redirect_uri
    manager = GitHubOAuthManager.from_environ(env, **extra)
    raw_targets = controlled_targets if controlled_targets is not None else env.get(ENV_CONTROLLED_TARGETS, "")
    targets = [t.name for t in parse_controlled_targets_env(raw_targets)]
    return manager, targets


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
        "--tls-certfile",
        default=None,
        help="Path to PEM TLS certificate file for HTTPS (mandatory for non-loopback --host unless --allow-insecure-non-loopback is set; or set DASHBOARD_TLS_CERTFILE)",
    )
    parser.add_argument(
        "--tls-keyfile",
        default=None,
        help="Path to PEM TLS private key file for HTTPS (or set DASHBOARD_TLS_KEYFILE)",
    )
    parser.add_argument(
        "--allow-insecure-non-loopback",
        action="store_true",
        default=None,
        help="Explicitly allow plaintext HTTP on a non-loopback --host when behind a trusted TLS-terminating proxy (or set DASHBOARD_ALLOW_INSECURE_NON_LOOPBACK=1)",
    )
    parser.add_argument(
        "--control-slot",
        choices=CONTROL_SLOTS,
        default=None,
        help=(
            "Control VM slot (a or b). Enables Cloudflare KV hydration and write-behind when "
            "CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_KV_NAMESPACE_ID, and CLOUDFLARE_KV_API_TOKEN are set, and "
            f"/api/control/* when {ENV_CONTROL_SECRET} is set"
        ),
    )
    parser.add_argument(
        "--controlled-targets",
        default=None,
        help=f"Comma-separated orgs and owner/repo targets on which OAuth users must be admins, in addition to every repo in the --config-repo repository (overrides {ENV_CONTROLLED_TARGETS}; default: none)",
    )
    parser.add_argument(
        "--oauth-redirect-uri",
        default=None,
        help="Absolute GitHub OAuth callback URL, for example https://spot.example.com/auth/github/callback (or set GITHUB_OAUTH_REDIRECT_URI; default: derived from the Host header)",
    )
    parser.add_argument(
        "--config-repo",
        default=None,
        help=(
            f"Runner configuration repository whose repos/<owner>/<repo>.yaml files are controlled targets "
            f"(or set {ENV_CONFIG_REPO}; default: <org>/{CONFIG_REPO_NAME})"
        ),
    )
    parser.add_argument(
        "--public-origin",
        default=None,
        help=(
            f"Browser-facing origin, for example https://spot.example.com, accepted on POSTs when a proxy "
            f"forwards under a different Host (or set {ENV_PUBLIC_ORIGIN}; default: the origin of the OAuth "
            "redirect URI)"
        ),
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
    from .log_format import configure_logging

    configure_logging()
    oauth_manager, controlled_targets = build_oauth_config(
        controlled_targets=args.controlled_targets,
        redirect_uri=args.oauth_redirect_uri,
    )
    kv_config = KvConfig.from_env()
    kv = CloudflareKv(kv_config) if kv_config is not None and args.control_slot else None
    if kv_config is not None and not args.control_slot:
        LOG.warning("Cloudflare KV credentials are set but --control-slot is not; state stays local only")
    server, _ = create_dashboard_server(
        host=args.host,
        port=args.port,
        state_path=args.state_file,
        project=args.project,
        org=args.org,
        auth_token=args.auth_token,
        tls_certfile=args.tls_certfile,
        tls_keyfile=args.tls_keyfile,
        allow_insecure_non_loopback=args.allow_insecure_non_loopback,
        oauth_manager=oauth_manager,
        controlled_targets=controlled_targets,
        control_slot=args.control_slot,
        control_secret=os.environ.get(ENV_CONTROL_SECRET) or None,
        kv=kv,
        public_origin=args.public_origin,
        config_repo=args.config_repo,
    )
    actual_host, actual_port = server.server_address[:2]
    scheme = "https" if isinstance(server.socket, ssl.SSLSocket) else "http"
    print(f"Spot Fleet & Rust Builder Web UI Dashboard listening on {scheme}://{actual_host}:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Operator requested graceful shutdown via Ctrl-C; close the listening socket in finally.
        return 0
    finally:
        server.server_close()
    return 0

