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
from typing import Any, Callable
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
from .dashboard_deferred_history import append_deferred_context, deferred_segments
from .dashboard_history import (normalize_unpriced_intervals, add_unpriced_interval, history_components,
                                history_in_window, compact_unpriced_intervals, tax_history_components,
                                split_legacy_tax_intervals)
from .fleet_inventory import fleet_source_from_env, gce_epoch, observe_detached_disks, builder_shape_compatible
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
    validate_builder_container_limits,
)
from .dashboard_scaling import (
    _assert_finite_json_tree,
    _is_recent_unhandled_oom,
    _is_eligible_github_oom,
    classify_github_ci_failure,
    default_scaling_config,
    evaluate_builder_scaling_decision,
    evaluate_scaling_decision,
    validate_github_oom_run,
)
from .rust_build_telemetry import (
    ALLOWED_BUILD_STATUSES,
    SUCCESSFUL_BUILD_STATUSES,
    UNFINISHED_BUILD_STATUSES,
    UNSUCCESSFUL_BUILD_STATUSES,
    _crate_counts,
    _finite_float,
    _finite_int,
    _iso_to_epoch,
    classify_ip_and_network,
    enrich_rust_build_record,
)

# A completed build is stored only with its own timing: requested and delivered times (reported, derived from
# reported durations, or the report's arrival for delivery) and a finish time only when one is known. Nothing
# stands in for a missing figure.
UNKNOWN_BUILD_TIMING_MESSAGE = (
    "A completed Rust build needs its timing: report requested_at, total_duration_seconds, or both "
    "compile_duration_seconds and delivery_duration_seconds (delivered_at defaults to when the report arrives)"
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
# Plan-only lifecycle endpoints: recomputed from current state, never stored, and answered even while the slot is
# frozen or unreconciled (they only read).
PLAN_ONLY_POST_PATHS = frozenset({"/api/instances/delete", "/api/instances/scale-region"})
NON_MUTATING_POST_PATHS = frozenset({"/api/qualify", "/api/cloud-init/parse", "/api/ip-intel",
                                     "/api/patches/validate", "/api/patches/preview"}) | PLAN_ONLY_POST_PATHS
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


def _iso_at_epoch(epoch: float) -> str:
    """UTC ISO timestamp of ``epoch``: whole seconds when it is one, else with microseconds."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if abs(epoch - round(epoch)) < 1e-6:
        return datetime.fromtimestamp(round(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    pricing: dict[str, Any] | None = None,
    provisioning: str = "spot",
) -> dict[str, Any]:
    ref_now = now_epoch if now_epoch is not None else time.time()
    family = machine_type.split("-", 1)[0]
    reg_info = next((r for r in REGIONS_CATALOG if r["region"] == region), REGIONS_CATALOG[0])
    tax_rate = (
        float(tax_rate_percent)
        if tax_rate_percent is not None
        else float(reg_info.get("tax_rate_default", 8.25))
    )
    if pricing is None:
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
            provisioning=provisioning,
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


# -- live instance inventory --------------------------------------------------------------------------------
#
# Instance records are written only by refresh_fleet_inventory from a read-only Compute Engine listing and carry
# ``inventory_source: "gce"``. Records without it (the demo instances earlier versions seeded into the default
# state, and records the old in-dashboard delete or scale-region actions fabricated) are dropped on load.
INSTANCE_SOURCE_KEY = "inventory_source"
INSTANCE_SOURCE_GCE = "gce"
FLEET_INVENTORY_INTERVAL_SECONDS = 120.0
# The inventory counts as stale when no listing has completed for this many refresh intervals.
FLEET_INVENTORY_MAX_AGE_INTERVALS = 3
# When the first successful inventory listing was applied (the baseline later growth is measured against).
FLEET_BASELINE_STATE_KEY = "fleet_inventory_baseline_at"
# Deleted VMs stay in the history this long (the longest cost window the dashboard offers is 30 days).
DELETED_INSTANCE_RETENTION_SECONDS = 31 * 86_400.0
MAX_WINDOW_HOURS = DELETED_INSTANCE_RETENTION_SECONDS / 3600.0
# Compute Engine states in which the VM is stopped: compute billing ended at lastStopTimestamp.
# DETACHED is a fleet disk no VM uses (see fleet_inventory.observe_detached_disks): storage only, like a stopped VM.
GCE_STOPPED_STATUSES = frozenset({"TERMINATED", "STOPPED", "SUSPENDED", "DETACHED"})
LIFECYCLE_ACTIONS_DISABLED_MESSAGE = (
    "The dashboard does not create, stop, or delete VMs: its instance list mirrors the live Compute Engine "
    "inventory. Run lifecycle changes with the reviewed tools instead (spot_runner_autoscaler.py drains and "
    "retires runner hosts; spot_runner_manager.py provisions them; render_builder_startup.py renders a dedicated "
    "builder). This response is a plan only; nothing changed."
)


# -- seeded demo telemetry ------------------------------------------------------------------------------------
#
# Earlier versions seeded demo Rust builds, GitHub CI failure runs, a journal OOM event, and job counters into the
# default state, which then persisted to the state file and the Cloudflare KV snapshot and showed as real. Loading
# a state without the migration marker (one written by such a version) drops these exact records. Legacy job
# counters have no source provenance and remain as reported. A record matches only when its id and the fields every
# seeded version gave it all match, so a real report merged into a seeded row survives. The default state carries
# the marker, so later loads never touch telemetry reported after the migration. The active slot then commits the
# migrated state as a new state_version (DashboardStore.commit_demo_telemetry_migration).
DEMO_TELEMETRY_MIGRATION_KEY = "demo_telemetry_migration"
DEMO_TELEMETRY_MIGRATION_VERSION = 1
# True while the drop is saved only at the loaded state_version (it survives a restart); any new version clears it.
DEMO_TELEMETRY_MIGRATION_PENDING_KEY = "demo_telemetry_migration_pending"
# build_id -> every field the seed set except its timestamps, which were relative to when the state was created.
# record_rust_build merges a partial update into the existing row, so a real update to a seeded build_id keeps
# whatever it omits; matching every field (and the seeded gaps between the timestamps) means any real change keeps
# the row.
SEEDED_RUST_BUILDS: dict[str, dict[str, Any]] = {
    "rb-20261015-001": {
        "project_name": "oxidex", "profile": "release", "target_triple": "x86_64-unknown-linux-gnu",
        "commit_sha": "af7c14f8cb2c", "status": "DELIVERED", "cache_hits": 412, "cache_misses": 18,
        "total_crates": 430, "instance_name": "builder-oxidex-turin-01", "zone": "us-south1-b",
        "machine_type": "c4d-highcpu-16", "vcpu": 16, "cpu_platform": "AMD Turin", "origin_ip": "185.159.157.42",
        "origin_hostname": "node-ch-14.protonvpn.net", "peak_ram_gib": 21.8, "container_ram_cap_gib": 28.0,
        "cpu_time_seconds": 1184.5,
    },
    "rb-20261015-002": {
        "project_name": "oxidex-cli", "profile": "release", "target_triple": "x86_64-unknown-linux-musl",
        "commit_sha": "ce4af62b32cc", "status": "DELIVERED", "cache_hits": 295, "cache_misses": 94,
        "total_crates": 389, "instance_name": "builder-oxidex-eu-01", "zone": "europe-west4-a",
        "machine_type": "n4-highcpu-16", "vcpu": 16, "cpu_platform": "Intel Emerald Rapids",
        "origin_ip": "185.220.101.34", "origin_hostname": "tor-exit-101.zwiebelfreunde.de", "peak_ram_gib": 24.6,
        "container_ram_cap_gib": 28.0, "cpu_time_seconds": 1892.0,
    },
    "rb-20261015-003": {
        "project_name": "oxidex", "profile": "debug", "target_triple": "x86_64-unknown-linux-gnu",
        "commit_sha": "f20f9f9b780f", "status": "DELIVERED", "cache_hits": 424, "cache_misses": 6,
        "total_crates": 430, "instance_name": "builder-oxidex-turin-01", "zone": "us-south1-b",
        "machine_type": "c4d-highcpu-16", "vcpu": 16, "cpu_platform": "AMD Turin", "origin_ip": "73.162.44.19",
        "origin_hostname": "m4air-workstation.local", "peak_ram_gib": 14.2, "container_ram_cap_gib": 28.0,
        "cpu_time_seconds": 542.0,
    },
    "rb-20261015-004": {
        "project_name": "nzbget-rust-ffi", "profile": "release", "target_triple": "aarch64-apple-darwin",
        "commit_sha": "e3c84b8190aa", "status": "DELIVERED", "cache_hits": 318, "cache_misses": 42,
        "total_crates": 360, "instance_name": "builder-oxidex-eu-01", "zone": "europe-west4-a",
        "machine_type": "n4-highcpu-16", "vcpu": 16, "cpu_platform": "Intel Emerald Rapids",
        "origin_ip": "193.138.218.77", "origin_hostname": "se-got-wg-004.mullvad.net", "peak_ram_gib": 19.7,
        "container_ram_cap_gib": 28.0, "cpu_time_seconds": 1510.4,
    },
    "rb-20261015-005": {
        "project_name": "oxidex-bench", "profile": "release", "target_triple": "x86_64-unknown-linux-gnu",
        "commit_sha": "3317b1b902ef", "status": "DELIVERED", "cache_hits": 260, "cache_misses": 170,
        "total_crates": 430, "instance_name": "builder-oxidex-turin-01", "zone": "us-south1-b",
        "machine_type": "c4d-highcpu-16", "vcpu": 16, "cpu_platform": "AMD Turin", "origin_ip": "24.28.119.82",
        "origin_hostname": "dev-macbook-pro.tx.rr.com", "peak_ram_gib": 26.1, "container_ram_cap_gib": 28.0,
        "cpu_time_seconds": 2480.0,
    },
}
# build_id -> seconds from requested_at to finished_at, and from finished_at to delivered_at
SEEDED_RUST_BUILD_GAPS = {
    "rb-20261015-001": (102, 12),
    "rb-20261015-002": (160, 16),
    "rb-20261015-003": (64, 8),
    "rb-20261015-004": (145, 14),
    "rb-20261015-005": (190, 14),
}
# Match source event content too: reports can replace seeded rows while retaining the same identity.
# RELATIVE timestamps in the earliest versions are checked against the unchanged build timestamps.
SEEDED_GITHUB_CI_RUNS = [
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
        "error_excerpt": "line 14: 15780 Killed python3 tools/exiftool-tables — ##[error]Process completed with exit code 137.",
        "deep_link": "https://github.com/swack-tools/oxidex/actions/runs/37192272062/job/111407847874#step:8:106"
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
        "error_excerpt": "test_committed_rules_and_ledger_equal_fresh_native_generation ... ##[error]Process completed with exit code 137.",
        "deep_link": "https://github.com/swack-tools/oxidex/actions/runs/37192994059/job/111409747367#step:7:77"
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
        "deep_link": "https://github.com/Appz4Fun/nzbget/actions/runs/38596411473/job/115964282184#step:2:50"
    },
    {
        "id": "gh-oom-38601924810",
        "repository": "swack-tools/oxidex",
        "workflow_name": "Rust 1.99 Qualification Matrix",
        "run_id": 38601924810,
        "job_id": 116012948102,
        "job_name": "cargo-nextest-heavy",
        "runner_name": "oxidex-za-jnb-01-3",
        "step_number": 4,
        "line_number": 118,
        "failed_at": "RELATIVE",
        "error_excerpt": "LLVM ERROR: out of memory / Allocation failed during LTO link pass (exit code 137)",
        "deep_link": "https://github.com/swack-tools/oxidex/actions/runs/38601924810/job/116012948102#step:4:118"
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
        "failed_at": "RELATIVE",
        "error_excerpt": "2026-10-14T16:25:26.2656514Z ##[error]Timeout reached, aborting! (Canceling Pages deployment)",
        "deep_link": "https://github.com/Appz4Fun/nzbget/actions/runs/38596411473/job/115964282184#step:2:50"
    },
    {
        "id": "gh-oom-38596411473",
        "repository": "Appz4Fun/nzbget",
        "workflow_name": "CI Build & Integration Suite",
        "run_id": 38596411473,
        "job_id": 115964282184,
        "job_name": "linux-x64-release-build",
        "runner_name": "oxidex-dallas-01-2",
        "step_number": 2,
        "line_number": 50,
        "failed_at": "RELATIVE",
        "error_excerpt": "error: could not compile `nzbget-core`: process didn't exit successfully (signal: 9, SIGKILL: kill) — container memory.max (16 GiB) exhausted",
        "deep_link": "https://github.com/Appz4Fun/nzbget/actions/runs/38596411473/job/115964282184#step:2:50"
    }
]
SEEDED_JOURNAL_OOM_EVENT = {
    "id": "oom-journal-01", "instance_name": "oxidex-runners-dallas-01", "zone": "us-south1-a",
    "pid": 18492, "process": "rustc", "constraint": "CONSTRAINT_MEMCG",
    "journal_line": (
        "kernel: oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),"
        "cpuset=docker-8f4a21.scope,mems_allowed=0,oom_memcg=/system.slice/docker-8f4a21.scope,"
        "task_memcg=/system.slice/docker-8f4a21.scope,task=rustc,pid=18492,uid=1001"
    ),
}
SEEDED_BUILD_REQUEST_OFFSETS = {
    "rb-20261015-001": 920, "rb-20261015-002": 2450, "rb-20261015-003": 4800,
    "rb-20261015-004": 7600, "rb-20261015-005": 11200,
}


# Earlier versions also gave the preset VM templates a ``source_instance`` naming a VM that never existed. The
# presets stay (they are shapes, not copies of a VM), but loading a state without this marker removes those exact
# (template id, source instance) pairs once. Templates created from a live VM keep their source.
DEMO_TEMPLATE_SOURCE_MIGRATION_KEY = "demo_template_source_migration"
DEMO_TEMPLATE_SOURCE_MIGRATION_VERSION = 1
SEEDED_TEMPLATE_SOURCE_INSTANCES = frozenset({
    ("tpl-runner-n4-std16", "oxidex-runners-dallas-01"),
    ("tpl-runner-za-n2-16", "oxidex-runners-za-jnb-01"),
    ("tpl-builder-c4d-16", "builder-oxidex-turin-01"),
})

# Dedicated builders run on n4-highcpu-16 (16 vCPU, 32 GiB RAM). Earlier versions shipped the builder preset as
# tpl-builder-c4d-16 on c4d-highcpu-16; loading a state without this marker replaces an unedited copy of that old
# preset with tpl-builder-n4-16 once and repoints profiles that used it. An edited copy is the owner's template
# and stays as it is.
BUILDER_PRESET_MIGRATION_KEY = "builder_preset_migration"
BUILDER_PRESET_MIGRATION_VERSION = 1
BUILDER_PRESET_ID = "tpl-builder-n4-16"
LEGACY_BUILDER_PRESET_ID = "tpl-builder-c4d-16"
# The old preset exactly as earlier versions wrote it, without its generated created_at (and without the source
# instance that drop_seeded_template_sources removes).
LEGACY_BUILDER_PRESET: dict[str, Any] = {
    "id": LEGACY_BUILDER_PRESET_ID,
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
}


def migrate_builder_preset(
    state: dict[str, Any],
    validate: Callable[[dict[str, Any], dict[str, Any], str], Any] | None = None,
) -> bool:
    """Replace an unedited legacy c4d builder preset with the n4 preset in place; True when it changed ``state``.

    ``validate(template, profile, role)`` raises ValueError when a profile can't use a template. When any profile
    that uses the old preset would reject the new one (a raised benchmark minimum, for example), nothing changes:
    the owner's working configuration stays as it is.
    """
    templates = state.get("vm_templates")
    if not isinstance(templates, list) or any(
            isinstance(t, dict) and t.get("id") == BUILDER_PRESET_ID for t in templates):
        return False
    index = next((i for i, tpl in enumerate(templates)
                  if isinstance(tpl, dict) and tpl.get("id") == LEGACY_BUILDER_PRESET_ID
                  and {k: v for k, v in tpl.items() if k != "created_at"} == LEGACY_BUILDER_PRESET), None)
    if index is None:
        return False
    default_preset = next(t for t in default_dashboard_state()["vm_templates"] if t["id"] == BUILDER_PRESET_ID)
    preset = dict(default_preset, created_at=templates[index].get("created_at", default_preset["created_at"]))
    profiles = state.get("profiles")
    users = [(role, profile) for role, profile in (profiles.items() if isinstance(profiles, dict) else [])
             if isinstance(profile, dict) and profile.get("vm_template_id") == LEGACY_BUILDER_PRESET_ID]
    if validate is not None:
        for role, profile in users:
            try:
                validate(preset, profile, str(profile.get("role", role)))
            except ValueError as exc:
                LOG.warning("Kept builder preset %s: profile %s would not accept %s (%s)",
                            LEGACY_BUILDER_PRESET_ID, role, BUILDER_PRESET_ID, exc)
                return False
    templates[index] = preset
    for _role, profile in users:
        profile["vm_template_id"] = BUILDER_PRESET_ID
    return True


def drop_seeded_template_sources(state: dict[str, Any]) -> int:
    """Remove the fabricated ``source_instance`` of the preset VM templates in place; returns how many it removed."""
    dropped = 0
    templates = state.get("vm_templates")
    if not isinstance(templates, list):
        return dropped
    for tpl in templates:
        if isinstance(tpl, dict) and (tpl.get("id"), tpl.get("source_instance")) in SEEDED_TEMPLATE_SOURCE_INSTANCES:
            del tpl["source_instance"]
            dropped += 1
    return dropped


def demo_telemetry_migrated(state: dict[str, Any]) -> bool:
    """True when ``state`` was written by a version that no longer seeds demo telemetry (any marker >= 1)."""
    marker = state.get(DEMO_TELEMETRY_MIGRATION_KEY)
    return (isinstance(marker, int) and not isinstance(marker, bool)
            and marker >= DEMO_TELEMETRY_MIGRATION_VERSION)


def _seeded_build_reference(record: dict[str, Any]) -> float | None:
    build_id = record.get("build_id")
    fingerprint = SEEDED_RUST_BUILDS.get(build_id)
    if fingerprint is None or any(record.get(k) != v or type(record.get(k)) is not type(v)
                                  for k, v in fingerprint.items()):
        return None
    # The original seeds always used whole-second UTC strings. A report may shift all three timestamps
    # equally (keeping their gaps), so fractional or reformatted timestamps still establish a correction.
    timestamps = [record.get(k) for k in ("requested_at", "finished_at", "delivered_at")]
    if any(not isinstance(stamp, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp) for stamp in timestamps):
        return None
    try:
        requested, finished, delivered = (_iso_to_epoch(stamp) for stamp in timestamps)
    except (KeyError, TypeError, ValueError):
        return None
    if (finished - requested, delivered - finished) != SEEDED_RUST_BUILD_GAPS[build_id]:
        return None
    return requested + SEEDED_BUILD_REQUEST_OFFSETS[build_id]


# Normalization and OOM handling may add these fields to an unchanged seed. Any other extra field is
# source evidence that the seed never supplied and must be preserved.
SEED_EVENT_DERIVED_FIELDS = frozenset({
    "failure_classification", "is_oom", "is_self_hosted_runner", "badge_text",
    "handled", "acknowledged", "handled_at", "baseline_runners", "baseline_source",
})


def _seed_fields_match(record: dict[str, Any], fingerprint: dict[str, Any]) -> bool:
    return (not (record.keys() - fingerprint.keys() - SEED_EVENT_DERIVED_FIELDS)
            and all(record.get(k) == v and type(record.get(k)) is type(v) for k, v in fingerprint.items()))


def _is_seeded_ci_run(record: dict[str, Any], reference: float | None) -> bool:
    for fingerprint in SEEDED_GITHUB_CI_RUNS:
        if fingerprint["failed_at"] != "RELATIVE":
            if _seed_fields_match(record, fingerprint):
                return True
        elif reference is not None:
            expected = dict(fingerprint, failed_at=_iso_epoch(reference - (
                1380 if fingerprint["id"] == "gh-oom-38601924810" else 540)))
            if _seed_fields_match(record, expected):
                return True
    return False


def _is_seeded_journal_oom(record: dict[str, Any], reference: float | None) -> bool:
    # Its timestamp was generated at startup. Without an unchanged build to recover that startup time,
    # the record cannot be distinguished from a later report with the same static fields; keep it.
    if reference is None:
        return False
    expected = dict(SEEDED_JOURNAL_OOM_EVENT, observed_at=_iso_epoch(reference - 420))
    return _seed_fields_match(record, expected)


def _original_seed_reference(state: dict[str, Any]) -> float | None:
    """Recover the original startup time from unchanged saved presets, independently of telemetry timings.

    At least two distinct preset IDs must agree, and any conflicting preset time makes the evidence ambiguous.
    Defaults filled in for absent saved fields must never serve as anchors (_apply_loaded_state passes loaded).
    """
    defaults = default_dashboard_state()
    epochs: dict[str, float] = {}
    for field, timestamp, offsets in (
        ("vm_templates", "created_at", {"tpl-runner-n4-std16": 12, "tpl-runner-za-n2-16": 18,
                                       "tpl-builder-c4d-16": 24}),
        ("cloud_init_templates", "updated_at", {"ci-runner-default": 2, "ci-runner-strict-pool": 6,
                                               "ci-rust-builder-199": 4}),
    ):
        expected = {row["id"]: {k: v for k, v in row.items() if k != timestamp}
                    for row in defaults[field]}
        if field == "vm_templates":
            # Seeded states hold the builder preset as it was then (see migrate_builder_preset).
            expected[LEGACY_BUILDER_PRESET_ID] = dict(LEGACY_BUILDER_PRESET)
        records = state.get(field)
        for row in records if isinstance(records, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] not in offsets:
                continue
            fingerprint = {k: v for k, v in row.items() if k != timestamp}
            # The preset shapes lost their fabricated source_instance after the seeds were removed.
            # Accept only those exact historical pairs; a reassigned source is still an edited preset.
            if field == "vm_templates" and (row["id"], row.get("source_instance")) in SEEDED_TEMPLATE_SOURCE_INSTANCES:
                fingerprint.pop("source_instance")
            if fingerprint != expected[row["id"]]:
                continue
            stamp = row.get(timestamp)
            if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp):
                continue
            try:
                epoch = _iso_to_epoch(stamp) + offsets[row["id"]] * 3600
            except ValueError:
                continue
            identity = field + "/" + row["id"]
            if identity in epochs and epochs[identity] != epoch:
                return None
            epochs[identity] = epoch
    times = set(epochs.values())
    return next(iter(times)) if len(epochs) >= 2 and len(times) == 1 else None


def drop_seeded_demo_telemetry(state: dict[str, Any], *, source_state: dict[str, Any] | None = None) -> int:
    """Remove seeded demo telemetry from ``state`` in place; returns how many records it dropped."""
    dropped = 0
    reference = _original_seed_reference(state if source_state is None else source_state)
    builds = state.get("rust_builds")
    if isinstance(builds, list):
        kept = [b for b in builds if not (
            reference is not None and isinstance(b, dict) and _seeded_build_reference(b) == reference)]
        dropped += len(builds) - len(kept)
        state["rust_builds"] = kept
    telem = state.get("scaling_telemetry")
    if not isinstance(telem, dict):
        return dropped
    for key, is_seed in (
        ("github_oom_runs", _is_seeded_ci_run),
        ("journal_oom_events", _is_seeded_journal_oom),
        ("builder_journal_oom_events", _is_seeded_journal_oom),
    ):
        records = telem.get(key)
        if isinstance(records, list):
            kept = [r for r in records if not (isinstance(r, dict) and is_seed(r, reference))]
            dropped += len(records) - len(kept)
            telem[key] = kept
    # Old partial reports could update counters while leaving all seeded event lists untouched. Neither the
    # numeric tuple nor the presence of old seeds proves the counters are demo data; preserve every saved value.
    return dropped


def _iso_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_live_instance_record(record: Any) -> bool:
    """True for an instance record written from the live Compute Engine inventory."""
    return isinstance(record, dict) and record.get(INSTANCE_SOURCE_KEY) == INSTANCE_SOURCE_GCE


def _has_unresolved_storage(record: dict[str, Any]) -> bool:
    """Storage still accruing or awaiting authoritative historical pricing."""
    return bool(record.get("disk_liability_pending") or record.get("unpaid_disk_details"))


def _disk_history_recovery_pending(record: dict[str, Any] | None) -> bool:
    """Keep exact disk coverage while a future backfill can overlap existing history."""
    if not record:
        return False
    return bool(record.get("deferred_disk_sources") or record.get("unpaid_disk_details")
                or record.get("disk_liability_pending") or _finite(record.get("disk_history_deferred_at"))
                or any(isinstance(d, dict) and _finite(d.get("billing_deferred_since"))
                       for d in record.get("disks_detail") or []))


def _select_instance_record(instances: list[Any], instance_id: str, instance_name: str) -> dict[str, Any] | None:
    """IDs are exact; names alone must identify one current or retained historical record."""
    matches = [i for i in instances if isinstance(i, dict)
               and (str(i.get("instance_id")) == instance_id if instance_id
                    else bool(instance_name) and i.get("name") == instance_name)]
    if not instance_id and len(matches) > 1:
        raise ValueError(f"Instance name '{instance_name}' is ambiguous; supply instance_id")
    return matches[0] if matches else None


def _instance_end_epoch(record: dict[str, Any]) -> float | None:
    """When compute billing for a record ended: its stop time, else its deletion time (None while running)."""
    for key in ("stopped_epoch", "deleted_epoch"):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    return None


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pricing_rates(pricing: dict[str, Any]) -> tuple[float, float, float]:
    """(compute all-in, compute only, disk all-in) hourly USD of a pricing record (tax applied to "all-in")."""
    compute_hourly = float(pricing.get("compute_hourly_usd", 0.0) or 0.0)
    total_hourly = float(pricing.get("total_hourly_usd", 0.0) or 0.0)
    tax_fraction = float(pricing.get("tax_rate_percent", 0.0) or 0.0) / 100.0
    disk_all_in = float(pricing.get("disk_hourly_usd", 0.0) or 0.0) * (1.0 + tax_fraction)
    return max(0.0, total_hourly - disk_all_in), compute_hourly, disk_all_in


def _prior_runs(record: dict[str, Any] | None) -> list[tuple[float, float, float | None, float | None]]:
    """Closed running intervals ``(start, end, compute_all_in, compute_only)`` of earlier runs, each with the
    rates that applied to it (None for an interval recorded without rates: the current rates apply)."""
    runs: list[tuple[float, float, float | None, float | None]] = []
    for item in (record or {}).get("prior_runs") or []:
        if not isinstance(item, (list, tuple)) or len(item) not in (2, 4) or not all(_finite(v) for v in item):
            continue
        if item[1] < item[0]:
            continue
        rates = (float(item[2]), float(item[3])) if len(item) == 4 else (None, None)
        runs.append((float(item[0]), float(item[1]), *rates))
    return runs


def _disk_segments(record: dict[str, Any] | None) -> list[tuple[float, float, float]]:
    """Closed disk intervals ``(start, end, disk_all_in)`` billed at an earlier disk shape's rate."""
    segments: list[tuple[float, float, float]] = []
    for item in (record or {}).get("disk_segments") or []:
        if isinstance(item, (list, tuple)) and len(item) == 3 and all(_finite(v) for v in item) and item[1] >= item[0]:
            segments.append((float(item[0]), float(item[1]), float(item[2])))
    return segments


def _overlap_hours(start: float, end: float, window_start: float) -> float:
    return max(0.0, end - max(start, window_start)) / 3600.0


# Size bounds that keep the state (and its KV snapshot, at most 25 MiB) small however busy the fleet is. Past them
# the oldest detail is folded into per-hour cost buckets, which still answer every window (to within an hour).
MAX_DETAILED_INSTANCE_RECORDS = 2000
MAX_DETAILED_SEGMENTS = 200
# Serialized budget for the instance records and compacted costs together, well below the 25 MiB KV value limit
# (the rest of the state shares that value). Past it, records are compacted until they fit.
MAX_INSTANCE_STATE_BYTES = 6 * 1024 * 1024
# Segments a live VM keeps in detail when the size budget forces compaction of live records.
MIN_DETAILED_SEGMENTS = 10
COMPACTED_COSTS_STATE_KEY = "compacted_instance_costs"
# Hour buckets: windows are answered to within an hour at their start.
_BUCKET_SECONDS = 3600.0


def _add_to_buckets(buckets: dict[str, list[float]], prefix: str, start: float, end: float,
                        all_in_rate: float, compute_rate: float, *, compute: bool,
                        span: float = _BUCKET_SECONDS) -> None:
    """Spread cost and duration into [compute_hours, all_in, compute, disk_hours] buckets."""
    cursor = start
    while cursor < end:
        hour = math.floor(cursor / span) * span
        chunk_end = min(end, hour + span)
        hours = (chunk_end - cursor) / 3600.0
        bucket = buckets.setdefault(f"{prefix}{int(hour)}", [0.0, 0.0, 0.0, 0.0])
        if len(bucket) == 3:
            bucket.append(0.0)  # Legacy history has no recorded disk duration.
        if compute:
            bucket[0] += hours
            bucket[2] += hours * compute_rate
        else:
            bucket[3] += hours
        bucket[1] += hours * all_in_rate
        cursor = chunk_end


def _bucket_window_totals(buckets: Any, window_start: float, now: float, prefix: str = "",
                          span: float = _BUCKET_SECONDS) -> tuple[float, float, float, float]:
    """Compute hours, all-in cost, compute cost, and recorded disk hours, pro rata by bucket.

    Three-slot legacy buckets retain costs but contain no recoverable disk duration.
    """
    hours = all_in = compute = disk_hours = 0.0
    for key, value in (buckets or {}).items() if isinstance(buckets, dict) else []:
        if not str(key).startswith(prefix) or not isinstance(value, list) or len(value) not in (3, 4):
            continue
        try:
            hour = float(str(key)[len(prefix):])
        except ValueError:
            continue
        span_end = min(hour + span, now)
        if span_end <= hour:
            continue
        fraction = max(0.0, span_end - max(hour, window_start)) / (span_end - hour)
        hours += float(value[0]) * fraction
        all_in += float(value[1]) * fraction
        compute += float(value[2]) * fraction
        if len(value) == 4:
            disk_hours += float(value[3]) * fraction
    return hours, all_in, compute, disk_hours


def _bucket_window_costs(buckets: Any, window_start: float, now: float, prefix: str = "",
                         span: float = _BUCKET_SECONDS) -> tuple[float, float, float]:
    """Preserve the three-value cost-reader API for legacy callers."""
    return _bucket_window_totals(buckets, window_start, now, prefix, span)[:3]


COARSE_BUCKET_SECONDS = 86_400.0


def _record_bucket_span(record: dict[str, Any] | None) -> float:
    span = (record or {}).get("folded_bucket_seconds")
    return float(span) if _finite(span) and span > 0 else _BUCKET_SECONDS


def _rebucket(buckets: dict[str, list[float]], prefix: str, from_span: float, to_span: float,
              until: float | None = None) -> dict[str, list[float]]:
    """Buckets re-keyed to ``to_span``: merged when coarser, spread evenly when finer (over the part of each
    bucket before ``until``, so a day that was still in progress keeps its cost in its elapsed hours)."""
    out: dict[str, list[float]] = {}
    for key, value in buckets.items():
        if not str(key).startswith(prefix):
            continue
        try:
            start = float(str(key)[len(prefix):])
        except ValueError:
            continue
        if to_span >= from_span:
            target = out.setdefault(f"{prefix}{int(math.floor(start / to_span) * to_span)}", [0.0, 0.0, 0.0, 0.0])
            for i in range(min(4, len(value))):
                target[i] += float(value[i])
        else:
            span_end = start + from_span if until is None else min(start + from_span, until)
            parts = max(1, int(math.ceil(max(0.0, span_end - start) / to_span)))
            for n in range(parts):
                target = out.setdefault(f"{prefix}{int(start + n * to_span)}", [0.0, 0.0, 0.0, 0.0])
                for i in range(min(4, len(value))):
                    target[i] += float(value[i]) / parts
    return out


def _prune_buckets(buckets: dict[str, list[float]], cutoff: float, span: float = _BUCKET_SECONDS) -> None:
    for key in list(buckets):
        try:
            hour = float(key.rsplit("|", 1)[-1])
        except ValueError:
            continue
        if hour + span < cutoff:
            del buckets[key]


def _current_run_end(record: dict[str, Any], now: float) -> float:
    if record.get("compute_start_known") is False:
        return float(record.get("started_epoch") or now)
    end = _instance_end_epoch(record)
    return now if record.get("status") == "RUNNING" or end is None else end


def instance_usage(record: dict[str, Any], now: float, window_start: float | None = None) -> dict[str, float]:
    """Compute (VM running) and disk (VM exists) hours and costs of a record, in total and inside the window.

    Compute accrues over every running interval: earlier runs at the rates recorded with them, then the current
    run (to its stop or deletion) at the current rates. Attached disks keep billing while the VM is stopped, from
    creation until deletion (or now): earlier disk shapes at their recorded rates, then the current one. Costs
    are unrounded; callers round for display.
    """
    pricing = record.get("pricing") if isinstance(record.get("pricing"), dict) else {}
    cur_compute_all_in, cur_compute_only, cur_disk_all_in = _pricing_rates(pricing)
    window = window_start if window_start is not None else float("-inf")
    started = float(record.get("started_epoch") or now)
    segment_start = record.get("compute_segment_start")
    open_start = max(started, float(segment_start)) if _finite(segment_start) else started
    runs = [(s, e, ca if ca is not None else cur_compute_all_in, co if co is not None else cur_compute_only)
            for s, e, ca, co in _prior_runs(record)]
    run_end = _current_run_end(record, now)
    if run_end > open_start:
        runs.append((open_start, run_end, cur_compute_all_in, cur_compute_only))
    deleted = record.get("deleted_epoch")
    if record.get("disk_liability_pending"):
        disk_end = now  # Its persistent disk may still exist: keep billing until a disk listing resolves it.
    elif _finite(record.get("disk_end_epoch")):
        disk_end = float(record["disk_end_epoch"])
    else:
        disk_end = float(deleted) if _finite(deleted) else now
    first_start = min([s for s, _e, _a, _c in runs] + [started])
    created = record.get("disk_start_epoch")
    disk_open_start = float(created) if _finite(created) else first_start
    disks = _disk_segments(record)
    if disks:
        disk_open_start = max(disk_open_start, max(e for _s, e, _r in disks))
    disk_seg_start = record.get("disk_segment_start")
    if _finite(disk_seg_start):
        disk_open_start = max(disk_open_start, float(disk_seg_start))
    if disk_end > disk_open_start:
        disks = disks + [(disk_open_start, disk_end, cur_disk_all_in)]
    pruned_disk = record.get("pruned_disk_hours")
    pruned = record.get("pruned_compute_hours")
    pruned_cost = record.get("pruned_cost_usd")
    pruned_all_in, pruned_compute = (
        (float(pruned_cost[0]), float(pruned_cost[1]))
        if isinstance(pruned_cost, (list, tuple)) and len(pruned_cost) == 2 and all(_finite(v) for v in pruned_cost)
        else (0.0, 0.0)
    )
    folded = record.get("folded_cost_buckets") if isinstance(record.get("folded_cost_buckets"), dict) else {}
    span = _record_bucket_span(record)
    folded_total = _bucket_window_totals(folded, float("-inf"), now, span=span)
    folded_window = _bucket_window_totals(folded, window, now, span=span)
    return {
        "compute_hours": sum(e - s for s, e, _a, _c in runs) / 3600.0 + (float(pruned) if _finite(pruned) else 0.0)
        + folded_total[0],
        "compute_hours_in_window": sum(_overlap_hours(s, e, window) for s, e, _a, _c in runs) + folded_window[0],
        "disk_hours": sum(e - s for s, e, _r in disks) / 3600.0 + folded_total[3]
        + (float(pruned_disk) if _finite(pruned_disk) else 0.0),
        "disk_hours_in_window": sum(_overlap_hours(s, e, window) for s, e, _r in disks) + folded_window[3],
        "cost_all_in": pruned_all_in + folded_total[1] + sum((e - s) / 3600.0 * a for s, e, a, _c in runs)
        + sum((e - s) / 3600.0 * r for s, e, r in disks),
        "cost_compute": pruned_compute + folded_total[2] + sum((e - s) / 3600.0 * c for s, e, _a, c in runs),
        "window_all_in": folded_window[1] + sum(_overlap_hours(s, e, window) * a for s, e, a, _c in runs)
        + sum(_overlap_hours(s, e, window) * r for s, e, r in disks),
        "window_compute": folded_window[2] + sum(_overlap_hours(s, e, window) * c for s, e, _a, c in runs),
    }


def instance_cost_usd(pricing: dict[str, Any], compute_hours: float, disk_hours: float) -> tuple[float, float]:
    """(all-in cost with disks and tax, compute-only cost) for the given compute and disk hours at one rate."""
    compute_all_in, compute_only, disk_all_in = _pricing_rates(pricing)
    return (round(compute_hours * compute_all_in + disk_hours * disk_all_in, 2),
            round(compute_hours * compute_only, 2))


def _with_extra_disks(pricing: dict[str, Any], extra_disk_hourly: float) -> dict[str, Any]:
    """``pricing`` with adjusted attached-disk cost, totals and tax recomputed."""
    if extra_disk_hourly == 0.0:
        return pricing
    out = dict(pricing)
    tax_fraction = float(out.get("tax_rate_percent", 0.0) or 0.0) / 100.0
    disk_hourly = max(0.0, float(out.get("disk_hourly_usd", 0.0) or 0.0) + extra_disk_hourly)
    compute_hourly = float(out.get("compute_hourly_usd", 0.0) or 0.0)
    tax_hourly = (compute_hourly + disk_hourly) * tax_fraction
    total_hourly = compute_hourly + disk_hourly + tax_hourly
    hours_per_month = 24.0 * 365.0 / 12.0
    out.update({
        "disk_hourly_usd": round(disk_hourly, 4),
        "disk_daily_usd": round(disk_hourly * 24.0, 2),
        "disk_monthly_usd": round(disk_hourly * hours_per_month, 2),
        "tax_hourly_usd": round(tax_hourly, 4),
        "tax_daily_usd": round(tax_hourly * 24.0, 2),
        "tax_monthly_usd": round(tax_hourly * hours_per_month, 2),
        "total_hourly_usd": round(total_hourly, 4),
        "total_daily_usd": round(total_hourly * 24.0, 2),
        "total_monthly_usd": round(total_hourly * hours_per_month, 2),
    })
    return out


def _primary_data_disk_gb(obs: dict[str, Any]) -> int:
    """GiB of the observation's data disks of its primary data-disk type (other types are priced separately)."""
    by_type = obs.get("data_disks")
    if isinstance(by_type, dict) and (by_type or "data_disks_attached_gb" in obs):
        # The billed breakdown (a disk shared with another fleet VM is billed there, not here).
        return int(by_type.get(str(obs.get("data_disk_type") or ""), 0) or 0)
    return int(obs.get("data_disk_gb") or 0)


def _secondary_data_disks(obs: dict[str, Any]) -> list[tuple[str, int]]:
    by_type = obs.get("data_disks")
    if not isinstance(by_type, dict):
        return []
    primary = str(obs.get("data_disk_type") or "")
    return [(str(t), int(gb)) for t, gb in sorted(by_type.items()) if t != primary and int(gb or 0) > 0]


def _with_secondary_disks(
    pricing: dict[str, Any],
    disks: dict[str, Any],
    *,
    region: str,
    family: str,
    project: str,
    live_pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``pricing`` (which covers the boot disk and the primary data-disk type) plus every other data-disk type,
    each at its own rate. ``disks`` is an observation or record with ``data_disks`` / ``data_disk_type``."""
    extra = 0.0
    unknown: set[str] = set()
    for disk_type, disk_gb in _secondary_data_disks(disks):
        priced = calculate_spot_pricing(
            family=family, vcpu=0, ram_gib=0.0, region=region, boot_disk_type=disk_type,
            boot_disk_gb=0, data_disk_type=disk_type, data_disk_gb=disk_gb,
            tax_rate_percent=float(pricing.get("tax_rate_percent", 0.0) or 0.0),
            machine_type=None, live_pricing=live_pricing, project=project,
        )
        extra += float(priced.get("disk_hourly_usd", 0.0) or 0.0)
        if priced.get("complete") is False:
            unknown.update(priced.get("disk_rate_unknown") or [disk_type])
    tax = float(pricing.get("tax_rate_percent") or 0.0)
    details = [d for d in disks.get("disks_detail") or [] if isinstance(d, dict)]
    if any(_finite(d.get("billing_deferred_since")) for d in details):
        # Unknown added disks have no trusted historical rate yet. Bill the known attachments now and fill the
        # deferred disk's own interval once its type and creation timestamp become authoritative.
        known_rate = sum(_single_disk_all_in(d, family=family, region=region, project=project,
                                            tax_rate_percent=tax, live_pricing=live_pricing) / (1.0 + tax / 100.0)
                         for d in details if d.get("billing_owner") is not False
                         and not _finite(d.get("billing_deferred_since")))
        extra = known_rate - float(pricing.get("disk_hourly_usd") or 0.0)
    out = _with_extra_disks(pricing, extra)
    if unknown:
        # A secondary disk type without a trusted rate leaves the whole price incomplete.
        out = {**out, "complete": False,
               "disk_rate_unknown": sorted(set(out.get("disk_rate_unknown") or []) | unknown)}
    return out


def _stopped_disk_hourly(pricing: dict[str, Any], *, include_tax: bool = True) -> float:
    """Hourly USD a stopped VM still bills for its attached disks."""
    disk = float(pricing.get("disk_hourly_usd", 0.0) or 0.0)
    return disk * (1.0 + float(pricing.get("tax_rate_percent", 0.0) or 0.0) / 100.0) if include_tax else disk


def _taxable_history_ranges(record: dict[str, Any], now: float) -> tuple[list[list[float]] | None, list[list[float]]]:
    """Actual charged intervals used to decode the old component-agnostic tax reason."""
    pricing = record.get("pricing") or {}
    compute: list[list[float]] | None = []
    if record.get("kind") != "disk":
        for start, end, all_in, principal in _prior_runs(record):
            if principal is None or principal > 0 or (all_in is not None and all_in > 0):
                compute.append([start, end])
        if float(pricing.get("compute_hourly_usd") or 0) > 0:
            started = record.get("started_epoch")
            if _finite(started):
                start = max(float(started), float(record["compute_segment_start"])
                            if _finite(record.get("compute_segment_start")) else float(started))
                end = min(now, _current_run_end(record, now))
                if end > start:
                    compute.append([start, end])
            else:
                compute = None  # Missing timestamps cannot prove there was no taxable compute.
    storage = [[start, end] for start, end, rate in _disk_segments(record) if rate > 0]
    for key, value in (record.get("folded_cost_buckets") or {}).items():
        if not isinstance(value, (list, tuple)) or len(value) not in (3, 4) or not all(_finite(v) for v in value):
            continue
        try:
            start = float(key)
        except (TypeError, ValueError):
            continue
        end = min(now, start + _record_bucket_span(record))
        if not _finite(start) or end <= start:
            continue
        if record.get("kind") != "disk" and float(value[2]) > 0 and compute is not None:
            compute.append([start, end])
        if float(value[1]) > float(value[2]):
            storage.append([start, end])
    disk_end = record.get("disk_end_epoch")
    if not _finite(disk_end):
        disk_end = record.get("deleted_epoch") if record.get("status") == "DELETED"             and not record.get("disk_liability_pending") else now
    end = min(now, float(disk_end)) if _finite(disk_end) else now
    if float(pricing.get("disk_hourly_usd") or 0) > 0:
        start = next((float(record[key]) for key in ("disk_segment_start", "disk_start_epoch", "started_epoch")
                      if _finite(record.get(key))), None)
        if start is not None and end > start:
            storage.append([start, end])
    for disk in list(record.get("disks_detail") or []) + list(record.get("unpaid_disk_details") or []):
        if not isinstance(disk, dict) or disk.get("billing_owner") is False or int(disk.get("gb") or 0) <= 0:
            continue
        if _finite(disk.get("billing_deferred_since")) or _finite(record.get("disk_history_deferred_at")):
            start = next((float(value) for value in (disk.get("created_epoch"), disk.get("billing_deferred_since"),
                record.get("disk_history_deferred_at"), record.get("disk_start_epoch"), record.get("started_epoch"))
                if _finite(value)), None)
            stop = min(end, float(disk["billing_missing_since"])) if _finite(disk.get("billing_missing_since")) else end
            if start is not None and stop > start:
                storage.append([start, stop])
    return compute, storage


def _component_tax_history(record: dict[str, Any], raw: Any = None, *, now: float | None = None) -> Any:
    raw = record.get("unpriced_history_intervals") if raw is None else raw
    if not isinstance(raw, dict) or "fallback_tax" not in raw:
        return raw
    compute, storage = _taxable_history_ranges(record, time.time() if now is None else now)
    return split_legacy_tax_intervals(raw, compute_intervals=compute, storage_intervals=storage)


def _unpriced_history_in_window(record: dict[str, Any], start: float, end: float,
                                *, include_storage: bool = True, include_tax: bool | None = None) -> bool:
    """Whether known missing historical charges overlap the selected cost components."""
    return history_in_window(_component_tax_history(record, now=end), start, end, include_storage,
                             include_tax=include_tax)


def _save_unpriced_intervals(record: dict[str, Any], intervals: dict[str, Any], now: float) -> None:
    intervals = _component_tax_history(record, intervals, now=now)
    compute_tax, storage_tax = tax_history_components(intervals)
    recent, compute, storage = compact_unpriced_intervals(intervals, now, DELETED_INSTANCE_RETENTION_SECONDS)
    record["unpriced_history_intervals"] = recent
    if compute_tax:
        record["unpriced_compute_tax_lifetime_history"] = True
    if storage_tax:
        record["unpriced_storage_tax_lifetime_history"] = True
    if compute:
        record["unpriced_compute_lifetime_history"] = True
    if storage:
        record["unpriced_storage_lifetime_history"] = True
    if compute or storage or compute_tax or storage_tax:
        record["unpriced_lifetime_history"] = True


def _lifetime_tax_components(record: dict[str, Any]) -> tuple[bool, bool]:
    compute, storage = tax_history_components(_component_tax_history(record))
    return (compute or bool(record.get("unpriced_compute_tax_lifetime_history")),
            storage or bool(record.get("unpriced_storage_tax_lifetime_history")))


def _lifetime_history_components(record: dict[str, Any], *, include_tax: bool = False) -> tuple[bool, bool]:
    compute, storage = history_components(record.get("unpriced_history_intervals"))
    compute = compute or bool(record.get("unpriced_compute_lifetime_history"))
    storage = storage or bool(record.get("unpriced_storage_lifetime_history"))
    compute_tax, storage_tax = _lifetime_tax_components(record)
    # Old states also used this generic flag for expired unpaid disk debt without interval bounds.
    if record.get("unpriced_lifetime_history") and not (compute or storage or compute_tax or storage_tax):
        storage = True
    return (compute or (include_tax and compute_tax), storage or (include_tax and storage_tax))


def _retain_unpriced_history(record: dict[str, Any], obs: dict[str, Any], previous: dict[str, Any] | None,
                             now: float) -> None:
    """Keep bounded uncertainty per cause; no fabricated price is added for unobserved runs or memory storage."""
    intervals = normalize_unpriced_intervals((previous or {}).get("unpriced_history_intervals"))
    for reason, ranges in normalize_unpriced_intervals(record.get("unpriced_history_intervals")).items():
        for start, end in ranges:
            add_unpriced_interval(intervals, reason, start, end)
    def add(reason: str, start: float, end: float | None) -> None:
        add_unpriced_interval(intervals, reason, start, end)

    resume_boundary = (previous or {}).get("suspension_resume_boundary")
    if (isinstance(resume_boundary, (list, tuple)) and len(resume_boundary) == 2
            and all(_finite(value) for value in resume_boundary) and resume_boundary[1] > resume_boundary[0]):
        record["suspension_resume_boundary"] = list(resume_boundary)
    else:
        resume_boundary = None
    terminal_boundary = (previous or {}).get("suspension_terminal_boundary")
    if (isinstance(terminal_boundary, (list, tuple)) and len(terminal_boundary) == 2
            and all(_finite(value) for value in terminal_boundary) and terminal_boundary[1] >= terminal_boundary[0]):
        record["suspension_terminal_boundary"] = list(terminal_boundary)
    else:
        terminal_boundary = None
    status = record.get("status")
    started = float(record["started_epoch"])
    if (previous or {}).get("status") == "SUSPENDED" and "suspended_memory" not in intervals:
        previous_stop = previous.get("stopped_epoch")
        add("suspended_memory", float(previous_stop) if _finite(previous_stop)
            else float(previous.get("started_epoch") or now), None)
    if status != "SUSPENDED":
        for ranges in intervals.values():
            for bounds in ranges:
                if bounds[1] is None:
                    bounds[1] = max(float(bounds[0]), min(now, started if started >= float(bounds[0]) else now))
    suspended = obs.get("last_suspended_epoch")
    if not _finite(suspended):
        suspended = (previous or {}).get("pending_suspension_epoch")
    if not _finite(suspended) and (previous or {}).get("status") == "SUSPENDED":
        suspended = previous.get("stopped_epoch")
        if not _finite(suspended):
            prior_memory = normalize_unpriced_intervals(previous.get("unpriced_history_intervals"))
            open_starts = [start for start, end in prior_memory.get("suspended_memory", []) if end is None]
            suspended = max(open_starts) if open_starts else None
    terminal_end = None
    if _finite(suspended) and status in ("TERMINATED", "STOPPED") and float(suspended) >= started:
        stopped = obs.get("last_stop_epoch")
        if _finite(stopped) and float(suspended) <= float(stopped) <= now:
            terminal_end = float(stopped)
        elif terminal_boundary is not None and terminal_boundary[0] == float(suspended):
            terminal_end = float(terminal_boundary[1])
        else:
            terminal_end = now  # The first terminal observation bounds saved-memory billing.
        terminal_boundary = [float(suspended), terminal_end]
        record["suspension_terminal_boundary"] = terminal_boundary
    elif (status != "SUSPENDED" and _finite(suspended) and terminal_boundary is not None
            and terminal_boundary[0] == float(suspended)):
        terminal_end = float(terminal_boundary[1])
    if terminal_end is not None:
        # A saved terminal boundary also survives a later staging/start and interval retention.
        intervals["suspended_memory"] = [[begin, min(end if end is not None else terminal_end, terminal_end)]
            for begin, end in intervals.get("suspended_memory", []) if begin <= terminal_end]
        add("suspended_memory", float(suspended), terminal_end)
        record.pop("pending_suspension_epoch", None)
    elif status == "SUSPENDED":
        at = float(suspended) if _finite(suspended) else float(record.get("stopped_epoch") or now)
        add("suspended_memory", min(at, now), None)
        record["pending_suspension_epoch"] = min(at, now)
    elif (_finite(suspended) and float(suspended) >= started
            and status not in ("TERMINATED", "STOPPED", "DELETED", "DETACHED")):
        # The actual resume start has not arrived, including transitional snapshots.
        record["pending_suspension_epoch"] = float(suspended)
        add("suspended_memory", float(suspended), None)
    elif _finite(suspended) and float(suspended) < started:
        # GCE retains the latest suspension timestamp across unrelated stop/start cycles.
        # Keep its confirmed resume independently of uncertainty intervals, which age out.
        previous_start = (previous or {}).get("started_epoch")
        if resume_boundary is not None and resume_boundary[0] == float(suspended):
            resumed = float(resume_boundary[1])
        elif _finite(previous_start) and float(previous_start) > float(suspended):
            # A legacy interval may have closed provisionally in STAGING. Only its
            # reported post-suspension start proves that a resume has happened.
            resumed = float(previous_start)
        else:
            # A transitional observation with the old start time cannot confirm a resume.
            resumed = started
        add("suspended_memory", float(suspended), resumed)
        record["suspension_resume_boundary"] = [float(suspended), resumed]
    stopped = obs.get("last_stop_epoch")
    earlier_ends = [float(t) for t in (stopped, suspended) if _finite(t) and float(t) < started]
    if previous is None and obs.get("kind") != "disk" and earlier_ends:
        created = obs.get("created_epoch")
        ended = max(earlier_ends)
        add("prior_compute", float(created) if _finite(created) and float(created) < ended else 0.0, ended)
    previous_stop = (previous or {}).get("stopped_epoch")
    if _finite(previous_stop):
        # A terminal timestamp after suspension ended saved memory, not a second compute run.
        # Only a still later end can prove unseen compute after that known storage-only interval.
        inference_start = float(previous_stop)
        if terminal_boundary is not None and terminal_boundary[0] == inference_start:
            inference_start = max(inference_start, float(terminal_boundary[1]))
        unseen_ends = [float(t) for t in (obs.get("last_stop_epoch"), obs.get("last_suspended_epoch"))
                       if _finite(t) and inference_start < float(t) < started]
        if unseen_ends:
            add("prior_compute", inference_start, max(unseen_ends))
    _save_unpriced_intervals(record, intervals, now)


def _pricing_component_reasons(record: dict[str, Any], *, historical: bool = False) -> tuple[list[str], list[str]]:
    """Separate current billable uncertainty from historical rate uncertainty when closing intervals."""
    compute, storage = [], []
    pricing = record.get("pricing") or {}
    disk_only = record.get("kind") == "disk" or record.get("retained_only_pricing")
    custom = "custom" in str(record.get("machine_type", "")) or str(record.get("machine_type", "")).endswith("-ext")
    if not disk_only:
        if record.get("status") == "RUNNING" and record.get("compute_start_known") is False:
            compute.append("the first compute start timestamp is not reported yet")
        if pricing.get("pricing_source") == "spot_baseline_for_on_demand_vm":
            compute.append("no on-demand rate for this standard (non-Spot) VM")
        if pricing.get("family_rate_unknown"):
            compute.append(f"no known rate for the {record.get('family') or 'unknown'} machine family")
        if custom and pricing.get("complete") is False and pricing.get("pricing_source") != "gcp_capacity_history":
            compute.append("custom or extended machine shape, which predefined rates do not price")
    if record.get("deferred_disk_sources") or record.get("unpaid_disk_details") or _finite(record.get("disk_history_deferred_at")):
        storage.append("new disk metadata unavailable")
    if record.get("status") == "SUSPENDED":
        storage.append("suspended: preserved memory storage is not priced here")
    regional_unknown = bool(record.get("regional_disks"))
    if record.get("retained_only_pricing"):
        retained = [d for d in record.get("disks_detail") or [] if isinstance(d, dict)
                    and d.get("auto_delete") is not True
                    and (d.get("billing_owner") is not False or _disk_ambiguous_roles(record, d))]
        if retained and all("regional" in d for d in retained):
            regional_unknown = any(d.get("regional") for d in retained)
    if regional_unknown:
        storage.append("regional replicated disk storage is not priced here")
    if record.get("disk_types_known") is False:
        storage.append("disk types unknown (no compute.disks.list)")
    if pricing.get("disk_rate_unknown"):
        storage.append("no known rate for disk type(s) " + ", ".join(pricing["disk_rate_unknown"]))
    performance_disk = any(gb > 0 and kind in ("pd-extreme", "hyperdisk-throughput") for gb, kind in (
        (int(record.get("boot_disk_billed_gb", record.get("boot_disk_gb")) or 0), record.get("boot_disk_type")),
        (_primary_data_disk_gb(record), record.get("data_disk_type"))))
    performance_unknown = bool(record.get("unpriced_disk_performance"))
    details = [d for d in list(record.get("disks_detail") or []) + list(record.get("unpaid_disk_details") or [])
               if isinstance(d, dict)]
    if record.get("retained_only_pricing") and details:
        performance_disk = any(int(d.get("gb") or 0) > 0 and d.get("type") in ("pd-extreme", "hyperdisk-throughput")
                               for d in details if d.get("auto_delete") is not True
                               and (d.get("billing_owner") is not False or _disk_ambiguous_roles(record, d)))
    if any("unpriced_disk_performance" in d or str(d.get("type") or "").startswith("hyperdisk-")
           or d.get("type") == "pd-extreme" for d in details):
        performance_unknown = any(
            (d.get("billing_owner") is not False or _disk_ambiguous_roles(record, d))
            and (d.get("unpriced_disk_performance") or d.get("type") in ("pd-extreme", "hyperdisk-throughput")
                 or (record.get("unpriced_disk_performance") and str(d.get("type") or "").startswith("hyperdisk-")))
            for d in details if not (record.get("retained_only_pricing") and d.get("auto_delete") is True))
    unbilled_performance = record.get("unpriced_disk_performance") and not performance_unknown and not performance_disk
    if performance_unknown or performance_disk:
        storage.append("provisioned IOPS or throughput is not priced here")
    if record.get("local_ssd_gb"):
        storage.append("Local SSD storage is not priced here")
    if record.get("storage_billing_ambiguous"):
        storage.append("shared disk billing role is ambiguous")
    if pricing.get("regional_compute_rate_unknown") and not disk_only:
        compute.append("region outside the dashboard's catalog: no trusted regional compute rate")
    if pricing.get("regional_disk_rate_unknown"):
        storage.append("region outside the dashboard's catalog: no trusted regional disk rate")
    if pricing.get("complete") is False and not compute and not storage and not unbilled_performance:
        (storage if disk_only else compute).append("pricing source is incomplete")
    if not historical and record.get("status") in GCE_STOPPED_STATUSES | {"DELETED"}:
        compute = []
    return compute, storage


def _tax_unknown(record: dict[str, Any]) -> bool:
    return record.get("tax_rate_known") is False and not record.get("tax_rate_overridden")


def _selected_tax_unknown(record: dict[str, Any], *, include_storage: bool) -> bool:
    if not _tax_unknown(record):
        return False
    pricing = record.get("pricing") or {}
    live_compute = (record.get("kind") != "disk" and not record.get("retained_only_pricing")
                    and record.get("status") not in GCE_STOPPED_STATUSES | {"DELETED"}
                    and float(pricing.get("compute_hourly_usd") or 0) > 0)
    live_storage = (include_storage and (record.get("status") != "DELETED" or record.get("disk_liability_pending"))
                    and (float(pricing.get("disk_hourly_usd") or 0) > 0
                         or (record.get("status") == "SUSPENDED" and float(record.get("ram_gib") or 0) > 0)))
    return bool(live_compute or live_storage)


def _selected_pricing_complete(record: dict[str, Any], *, include_storage: bool, include_tax: bool = False) -> bool:
    compute, storage = _pricing_component_reasons(record)
    return not compute and not (include_storage and storage) and not (
        include_tax and _selected_tax_unknown(record, include_storage=include_storage))


def _mark_pricing_completeness(record: dict[str, Any]) -> None:
    """Keep the all-in summary while component-specific consumers select their own costs."""
    compute, storage = _pricing_component_reasons(record)
    tax = ["regional tax rate is unknown"] if _selected_tax_unknown(record, include_storage=True) else []
    record["pricing_complete"] = not (compute or storage or tax)
    record["pricing_incomplete_reason"] = "; ".join(compute + storage + tax)
    record["lifetime_cost_complete"] = not (compute or storage or tax or record.get("unpriced_lifetime_history"))


def _nonrunning_hourly(inst: dict[str, Any], *, include_disk: bool, include_tax: bool) -> float:
    """Hourly USD a live inventory VM that is neither RUNNING nor DELETED bills.

    A stopped VM (TERMINATED, STOPPED, SUSPENDED) bills only its disks. A VM in a transitional state
    (PROVISIONING, STAGING, STOPPING, REPAIRING, ...) is about to run or still is, so it counts at its full rate.
    """
    pricing = inst.get("pricing") or {}
    if inst.get("status") in GCE_STOPPED_STATUSES or inst.get("status") == "DELETED":
        # A deleted record is counted here only while it still bills an unresolved disk.
        return _stopped_disk_hourly(pricing, include_tax=include_tax) if include_disk else 0.0
    return DashboardStore._instance_hourly_for_budget(pricing, include_disk=include_disk, include_tax=include_tax)


def _is_transitional(inst: Any) -> bool:
    return (is_live_instance_record(inst) and inst.get("status") not in GCE_STOPPED_STATUSES
            and inst.get("status") not in ("RUNNING", "DELETED"))


def _disk_key(disk: dict[str, Any]) -> str:
    """A disk's identity across records: its immutable numeric ID (a reused name is a different disk), else its
    source URL."""
    disk_id = str(disk.get("id") or "").strip()
    return f"id:{disk_id}" if disk_id else str(disk.get("source") or "")



def _same_disk(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("id") and right.get("id"):
        return str(left["id"]) == str(right["id"])
    if not left.get("source") or left.get("source") != right.get("source"):
        return False
    for unpaid, resource in ((left, right), (right, left)):
        if _finite(unpaid.get("billing_missing_since")):
            same_interval = (_finite(resource.get("billing_deferred_since"))
                             and resource["billing_deferred_since"] == unpaid.get("billing_deferred_since"))
            old_creation = (_finite(resource.get("created_epoch"))
                            and float(resource["created_epoch"]) <= float(unpaid.get("billing_deferred_since", 0)))
            if not same_interval and not old_creation:
                return False
        if (_finite(unpaid.get("billing_deferred_since")) and _finite(resource.get("created_epoch"))
                and float(resource["created_epoch"]) > float(unpaid["billing_deferred_since"])):
            return False  # It was created after the unresolved attachment was observed: a reused name.
    return True


def _disk_ambiguous_roles(record: dict[str, Any], disk: dict[str, Any]) -> list[str]:
    """Disk-local evidence wins, including an explicit empty list. Migrate older record-level evidence only
    onto known shared attachments where possible; a legacy record with no disk attribution stays conservative."""
    if "storage_billing_ambiguous_roles" in disk:
        return sorted(set(disk.get("storage_billing_ambiguous_roles") or []))
    if not record.get("storage_billing_ambiguous"):
        return []
    shared = [d for d in record.get("shared_attachments") or [] if isinstance(d, dict)]
    if shared and not any(_same_disk(disk, other) for other in shared):
        return []
    return sorted(set(record.get("storage_billing_ambiguous_roles") or ["cicd_runner", "rust_builder"]))


def _sync_disk_ambiguity(record: dict[str, Any]) -> None:
    """The VM/debt summary is derived from physical disks, never copied back onto unrelated attachments."""
    evidence: set[str] = set()
    for disk in list(record.get("disks_detail") or []) + list(record.get("unpaid_disk_details") or []):
        if not isinstance(disk, dict):
            continue
        roles = _disk_ambiguous_roles(record, disk)
        if roles or "storage_billing_ambiguous_roles" in disk or record.get("storage_billing_ambiguous"):
            disk["storage_billing_ambiguous_roles"] = roles
        if record.get("retained_only_pricing") and disk.get("auto_delete") is True:
            continue
        evidence.update(roles)
    if len(evidence) > 1:
        record["storage_billing_ambiguous"] = True
        record["storage_billing_ambiguous_roles"] = sorted(evidence)
    else:
        record.pop("storage_billing_ambiguous", None)
        record.pop("storage_billing_ambiguous_roles", None)


def _disk_known(disk: dict[str, Any], known: frozenset[str]) -> bool:
    """True when another record already billed this disk: by immutable ID when the listing gives one (a reused
    name with a new ID is a new disk), else by source URL."""
    disk_id = str(disk.get("id") or "").strip()
    source = str(disk.get("source") or "")
    if disk_id:
        # By ID; or by source when the stored record only ever knew this disk by its source (its ID was never
        # read, for example during a disk-listing outage). A source stored with a different ID is a reused name.
        if f"id:{disk_id}" in known:
            return True
        if not source or f"srconly:{source}" not in known:
            return False
        # A disk created after the old VM vanished cannot be its retained attachment.
        prefix = f"srcbound:{source}|"
        bounds = [float(k[len(prefix):]) for k in known if k.startswith(prefix)]
        created = disk.get("created_epoch")
        return not _finite(created) or not bounds or float(created) <= min(bounds)
    return bool(source) and source in known


def _single_disk_all_in(disk: dict[str, Any], *, family: str, region: str, project: str,
                        tax_rate_percent: float, live_pricing: dict[str, Any] | None = None) -> float:
    """Hourly USD (with tax) of one persistent disk."""
    priced = calculate_spot_pricing(
        family=family, vcpu=0, ram_gib=0.0, region=region, boot_disk_type=str(disk.get("type") or "pd-balanced"),
        boot_disk_gb=int(disk.get("gb") or 0), data_disk_type=str(disk.get("type") or "pd-balanced"), data_disk_gb=0,
        tax_rate_percent=tax_rate_percent, machine_type=None, project=project, live_pricing=live_pricing)
    return float(priced.get("disk_hourly_usd", 0.0) or 0.0) * (1.0 + tax_rate_percent / 100.0)


def _record_disk_keys(record: dict[str, Any]) -> frozenset[str]:
    """A record's disk identities: ``id:<id>`` keys, source aliases, and ``srconly:<source>`` for disks it knows
    only by source (no ID was ever read)."""
    sources = [k for k in (record.get("disk_sources") or []) if not str(k).startswith("id:")]
    with_ids = set((record.get("disk_ids") or {}).keys())
    # Source-only matching is restricted to an unresolved liability. An ordinary VM attachment, even on a
    # live VM, cannot prove a later same-named disk with a newly learned ID is the same physical disk.
    unresolved = bool(record.get("disk_liability_pending"))
    aliases = [s for s in sources if unresolved and s not in with_ids]
    bounds = ([f"srcbound:{s}|{float(record['deleted_epoch'])}" for s in aliases]
              if _finite(record.get("deleted_epoch")) else [])
    return frozenset(list(record.get("disk_sources") or []) + [f"srconly:{s}" for s in aliases] + bounds)



def _billed_record_disk_keys(record: dict[str, Any]) -> frozenset[str]:
    """Attachment metadata alone is not evidence that a record billed a shared disk."""
    unowned = {key for d in record.get("disks_detail") or []
               if isinstance(d, dict) and (d.get("billing_owner") is False or _finite(d.get("billing_deferred_since")))
               for key in (_disk_key(d), str(d.get("source") or ""), f"srconly:{d.get('source', '')}")}
    bounds = record.get("billed_disk_source_bounds") or {}
    identified = set(record.get("billed_disk_identified_sources") or [])
    identified.update(str(d.get("source")) for d in record.get("disks_detail") or []
                      if isinstance(d, dict) and d.get("billing_owner") is not False
                      and not _finite(d.get("billing_deferred_since"))
                      and (d.get("id") or (record.get("disk_ids") or {}).get(str(d.get("source")))))
    aliases = {key for source, bound in bounds.items() if _finite(bound) and source not in identified
               for key in (f"srconly:{source}", f"srcbound:{source}|{float(bound)}")}
    return (_record_disk_keys(record) - unowned) | frozenset(record.get("billed_disk_sources") or []) | aliases


def _deferred_pricing_context(record: dict[str, Any], project: str,
                               live_pricing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Snapshot regional storage rates, including types not identified until a later disk listing."""
    from . import dashboard_models
    region = str(record.get("region") or "")
    regional = next((r for r in REGIONS_CATALOG if r["region"] == region), None)
    mult = float((regional or {}).get("price_multiplier", 1.0))
    rates = {kind: float(rate) * mult for kind, rate in dashboard_models.DISK_MONTHLY_RATES_USD_PER_GIB.items()}
    live = live_pricing if live_pricing is not None else dashboard_models.fetch_live_spot_pricing(
        project=project, regions=[region], machine_types=None)
    raw = (live or {}).get("disk_monthly_gib") or {}
    kinds = set(rates)
    for key in raw:
        if isinstance(key, tuple) and len(key) == 2 and key[0] == region:
            kinds.add(str(key[1]))
        elif isinstance(key, str) and ("/" not in key or key.startswith(region + "/")):
            kinds.add(key.split("/", 1)[-1])
    trusted = set(rates) if regional is not None else set()
    for kind in kinds:
        rate = next((raw[key] for key in ((region, kind), f"{region}/{kind}", kind)
                     if raw.get(key) is not None), None)
        if rate is not None:
            rates[kind] = float(rate)
            trusted.add(kind)
    context = {"tax_rate_percent": float((record.get("pricing") or {}).get("tax_rate_percent") or 0),
               "tax_rate_known": not _tax_unknown(record), "disk_monthly_gib": rates,
               "known_disk_types": sorted(trusted)}
    return context


def _snapshot_deferred_pricing(record: dict[str, Any], now: float, project: str,
                               live_pricing: dict[str, Any] | None = None) -> bool:
    initial = set(record.get("disk_history_deferred_sources") or [])
    global_deferred = _finite(record.get("disk_history_deferred_at"))
    disks = [d for d in record.get("disks_detail") or [] if isinstance(d, dict)
             and d.get("billing_owner") is not False and (_finite(d.get("billing_deferred_since"))
                 or (global_deferred and (not initial or str(d.get("source")) in initial)))]
    disks += [d for d in record.get("unpaid_disk_details") or [] if isinstance(d, dict)]
    if not disks:
        return False
    context = _deferred_pricing_context(record, project, live_pricing)
    changed = False
    for disk in disks:
        changed = append_deferred_context(disk, now, context) or changed
    return changed


def _deferred_disk_rate(disk: dict[str, Any], context: dict[str, Any], *, family: str,
                        region: str, project: str) -> float:
    return _single_disk_all_in(disk, family=family, region=region, project=project,
        tax_rate_percent=float(context.get("tax_rate_percent") or 0),
        live_pricing={"disk_monthly_gib": context.get("disk_monthly_gib") or {}})


def _deferred_context_at(disk: dict[str, Any], at: float) -> dict[str, Any] | None:
    timeline = disk.get("billing_deferred_pricing") or []
    if not timeline:
        return None
    chosen = timeline[0][1]
    for boundary, context in timeline:
        if float(boundary) <= at:
            chosen = context
    return chosen


def _remember_deferred_trust(record: dict[str, Any], disk: dict[str, Any], start: float, end: float,
                               now: float) -> None:
    """Recovering an identity cannot manufacture old catalog prices or a formerly unknown tax rate."""
    intervals = normalize_unpriced_intervals(record.get("unpriced_history_intervals"))
    bounds = sorted({start, end} | {float(at) for at, _context in disk.get("billing_deferred_pricing") or []
                                    if start < float(at) < end})
    for begin, finish in zip(bounds, bounds[1:]):
        context = _deferred_context_at(disk, begin)
        if context is None:
            continue  # Legacy debt has no recoverable context timeline.
        if str(disk.get("type")) not in context.get("known_disk_types", []):
            add_unpriced_interval(intervals, "fallback_storage", begin, finish)
        if context.get("tax_rate_known") is False:
            add_unpriced_interval(intervals, "fallback_storage_tax", begin, finish)
    _save_unpriced_intervals(record, intervals, now)


def _initial_disk_history(
    obs: dict[str, Any], *, region: str, family: str, project: str, tax_rate_percent: float, now: float,
) -> tuple[list[tuple[float, float, float]], float | None]:
    """(closed disk segments, open segment start) for a VM seen for the first time.

    Between disk creation or observed resize times the rate is that of the disks and sizes then observed. The
    open segment starts at the newest boundary. Without every creation time, nothing is
    backfilled: the open segment starts now.
    """
    disks = [d for d in obs.get("disks_detail") or []
             if isinstance(d, dict) and d.get("billing_owner") is not False
             and not _finite(d.get("billing_deferred_since"))]
    if not disks:
        # Deferred attachments start no known billing interval before this observation.
        return ([], now) if obs.get("disks_detail") else ([], None)
    if not all(_finite(d.get("created_epoch")) for d in disks):
        return [], now
    def sizes(disk: dict[str, Any]) -> list[list[float]]:
        return disk.get("recovered_sizes") or [[float(disk["created_epoch"]), int(disk.get("gb") or 0)]]

    times = sorted({min(float(d["created_epoch"]), now) for d in disks}
                   | {min(now, max(float(d["created_epoch"]), float(at))) for d in disks for at, _gb in sizes(d)}
                   | {min(now, max(float(d["created_epoch"]), float(at))) for d in disks
                      for at, _context in d.get("billing_deferred_pricing") or []})
    segments = []
    for start, end in zip(times, times[1:]):
        rate = 0.0
        for disk in disks:
            if float(disk["created_epoch"]) > start:
                continue
            history = sizes(disk)
            gb = history[0][1]
            for at, size in history:
                if float(at) <= start:
                    gb = size
            if disk.get("billing_deferred_pricing"):
                context = disk["billing_deferred_pricing"][0][1]
                for at, selected in disk["billing_deferred_pricing"]:
                    if float(at) <= start:
                        context = selected
                rate += _deferred_disk_rate(dict(disk, gb=gb), context, family=family, region=region, project=project)
            else:
                rate += _single_disk_all_in(dict(disk, gb=gb), family=family, region=region, project=project,
                                           tax_rate_percent=tax_rate_percent)
        segments.append((start, end, rate))
    return segments, times[-1]


def _live_instance_record(
    obs: dict[str, Any],
    *,
    project: str,
    now: float,
    previous: dict[str, Any] | None = None,
    known_disk_sources: frozenset[str] = frozenset(),
    tax_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    """A dashboard instance record for one Compute Engine observation (see fleet_inventory.observe_fleet_items)."""
    tax_overrides = tax_overrides or {}
    if obs.get("disk_types_known") is False:
        obs = dict(obs)
        if previous is not None:  # Even if the previous listing was also unverified: keep the last known types.
            # This listing could not read disk types: keep the stored ones where the disks are unchanged in size.
            if int(previous.get("boot_disk_gb") or 0) == int(obs.get("boot_disk_gb") or 0):
                obs["boot_disk_type"] = previous.get("boot_disk_type") or obs.get("boot_disk_type")
            if int(previous.get("data_disk_gb") or 0) == int(obs.get("data_disk_gb") or 0):
                obs["data_disk_type"] = previous.get("data_disk_type") or obs.get("data_disk_type")
                obs["data_disks"] = previous.get("data_disks") or obs.get("data_disks")
                obs["unpriced_disk_performance"] = bool(previous.get("unpriced_disk_performance"))
                obs["regional_disks"] = bool(obs.get("regional_disks") or previous.get("regional_disks"))
            # Per attachment too: same source and size keeps its stored type, ID, creation, and auto-delete.
            known = {str(d.get("source")): d for d in previous.get("disks_detail") or []
                     if isinstance(d, dict) and d.get("source")}
            merged = []
            for d in obs.get("disks_detail") or []:
                if not isinstance(d, dict):
                    continue
                old = known.get(str(d.get("source") or ""))
                if old is not None and int(old.get("gb") or 0) == int(d.get("gb") or 0):
                    d = {**d, **{k: old.get(k) for k in ("type", "id", "created_epoch", "auto_delete")
                                 if old.get(k) not in (None, "")
                                 and (k != "auto_delete" or not isinstance(d.get(k), bool))}}
                merged.append(d)
            obs["disks_detail"] = merged
            if merged and (all("boot" in d for d in merged) or not int(obs.get("boot_disk_gb") or 0)):
                # Restore type evidence without restoring the former billing owner: ownership comes from this
                # complete instance snapshot, even during a disk-listing outage.
                billed_data: dict[str, int] = {}
                obs["boot_disk_billed_gb"] = 0
                for d in merged:
                    if d.get("billing_owner") is False:
                        continue
                    if d.get("boot"):
                        obs["boot_disk_billed_gb"] += int(d.get("gb") or 0)
                    else:
                        kind = str(d.get("type") or "pd-balanced")
                        billed_data[kind] = billed_data.get(kind, 0) + int(d.get("gb") or 0)
                obs["data_disks"] = billed_data
            # Still unverified: a same-size disk may have been replaced by another type, so pricing stays incomplete.
    deferred_disks = []
    recovered_disks = []
    initial_recoveries: dict[str, dict[str, Any]] = {}
    if obs.get("disks_detail"):
        prior_details = {str(d.get("source")): d for d in (previous or {}).get("disks_detail") or []
                         if isinstance(d, dict) and d.get("source")}
        details = []
        for disk in obs.get("disks_detail") or []:
            if not isinstance(disk, dict):
                continue
            disk = dict(disk)
            old = prior_details.get(str(disk.get("source")))
            if old is not None and not _same_disk(old, disk):
                old = None
            deferred = (old or {}).get("billing_deferred_since", disk.get("billing_deferred_since"))
            if (not _finite(deferred) and old is not None and obs.get("disk_types_known") is False
                    and int(old.get("gb") or 0) != int(disk.get("gb") or 0)):
                # A resized attachment is newly unpriced capacity even though its source already existed. Keep
                # each observed size boundary until authoritative disk metadata can repair this interval.
                deferred = now
            if (not _finite(deferred) and old is None and not _finite(disk.get("created_epoch"))
                    and obs.get("disk_types_known") is False
                    and (previous is not None or not _disk_known(disk, known_disk_sources))
                    and (previous is not None or not any(_finite(d.get("created_epoch"))
                         for d in obs.get("disks_detail") or [] if isinstance(d, dict)))):
                deferred = now
            if _finite(deferred) and disk.get("billing_owner") is not False:
                for key in ("billing_initial_history", "billing_history_credits", "billing_deferred_pricing"):
                    if key in (old or {}):
                        disk[key] = copy.deepcopy(old[key])
                sizes = copy.deepcopy((old or {}).get("billing_deferred_sizes")
                                      or disk.get("billing_deferred_sizes")
                                      or [[float(deferred), int((old or disk).get("gb") or 0)]])
                if int(sizes[-1][1]) != int(disk.get("gb") or 0):
                    sizes.append([now, int(disk.get("gb") or 0)])
                disk["billing_deferred_sizes"] = sizes
                if _finite(disk.get("created_epoch")) and obs.get("disk_types_known") is not False:
                    disk.pop("billing_deferred_since", None)
                    sizes = disk.pop("billing_deferred_sizes")
                    recovered = dict(disk, billing_deferred_since=float(deferred), recovered_sizes=sizes)
                    disk.pop("billing_initial_history", None)
                    disk.pop("billing_history_credits", None)
                    disk.pop("billing_deferred_pricing", None)
                    if previous is not None:
                        recovered_disks.append(recovered)
                    else:
                        initial_recoveries[str(disk.get("source"))] = recovered
                else:
                    disk["billing_deferred_since"] = float(deferred)
                    deferred_disks.append(str(disk.get("source") or ""))
            details.append(disk)
        obs = dict(obs, disks_detail=details)
    obs = dict(obs, deferred_disk_sources=sorted(deferred_disks))
    status = str(obs.get("status") or "UNKNOWN").upper()
    previous_start_known = bool(previous is not None and previous.get("compute_start_known",
        previous.get("status") not in ("PROVISIONING", "STAGING") or bool(_prior_runs(previous))))
    start_known = _finite(obs.get("last_start_epoch")) or previous_start_known
    started = (obs.get("last_start_epoch") if _finite(obs.get("last_start_epoch")) else
               previous.get("started_epoch") if previous_start_known else obs.get("created_epoch"))
    started_epoch = float(started) if isinstance(started, (int, float)) else now
    started_epoch = min(started_epoch, now)
    role = str(obs.get("role") or "cicd_runner")
    region = str(obs.get("region") or "")
    region_known = any(r["region"] == region for r in REGIONS_CATALOG)
    provisioning = "spot" if obs.get("spot", True) else "ondemand"
    shape_keys = ("spot", "region", "machine_type", "vcpu", "ram_gib", "boot_disk_type", "boot_disk_gb",
                  "data_disk_type", "data_disk_gb", "data_disks", "local_ssd_gb", "unpriced_disk_performance",
                  "regional_disks", "boot_disk_billed_gb", "data_disks_attached_gb", "deferred_disk_sources")
    reuse_pricing = (
        previous.get("pricing")
        if previous is not None and isinstance(previous.get("pricing"), dict)
        and all((previous.get(k) or None) == (obs.get(k) or None) for k in shape_keys)
        else None
    )
    if recovered_disks:
        reuse_pricing = None
    record = _build_instance_record(
        instance_id=str(obs["instance_id"]),
        name=str(obs["name"]),
        role=role,
        status=status,
        region=region,
        zone=str(obs.get("zone") or ""),
        machine_type=str(obs.get("machine_type") or ""),
        vcpu=int(obs.get("vcpu") or 0),
        ram_gib=float(obs.get("ram_gib") or 0.0),
        cpu_platform=str(obs.get("cpu_platform") or ""),
        boot_disk_type=str(obs.get("boot_disk_type") or "pd-balanced"),
        boot_disk_gb=int(obs.get("boot_disk_billed_gb", obs.get("boot_disk_gb")) or 0),
        data_disk_type=str(obs.get("data_disk_type") or "pd-balanced"),
        data_disk_gb=_primary_data_disk_gb(obs),
        runners_count=int(obs.get("runners_count") or 0),
        runner_vcpu=int(obs.get("runner_vcpu") or 0),
        runner_ram_gib=float(obs.get("runner_ram_gib") or 0.0),
        started_hours_ago=max(0.0, (now - started_epoch) / 3600.0),
        deleted_hours_ago=None,
        deletion_trigger="",
        cloud_init_template_id="",
        vm_template_id=None,
        cpu_utilization_percent=0.0,
        ram_utilization_percent=0.0,
        project=project,
        now_epoch=now,
        # Outside the catalog no regional tax default is known: never borrow another region's.
        # The profile's tax override when set (so first-seen history is not archived at another rate), else the
        # region's default; outside the catalog no default is known, so never borrow another region's.
        tax_rate_percent=(tax_overrides[role] if role in tax_overrides else None if region_known else 0.0),
        pricing=copy.deepcopy(reuse_pricing) if reuse_pricing is not None else None,
        provisioning=provisioning,
    )
    if reuse_pricing is None:
        # Data disks of other types than the first are priced at their own rates.
        record["pricing"] = _with_secondary_disks(record["pricing"], obs, region=region,
                                                  family=record["family"], project=project)
    record["compute_start_known"] = start_known
    record["boot_disk_gb"] = int(obs.get("boot_disk_gb") or 0)
    if "boot_disk_billed_gb" in obs:
        record["boot_disk_billed_gb"] = obs["boot_disk_billed_gb"]
    record["data_disks"] = obs.get("data_disks") or {}
    if "data_disks_attached_gb" in obs:
        record["data_disks_attached_gb"] = obs["data_disks_attached_gb"]
    record["shared_attachments"] = obs.get("shared_attachments") or []
    record["disks_detail"] = [dict(d) for d in obs.get("disks_detail") or [] if isinstance(d, dict)]
    record["deferred_disk_sources"] = obs["deferred_disk_sources"]
    # Disk identities: the immutable ID (kept from the previous record when this listing could not read it) and the
    # source URL as an alias, so a handoff is recognized by either.
    previous_ids = (previous or {}).get("disk_ids") or {}
    disk_ids = {}
    for d in obs.get("disks_detail") or []:
        if isinstance(d, dict) and d.get("source"):
            disk_id = str(d.get("id") or previous_ids.get(str(d["source"])) or "")
            if disk_id:
                disk_ids[str(d["source"])] = disk_id
    record["disk_ids"] = disk_ids
    record["disk_sources"] = sorted({f"id:{i}" for i in disk_ids.values()}
                                    | {str(d.get("source")) for d in obs.get("disks_detail") or []
                                       if isinstance(d, dict) and d.get("source")})
    bounds = dict((previous or {}).get("billed_disk_source_bounds") or {})
    for disk in record["disks_detail"]:
        if disk.get("source") and disk.get("billing_owner") is not False and not _finite(disk.get("billing_deferred_since")):
            bounds.setdefault(str(disk["source"]), now)
    record["unpriced_lifetime_history"] = bool((previous or {}).get("unpriced_lifetime_history"))
    for component, unknown in zip(("compute", "storage"), _lifetime_history_components(previous or {})):
        if unknown:
            record[f"unpriced_{component}_lifetime_history"] = True
    for component, unknown in zip(("compute_tax", "storage_tax"), _lifetime_tax_components(previous or {})):
        if unknown:
            record[f"unpriced_{component}_lifetime_history"] = True
    record["billed_disk_source_bounds"] = bounds
    record["billed_disk_identified_sources"] = sorted(set((previous or {}).get("billed_disk_identified_sources") or [])
        | {str(d["source"]) for d in record["disks_detail"] if d.get("source") and d.get("id")
           and d.get("billing_owner") is not False and not _finite(d.get("billing_deferred_since"))})
    record["billed_disk_sources"] = sorted({key for key in _billed_record_disk_keys(record)
        | _billed_record_disk_keys(previous or {}) if key and not key.startswith(("srconly:", "srcbound:"))})
    # Capacity this VM adds, not yet counted toward OOM handling: set for a new identity, kept until counted.
    record["capacity_pending"] = bool(previous.get("capacity_pending")) if previous is not None else True
    first_ready = (previous or {}).get("capacity_first_ready_lower_bound")
    if not _finite(first_ready) and status == "RUNNING" and obs.get("runners_count_known", True):
        # Metadata confirmation is not dated readiness. Only identity creation bounds its earliest possible
        # contribution; missing creation evidence is conservatively earlier than every normal incident.
        first_ready = float(obs["created_epoch"]) if _finite(obs.get("created_epoch")) else 0.0
    record["capacity_first_ready_lower_bound"] = first_ready
    record["local_ssd_gb"] = int(obs.get("local_ssd_gb") or 0)
    record["unpriced_disk_performance"] = bool(obs.get("unpriced_disk_performance"))
    record["regional_disks"] = bool(obs.get("regional_disks"))
    record["disk_types_known"] = obs.get("disk_types_known") is not False
    # The record shows the total data-disk size (pricing already covers each type).
    record["data_disk_gb"] = int(obs.get("data_disk_gb") or 0)
    if role in tax_overrides:
        record["tax_rate_overridden"] = True
    if not region_known:
        # Outside the catalog the record must not borrow another region's location or tax.
        record.update({"display_location": region, "continent": "", "lat": None, "lon": None,
                       "map_x": None, "map_y": None, "tax_rate_known": False})
    stopped_epoch: float | None = None
    if status == "RUNNING":
        trigger = "Active — RUNNING in the live Compute Engine inventory"
    elif status == "DETACHED":
        stopped_epoch = started_epoch  # A disk on its own never runs compute.
        trigger = "Fleet disk that no VM uses (for example a builder cache disk); it bills storage until deleted"
    elif status in GCE_STOPPED_STATUSES:
        stops = (obs.get("last_suspended_epoch"),) if status == "SUSPENDED" else (
            obs.get("last_stop_epoch"), obs.get("last_suspended_epoch"))
        ends = [float(stop) for stop in stops if _finite(stop) and started_epoch <= float(stop) <= now]
        previous_stop = (previous or {}).get("stopped_epoch")
        if (_finite(previous_stop) and (previous or {}).get("started_epoch") == started_epoch
                and started_epoch <= float(previous_stop) <= now):
            ends.append(float(previous_stop))  # A later terminal timestamp cannot reopen known stopped compute.
        if ends:
            # Saved memory can outlive compute: termination must not bill the suspended gap as a run.
            stopped_epoch = min(ends)
        elif previous is not None and _finite(previous.get("stopped_epoch")) \
                and started_epoch <= float(previous["stopped_epoch"]) <= now:
            stopped_epoch = float(previous["stopped_epoch"])
        else:
            stopped_epoch = now
        trigger = (f"Stopped in Compute Engine ({status}): Spot preemption or a manual stop; "
                   "the VM and its disks still exist")
    else:
        trigger = f"Compute Engine status {status}"
        # A restart can still report the completed run's lastStartTimestamp. Its known stop must not disappear
        # merely because the VM is staging again; pending capacity still reserves its full active budget rate.
        previous_stop = (previous or {}).get("stopped_epoch")
        if (_finite(previous_stop) and float(previous_stop) >= started_epoch
                and float((previous or {}).get("started_epoch") or started_epoch) == started_epoch):
            stopped_epoch = float(previous_stop)
        else:
            ends = [float(t) for t in (obs.get("last_stop_epoch"), obs.get("last_suspended_epoch"))
                    if _finite(t) and started_epoch <= float(t) <= now]
            if ends:
                stopped_epoch = max(ends)
    transitional = status not in GCE_STOPPED_STATUSES and status not in ("RUNNING", "DELETED", "DETACHED")
    prior_transition = (previous or {}).get("transitional_since_epoch")
    if transitional:
        record["transitional_since_epoch"] = (float(prior_transition) if _finite(prior_transition)
            and float((previous or {}).get("started_epoch") or started_epoch) == started_epoch else now)
    created = obs.get("created_epoch")
    if not _finite(created) and previous is not None:
        created = previous.get("disk_start_epoch")
    disk_start = min(float(created), started_epoch) if _finite(created) else started_epoch
    if previous is not None and _finite(previous.get("disk_start_epoch")):
        disk_start = min(disk_start, float(previous["disk_start_epoch"]))
    if previous is None and known_disk_sources:
        # A disk that another record already billed (a cache disk moving between a VM and its own DETACHED record)
        # starts here at the handoff, not again at its creation.
        handed_off = [dict(d, created_epoch=max(float(d.get("created_epoch") or now),
                          float(initial_recoveries.get(str(d.get("source")), {}).get("billing_deferred_since", now))))
                      if _disk_known(d, known_disk_sources)
                      and not initial_recoveries.get(str(d.get("source")), {}).get("billing_initial_history") else d
                      for d in obs.get("disks_detail") or [] if isinstance(d, dict)]
        if handed_off:
            obs = dict(obs, disks_detail=handed_off)
    if previous is None and initial_recoveries:
        context = _deferred_pricing_context(record, project)
        for disk in initial_recoveries.values():
            append_deferred_context(disk, now, context)
        obs = dict(obs, disks_detail=[dict(d, recovered_sizes=initial_recoveries[str(d.get("source"))]["recovered_sizes"],
                    billing_deferred_pricing=initial_recoveries[str(d.get("source"))].get("billing_deferred_pricing") or [])
                   if str(d.get("source")) in initial_recoveries else d for d in obs.get("disks_detail") or []])
    recovered_sources = {str(d.get("source")) for d in recovered_disks}
    pending_open_disk_rate: float | None = None
    prior_runs = _prior_runs(previous)
    disk_segments = _disk_segments(previous)
    revived_at: float | None = None
    if previous is not None and previous.get("status") == "DELETED":
        # Back after it was gone (a cache disk detached again after a VM used it): its earlier standalone interval
        # stays closed, and billing here restarts now; the time in between was billed by the VM's record.
        ended = previous.get("deleted_epoch")
        if _finite(ended) and isinstance(previous.get("pricing"), dict):
            open_from = max([float(previous.get("disk_start_epoch") or ended)] + [e for _s, e, _r in disk_segments]
                            + ([float(previous["disk_segment_start"])] if _finite(previous.get("disk_segment_start"))
                               else []))
            if ended > open_from:
                disk_segments.append((open_from, float(ended), _pricing_rates(previous["pricing"])[2]))
        revived_at = now
    initial_disk_segment_start: float | None = None
    original_sources: list[str] = []
    resolved_initial_sources: set[str] = set()
    deferred_at = (previous or {}).get("disk_history_deferred_at")
    if previous is not None and _finite(deferred_at):
        # Disk history was deferred (first seen without disk creation times). Once every attachment has a creation
        # time, bill the deferred interval per disk from its creation (disks another record billed excepted).
        original_sources = previous.get("disk_history_deferred_sources")
        if not isinstance(original_sources, list):
            original_sources = [str(d.get("source")) for d in previous.get("disks_detail") or []
                                if isinstance(d, dict) and not _finite(d.get("billing_deferred_since"))]
        disks_now = [d for d in obs.get("disks_detail") or []
                     if isinstance(d, dict) and d.get("billing_owner") is not False
                     and str(d.get("source")) in original_sources and _finite(d.get("created_epoch"))]
        if disks_now:
            resolved_initial_sources = {str(d.get("source")) for d in disks_now}
            backfill = [dict(d, created_epoch=float(deferred_at)) if _disk_known(d, known_disk_sources) else d
                        for d in disks_now]
            segments, open_from = _initial_disk_history(
                dict(obs, disks_detail=backfill), region=region,
                family=str(obs.get("machine_type") or "").split("-", 1)[0], project=project,
                tax_rate_percent=float(record["pricing"].get("tax_rate_percent", 0.0) or 0.0), now=float(deferred_at))
            if _finite(open_from) and float(deferred_at) > float(open_from):
                tax_pct = float(record["pricing"].get("tax_rate_percent", 0.0) or 0.0)
                full = sum(_single_disk_all_in(d, family=record["family"], region=region, project=project,
                                               tax_rate_percent=tax_pct) for d in backfill)
                segments = segments + [(float(open_from), float(deferred_at), full)]
            disk_segments_backfill = segments
        else:
            disk_segments_backfill = None
    else:
        disk_segments_backfill = None
    if previous is None:
        # First sight of this VM: bill earlier disk time per disk from each disk's own creation, never a later-
        # attached disk back to the VM's creation; with any creation time unknown, start disk history now.
        disk_segments, initial_disk_segment_start = _initial_disk_history(
            obs, region=region, family=str(obs.get("machine_type") or "").split("-", 1)[0], project=project,
            tax_rate_percent=float(record["pricing"].get("tax_rate_percent", 0.0) or 0.0), now=now)
        if _finite(initial_disk_segment_start):
            disk_start = min([disk_start, float(initial_disk_segment_start)] + [start for start, _end, _rate in disk_segments])
    if disk_segments_backfill:
        disk_segments = list(disk_segments_backfill) + disk_segments
        disk_start = min([disk_start] + [seg[0] for seg in disk_segments_backfill])
    compute_segment_start = started_epoch
    if previous is not None and _finite(previous.get("compute_segment_start")) \
            and float(previous.get("started_epoch") or 0.0) == started_epoch:
        # Same run: keep the boundary of an earlier rate change, or that time would be counted twice.
        compute_segment_start = max(started_epoch, float(previous["compute_segment_start"]))
    disk_segment_start = (revived_at if revived_at is not None
                          else previous.get("disk_segment_start") if previous is not None
                          else initial_disk_segment_start)
    if previous is not None and isinstance(previous.get("pricing"), dict):
        prev_compute_all_in, prev_compute_only, prev_disk_all_in = _pricing_rates(previous["pricing"])
        prev_start = float(previous.get("started_epoch") or started_epoch)
        prev_seg = previous.get("compute_segment_start")
        prev_open = max(prev_start, float(prev_seg)) if _finite(prev_seg) else prev_start
        if started_epoch > prev_start and not previous_start_known:
            # Creation was only a placeholder before the first lastStartTimestamp. It never represented a run;
            # discard any provisional closed intervals rather than preserving pre-start compute charges.
            prior_runs = [(s0, e0, ca, co) for s0, e0, ca, co in prior_runs if e0 <= prev_start]
        elif started_epoch > prev_start:
            # Started again: close the earlier run (to its recorded stop, else the stop GCE reports, else the new
            # start) at the rates that applied to it.
            prev_end = previous.get("stopped_epoch")
            if not _finite(prev_end):
                # A stop or a suspension (resumed between listings) ended that run: take the latest one that falls
                # between its start and the new start.
                ends = [t for t in (obs.get("last_stop_epoch"), obs.get("last_suspended_epoch"))
                        if _finite(t) and prev_start <= t <= started_epoch]
                prev_end = max(ends) if ends else started_epoch
            prev_end = min(float(prev_end), started_epoch)
            # Intervals of that run closed after its real stop (for example at a price change while the listing
            # still said RUNNING) end at the stop.
            trimmed = []
            for s0, e0, ca, co in prior_runs:
                if s0 >= prev_start and e0 > prev_end:
                    if s0 >= prev_end:
                        continue
                    e0 = prev_end
                trimmed.append((s0, e0, ca, co))
            prior_runs = trimmed
            if prev_end > prev_open:
                prior_runs.append((prev_open, prev_end, prev_compute_all_in, prev_compute_only))
        elif reuse_pricing is None:
            # Same run, new shape or disks: bill the run so far, and the disks so far, at the earlier rates.
            freeze_end = min(_current_run_end(previous, now), now)
            if stopped_epoch is not None:
                # Stopped and changed in one listing: compute ended at the reported stop, not at this refresh.
                freeze_end = min(freeze_end, stopped_epoch)
            if freeze_end > prev_open:
                prior_runs.append((prev_open, freeze_end, prev_compute_all_in, prev_compute_only))
            compute_segment_start = max(prev_open, freeze_end)
        if reuse_pricing is None:
            disk_open = max([disk_start] + [e for _s, e, _r in disk_segments]
                            + ([float(disk_segment_start)] if _finite(disk_segment_start) else []))
            # Where the old disk rate ends: when the only change is added disks whose creation times are known
            # (and that no other record billed), at the newest one's creation; otherwise at this listing.
            own_keys = _billed_record_disk_keys(previous)
            added = [d for d in obs.get("disks_detail") or [] if isinstance(d, dict) and d.get("billing_owner") is not False
                     and str(d.get("source")) not in recovered_sources and not _disk_known(d, own_keys)]
            split_at = now
            if added and all(_finite(d.get("created_epoch")) for d in added) \
                    and not any(_disk_known(d, known_disk_sources) for d in added):
                # One step per added disk: between creations the rate is the old disks plus those added so far.
                tax_pct = float(record["pricing"].get("tax_rate_percent", 0.0) or 0.0)
                cursor, rate = disk_open, prev_disk_all_in
                for d in sorted(added, key=lambda item: float(item["created_epoch"])):
                    at = min(now, max(cursor, float(d["created_epoch"])))
                    if at > cursor:
                        disk_segments.append((cursor, at, rate))
                    cursor = at
                    rate += _single_disk_all_in(d, family=record["family"], region=region, project=project,
                                                tax_rate_percent=tax_pct)
                disk_open, split_at = cursor, cursor
                pending_open_disk_rate = rate
            if split_at > disk_open:
                disk_segments.append((disk_open, split_at, prev_disk_all_in))
            disk_segment_start = split_at
    if recovered_disks and pending_open_disk_rate is not None and _finite(disk_segment_start) and disk_segment_start < now:
        disk_segments.append((float(disk_segment_start), now, pending_open_disk_rate))
        disk_segment_start = now
    for disk in recovered_disks:
        start = min(now, float(disk["created_epoch"]))
        if not disk.get("billing_initial_history") and _disk_known(
                disk, known_disk_sources | _billed_record_disk_keys(previous or {})):
            start = max(start, float(disk["billing_deferred_since"]))
        if start < now:
            _remember_deferred_trust(record, disk, start, now, now)
            fallback = _deferred_pricing_context(record, project)
            disk_segments.extend(deferred_segments(disk, start, now, fallback,
                lambda detail, context: _deferred_disk_rate(detail, context,
                    family=record["family"], region=region, project=project)))
            disk_start = min(disk_start, start)
    history_credits = [credit for disk in recovered_disks + list(initial_recoveries.values())
                       for credit in disk.get("billing_history_credits") or []]
    if history_credits:
        if previous is None and _finite(initial_disk_segment_start) and initial_disk_segment_start < now:
            disk_segments.append((float(initial_disk_segment_start), now, _pricing_rates(record["pricing"])[2]))
            disk_segment_start = now
        disk_segments.extend((float(start), float(end), -float(rate)) for start, end, rate in history_credits)
    if recovered_disks or disk_segments_backfill is not None or history_credits:
        # Backfill adds a per-disk rate to aggregate history. Combine overlaps into disjoint intervals so storage
        # hours remain elapsed time rather than counting the same time once per attachment.
        boundaries = sorted({t for start, end, _rate in disk_segments for t in (start, end)})
        disk_segments = [(start, end, sum(rate for s0, e0, rate in disk_segments if s0 <= start < e0))
                         for start, end in zip(boundaries, boundaries[1:])
                         if any(s0 <= start < e0 for s0, e0, _rate in disk_segments)]
    if stopped_epoch is not None:
        # A stop reported late: intervals of this run closed (for example at a price change) after the real stop
        # end at the stop instead.
        trimmed = []
        for s0, e0, ca, co in prior_runs:
            if s0 >= started_epoch and e0 > stopped_epoch:
                if s0 >= stopped_epoch:
                    continue
                e0 = stopped_epoch
            trimmed.append((s0, e0, ca, co))
        prior_runs = trimmed
        compute_segment_start = min(compute_segment_start, max(started_epoch, stopped_epoch))
    # Preserve the effective open boundary before removing the last detailed interval.
    if disk_segments:
        disk_segment_start = max([end for _start, end, _rate in disk_segments]
                                 + ([float(disk_segment_start)] if _finite(disk_segment_start) else []))
    # Prune by time only (every accepted window is inside the retention period); keep lifetime totals.
    cutoff = now - DELETED_INSTANCE_RETENTION_SECONDS
    pruned_hours = float(previous.get("pruned_compute_hours") or 0.0) if previous is not None else 0.0
    prior_disk_hours = (previous or {}).get("pruned_disk_hours")
    pruned_disk_hours = float(prior_disk_hours) if _finite(prior_disk_hours) else 0.0
    prev_pruned = (previous or {}).get("pruned_cost_usd")
    pruned_all_in, pruned_compute = (
        (float(prev_pruned[0]), float(prev_pruned[1]))
        if isinstance(prev_pruned, (list, tuple)) and len(prev_pruned) == 2 and all(_finite(v) for v in prev_pruned)
        else (0.0, 0.0)
    )
    cur_compute_all_in, cur_compute_only, _cur_disk = _pricing_rates(record["pricing"])
    kept_runs = []
    for s0, e0, ca, co in prior_runs:
        if e0 < cutoff:
            hours = (e0 - s0) / 3600.0
            pruned_hours += hours
            pruned_all_in += hours * (ca if ca is not None else cur_compute_all_in)
            pruned_compute += hours * (co if co is not None else cur_compute_only)
        else:
            kept_runs.append((s0, e0, ca, co))
    # The prior record also carries debt not yet reattached by reconciliation. Keep coverage
    # through the recovery poll; a subsequent poll can prune/fold the merged disjoint history.
    preserve_disk_detail = (_disk_history_recovery_pending(previous)
                            or _disk_history_recovery_pending(record))
    kept_disks = []
    for s0, e0, r0 in disk_segments:
        if e0 < cutoff and not preserve_disk_detail:
            pruned_disk_hours += (e0 - s0) / 3600.0
            pruned_all_in += (e0 - s0) / 3600.0 * r0
        else:
            kept_disks.append((s0, e0, r0))
    folded = copy.deepcopy((previous or {}).get("folded_cost_buckets") or {})
    folded_span = _record_bucket_span(previous)
    if len(kept_runs) > MAX_DETAILED_SEGMENTS:
        kept_runs.sort(key=lambda run: run[0])
        for s0, e0, ca, co in kept_runs[:-MAX_DETAILED_SEGMENTS]:
            _add_to_buckets(folded, "", s0, e0, ca if ca is not None else cur_compute_all_in,
                                co if co is not None else cur_compute_only, compute=True, span=folded_span)
        kept_runs = kept_runs[-MAX_DETAILED_SEGMENTS:]
    if len(kept_disks) > MAX_DETAILED_SEGMENTS and not preserve_disk_detail:
        kept_disks.sort(key=lambda seg: seg[0])
        for s0, e0, r0 in kept_disks[:-MAX_DETAILED_SEGMENTS]:
            _add_to_buckets(folded, "", s0, e0, r0, 0.0, compute=False, span=folded_span)
        kept_disks = kept_disks[-MAX_DETAILED_SEGMENTS:]
    expired_hours, expired_all_in, expired_compute, expired_disk_hours = _bucket_window_totals(
        {k: v for k, v in folded.items() if float(k) + folded_span < cutoff}, float("-inf"), now, span=folded_span)
    pruned_disk_hours += expired_disk_hours
    pruned_hours += expired_hours
    pruned_all_in += expired_all_in
    pruned_compute += expired_compute
    _prune_buckets(folded, cutoff, folded_span)
    record["folded_cost_buckets"] = folded
    record["folded_bucket_seconds"] = folded_span
    record.update({
        "started_epoch": started_epoch, "stopped_epoch": stopped_epoch, "deleted_epoch": None, "status": status,
        "prior_runs": [[s0, e0] if ca is None else [s0, e0, ca, co] for s0, e0, ca, co in kept_runs],
        "disk_segments": [list(seg) for seg in kept_disks],
        "compute_segment_start": compute_segment_start,
        "disk_segment_start": float(disk_segment_start) if _finite(disk_segment_start) else None,
        "pruned_compute_hours": round(pruned_hours, 4),
        "pruned_disk_hours": pruned_disk_hours,
        "pruned_cost_usd": [round(pruned_all_in, 6), round(pruned_compute, 6)],
        "disk_start_epoch": disk_start,
        "disk_history_deferred_at": (
            None if disk_segments_backfill is not None and not (set(original_sources) - resolved_initial_sources)
            else float(initial_disk_segment_start) if previous is None and initial_disk_segment_start is not None
            and not all(_finite(d.get("created_epoch")) for d in obs.get("disks_detail") or []
                        if isinstance(d, dict) and d.get("billing_owner") is not False
                        and not _finite(d.get("billing_deferred_since")))
            else (previous or {}).get("disk_history_deferred_at")),
    })
    if previous is None and _finite(record.get("disk_history_deferred_at")):
        record["disk_history_deferred_sources"] = [str(d.get("source")) for d in obs.get("disks_detail") or []
                                                  if isinstance(d, dict) and d.get("billing_owner") is not False
                                                  and not _finite(d.get("billing_deferred_since"))]
    elif _finite(record.get("disk_history_deferred_at")):
        record["disk_history_deferred_sources"] = [source for source in original_sources
                                                    if source not in resolved_initial_sources]
    else:
        record["disk_history_deferred_sources"] = []
    usage = instance_usage(record, now)
    runtime_hours = round(usage["compute_hours"], 2)
    total_cost, compute_cost = round(usage["cost_all_in"], 2), round(usage["cost_compute"], 2)
    record.update({
        INSTANCE_SOURCE_KEY: INSTANCE_SOURCE_GCE,
        "gce_status": status,
        "spot": bool(obs.get("spot")),
        "specs_known": bool(obs.get("specs_known", True)),
        "runner_limits_known": bool(obs.get("runner_limits_known", True)),
        "runners_count_known": bool(obs.get("runners_count_known", True)),
        "kind": str(obs.get("kind") or "vm"),
        "started_epoch": started_epoch,
        "started_at": _iso_epoch(started_epoch),
        "stopped_epoch": stopped_epoch,
        "stopped_at": _iso_epoch(stopped_epoch) if stopped_epoch is not None else None,
        "deleted_epoch": None,
        "deleted_at": None,
        "runtime_hours": runtime_hours,
        "total_cost_usd": total_cost,
        "total_cost_compute_only_usd": compute_cost,
        "deletion_trigger": trigger,
        # Utilization is not part of the inventory listing.
        "utilization_known": False,
    })
    if obs.get("storage_billing_ambiguous"):
        record["storage_billing_ambiguous"] = True
        if obs.get("storage_billing_ambiguous_roles"):
            record["storage_billing_ambiguous_roles"] = sorted(obs["storage_billing_ambiguous_roles"])
    else:
        record.pop("storage_billing_ambiguous", None)
        record.pop("storage_billing_ambiguous_roles", None)
    _sync_disk_ambiguity(record)
    _retain_unpriced_history(record, obs, previous, now)
    for source, recovered in initial_recoveries.items():
        current = next((d for d in obs.get("disks_detail") or [] if str(d.get("source")) == source), recovered)
        _remember_deferred_trust(record, recovered, min(now, float(current.get("created_epoch") or now)), now, now)
    if previous is not None:
        old_pricing = previous.get("pricing") or {}
        if old_pricing != record.get("pricing") or previous.get("pricing_complete") is False:
            old_compute_start = float(previous.get("compute_segment_start") or previous.get("started_epoch") or now)
            old_run_end = min(now, _current_run_end(previous, now))
            for stop in (obs.get("last_stop_epoch"), obs.get("last_suspended_epoch")):
                if _finite(stop) and old_compute_start <= float(stop) <= now and (
                        record.get("status") in GCE_STOPPED_STATUSES or float(stop) < float(record["started_epoch"])):
                    old_run_end = min(old_run_end, float(stop))
            old_disk_end = min(now, float(previous.get("disk_end_epoch") or
                               (previous.get("deleted_epoch") if not previous.get("disk_liability_pending") else None) or now))
            _remember_incomplete_pricing(record, previous, old_pricing, old_compute_start, old_run_end,
                float(previous.get("disk_segment_start") or previous.get("disk_start_epoch") or now), old_disk_end)
    if previous is None:
        start = float(record.get("compute_segment_start") or record.get("started_epoch") or now)
        _remember_incomplete_pricing(record, record, record.get("pricing") or {},
            start, min(now, _current_run_end(record, now)),
            float(record.get("disk_segment_start") or record.get("disk_start_epoch") or now), now)
    _snapshot_deferred_pricing(record, now, project)
    _mark_pricing_completeness(record)
    if role == "cicd_runner":
        count = int(obs.get("runners_count") or 0)
        record["workload_summary"] = (
            f"{count} GitHub runner(s) recorded at provisioning" if count
            else "Runner host (runner count not recorded in instance metadata)"
        )
    elif role == "rust_builder" and obs.get("runner_limits_known") is False:
        record["workload_summary"] = (
            "Builder host (fixed n4-highcpu-16 / 16 vCPU / 32 GiB host shape is not verified; container limits unknown)"
        )
    return record


def _retained_disks(record: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The record's disks that outlive the VM (auto-delete off), or None when the listing gave no auto-delete
    information (then all disks count, conservatively)."""
    disks = record.get("disks_detail") or []
    if not disks or any(not isinstance(d, dict) or d.get("auto_delete") is None for d in disks):
        return None
    return [d for d in disks if d.get("auto_delete") is False and d.get("billing_owner") is not False]


def _retained_pricing(record: dict[str, Any], *, tax_rate_percent: float, project: str,
                      live_pricing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pricing of only the disks a deleted VM leaves behind (no compute)."""
    retained = _retained_disks(record) or []
    family = str(record.get("family") or "")
    region = str(record.get("region") or "")
    priced = calculate_spot_pricing(
        family=family, vcpu=0, ram_gib=0.0, region=region, boot_disk_type="pd-balanced", boot_disk_gb=0,
        data_disk_type="pd-balanced", data_disk_gb=0, tax_rate_percent=tax_rate_percent, machine_type=None,
        live_pricing=live_pricing, project=project)
    parts = [calculate_spot_pricing(
        family=family, vcpu=0, ram_gib=0.0, region=region,
        boot_disk_type=str(d.get("type") or "pd-balanced"), boot_disk_gb=int(d.get("gb") or 0),
        data_disk_type="pd-balanced", data_disk_gb=0, tax_rate_percent=tax_rate_percent, machine_type=None,
        live_pricing=live_pricing, project=project) for d in retained
        if not _finite(d.get("billing_deferred_since"))]
    out = _with_extra_disks(priced, sum(float(part.get("disk_hourly_usd") or 0.0) for part in parts))
    unknown = sorted({kind for part in parts for kind in part.get("disk_rate_unknown") or []})
    if any(part.get("complete") is False for part in parts):
        out["complete"] = False
    if unknown:
        out["disk_rate_unknown"] = unknown
    return out


def _mark_instance_deleted(record: dict[str, Any], now: float, *, disks_pending: bool = False,
                           project: str = "homelab-424523") -> dict[str, Any]:
    """The record of a VM that is no longer in the Compute Engine inventory."""
    out = copy.deepcopy(record)
    pricing = record.get("pricing") or {}
    started = float(record.get("started_epoch") or now)
    _remember_incomplete_pricing(out, record, pricing,
        max(started, float(record.get("compute_segment_start") or started)),
        min(now, _current_run_end(record, now)),
        float(record.get("disk_segment_start") or record.get("disk_start_epoch") or now),
        min(now, float(record.get("disk_end_epoch") or
            (record.get("deleted_epoch") if not record.get("disk_liability_pending") else None) or now)))
    if disks_pending:
        retained = _retained_disks(out)
        if retained == []:
            disks_pending = False  # Every disk was auto-deleted with the VM: nothing left to bill.
        elif retained is not None:
            # Only the disks that outlive the VM keep billing: close compute and the full disk rate here, and
            # continue at the rate of the retained disks alone.
            pricing = out.get("pricing") if isinstance(out.get("pricing"), dict) else {}
            compute_all_in, compute_only, full_disk = _pricing_rates(pricing)
            if out.get("status") == "RUNNING" or _instance_end_epoch(out) is None:
                out["stopped_epoch"] = now
            started = float(out.get("started_epoch") or now)
            seg = out.get("compute_segment_start")
            open_start = max(started, float(seg)) if _finite(seg) else started
            run_end = min(_current_run_end(out, now), now)
            if run_end > open_start:
                out["prior_runs"] = list(out.get("prior_runs") or []) + [[open_start, run_end, compute_all_in,
                                                                          compute_only]]
            out["compute_segment_start"] = max(open_start, run_end)
            disks = _disk_segments(out)
            disk_open = max([float(out.get("disk_start_epoch") or now)] + [e for _s, e, _r in disks]
                            + ([float(out["disk_segment_start"])] if _finite(out.get("disk_segment_start")) else []))
            if now > disk_open:
                out["disk_segments"] = [list(d) for d in disks] + [[disk_open, now, full_disk]]
            out["disk_segment_start"] = now
            out["retained_only_pricing"] = True
            out["pricing"] = _retained_pricing(
                out, tax_rate_percent=float(pricing.get("tax_rate_percent", 0.0) or 0.0), project=project)
    if out.get("status") == "RUNNING" or _instance_end_epoch(out) is None:
        # It ran until it disappeared (between two listings; the observation time bounds it).
        out["stopped_epoch"] = now
    out.update({"status": "DELETED", "deleted_epoch": now, "disk_liability_pending": bool(disks_pending)})
    usage = instance_usage(out, now)
    total_cost, compute_cost = round(usage["cost_all_in"], 2), round(usage["cost_compute"], 2)
    out.update({
        "status": "DELETED",
        "deleted_epoch": now,
        "deleted_at": _iso_epoch(now),
        "runtime_hours": round(usage["compute_hours"], 2),
        "total_cost_usd": total_cost,
        "total_cost_compute_only_usd": compute_cost,
        "deletion_trigger": (f"No longer in the Compute Engine inventory (first missing at {_iso_epoch(now)}): "
                             "deleted by Spot preemption, the autoscaler, or an operator"),
        "cpu_utilization_percent": 0.0,
        "ram_utilization_percent": 0.0,
    })
    intervals = normalize_unpriced_intervals(out.get("unpriced_history_intervals"))
    for ranges in intervals.values():
        for bounds in ranges:
            if bounds[1] is None:
                bounds[1] = max(float(bounds[0]), now)
    _save_unpriced_intervals(out, intervals, now)
    _sync_disk_ambiguity(out)
    _mark_pricing_completeness(out)
    return out


# Record fields recomputed from the clock on every listing; any other stored field changing is a real change.
_VOLATILE_RECORD_FIELDS = frozenset({"runtime_hours", "total_cost_usd", "total_cost_compute_only_usd"})


def _instance_fingerprint(record: Any) -> str:
    """Every stored field except the clock-derived ones, so any change that affects telemetry, pricing completeness,
    or history (worth a new state version and a KV publish) is detected."""
    if not isinstance(record, dict):
        return repr(record)
    return json.dumps({k: v for k, v in record.items() if k not in _VOLATILE_RECORD_FIELDS},
                      sort_keys=True, default=repr)


def _compact_deleted_record(record: dict[str, Any], buckets: dict[str, list[float]], now: float) -> None:
    """Fold a deleted record's whole cost history into role-scoped hour buckets ``"<role>|<day>"``."""
    prefix = f"{record.get('role', 'cicd_runner')}|"
    pricing = record.get("pricing") if isinstance(record.get("pricing"), dict) else {}
    cur_all_in, cur_compute, cur_disk = _pricing_rates(pricing)
    usage_end = float(record.get("deleted_epoch") or now)
    started = float(record.get("started_epoch") or usage_end)
    seg = record.get("compute_segment_start")
    open_start = max(started, float(seg)) if _finite(seg) else started
    for s0, e0, ca, co in _prior_runs(record):
        _add_to_buckets(buckets, prefix, s0, e0, ca if ca is not None else cur_all_in,
                            co if co is not None else cur_compute, compute=True)
    run_end = _current_run_end(record, usage_end)
    if run_end > open_start:
        _add_to_buckets(buckets, prefix, open_start, run_end, cur_all_in, cur_compute, compute=True)
    disks = _disk_segments(record)
    disk_open = max([float(record.get("disk_start_epoch") or started)] + [e for _s, e, _r in disks]
                    + ([float(record["disk_segment_start"])] if _finite(record.get("disk_segment_start")) else []))
    for s0, e0, r0 in disks:
        _add_to_buckets(buckets, prefix, s0, e0, r0, 0.0, compute=False)
    # Disks bill until their own end (a resolved liability can end after the VM was deleted).
    disk_end = float(record["disk_end_epoch"]) if _finite(record.get("disk_end_epoch")) else usage_end
    if disk_end > disk_open:
        _add_to_buckets(buckets, prefix, disk_open, disk_end, cur_disk, 0.0, compute=False)
    own = _rebucket(record.get("folded_cost_buckets") or {}, "", _record_bucket_span(record), _BUCKET_SECONDS,
                    until=max(usage_end, disk_end))
    for key, value in own.items():
        bucket = buckets.setdefault(f"{prefix}{key}", [0.0, 0.0, 0.0, 0.0])
        if len(bucket) == 3:
            bucket.append(0.0)
        for i in range(min(4, len(value))):
            bucket[i] += float(value[i])


def _remember_incomplete_pricing(record: dict[str, Any], pricing_record: dict[str, Any],
                                  old_pricing: dict[str, Any], compute_start: float, compute_end: float,
                                  disk_start: float, disk_end: float) -> None:
    """Save trust bounds with closed fallback-priced intervals, independently of the new prospective price."""
    if old_pricing.get("complete") is not False and pricing_record.get("pricing_complete") is not False             and not _tax_unknown(pricing_record):
        return
    compute_reasons, storage_reasons = _pricing_component_reasons(dict(pricing_record, pricing=old_pricing), historical=True)
    compute_unknown = bool(compute_reasons)
    # Suspension memory has its own exact interval; it does not invalidate the attached disks' rate.
    disk_unknown = any(reason != "suspended: preserved memory storage is not priced here" for reason in storage_reasons)
    # Per-disk debt is recoverable and its rates are corrected on recovery; don't turn that debt into a permanent
    # incomplete-history claim. Other fallback prices have no authoritative historical rate to repair them.
    debt_sources = set(pricing_record.get("deferred_disk_sources") or [])
    debt_sources.update(str(d.get("source")) for d in pricing_record.get("unpaid_disk_details") or []
                        if isinstance(d, dict))
    if _finite(pricing_record.get("disk_history_deferred_at")):
        debt_sources.update(pricing_record.get("disk_history_deferred_sources") or
            [str(d.get("source")) for d in pricing_record.get("disks_detail") or [] if isinstance(d, dict)])
    if debt_sources:
        details = [d for d in pricing_record.get("disks_detail") or []
                   if isinstance(d, dict) and d.get("billing_owner") is not False]
        paid = [d for d in details if str(d.get("source")) not in debt_sources
                and not _finite(d.get("billing_deferred_since"))]
        unknown_types = set(old_pricing.get("disk_rate_unknown") or [])
        independent_rates = any(str(d.get("type")) in unknown_types for d in paid) or any(
            kind not in {str(d.get("type")) for d in details} for kind in unknown_types)
        current_details = [d for d in record.get("disks_detail") or [] if isinstance(d, dict)]
        # A project-wide denied-listing flag does not invalidate an unchanged, previously identified disk merely
        # because another attachment has recoverable debt. On recovery, changed identities/types remain unknown.
        unverified_paid = any(not d.get("id") or (record.get("disk_types_known") is True and not any(
            str(current.get("id")) == str(d["id"]) and current.get("type") == d.get("type")
            and int(current.get("gb") or 0) == int(d.get("gb") or 0) for current in current_details)) for d in paid)
        disk_unknown = bool(independent_rates or "provisioned IOPS or throughput is not priced here" in storage_reasons
                            or "regional replicated disk storage is not priced here" in storage_reasons or pricing_record.get("local_ssd_gb")
                            or pricing_record.get("storage_billing_ambiguous")
                            or old_pricing.get("regional_disk_rate_unknown")
                            or (unverified_paid and pricing_record.get("disk_types_known") is False))
    intervals = normalize_unpriced_intervals(record.get("unpriced_history_intervals"))
    for reason, start, end, uncertain in (("fallback_compute", compute_start, compute_end, compute_unknown),
                                         ("fallback_storage", disk_start, disk_end, disk_unknown)):
        if not uncertain or end <= start:
            continue
        add_unpriced_interval(intervals, reason, start, end)
    if _tax_unknown(pricing_record):
        if (pricing_record.get("kind") != "disk" and float(old_pricing.get("compute_hourly_usd") or 0) > 0
                and compute_end > compute_start):
            add_unpriced_interval(intervals, "fallback_compute_tax", compute_start, compute_end)
        if float(old_pricing.get("disk_hourly_usd") or 0) > 0 and disk_end > disk_start:
            add_unpriced_interval(intervals, "fallback_storage_tax", disk_start, disk_end)
    _save_unpriced_intervals(record, intervals, max(compute_end, disk_end))


def _close_open_segments(record: dict[str, Any], old_pricing: dict[str, Any], now: float,
                           *, pricing_record: dict[str, Any] | None = None) -> None:
    """Close a live record's open compute and disk intervals at ``old_pricing``'s rates up to ``now``."""
    compute_all_in, compute_only, disk_all_in = _pricing_rates(old_pricing)
    started = float(record.get("started_epoch") or now)
    seg = record.get("compute_segment_start")
    open_start = max(started, float(seg)) if _finite(seg) else started
    run_end = min(_current_run_end(record, now), now)
    runs = record.setdefault("prior_runs", [])
    if run_end > open_start:
        runs.append([open_start, run_end, compute_all_in, compute_only])
    record["compute_segment_start"] = max(open_start, run_end)
    disks = _disk_segments(record)
    disk_open = max([float(record.get("disk_start_epoch") or started)] + [e for _s, e, _r in disks]
                    + ([float(record["disk_segment_start"])] if _finite(record.get("disk_segment_start")) else []))
    _remember_incomplete_pricing(record, pricing_record if pricing_record is not None else record,
                                  old_pricing, open_start, run_end, disk_open, now)
    if now > disk_open:
        record.setdefault("disk_segments", []).append([disk_open, now, disk_all_in])
    record["disk_segment_start"] = now


class InventoryTooLargeError(RuntimeError):
    """The live inventory cannot be compacted into the state size budget."""


def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), allow_nan=False))


def _fold_record_detail(record: dict[str, Any], keep: int) -> dict[str, Any]:
    """A live record with all but its newest ``keep`` runs and disk segments folded into its hour buckets."""
    out = dict(record)
    pricing = out.get("pricing") if isinstance(out.get("pricing"), dict) else {}
    cur_all_in, cur_compute, _disk = _pricing_rates(pricing)
    span = _record_bucket_span(out)
    folded = copy.deepcopy(out.get("folded_cost_buckets") or {})
    runs = sorted(_prior_runs(out), key=lambda run: run[0])
    fold_runs, keep_runs = (runs[:-keep], runs[-keep:]) if keep else (runs, [])
    for s0, e0, ca, co in fold_runs:
        _add_to_buckets(folded, "", s0, e0, ca if ca is not None else cur_all_in,
                        co if co is not None else cur_compute, compute=True, span=span)
    disks = sorted(_disk_segments(out), key=lambda seg: seg[0])
    # Aggregated duration cannot be unioned with later per-disk backfill. Retain exact
    # intervals until recovery, even if _fit_size_budget must refuse the oversized state.
    if _disk_history_recovery_pending(out):
        fold_disks, keep_disks = [], disks
    else:
        fold_disks, keep_disks = (disks[:-keep], disks[-keep:]) if keep else (disks, [])
    for s0, e0, r0 in fold_disks:
        _add_to_buckets(folded, "", s0, e0, r0, 0.0, compute=False, span=span)
    out["prior_runs"] = [[s0, e0] if ca is None else [s0, e0, ca, co] for s0, e0, ca, co in keep_runs]
    if disks:
        prior_start = out.get("disk_segment_start")
        out["disk_segment_start"] = max([end for _start, end, _rate in disks]
                                        + ([float(prior_start)] if _finite(prior_start) else []))
    out["disk_segments"] = [list(seg) for seg in keep_disks]
    out["folded_cost_buckets"] = folded
    return out


def _coarsen_record_buckets(record: dict[str, Any]) -> dict[str, Any]:
    """A live record whose folded buckets are merged into day buckets (windows then resolve to within a day)."""
    out = dict(record)
    out["folded_cost_buckets"] = _rebucket(out.get("folded_cost_buckets") or {}, "", _record_bucket_span(out),
                                           COARSE_BUCKET_SECONDS)
    out["folded_bucket_seconds"] = COARSE_BUCKET_SECONDS
    # Empty optional billing indexes have exactly the same meaning as absent indexes. Under size pressure,
    # omit that bookkeeping overhead while preserving every nonempty physical-disk identity and cost bucket.
    for key in ("billed_disk_sources", "billed_disk_source_bounds", "billed_disk_identified_sources"):
        if out.get(key) in ([], {}):
            out.pop(key, None)
    return out


def _fit_size_budget(live: list[dict[str, Any]], gone: list[dict[str, Any]], compacted: dict[str, list[float]],
                     now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compact until the records and compacted costs serialize within MAX_INSTANCE_STATE_BYTES: first the oldest
    deleted records (into ``compacted``), then the detailed history of live records. Window totals keep every
    compacted cost (to within an hour)."""
    size = _serialized_size([live, gone, compacted])
    while size > MAX_INSTANCE_STATE_BYTES and gone:
        batch = max(1, len(gone) // 10)
        for record in gone[-batch:]:
            _compact_deleted_record(record, compacted, now)
        gone = gone[:-batch]
        size = _serialized_size([live, gone, compacted])
    for keep in (MIN_DETAILED_SEGMENTS, 0):
        if size <= MAX_INSTANCE_STATE_BYTES:
            break
        live = [_fold_record_detail(r, keep) for r in live]
        size = _serialized_size([live, gone, compacted])
    if size > MAX_INSTANCE_STATE_BYTES:
        live = [_coarsen_record_buckets(r) for r in live]
        size = _serialized_size([live, gone, compacted])
    if size > MAX_INSTANCE_STATE_BYTES:
        # Never save (or publish) state that may not fit in KV: the refresh fails and the inventory reads as stale.
        raise InventoryTooLargeError(
            f"instance records are {size} bytes after compaction (budget {MAX_INSTANCE_STATE_BYTES})")
    return live, gone


def _portable_initial_disk_history(record: dict[str, Any], now: float, project: str) -> dict[str, Any]:
    """Move legacy unverified initial history onto disks, retaining credits for fallback costs already billed."""
    deferred_at = record.get("disk_history_deferred_at")
    if not _finite(deferred_at) or record.get("disk_types_known") is not False:
        return record
    details = [d for d in record.get("disks_detail") or [] if isinstance(d, dict)]
    sources = record.get("disk_history_deferred_sources")
    pending = [d for d in details if d.get("billing_owner") is not False
               and (not isinstance(sources, list) or str(d.get("source")) in sources)
               and not _finite(d.get("billing_deferred_since"))]
    if not pending:
        return record
    out = copy.deepcopy(record)
    pricing = out.get("pricing") or {}
    tax = float(pricing.get("tax_rate_percent") or 0.0)
    def rate_at(disk: dict[str, Any], at: float) -> float:
        context = _deferred_context_at(disk, at)
        if context is not None:
            return _deferred_disk_rate(disk, context, family=str(record.get("family") or ""),
                                       region=str(record.get("region") or ""), project=project)
        return _single_disk_all_in(disk, family=str(record.get("family") or ""),
            region=str(record.get("region") or ""), project=project, tax_rate_percent=tax)
    billed = [d for d in details if d.get("billing_owner") is not False
              and not _finite(d.get("billing_deferred_since"))]
    rates = {str(d.get("source")): rate_at(d, now) for d in billed}
    total = sum(rates.values())
    # Close the legacy fallback interval before removing its open rate. Its recorded charges stay on the old
    # carrier; recovery adds the authoritative full history less these credits, including in window queries.
    end = now if out.get("disk_liability_pending") else min(now, float(
        out.get("disk_end_epoch") or out.get("deleted_epoch") or now))
    _close_open_segments(out, pricing, end)
    segments = _disk_segments(out)
    pending_sources = {str(d.get("source")) for d in pending}
    for disk in out.get("disks_detail") or []:
        source = str(disk.get("source"))
        if source not in pending_sources or disk.get("billing_owner") is False:
            continue
        credits = []
        for start, finish, rate in segments:
            start = max(start, float(deferred_at))
            if finish <= start:
                continue
            bounds = sorted({start, finish} | {float(at) for detail in billed
                for at, _context in detail.get("billing_deferred_pricing") or [] if start < float(at) < finish})
            for begin, end in zip(bounds, bounds[1:]):
                total_at = sum(rate_at(detail, begin) for detail in billed)
                if total_at > 0:
                    credits.append([begin, end, rate * rate_at(disk, begin) / total_at])
        disk.update(billing_deferred_since=float(deferred_at), billing_initial_history=True,
                    billing_deferred_sizes=[[float(deferred_at), int(disk.get("gb") or 0)]],
                    billing_history_credits=credits)
    share = sum(rates[s] for s in pending_sources) / total if total > 0 else 0.0
    out["pricing"] = _with_extra_disks(pricing, -float(pricing.get("disk_hourly_usd") or 0.0) * share)
    out["deferred_disk_sources"] = sorted(set(out.get("deferred_disk_sources") or []) | pending_sources)
    remaining = set(sources or []) - pending_sources
    out["disk_history_deferred_sources"] = sorted(remaining)
    out["disk_history_deferred_at"] = deferred_at if remaining else None
    return out


def reconcile_instance_records(
    existing: list[Any],
    observed: list[dict[str, Any]],
    *,
    project: str,
    now: float,
    compacted: dict[str, list[float]] | None = None,
    tax_overrides: dict[str, float] | None = None,
    disks_listed: bool = True,
    disk_inventory: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Merge a complete Compute Engine fleet listing into the stored instance records.

    Returns ``(records, changed)``. A VM in the listing gets its live status (RUNNING, TERMINATED, STOPPED, ...);
    a stored record whose VM is missing becomes DELETED with the observation time as its deletion time; deleted
    records older than the retention window, and records that did not come from the inventory, are dropped.
    ``observed`` must be the complete listing: a partial one would mark the missing VMs deleted.
    """
    # Repricing may append to nested run and disk-segment arrays before persistence succeeds. Every working
    # record must be detached from self.state, including records preserved during denied disk listings.
    existing = [_portable_initial_disk_history(r, now, project) if is_live_instance_record(r) else r
                for r in copy.deepcopy(existing)]
    # A not-yet-priced disk can change billing owners while disk listing is unavailable. Carry its unpaid
    # interval with the physical attachment rather than losing it when the former owner becomes a non-owner.
    for record in existing:
        if is_live_instance_record(record):
            _sync_disk_ambiguity(record)
    deferred_entries = []
    for old_record in existing:
        if not is_live_instance_record(old_record):
            continue
        details = list(old_record.get("unpaid_disk_details") or [])
        if old_record.get("status") != "DELETED" or old_record.get("disk_liability_pending"):
            details += [d for d in old_record.get("disks_detail") or []
                        if isinstance(d, dict) and d.get("billing_owner") is not False]
        deferred_entries += [(str(old_record.get("instance_id")), d) for d in details
                             if isinstance(d, dict) and _finite(d.get("billing_deferred_since"))]
    deferred_details = [d for _carrier, d in deferred_entries]
    if disks_listed and disk_inventory:
        # Resolve only physical disks already tracked as unpaid, even when their names are outside fleet prefixes.
        roles = {str(r.get("instance_id")): r.get("role") for r in existing if isinstance(r, dict)}
        tracked_roles = {}
        tracked_ambiguities: dict[str, list[str]] = {}
        for record in existing:
            if (is_live_instance_record(record)
                    and (record.get("status") != "DELETED" or _has_unresolved_storage(record))):
                for disk in record.get("disks_detail") or []:
                    if isinstance(disk, dict) and disk.get("id") and disk.get("billing_owner") is not False:
                        key = "id:" + str(disk["id"])
                        tracked_roles[key] = record["role"]
                        evidence = _disk_ambiguous_roles(record, disk)
                        if evidence:
                            tracked_ambiguities[key] = sorted(set(tracked_ambiguities.get(key, [])) | set(evidence))
        attached = frozenset(str(d.get("source")) for obs in observed if isinstance(obs, dict)
                             for d in obs.get("disks_detail") or [] if isinstance(d, dict))
        for source, detail in disk_inventory.items():
            if not isinstance(detail, dict):
                continue
            disk = {"source": source, "id": detail.get("id", ""),
                    "created_epoch": gce_epoch(detail.get("creationTimestamp"))}
            found = next(((carrier, unpaid) for carrier, unpaid in deferred_entries if _same_disk(disk, unpaid)), None)
            if found is not None and roles.get(found[0]):
                carrier, unpaid = found
                key = "id:" + str(detail.get("id", ""))
                tracked_roles[key] = roles[carrier]
                evidence = unpaid.get("storage_billing_ambiguous_roles") or []
                if evidence:
                    tracked_ambiguities[key] = sorted(set(tracked_ambiguities.get(key, [])) | set(evidence))
        recovered = observe_detached_disks(disk_inventory, attached, tracked_roles=tracked_roles,
                                           tracked_ambiguities=tracked_ambiguities)
        recovered_ids = {obs["instance_id"] for obs in recovered}
        # An external owner ends fleet billing, but complete metadata can still settle the unpaid fleet interval.
        for source, detail in disk_inventory.items():
            if (not isinstance(detail, dict) or not detail.get("users")
                    or "id:" + str(detail.get("id", "")) not in tracked_roles
                    or "disk-" + str(detail.get("id", "")) in recovered_ids or source in attached):
                continue
            historical = observe_detached_disks({source: dict(detail, users=[])}, tracked_roles=tracked_roles,
                                                tracked_ambiguities=tracked_ambiguities)
            recovered.extend(dict(obs, disk_history_only=True) for obs in historical)
        known_ids = {str(obs.get("instance_id")) for obs in observed if isinstance(obs, dict)}
        observed = list(observed) + [obs for obs in recovered if obs["instance_id"] not in known_ids
                                   and "id:" + obs["instance_id"].removeprefix("disk-") in tracked_roles]
    # Active disk-only owners and unresolved retained liabilities carry the same physical metadata as a VM.
    # Final deleted records cannot prove a later same-named disk's identity or rate.
    trusted_disks = [d for r in existing if is_live_instance_record(r)
                     and (r.get("status") != "DELETED" or r.get("disk_liability_pending"))
                     for d in r.get("disks_detail") or [] if isinstance(d, dict)
                     and (r.get("status") != "DELETED" or d.get("auto_delete") is not True)
                     and not _finite(d.get("billing_deferred_since"))
                     and (r.get("disk_types_known") is not False or d.get("id"))]
    normalized = []
    for obs in observed:
        if not isinstance(obs, dict):
            normalized.append(obs)
            continue
        details = []
        for disk in obs.get("disks_detail") or []:
            if not isinstance(disk, dict):
                continue
            if obs.get("disk_types_known") is False:
                trusted = next((d for d in trusted_disks if _same_disk(disk, d)
                                and int(d.get("gb") or 0) == int(disk.get("gb") or 0)
                                and (not _finite(disk.get("created_epoch")) or not _finite(d.get("created_epoch"))
                                     or float(disk["created_epoch"]) == float(d["created_epoch"]))), None)
                if trusted is not None:
                    disk = {**disk, **{k: trusted[k] for k in ("type", "id", "created_epoch")
                                      if trusted.get(k) not in (None, "")}}
                    if trusted.get("storage_billing_ambiguous_roles"):
                        disk = dict(disk, storage_billing_ambiguous_roles=sorted(
                            set(disk.get("storage_billing_ambiguous_roles") or []) |
                            set(trusted["storage_billing_ambiguous_roles"])))
            candidates = [d for d in deferred_details if _same_disk(disk, d)]
            if candidates and disk.get("billing_owner") is not False:
                earliest = min(candidates, key=lambda d: float(d["billing_deferred_since"]))
                if (obs.get("disk_types_known") is False and earliest.get("id")
                        and int(earliest.get("gb") or 0) == int(disk.get("gb") or 0)):
                    disk = {**disk, **{key: copy.deepcopy(earliest[key]) for key in (
                        "id", "type", "created_epoch", "unpriced_disk_performance", "regional")
                        if key in earliest}}
                disk = dict(disk, billing_deferred_since=float(earliest["billing_deferred_since"]),
                            billing_deferred_sizes=copy.deepcopy(earliest.get("billing_deferred_sizes")
                                or [[float(earliest["billing_deferred_since"]), int(earliest.get("gb") or 0)]]))
                for key in ("billing_initial_history", "billing_history_credits", "billing_deferred_pricing"):
                    if key in earliest:
                        disk = dict(disk, **{key: copy.deepcopy(earliest[key])})
                if obs.get("disk_types_known") is False and earliest.get("storage_billing_ambiguous_roles"):
                    disk = dict(disk, storage_billing_ambiguous_roles=sorted(
                        set(disk.get("storage_billing_ambiguous_roles") or []) |
                        set(earliest["storage_billing_ambiguous_roles"])))
            details.append(disk)
        if obs.get("disk_types_known") is False and details and all("boot" in d for d in details):
            billed_data: dict[str, int] = {}
            boot = 0
            for disk in details:
                if disk.get("billing_owner") is False:
                    continue
                if disk.get("boot"):
                    boot += int(disk.get("gb") or 0)
                    obs = dict(obs, boot_disk_type=disk.get("type"))
                else:
                    kind = str(disk.get("type") or "pd-balanced")
                    billed_data[kind] = billed_data.get(kind, 0) + int(disk.get("gb") or 0)
            obs = dict(obs, data_disks=billed_data, boot_disk_billed_gb=boot)
        normalized.append(dict(obs, disks_detail=details))
    observed = normalized
    owners = [d for obs in observed if isinstance(obs, dict) for d in obs.get("disks_detail") or []
              if isinstance(d, dict) and d.get("billing_owner") is not False]
    current_vm_ids = {str(obs.get("instance_id")) for obs in observed if isinstance(obs, dict) and obs.get("kind") != "disk"}
    if not disks_listed:
        # A known disk removed from a still-existing VM may keep billing while the disk listing is unavailable.
        # Its last paid boundary is this observation, not its first historical membership in another record.
        for old_record in existing:
            if not is_live_instance_record(old_record) or old_record.get("kind") == "disk":
                continue
            if str(old_record.get("instance_id")) not in current_vm_ids:
                continue  # Missing VMs already keep their retained-disk liability through the deletion path.
            for disk in old_record.get("disks_detail") or []:
                if (isinstance(disk, dict) and disk.get("source") and disk.get("billing_owner") is not False
                        and not _finite(disk.get("billing_deferred_since"))
                        and not any(_same_disk(disk, owned) for owned in owners)):
                    deferred_entries.append((str(old_record["instance_id"]), dict(disk, billing_deferred_since=now,
                                             billing_deferred_sizes=[[now, int(disk.get("gb") or 0)]])))
    catalog_disks = owners if disk_inventory is None else [
        {"source": source, "id": detail.get("id", ""), "created_epoch": gce_epoch(detail.get("creationTimestamp"))}
        if isinstance(detail, dict) else {"source": source} for source, detail in disk_inventory.items()]
    unassigned = []
    expired_carriers: set[str] = set()
    for carrier, disk in deferred_entries:
        if any(_same_disk(disk, owned) for owned in owners) or any(_same_disk(disk, old) for _id, old in unassigned):
            continue
        disk = dict(disk)
        if disks_listed:
            if any(_same_disk(disk, listed) for listed in catalog_disks):
                disk.pop("billing_missing_since", None)
            else:
                disk.setdefault("billing_missing_since", now)
        ended = disk.get("billing_missing_since")
        if disks_listed and _finite(ended) and now - float(ended) >= DELETED_INSTANCE_RETENTION_SECONDS:
            expired_carriers.add(carrier)
        else:
            unassigned.append((carrier, disk))
    by_id: dict[str, dict[str, Any]] = {}
    for obs in observed:
        if isinstance(obs, dict) and obs.get("instance_id") and obs.get("name"):
            by_id[str(obs["instance_id"])] = obs
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Disks the observed VMs have attached (IDs and sources), so a detached record can be retired at the handoff.
    observed_disk_keys = {key for obs in observed if isinstance(obs, dict) and obs.get("kind") != "disk"
                          for d in obs.get("disks_detail") or [] if isinstance(d, dict)
                          for key in (_disk_key(d), str(d.get("source") or "")) if key}
    # Disks some stored record already bills (or billed): a new record that takes one over starts it at the handoff.
    known_disks = frozenset(key for r in existing if is_live_instance_record(r)
                            and (r.get("status") != "DELETED" or _has_unresolved_storage(r))
                            for key in _billed_record_disk_keys(r))
    for record in existing:
        if not is_live_instance_record(record):
            continue
        instance_id = str(record.get("instance_id", ""))
        if instance_id in seen:
            continue
        obs = by_id.get(instance_id)
        if obs is not None:
            seen.add(instance_id)
            records.append(_live_instance_record(
                obs, project=project, now=now, previous=record, tax_overrides=tax_overrides,
                known_disk_sources=frozenset(
                    key for other in existing if is_live_instance_record(other)
                    and str(other.get("instance_id", "")) != instance_id
                    for key in _billed_record_disk_keys(other))))
        elif not disks_listed and record.get("kind") == "disk" and record.get("status") != "DELETED":
            if set(record.get("disk_sources") or []) & observed_disk_keys:
                # Even without the disk listing, a listed VM shows this disk attached: its record takes it over.
                records.append(_mark_instance_deleted(record, now))
            else:
                # The disk listing is unavailable: a detached disk is not seen, not gone. Keep it (still billing).
                records.append(record)
        elif record.get("status") == "DELETED" and record.get("disk_liability_pending"):
            attached = [d for observed_vm in observed if isinstance(observed_vm, dict)
                        and observed_vm.get("kind") != "disk"
                        for d in observed_vm.get("disks_detail") or [] if isinstance(d, dict)]
            retained = _retained_disks(record)
            moved = [d for d in retained or [] if any(
                _disk_known(current_disk, _record_disk_keys(dict(record, disks_detail=[d],
                    disk_sources=[_disk_key(d), str(d.get("source") or "")],
                    disk_ids={str(d.get("source")): d["id"]} if d.get("id") else {})))
                for current_disk in attached)]
            transferred = bool(retained) and len(moved) == len(retained)
            if not disks_listed and moved and not transferred:
                out = copy.deepcopy(record)
                pricing = out.get("pricing") or {}
                _close_open_segments(out, pricing, now)
                out["disks_detail"] = [dict(d, billing_owner=False) if d in moved else d
                                       for d in out.get("disks_detail") or []]
                out["pricing"] = _retained_pricing(out, project=project,
                    tax_rate_percent=float(pricing.get("tax_rate_percent") or 0.0))
                records.append(out)
            elif disks_listed or transferred:
                # The disk listing is back: the cache disk is either listed on its own now (its DETACHED record
                # takes over from this moment) or gone. Close the liability's rate and its trust bounds together.
                _close_open_segments(record, record.get("pricing") or {}, now)
                records.append(dict(record, disk_liability_pending=False, disk_end_epoch=now))
            else:
                records.append(record)
        elif record.get("status") == "DELETED":
            deleted = record.get("deleted_epoch")
            if _finite(record.get("disk_end_epoch")):
                deleted = max(float(deleted or 0), float(record["disk_end_epoch"]))
            if record.get("unpaid_disk_details") or (isinstance(deleted, (int, float))
                    and now - float(deleted) <= DELETED_INSTANCE_RETENTION_SECONDS):
                records.append(record)
        else:
            # A builder's cache disk outlives its VM (auto-delete=no); while disks cannot be listed nobody can tell
            # whether it still exists, so its storage keeps billing here until a disk listing resolves it.
            pending = (not disks_listed and str(record.get("kind") or "vm") != "disk"
                       and _retained_disks(record) != [])
            out = _mark_instance_deleted(record, now, disks_pending=pending, project=project)
            retained = _retained_disks(out) if pending else []
            moved = [d for d in retained or [] if any(_same_disk(d, owned) for owned in owners)]
            if moved:
                pricing = out.get("pricing") or {}
                _close_open_segments(out, pricing, now)
                out["disks_detail"] = [dict(d, billing_owner=False) if d in moved else d
                                       for d in out.get("disks_detail") or []]
                out["pricing"] = _retained_pricing(out, project=project,
                    tax_rate_percent=float(pricing.get("tax_rate_percent") or 0.0))
                if len(moved) == len(retained):
                    out.update(disk_liability_pending=False, disk_end_epoch=now)
            records.append(out)
    for instance_id, obs in by_id.items():
        if instance_id not in seen:
            records.append(_live_instance_record(obs, project=project, now=now, known_disk_sources=known_disks,
                                                 tax_overrides=tax_overrides))
    history_only_ids = {str(obs.get("instance_id")) for obs in observed
                        if isinstance(obs, dict) and obs.get("disk_history_only")}
    records = [_mark_instance_deleted(record, now, project=project)
               if str(record.get("instance_id")) in history_only_ids else record for record in records]
    # An unpaid interval outlives an attachment. Keep it on its original carrier until another observation can
    # recover the same physical disk, even if that carrier no longer has the disk attached.
    records = [dict(record, unpaid_disk_details=[copy.deepcopy(d) for carrier, d in unassigned
                                               if carrier == str(record.get("instance_id"))],
                    unpriced_lifetime_history=bool(record.get("unpriced_lifetime_history"))
                        or str(record.get("instance_id")) in expired_carriers) for record in records]
    for record in records:
        if str(record.get("instance_id")) in expired_carriers:
            record["unpriced_storage_lifetime_history"] = True
        if record.get("status") == "DELETED":
            # The receiving owner consumed this debt. Do not leave a false lifetime hold on the old carrier,
            # or replay its initial-history credits if that record is revived later.
            record["disks_detail"] = [
                {k: v for k, v in dict(d, billing_owner=False).items()
                 if k not in ("billing_deferred_since", "billing_deferred_sizes", "billing_initial_history",
                              "billing_history_credits", "billing_deferred_pricing")}
                if _finite(d.get("billing_deferred_since")) and any(_same_disk(d, owner) for owner in owners)
                else d for d in record.get("disks_detail") or []]
            record["deferred_disk_sources"] = [str(d.get("source")) for d in record.get("disks_detail") or []
                                               if _finite(d.get("billing_deferred_since"))]
        _sync_disk_ambiguity(record)
        _mark_pricing_completeness(record)
    # Live VMs first (newest start first), then deleted history (newest deletion first), bounded in size.
    live = sorted((r for r in records if r.get("status") != "DELETED"),
                  key=lambda r: -float(r.get("started_epoch") or 0.0))
    gone = sorted((r for r in records if r.get("status") == "DELETED" and not r.get("disk_liability_pending") and not r.get("unpaid_disk_details")
                   and not _unpriced_history_in_window(r, now - DELETED_INSTANCE_RETENTION_SECONDS, now)),
                  key=lambda r: -float(r.get("deleted_epoch") or 0.0))
    # Records still billing an unresolved disk stay detailed (never compacted) until resolved.
    live += [r for r in records if r.get("status") == "DELETED"
             and (r.get("disk_liability_pending") or r.get("unpaid_disk_details")
                  or _unpriced_history_in_window(r, now - DELETED_INSTANCE_RETENTION_SECONDS, now))]
    # Every record inside the retention period counts in every accepted window (at most MAX_WINDOW_HOURS). Past
    # MAX_DETAILED_INSTANCE_RECORDS the oldest deleted ones are folded into ``compacted`` hour buckets, so the
    # state stays far below the KV value limit while window totals keep their cost.
    keep = max(0, MAX_DETAILED_INSTANCE_RECORDS - len(live))
    if compacted is not None and len(gone) > keep:
        for record in gone[keep:]:
            _compact_deleted_record(record, compacted, now)
        gone = gone[:keep]
    if compacted is not None:
        _prune_buckets(compacted, now - DELETED_INSTANCE_RETENTION_SECONDS)
        live, gone = _fit_size_budget(live, gone, compacted, now)
    records = live + gone
    changed = [_instance_fingerprint(r) for r in existing] != [_instance_fingerprint(r) for r in records]
    return records, changed


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
            "created_at": _iso_hours_ago(18.0, ref_now),
        },
        {
            "id": BUILDER_PRESET_ID,
            "name": "Dedicated Rust Builder N4-HighCPU-16 (Intel Emerald Rapids)",
            "role": "rust_builder",
            "machine_type": "n4-highcpu-16",
            "vcpu": 16,
            "ram_gib": 32,
            "cpu_platform": "Intel Emerald Rapids",
            "benchmark_score": compute_cpu_benchmark_score("Intel Emerald Rapids", 16, "n4-highcpu-16"),
            "disk_category": "hyperdrive",
            "boot_disk_type": "hyperdisk-balanced",
            "boot_disk_gb": 40,
            "data_disk_type": "hyperdisk-balanced",
            "data_disk_gb": 100,
            "runners_count": 1,
            "runner_vcpu": 16,
            "runner_ram_gib": 28,
            "cloud_init_template_id": "ci-rust-builder-199",
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
            "vm_template_id": BUILDER_PRESET_ID,
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

    # Instances come only from the live Compute Engine inventory (see fleet_inventory and
    # DashboardStore.refresh_fleet_inventory); the dashboard never invents VM records.
    instances: list[dict[str, Any]] = []

    # Telemetry starts empty: GitHub CI runs, journal OOM events, job counters, and Rust builds are recorded only
    # when reported (update_scaling, record_rust_build). Demo records earlier versions seeded here are dropped on
    # load (see drop_seeded_demo_telemetry).
    scaling_telemetry: dict[str, Any] = {
        "current_runners": 0,
        "jobs_ran_past_hour": 0,
        "provisioned_jobs": 0,
        "queued_jobs": 0,
        "instance_cpu_percentages": [],
        "journal_oom_events": [],
        "github_oom_runs": [],
    }
    rust_builds: list[dict[str, Any]] = []

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
        # Fresh states hold no seeded demo telemetry or template sources; see drop_seeded_demo_telemetry and
        # drop_seeded_template_sources.
        DEMO_TELEMETRY_MIGRATION_KEY: DEMO_TELEMETRY_MIGRATION_VERSION,
        DEMO_TEMPLATE_SOURCE_MIGRATION_KEY: DEMO_TEMPLATE_SOURCE_MIGRATION_VERSION,
        BUILDER_PRESET_MIGRATION_KEY: BUILDER_PRESET_MIGRATION_VERSION,
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
        fleet_source: Any = None,
        fleet_inventory_interval: float = FLEET_INVENTORY_INTERVAL_SECONDS,
    ) -> None:
        self._lock = threading.RLock()
        self.state_path = state_path
        # Read-only live inventory (fleet_inventory.gce_fleet_source); None leaves the instance list empty.
        self.fleet_source = fleet_source
        self.fleet_inventory_interval = float(fleet_inventory_interval)
        self._fleet_status: dict[str, Any] = {
            "source": "gce" if fleet_source is not None else "none",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_change_at": None,
            "observed_count": None,
            "last_error": "",
            # True only while the most recent attempt listed the complete fleet; scaling holds otherwise.
            "fresh": False,
        }
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
        # Local load, KV hydration, recovery and control-plane import share this path.
        # Retain legacy limits for repair/budget edits; qualification and plans fail closed.
        for section in ("profiles", "scaling"):
            try:
                validate_builder_container_limits(base.get(section, {}).get("rust_builder", {}))
            except ValueError as exc:
                LOG.warning("Retained incompatible legacy builder %s: %s", section, exc)
        instances = base.get("instances")
        if isinstance(instances, list):
            # Malformed (non-object) entries stay so that validation below rejects the whole state.
            kept = [r for r in instances if not isinstance(r, dict) or is_live_instance_record(r)]
            if getattr(self, "fleet_source", None) is None:
                # Without a live source nothing would ever update these records: showing them would be stale.
                kept = [r for r in kept if not isinstance(r, dict)]
                # The baseline belonged to the dropped records; a later re-enabled inventory starts a new one.
                base.pop(FLEET_BASELINE_STATE_KEY, None)
                base.pop(COMPACTED_COSTS_STATE_KEY, None)
            if len(kept) != len(instances):
                # Demo seed records and VMs fabricated by the old in-dashboard lifecycle actions never existed in
                # Compute Engine; only inventory records survive a load.
                LOG.warning("Dropped %d dashboard instance record(s) that did not come from the live inventory",
                            len(instances) - len(kept))
            base["instances"] = kept
        dropped_seeds = 0
        if not demo_telemetry_migrated(loaded):
            # A state written before the migration: drop its seeds once (a newer marker is kept as it is).
            dropped_seeds = drop_seeded_demo_telemetry(base, source_state=loaded)
            base[DEMO_TELEMETRY_MIGRATION_KEY] = DEMO_TELEMETRY_MIGRATION_VERSION
            # Adding the marker changes content even when nothing matched (an operator may have cleared the seeds):
            # a copy without it at the same version would look current to KV and to a rollback, and losing the
            # one-time marker would let a later upgrade rerun the cleanup. Commit it as a new version either way.
            base[DEMO_TELEMETRY_MIGRATION_PENDING_KEY] = True
        if base.get(DEMO_TELEMETRY_MIGRATION_PENDING_KEY) is not True:
            base.pop(DEMO_TELEMETRY_MIGRATION_PENDING_KEY, None)
        if dropped_seeds:
            LOG.warning("Dropped %d seeded demo telemetry record(s) and counter(s) from the dashboard state",
                        dropped_seeds)
        template_marker = loaded.get(DEMO_TEMPLATE_SOURCE_MIGRATION_KEY)
        if not (isinstance(template_marker, int) and not isinstance(template_marker, bool)
                and template_marker >= DEMO_TEMPLATE_SOURCE_MIGRATION_VERSION):
            dropped_sources = drop_seeded_template_sources(base)
            base[DEMO_TEMPLATE_SOURCE_MIGRATION_KEY] = DEMO_TEMPLATE_SOURCE_MIGRATION_VERSION
            # The one-time marker must also reach KV when no source pairs needed removal.
            base[DEMO_TELEMETRY_MIGRATION_PENDING_KEY] = True
            if dropped_sources:
                LOG.warning("Removed the nonexistent source instance from %d preset VM template(s)", dropped_sources)
        preset_marker = loaded.get(BUILDER_PRESET_MIGRATION_KEY)
        if not (isinstance(preset_marker, int) and not isinstance(preset_marker, bool)
                and preset_marker >= BUILDER_PRESET_MIGRATION_VERSION):
            base[BUILDER_PRESET_MIGRATION_KEY] = BUILDER_PRESET_MIGRATION_VERSION
            # Like the template marker, the new marker must reach KV even when nothing else changed.
            base[DEMO_TELEMETRY_MIGRATION_PENDING_KEY] = True
            if migrate_builder_preset(base, self._validate_vm_template_compatibility):
                LOG.warning("Replaced the c4d-highcpu-16 builder preset %s with %s (n4-highcpu-16)",
                            LEGACY_BUILDER_PRESET_ID, BUILDER_PRESET_ID)
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
        if dropped_seeds:
            LOG.warning("Dropped %d seeded demo telemetry record(s) from the dashboard state",
                        dropped_seeds)

    def commit_demo_telemetry_migration(self) -> bool:
        """Persist seeded telemetry or template-source cleanup as a new state_version; True when it did.

        The drop changes content without a version, so a slot holding the seeded copy at the same version would
        look caught up (to a rollback, or to KV, which never takes different content at an equal version). The
        commit goes through the same gates as every change, so only the active, hydrated, reconciled, unfrozen
        slot advances the version; elsewhere it waits (a standby that bumped could shadow the active slot's next
        change), and a handover import replaces the state anyway. The pending flag is part of the state, so it
        survives a restart between the load (whose write-back keeps the loaded version) and the commit.
        """
        if self.state.get(DEMO_TELEMETRY_MIGRATION_PENDING_KEY) is not True:
            return False

        def apply() -> tuple[int, Any]:
            if self.state.get(DEMO_TELEMETRY_MIGRATION_PENDING_KEY) is not True:
                return 200, {"changed": False}
            self._persist()  # a new version clears the pending flag
            return 200, {"changed": True}

        try:
            _status, body, _replayed = self.run_request(None, "POST", "/internal/demo-telemetry-migration", apply)
        except DashboardFrozenError:
            return False
        except OSError as exc:
            LOG.warning("Could not commit the demo telemetry migration: %s", exc)
            return False
        if body.get("changed"):
            LOG.warning("Committed the demo telemetry migration as state_version %d", self.state_version())
            return True
        return False

    def state_version(self) -> int:
        version = self.state.get("state_version", 0)
        return version if isinstance(version, int) and not isinstance(version, bool) and version >= 0 else 0

    def start(self) -> None:
        """Hydrate (in a background thread when KV is configured), start KV write-behind and the inventory loop."""
        if self.kv is None:
            self.hydrate()
            self.commit_demo_telemetry_migration()
        else:
            thread = threading.Thread(target=self._background_main, name="dashboard-kv-sync", daemon=True)
            self._threads.append(thread)
            thread.start()
        if self.fleet_source is not None:
            inventory = threading.Thread(target=self._fleet_inventory_main, name="dashboard-fleet-inventory",
                                         daemon=True)
            self._threads.append(inventory)
            inventory.start()

    def _fleet_inventory_main(self) -> None:
        while not self._stop_event.is_set():
            if not self.hydrated:
                self._stop_event.wait(min(5.0, self.fleet_inventory_interval))
                continue
            try:
                self.refresh_fleet_inventory()
            except Exception:  # noqa: BLE001 - the inventory loop must keep running
                LOG.exception("Dashboard fleet inventory refresh failed")
            self._stop_event.wait(self.fleet_inventory_interval)

    def _consume_ooms_for_fleet_growth(self, previous: list[Any], current: list[dict[str, Any]]) -> None:
        """Mark runner OOM events handled once the live fleet gained ready runner capacity.

        Builder OOM events are never handled from the inventory: a dedicated builder runs without a service
        account, so nothing in its instance record shows that its build container came up. They stay active until
        an operator acknowledges them.
        """

        def ready_slots(records: list[Any]) -> int:
            return sum(int(r.get("runners_count", 0) or 0) for r in records
                       if isinstance(r, dict) and r.get("status") == "RUNNING" and r.get("role") == "cicd_runner"
                       and r.get("runners_count_known") is not False)

        previous_slots = ready_slots(previous)
        current_slots = ready_slots(current)
        # Each runner OOM event keeps the ready slots from when the reconcile first saw it, so capacity that only
        # comes back (a restart, or a replacement in this or a later listing) never reads as growth.
        def event_epoch(evt: dict[str, Any]) -> float | None:
            raw = evt.get("failed_at") or evt.get("observed_at") or evt.get("timestamp")
            try:
                parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00")) if raw else None
            except ValueError:
                return None
            if parsed is None:
                return None
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()

        previous_ids = {str(r.get("instance_id")) for r in previous if isinstance(r, dict)}
        pending_ready = [r for r in previous if isinstance(r, dict) and r.get("capacity_pending")
                         and r.get("status") == "RUNNING" and r.get("role") == "cicd_runner"
                         and r.get("runners_count_known") is not False]
        cfg = {**default_scaling_config()["cicd_runner"], **(self.state.get("scaling", {}).get("cicd_runner") or {})}
        lookback_seconds = max(60.0, float(cfg.get("oom_lookback_minutes", 60) or 60) * 60.0)
        now = self._clock()
        for evt in self._runner_oom_events():
            # Only events active for scaling (unhandled, inside the lookback) need a capacity baseline.
            if not _is_recent_unhandled_oom(evt, now, lookback_seconds):
                continue
            # The capacity running when the OOM happened: a late report must not count capacity that started after
            # it (still pending) as part of its baseline. Lowers a baseline the scaling evaluation set from the
            # already-grown count.
            when = event_epoch(evt)
            later = sum(int(r.get("runners_count", 0) or 0) for r in pending_ready
                        if when is not None and float(r["capacity_first_ready_lower_bound"] if _finite(r.get("capacity_first_ready_lower_bound"))
                                                       else r.get("started_epoch") or 0.0) > when)
            # Hosts first seen in this same listing that were already running before the OOM were part of the
            # capacity that failed, not an answer to it.
            earlier_new = sum(int(r.get("runners_count", 0) or 0) for r in current
                              if isinstance(r, dict) and str(r.get("instance_id")) not in previous_ids
                              and r.get("status") == "RUNNING" and r.get("role") == "cicd_runner"
                              and r.get("runners_count_known") is not False
                              and when is not None and float(r.get("started_epoch") or now) <= when)
            # A replaced/stopped host can still have been part of the fleet when a late OOM occurred.
            # Include that historical capacity instead of treating its replacement as net growth.
            ended_after_event = sum(int(r.get("runners_count", 0) or 0) for r in previous
                                    if isinstance(r, dict) and r.get("role") == "cicd_runner"
                                    and r.get("status") != "RUNNING" and r.get("runners_count_known") is not False
                                    and when is not None and float(r.get("started_epoch") or now) <= when
                                    and (_instance_end_epoch(r) is None or _instance_end_epoch(r) >= when))
            computed = max(0, previous_slots - later + earlier_new + ended_after_event)
            existing = evt.get("baseline_runners")
            preexisting_ready = sum(int(r.get("runners_count", 0) or 0) for r in current
                                    if r.get("status") == "RUNNING" and r.get("role") == "cicd_runner"
                                    and r.get("runners_count_known") is not False and when is not None
                                    and _finite(r.get("capacity_first_ready_lower_bound"))
                                    and float(r["capacity_first_ready_lower_bound"]) <= when)
            inferred = (int(existing) if evt.get("baseline_source") == "inventory" and isinstance(existing, int)
                        else min(int(existing), computed) if isinstance(existing, int) else computed)
            evt["baseline_runners"] = max(preexisting_ready, inferred)
            evt["baseline_source"] = "inventory"
        # Only VMs first seen after the baseline add capacity (capacity_pending), and only once ready: RUNNING with
        # a bootstrap confirmed in this boot. A restart of capacity that already existed adds none.
        new_ready = [r for r in current if isinstance(r, dict) and r.get("capacity_pending")
                     and r.get("status") == "RUNNING" and r.get("role") == "cicd_runner"
                     and r.get("runners_count_known") is not False]
        if new_ready:
            pol = self.state.get("scaling", {}).get("cicd_runner", {})
            prof = self.state.get("profiles", {}).get("cicd_runner", {})
            self._consume_active_oom_events(
                role="cicd_runner",
                previous_runners=previous_slots,
                current_runners=current_slots,
                step_runners=max(1, int(pol.get("scale_up_step_runners", 2))),
                max_runners=max(int(prof.get("min_runners", 1)), int(pol.get("max_runners", 24))),
                recent_within=lookback_seconds,
                new_capacity_epoch=max(float(r["capacity_first_ready_lower_bound"])
                                       if _finite(r.get("capacity_first_ready_lower_bound")) else 0.0
                                       for r in new_ready),
            )
        # New capacity stays pending through the OOM lookback (an OOM reported late can still be answered by it)
        # and while an OOM it could still satisfy is active; after that it is ordinary capacity.
        cfg = {**default_scaling_config()["cicd_runner"], **(self.state.get("scaling", {}).get("cicd_runner") or {})}
        lookback_seconds = max(60.0, float(cfg.get("oom_lookback_minutes", 60) or 60) * 60.0)
        now = self._clock()
        if not self._active_oom_events("cicd_runner"):
            for record in new_ready:
                if now - float(record.get("started_epoch") or now) > lookback_seconds:
                    record["capacity_pending"] = False
        for record in current:
            if isinstance(record, dict) and record.get("role") == "rust_builder":
                record["capacity_pending"] = False

    def _runner_oom_events(self) -> list[dict[str, Any]]:
        """Unhandled runner OOM events (GitHub OOM runs and journal events on runner hosts)."""
        telem = self.state.get("scaling_telemetry", {})
        events = [evt for evt in (telem.get("github_oom_runs") or [])
                  if isinstance(evt, dict) and _is_eligible_github_oom(evt)]
        events += [evt for evt in (telem.get("journal_oom_events") or []) if isinstance(evt, dict)
                   and str(evt.get("role", "")).strip().lower() != "rust_builder"
                   and not str(evt.get("instance_name", "")).strip().lower().startswith("builder-")]
        return [evt for evt in events if not evt.get("handled")]

    def _active_oom_events(self, role: str) -> bool:
        """True while an OOM event of the role is active for scaling: unhandled and inside the policy's lookback
        window (the same test the scaling evaluation applies)."""
        telem = self.state.get("scaling_telemetry", {})
        cfg = {**default_scaling_config()[role], **(self.state.get("scaling", {}).get(role) or {})}
        lookback_seconds = max(60.0, float(cfg.get("oom_lookback_minutes", 60) or 60) * 60.0)
        now = self._clock()

        def builder_event(evt: dict[str, Any]) -> bool:
            return (str(evt.get("role", "")).strip().lower() == "rust_builder"
                    or str(evt.get("instance_name", "")).strip().lower().startswith("builder-"))

        events: list[Any] = list(telem.get("journal_oom_events") or [])
        if role == "cicd_runner":
            events += [evt for evt in telem.get("github_oom_runs") or []
                       if isinstance(evt, dict) and _is_eligible_github_oom(evt)]
        else:
            events += list(telem.get("builder_journal_oom_events") or [])
        return any(
            isinstance(evt, dict) and _is_recent_unhandled_oom(evt, now, lookback_seconds)
            and (builder_event(evt) if role == "rust_builder" else not builder_event(evt))
            for evt in events
        )

    def _runner_burn_for_builder_budget(self, cicd_eval: dict[str, Any], cicd_qual: dict[str, Any] | None = None) -> float:
        """The runner fleet's burn as the builder's shared budget sees it: running runners plus any runner VMs the
        CI evaluation adds, all priced with the builder budget's disk and tax flags (stopped storage is added
        separately by _outside_burn)."""
        running = sum(self._running_role_hourly_rates("cicd_runner", budget_role="rust_builder"))
        added = max(0, int(cicd_eval.get("desired_instances") or 0) - int(cicd_eval.get("current_instances") or 0))
        if not added:
            return round(running, 4)
        unit = self._resolve_scaling_unit_hourly_usd("cicd_runner", cicd_qual, budget_role="rust_builder")
        return round(running + added * unit, 4)

    def _builder_evaluation_telemetry(self) -> dict[str, Any]:
        """Telemetry for the builder evaluation. With a live inventory the running builder count says nothing about
        working build capacity (builders report no readiness), so the evaluation gets a copy without builder OOM
        baselines: it can never mark a builder OOM handled from that count, and the events stay active until an
        operator acknowledges them."""
        telem = self.state.get("scaling_telemetry", {})
        if self.fleet_source is None:
            return telem
        view = copy.deepcopy(telem)
        for key in ("journal_oom_events", "builder_journal_oom_events"):
            for evt in view.get(key) or []:
                if isinstance(evt, dict):
                    evt.pop("baseline_instances", None)
        return view

    def _tax_overrides(self) -> dict[str, float]:
        """``{role: tax_rate_percent}`` for profiles whose budget overrides the regional tax rate."""
        out = {}
        for role, prof in (self.state.get("profiles") or {}).items():
            budget = prof.get("budget") if isinstance(prof, dict) else None
            if isinstance(budget, dict) and budget.get("override_regional_tax"):
                out[role] = float(budget.get("tax_rate_percent", 0.0) or 0.0)
        return out

    def fleet_inventory_status(self) -> dict[str, Any]:
        """Where the instance list comes from and how fresh it is (in memory; not part of the snapshot). ``fresh``
        is recomputed (a hung refresh ages out) and ``stale_reason`` says why scaling holds, if it does."""
        with self._lock:
            status = dict(self._fleet_status)
            reason = self._inventory_stale_reason()
        status["fresh"] = not reason
        status["stale_reason"] = reason
        return status

    def refresh_fleet_inventory(self) -> bool:
        """List the live fleet (read-only) and reconcile the stored instance records with it.

        Returns True when the instance records changed (and were persisted as a new state version). Runs only
        where changes are accepted (hydrated, not frozen, reconciled, the active slot): elsewhere the listing is
        skipped and the stored records stay as the active slot last published them.
        """
        if self.fleet_source is None:
            return False
        now = self._clock()
        with self._lock:
            self._fleet_status["last_attempt_at"] = _iso_epoch(now)
        try:
            # Check eligibility before listing, so a frozen, unreconciled, or inactive slot makes no cloud calls.
            self._mutation_preflight()
            with self._lock:
                self._mutation_locked_checks()
        except DashboardFrozenError as exc:
            with self._lock:
                self._fleet_status["last_error"] = f"skipped: {exc}"
                self._fleet_status["fresh"] = False
            return False
        try:
            listing = self.fleet_source()
            disks_listed = bool(getattr(listing, "disks_listed", True))
            observed = list(listing)
            disk_inventory = getattr(listing, "disk_inventory", None)
        except Exception as exc:  # noqa: BLE001 - keep the last known records; report the failure
            with self._lock:
                self._fleet_status["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
                self._fleet_status["fresh"] = False
            LOG.warning("Dashboard fleet inventory listing failed: %s: %s", type(exc).__name__, exc)
            return False

        def apply() -> tuple[int, Any]:
            compacted = copy.deepcopy(self.state.get(COMPACTED_COSTS_STATE_KEY) or {})
            records, changed = reconcile_instance_records(
                self.state.get("instances", []), observed, project=self.project, now=self._clock(),
                compacted=compacted, tax_overrides=self._tax_overrides(), disks_listed=disks_listed,
                disk_inventory=disk_inventory)
            changed = changed or compacted != (self.state.get(COMPACTED_COSTS_STATE_KEY) or {})
            # Current prices and tax settings, applied here (and so persisted) rather than on reads.
            changed = self._refresh_instances_pricing(records, live=True) or changed
            # The first successful listing (after an upgrade, a migration, or enabling the inventory) is the
            # baseline: the fleet it finds already existed, so it is not growth that handles an OOM event.
            baselined = bool(self.state.get(FLEET_BASELINE_STATE_KEY))
            previous_instances = self.state.get("instances", [])
            previous_telemetry = copy.deepcopy(self.state.get("scaling_telemetry", {}))
            if baselined:
                # OOM telemetry can arrive while the fleet is quiet: reconcile it on every listing, and save when
                # it changed something (an event handled, a baseline recorded, a pending marker cleared).
                try:
                    self._consume_ooms_for_fleet_growth(previous_instances, records)
                except Exception:
                    self.state["scaling_telemetry"] = previous_telemetry
                    raise
                changed = (changed or self.state.get("scaling_telemetry") != previous_telemetry
                           or [_instance_fingerprint(r) for r in previous_instances]
                           != [_instance_fingerprint(r) for r in records])
            if not changed and baselined:
                return 200, {"changed": False}
            previous_compacted = self.state.get(COMPACTED_COSTS_STATE_KEY)
            self.state["instances"] = records
            self.state[COMPACTED_COSTS_STATE_KEY] = compacted
            try:
                if baselined:
                    pass  # OOM state was reconciled above.
                else:
                    self.state[FLEET_BASELINE_STATE_KEY] = _iso_epoch(self._clock())
                    for record in records:
                        record["capacity_pending"] = False  # The baseline fleet already existed.
                self._sync_fleet_telemetry()
                self._persist()
            except Exception:
                self.state["instances"] = previous_instances
                self.state["scaling_telemetry"] = previous_telemetry
                self.state[COMPACTED_COSTS_STATE_KEY] = previous_compacted
                if not baselined:
                    self.state.pop(FLEET_BASELINE_STATE_KEY, None)
                raise
            return 200, {"changed": changed}

        try:
            _status, body, _replayed = self.run_request(None, "POST", "/internal/fleet-inventory", apply)
        except DashboardFrozenError as exc:
            with self._lock:
                self._fleet_status["last_error"] = f"not applied: {exc}"
                self._fleet_status["fresh"] = False
            return False
        except Exception as exc:  # noqa: BLE001 - for example a full disk: the stored records are not current
            with self._lock:
                self._fleet_status["last_error"] = f"not applied: {type(exc).__name__}: {exc}"[:500]
                self._fleet_status["fresh"] = False
            LOG.warning("Dashboard fleet inventory could not be applied: %s: %s", type(exc).__name__, exc)
            return False
        done = self._clock()  # Freshness counts from when the listing was applied, not when it started.
        with self._lock:
            self._fleet_status.update({"last_success_at": _iso_epoch(done), "last_success_epoch": done,
                                       "disks_listed": disks_listed,
                                       "observed_count": len(observed),
                                       "fresh": True,
                                       "last_error": ""})
            if body.get("changed"):
                self._fleet_status["last_change_at"] = _iso_epoch(done)
        return bool(body.get("changed"))

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
        if self.hydrated and not self.frozen and self.kv_writer:
            self.commit_demo_telemetry_migration()
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
            inst_name = str(build.get("instance_name") or "")
            zone = str(build.get("zone") or "")
            if inst_name and zone:
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
            try:
                validate_builder_container_limits(b_prof)
            except ValueError:
                # A budget edit/load must not re-render incompatible legacy container limits.
                return
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
                    # A build stamped at the report's arrival can round a microsecond past ref_ts.
                    if -1.0 <= age_sec <= 3600.0:
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
        if vm_tpl.get("shared_attachments") or vm_tpl.get("storage_billing_ambiguous"):
            raise ValueError("Shared or ambiguous disk attachments cannot be used as a VM template source")
        if role == "rust_builder":
            validate_builder_container_limits(profile)
            validate_builder_container_limits(vm_tpl)
            if not builder_shape_compatible(vm_tpl):
                raise ValueError("Dedicated builders require n4-highcpu-16 (16 vCPU / 32 GiB)")
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

    def _running_role_hourly_rates(self, role: str, *, budget_role: str | None = None, usable_only: bool = False) -> list[float]:
        """Return per-instance hourly USD rates for all currently RUNNING instances of the given role, under the
        disk and tax flags of ``budget_role``'s budget (default: the role's own)."""
        prof = self.state.get("profiles", {}).get(budget_role or role, {})
        pol = self.state.get("scaling", {}).get(budget_role or role, {})
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
            and not (usable_only and role == "rust_builder" and inst.get("runner_limits_known") is False)
        ]
        return [
            self._instance_hourly_for_budget(
                inst.get("pricing", {}),
                include_disk=include_disk,
                include_tax=include_tax,
            )
            for inst in running
        ]

    def _role_shares_budget(self, role: str) -> bool:
        return bool(self.state.get("scaling", {}).get(role, {}).get("shared_global_budget")
                    or self.state.get("global_budget_enforced"))

    def _incomplete_cost_hosts(self, role: str) -> list[str]:
        """Inventory VMs (of the role, and of the other role under a shared budget) whose cost is understated."""
        roles = {role, "rust_builder" if role == "cicd_runner" else "cicd_runner"} if self._role_shares_budget(role) \
            else {role}
        def budget_of(name: Any) -> dict[str, Any]:
            budget = self.state.get("profiles", {}).get(name, {}).get("budget", {})
            return budget if isinstance(budget, dict) else {}

        evaluated = budget_of(role)
        now = time.time()
        includes_storage = bool(self.state.get("scaling", {}).get(role, {}).get("include_disk_in_scale_budget",
                                                                            evaluated.get("include_disk_costs", True)))

        def tax_unknown(inst: dict[str, Any]) -> bool:
            # Regional tax uncertainty matters only to the evaluated budget. Component rates may independently
            # be trusted, and the owning profile can supply an explicit tax rate.
            own = budget_of(inst.get("role"))
            includes = bool(evaluated.get("include_taxes", False))
            return _selected_tax_unknown(inst, include_storage=includes_storage) and includes and not own.get("override_regional_tax")

        return [
            str(inst.get("name")) for inst in self.state.get("instances", [])
            if is_live_instance_record(inst) and (inst.get("role") in roles or
                 (includes_storage and inst.get("storage_billing_ambiguous")))
            and (inst.get("status") != "DELETED" or _has_unresolved_storage(inst)
                 or _unpriced_history_in_window(inst, now - DELETED_INSTANCE_RETENTION_SECONDS, now))
            and (not _selected_pricing_complete(inst, include_storage=includes_storage) or tax_unknown(inst)
                 or _unpriced_history_in_window(inst, now - DELETED_INSTANCE_RETENTION_SECONDS, now,
                     include_storage=includes_storage, include_tax=bool(evaluated.get("include_taxes", False))))
        ]

    def _transitional_hosts(self, role: str) -> list[str]:
        """The role's inventory VMs in a transitional GCE state (their capacity is about to change)."""
        return [str(inst.get("name")) for inst in self.state.get("instances", [])
                if _is_transitional(inst) and inst.get("role") == role]

    def _held(self, evaluation: dict[str, Any], reason: str, role: str) -> dict[str, Any]:
        # A held recommendation projects the fleet it keeps: its running VMs plus the storage its stopped VMs and
        # detached disks keep billing. The other role's burn under a shared budget is already taken off the cap.
        projected = round(sum(self._running_role_hourly_rates(role)) + self._stopped_disk_burn(role), 4)
        return {
            **evaluation,
            "direction": "hold",
            "desired_runners": evaluation.get("current_runners", evaluation.get("desired_runners")),
            "desired_instances": evaluation.get("current_instances", evaluation.get("desired_instances")),
            "scale_up_reasons": [],
            "scale_down_reasons": [],
            "hold_reason": reason,
            "projected_hourly_usd": projected,
            "projected_daily_usd": round(projected * 24.0, 2),
            "projected_monthly_usd": round(projected * 24.0 * 365.0 / 12.0, 2),
        }

    def _inventory_stale_reason(self) -> str:
        """Why the live inventory cannot be trusted right now (empty when it can); without a configured inventory
        the fleet is unknown, so scaling holds there too."""
        if self.fleet_source is None:
            return ("The live Compute Engine inventory is not configured (DASHBOARD_FLEET_INVENTORY=gce), so the "
                    "fleet and its burn are unknown")
        with self._lock:
            status = dict(self._fleet_status)
        last_success = status.get("last_success_epoch")
        max_age = FLEET_INVENTORY_MAX_AGE_INTERVALS * self.fleet_inventory_interval
        if status.get("fresh") and status.get("disks_listed") is False:
            # Without the disk listing, fleet disks that no VM uses (a builder cache disk after its VM is gone)
            # still bill but cannot be seen.
            return ("The disk listing is unavailable (compute.disks.list), so disks that outlive their VM cannot "
                    "be counted")
        if status.get("fresh") and _finite(last_success) and self._clock() - float(last_success) <= max_age:
            return ""
        detail = status.get("last_error") or "no complete listing yet"
        if status.get("fresh"):
            # A refresh that never returns (for example a hung API connection) must not keep old counts trusted.
            detail = f"no complete listing for more than {int(max_age)} seconds"
        return f"The live inventory is not current ({detail}); fleet counts and burn may be out of date"

    def _fail_closed_evaluation(self, evaluation: dict[str, Any], role: str) -> dict[str, Any]:
        """Hold recommendations that cannot be checked: a stale inventory or unknown runner totals (any change),
        or an understated fleet cost (scale-ups, whose budget check would pass on the low figure)."""
        if role == "rust_builder":
            try:
                validate_builder_container_limits(self.state.get("profiles", {}).get(role, {}))
                validate_builder_container_limits(self.state.get("scaling", {}).get(role, {}))
            except ValueError as exc:
                return self._held(evaluation, str(exc), role)
        stale = self._inventory_stale_reason()
        if stale:
            return self._held(evaluation, stale, role)
        if role == "cicd_runner":
            unknown = self.state.get("scaling_telemetry", {}).get("unknown_runner_count_hosts") or []
            if unknown:
                return self._held(evaluation, f"Runner count not recorded on {len(unknown)} running host(s) "
                                              f"({', '.join(unknown[:5])}); slot totals are unknown", role)
        pending = self._transitional_hosts(role)
        if pending:
            return self._held(evaluation, f"{len(pending)} VM(s) ({', '.join(pending[:5])}) are starting, stopping, "
                                          "or repairing; their capacity is not settled yet", role)
        incomplete = self._incomplete_cost_hosts(role)
        if incomplete and evaluation.get("direction") == "scale_up":
            return self._held(evaluation, f"Cost of {len(incomplete)} VM(s) ({', '.join(incomplete[:5])}) is not fully "
                                          "priced, so the scale-up budget check cannot be trusted", role)
        return evaluation

    def _stopped_disk_burn(self, role: str, *, budget_role: str | None = None) -> float:
        """Hourly USD of the role's not-running inventory VMs (disks of stopped ones, full rate of transitional
        ones), under the disk and tax flags of ``budget_role``'s budget (default: the role's own)."""
        flags_role = budget_role or role
        prof = self.state.get("profiles", {}).get(flags_role, {})
        pol = self.state.get("scaling", {}).get(flags_role, {})
        budget = prof.get("budget", {}) if isinstance(prof.get("budget"), dict) else {}
        include_disk = bool(pol.get("include_disk_in_scale_budget", budget.get("include_disk_costs", True)))
        include_tax = bool(budget.get("include_taxes", False))
        return round(sum(
            _nonrunning_hourly(inst, include_disk=include_disk, include_tax=include_tax)
            for inst in self.state.get("instances", [])
            if is_live_instance_record(inst) and inst.get("role") == role
            and (inst.get("status") not in ("RUNNING", "DELETED") or inst.get("disk_liability_pending"))
        ), 4)

    def _outside_burn(self, role: str, *, shared: bool, other_running_burn: float) -> float | None:
        """Burn a role's scaling budget must leave room for: its stopped VMs' disks, plus (under a shared budget)
        the other role's running fleet and stopped disks. None when there is none and the budget is not shared."""
        other_role = "rust_builder" if role == "cicd_runner" else "cicd_runner"
        own = self._stopped_disk_burn(role)
        if role == "rust_builder" and self.fleet_source is not None:
            # Incompatible hosts add no usable fixed-shape slots, but their compute and storage still bill.
            own += max(0.0, sum(self._running_role_hourly_rates(role))
                       - sum(self._running_role_hourly_rates(role, usable_only=True)))
        if not shared:
            return own if own > 0.0 else None
        # Everything charged against this role's shared budget is priced with this budget's disk and tax flags.
        return round(own + other_running_burn + self._stopped_disk_burn(other_role, budget_role=role), 4)

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
            and inst.get("runner_limits_known") is not False
        ]
        telem = self.state.setdefault("scaling_telemetry", {})
        # Ready runner slots only: a host whose bootstrap is unconfirmed has registered nothing yet.
        telem["current_runners"] = sum(int(inst.get("runners_count", 0)) for inst in running_runners
                                       if inst.get("runners_count_known") is not False)
        # Hosts whose runner count is not recorded: slot totals are unknown, so slot-based decisions hold.
        telem["unknown_runner_count_hosts"] = [
            str(inst.get("name")) for inst in running_runners if inst.get("runners_count_known") is False
        ]
        telem["current_instances"] = len(running_runners)
        # Inventory records carry no utilization: a made-up 0% would read as idle and recommend scale-in.
        telem["instance_cpu_percentages"] = [
            float(inst.get("cpu_utilization_percent", 0.0)) for inst in running_runners
            if inst.get("utilization_known", True)
        ]
        telem["running_instance_hourly_rates"] = self._running_role_hourly_rates("cicd_runner")
        telem["current_builder_instances"] = len(running_builders)
        telem["builder_cpu_percentages"] = [
            float(inst.get("cpu_utilization_percent", 0.0)) for inst in running_builders
            if inst.get("utilization_known", True)
        ]
        telem["builder_instance_hourly_rates"] = self._running_role_hourly_rates("rust_builder", usable_only=True)
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
        budget_role: str | None = None,
    ) -> float:
        """Resolve per-instance hourly USD for scaling evaluation matching active template or density (under the
        disk and tax flags of ``budget_role``'s budget, default the role's own)."""
        prof = self.state.get("profiles", {}).get(role, {})
        pol = self.state.get("scaling", {}).get(role, {})
        flags_prof = self.state.get("profiles", {}).get(budget_role or role, {})
        flags_pol = self.state.get("scaling", {}).get(budget_role or role, {})
        budget = flags_prof.get("budget", {})
        include_disk = bool(
            flags_pol.get("include_disk_in_scale_budget", budget.get("include_disk_costs", True))
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
        if bump:
            # Every new version carries the content without the seeds, which is all the pending commit waited for.
            candidate_state.pop(DEMO_TELEMETRY_MIGRATION_PENDING_KEY, None)
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
            self.state.pop(DEMO_TELEMETRY_MIGRATION_PENDING_KEY, None)
            self._mark_kv_dirty()

    def _remember_repriced_history(self, inst: dict[str, Any], previous: dict[str, Any]) -> None:
        now = self._clock()
        _remember_incomplete_pricing(inst, previous, previous.get("pricing") or {},
            float(previous.get("compute_segment_start") or previous.get("started_epoch") or now),
            min(now, _current_run_end(previous, now)),
            float(previous.get("disk_segment_start") or previous.get("disk_start_epoch") or now),
            min(now, float(previous.get("disk_end_epoch") or
                (previous.get("deleted_epoch") if not previous.get("disk_liability_pending") else None) or now)))
        _mark_pricing_completeness(inst)

    def _refresh_instances_pricing(self, records: list[Any] | None = None, *, live: bool = False) -> bool:
        """Recompute active instance pricing using current live/catalog machine rates while preserving historical rates on deleted instances.

        Reads (``live=False``) reprice only records that did not come from the inventory. Inventory records
        (``live=True``, stopped ones included: their disks keep billing at current tax rates) are repriced only
        by the inventory reconcile, which persists the change: a rate change first closes the open intervals at
        the old rates, so it must be durable. Returns True when an inventory record's rates changed.
        """
        source_records = self.state.get("instances", []) if records is None else records
        running_insts = [
            inst
            for inst in source_records
            if isinstance(inst, dict)
            and (is_live_instance_record(inst)
                 and (inst.get("status") != "DELETED" or bool(inst.get("disk_liability_pending"))) if live
                 else inst.get("status") == "RUNNING" and not is_live_instance_record(inst))
            and inst.get("region")
            # Disk-only records (DETACHED) have no machine type but still reprice their storage.
            and (inst.get("machine_type") or (live and inst.get("kind") == "disk"))
        ]
        debt_only = [inst for inst in source_records if live and is_live_instance_record(inst)
                     and inst.get("unpaid_disk_details") and inst.get("region")
                     and not any(inst is active for active in running_insts)]
        debt_only_ids = {id(inst) for inst in debt_only}
        running_insts += debt_only
        repriced = False
        if not running_insts:
            return repriced
        active_regions = sorted({str(inst["region"]) for inst in running_insts})
        active_mtypes = sorted({str(inst["machine_type"]) for inst in running_insts if inst.get("machine_type")})
        shared_live_pricing = fetch_live_spot_pricing(
            project=self.project,
            regions=active_regions,
            machine_types=active_mtypes,
        )
        for inst in running_insts:
            if live and _finite(inst.get("disk_history_deferred_at")) and inst.get("disk_types_known") is False:
                portable = _portable_initial_disk_history(inst, self._clock(), self.project)
                if portable != inst:
                    inst.clear()
                    inst.update(portable)
                    repriced = True
            cur_pricing = inst.get("pricing") if isinstance(inst.get("pricing"), dict) else {}
            inst_role = str(inst.get("role", "cicd_runner"))
            prof_budget = (
                self.state.get("profiles", {})
                .get(inst_role, {})
                .get("budget", {})
            )
            reg_info = next(
                (r for r in REGIONS_CATALOG if r["region"] == str(inst["region"])),
                # Outside the catalog no regional default exists: neutral, never another region's.
                {"tax_rate_default": 0.0} if is_live_instance_record(inst) else REGIONS_CATALOG[0],
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
            old_pricing = inst.get("pricing") if isinstance(inst.get("pricing"), dict) else None
            old_completeness = (inst.get("pricing_complete"), inst.get("pricing_incomplete_reason"))
            old_record = copy.deepcopy(inst) if is_live_instance_record(inst) else None
            if is_live_instance_record(inst):
                if isinstance(prof_budget, dict) and prof_budget.get("override_regional_tax"):
                    inst["tax_rate_overridden"] = True
                else:
                    inst.pop("tax_rate_overridden", None)
                context_record = dict(inst, pricing=dict(cur_pricing, tax_rate_percent=tax_rate_pct))
                repriced = _snapshot_deferred_pricing(context_record, self._clock(), self.project,
                                                      shared_live_pricing) or repriced
            tax_trust_changed = old_record is not None and _tax_unknown(old_record) != _tax_unknown(inst)
            if id(inst) in debt_only_ids:
                continue  # Closed VM rates remain historical; only its unresolved disk context advances.
            if inst.get("retained_only_pricing"):
                # A deleted builder still billing its cache disk: reprice that disk alone (compute is closed).
                new_pricing = _retained_pricing(inst, tax_rate_percent=tax_rate_pct, project=self.project,
                                                live_pricing=shared_live_pricing)
                if old_pricing is not None and (_pricing_rates(old_pricing) != _pricing_rates(new_pricing) or tax_trust_changed):
                    _close_open_segments(inst, old_pricing, self._clock(), pricing_record=old_record)
                    repriced = True
                elif old_pricing != new_pricing:
                    repriced = True
                inst["pricing"] = new_pricing
                if old_record is not None and (old_pricing != new_pricing or tax_trust_changed):
                    self._remember_repriced_history(inst, old_record)
                _mark_pricing_completeness(inst)
                continue
            inst["pricing"] = calculate_spot_pricing(
                family=str(inst.get("family") or str(inst["machine_type"]).split("-", 1)[0]),
                vcpu=int(inst.get("vcpu", 16)),
                ram_gib=float(inst.get("ram_gib", 64)),
                region=str(inst["region"]),
                boot_disk_type=str(inst.get("boot_disk_type", "hyperdisk-balanced")),
                boot_disk_gb=int(inst.get("boot_disk_billed_gb", inst.get("boot_disk_gb", 40))),
                data_disk_type=str(inst.get("data_disk_type", "hyperdisk-balanced")),
                data_disk_gb=_primary_data_disk_gb(inst) if is_live_instance_record(inst)
                else int(inst.get("data_disk_gb", 100)),
                tax_rate_percent=tax_rate_pct,
                machine_type=str(inst["machine_type"]),
                live_pricing=shared_live_pricing,
                project=self.project,
                provisioning="ondemand" if inst.get("spot") is False else "spot",
            )
            if is_live_instance_record(inst):
                # Keep each data-disk type at its own rate, as the inventory reconcile priced it.
                inst["pricing"] = _with_secondary_disks(
                    inst["pricing"], inst, region=str(inst["region"]),
                    family=str(inst.get("family") or str(inst["machine_type"]).split("-", 1)[0]),
                    project=self.project, live_pricing=shared_live_pricing)
                _mark_pricing_completeness(inst)
                if old_record is not None and (old_pricing != inst["pricing"] or tax_trust_changed):
                    self._remember_repriced_history(inst, old_record)
                if old_pricing is not None and (_pricing_rates(old_pricing) != _pricing_rates(inst["pricing"]) or tax_trust_changed):
                    # New rates apply from now on: time already billed keeps the rates it had.
                    _close_open_segments(inst, old_pricing, self._clock(), pricing_record=old_record)
                    repriced = True
                elif old_pricing != inst["pricing"] or old_completeness != (
                        inst.get("pricing_complete"), inst.get("pricing_incomplete_reason")):
                    # Same rates, but the price's source or trust changed: save it (budgets rely on completeness).
                    repriced = True
        return repriced

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
            running_builder_rates = self._running_role_hourly_rates("rust_builder", usable_only=self.fleet_source is not None)
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
            scaling_eval = self._fail_closed_evaluation(evaluate_scaling_decision(
                self.state["scaling"]["cicd_runner"],
                self.state["scaling_telemetry"],
                per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd(
                    "cicd_runner", cicd_qual
                ),
                max_instances=int(
                    self.state["profiles"]["cicd_runner"].get("max_instances", 8)
                ),
                other_role_hourly_usd=self._outside_burn(
                    "cicd_runner", shared=ci_shared,
                    # The other role's running VMs, priced with this (the evaluated) budget's flags.
                    other_running_burn=sum(self._running_role_hourly_rates("rust_builder", budget_role="cicd_runner"))),
            ), "cicd_runner")
            builder_scaling_eval = self._fail_closed_evaluation(evaluate_builder_scaling_decision(
                self.state["scaling"]["rust_builder"],
                self._builder_evaluation_telemetry(),
                per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd(
                    "rust_builder", builder_qual
                ),
                other_role_hourly_usd=self._outside_burn(
                    "rust_builder",
                    shared=builder_shared,
                    other_running_burn=self._runner_burn_for_builder_budget(scaling_eval, cicd_qual),
                ),
            ), "rust_builder")
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
                "fleet_inventory": self.fleet_inventory_status(),
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
            # History (deleted VMs, earlier runs) is kept for the retention period, so no longer window is offered.
            window_hours = min(MAX_WINDOW_HOURS, max(0.5, _finite_float(window_hours, "window_hours", min_val=0.0)))
            window_start_ts = now_ts - window_hours * 3600.0
            q = (search_filter or "").strip().lower()
            r_filter = (role_filter or "").strip().lower()

            rows: list[dict[str, Any]] = []
            exact_window_costs: list[float] = []
            for inst in self.state["instances"]:
                is_deleted = inst["status"] != "RUNNING"
                # A stopped inventory VM still exists (and bills its disks): only DELETED is "deleted" here.
                hidden_as_deleted = (inst["status"] == "DELETED" and not _has_unresolved_storage(inst)
                                     if is_live_instance_record(inst) else is_deleted)
                if hidden_as_deleted and not include_deleted:
                    continue
                deleted_ts = _instance_end_epoch(inst)
                started_ts = float(inst.get("started_epoch", now_ts - 3600.0))
                end_ts = float(deleted_ts) if (is_deleted and deleted_ts is not None) else now_ts
                if is_live_instance_record(inst):
                    # Disks bill until deletion: a stopped VM stays in every window until it is deleted, and a
                    # deleted one stays in the windows that include its deletion (not just its last stop).
                    deleted_epoch = inst.get("deleted_epoch")
                    end_ts = (float(deleted_epoch) if inst["status"] == "DELETED" and isinstance(deleted_epoch, (int, float))
                              and not _has_unresolved_storage(inst) else now_ts)
                    if _finite(inst.get("disk_end_epoch")):
                        end_ts = max(end_ts, float(inst["disk_end_epoch"]))

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
                window_cost_exact = total_cost_in_window
                if is_live_instance_record(inst):
                    # Inventory records: compute accrues over every running interval and disks while the VM exists,
                    # each at the rates recorded for it.
                    usage = instance_usage(inst, now_ts, window_start_ts)
                    hours_in_window = round(usage["compute_hours_in_window"], 2)
                    live_runtime_hours = round(usage["compute_hours"], 2)
                    window_cost_exact = usage["window_all_in"] if include_disk_and_tax else usage["window_compute"]
                    total_cost_in_window = round(window_cost_exact, 2)
                    live_total_cost_usd = round(usage["cost_all_in"], 2)
                    live_total_cost_compute_only_usd = round(usage["cost_compute"], 2)
                    lifetime_cost = (live_total_cost_usd if include_disk_and_tax
                                     else live_total_cost_compute_only_usd)
                    if inst["status"] == "DELETED" and inst.get("disk_liability_pending"):
                        # Gone, but its retained disks may still bill: show that storage rate until resolved.
                        hourly_rate = round(_stopped_disk_hourly(pricing) if include_disk_and_tax else 0.0, 4)
                        daily_rate = round(hourly_rate * 24.0, 2)
                        monthly_rate = round(hourly_rate * 24.0 * 365.0 / 12.0, 2)
                    elif inst["status"] == "DELETED":
                        # Gone with its disks: no current rate (its history keeps its costs).
                        hourly_rate, daily_rate, monthly_rate = 0.0, 0.0, 0.0
                    elif inst["status"] in GCE_STOPPED_STATUSES:
                        # A stopped VM bills only its disks now (nothing when disk costs are excluded).
                        hourly_rate = round(_stopped_disk_hourly(pricing) if include_disk_and_tax else 0.0, 4)
                        daily_rate = round(hourly_rate * 24.0, 2)
                        monthly_rate = round(hourly_rate * 24.0 * 365.0 / 12.0, 2)

                row = {
                    **inst,
                    "runtime_hours": live_runtime_hours,
                    "total_cost_usd": live_total_cost_usd,
                    "total_cost_compute_only_usd": live_total_cost_compute_only_usd,
                    "active_hourly_usd": hourly_rate,
                    "active_daily_usd": daily_rate,
                    "active_monthly_usd": monthly_rate,
                    "compute_pricing_complete": not _pricing_component_reasons(inst)[0],
                    "storage_pricing_complete": not _pricing_component_reasons(inst)[1],
                    "window_cost_complete": bool(_selected_pricing_complete(inst, include_storage=include_disk_and_tax, include_tax=include_disk_and_tax)
                        and not _unpriced_history_in_window(inst, window_start_ts, now_ts,
                                                           include_storage=include_disk_and_tax)),
                    "hours_in_window": hours_in_window,
                    "total_cost_in_window_usd": total_cost_in_window,
                    "lifetime_cost_selected_mode_usd": lifetime_cost,
                    "lifetime_selected_cost_complete": bool(_selected_pricing_complete(inst, include_storage=include_disk_and_tax, include_tax=include_disk_and_tax)
                        and not _lifetime_history_components(inst, include_tax=include_disk_and_tax)[0]
                        and not (include_disk_and_tax and _lifetime_history_components(inst, include_tax=True)[1])),
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
                # Unrounded, so the fleet total does not lose sub-cent costs of many short-lived VMs.
                exact_window_costs.append(window_cost_exact)

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
            # Confirmed (ready) runner slots only; hosts whose bootstrap is unconfirmed are counted separately.
            running_runner_slots = sum(int(r.get("runners_count", 0)) for r in running_runner_rows
                                       if r.get("runners_count_known") is not False)
            unconfirmed_runner_hosts = sum(1 for r in running_runner_rows if r.get("runners_count_known") is False)
            if include_deleted and not q:
                # Deleted VMs compacted into hour buckets (see MAX_DETAILED_INSTANCE_RECORDS) still count.
                compacted_prefixes = ([f"{r_filter}|"] if r_filter and r_filter != "all"
                                      else ["cicd_runner|", "rust_builder|"])
                for prefix in compacted_prefixes:
                    _h, c_all_in, c_compute = _bucket_window_costs(
                        self.state.get(COMPACTED_COSTS_STATE_KEY), window_start_ts, now_ts, prefix)
                    exact_window_costs.append(c_all_in if include_disk_and_tax else c_compute)
            total_window_spend = round(sum(exact_window_costs), 2)
            # A stopped inventory VM still bills its attached disks (with tax) until it is deleted; a transitional
            # one (provisioning, stopping, repairing, ...) counts at its full rate.
            stopped_disk_burn = sum(
                _nonrunning_hourly(r, include_disk=include_disk_and_tax, include_tax=include_disk_and_tax)
                for r in rows
                if is_live_instance_record(r)
                and (r["status"] not in ("RUNNING", "DELETED") or r.get("disk_liability_pending"))
            )
            active_hourly_burn = round(
                sum(r["active_hourly_usd"] for r in running_rows) + stopped_disk_burn, 4
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
                "unconfirmed_runner_hosts": unconfirmed_runner_hosts,
                "running_builder_vms": len(running_builder_rows),
                "window_cost_complete": all(r.get("window_cost_complete", True) for r in rows),
                "total_window_spend_usd": total_window_spend,
                "active_hourly_burn_usd": active_hourly_burn,
                "active_daily_burn_usd": round(active_hourly_burn * 24.0, 2),
                "active_monthly_burn_usd": round(active_hourly_burn * 24.0 * (365.0 / 12.0), 2),
                "instances": rows,
                "fleet_inventory": self.fleet_inventory_status(),
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
            prev_instances = copy.deepcopy(self.state["instances"])
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
                # Inventory records follow the new tax settings in this same saved change.
                self._refresh_instances_pricing(live=True)
                self._persist()
            except Exception:
                self.state["instances"] = prev_instances
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
        if role == "rust_builder":
            validate_builder_container_limits(payload)
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
            template_fields = {"runner_vcpu", "runner_ram_gib", "host_overhead_gib", "min_instance_vcpu",
                               "min_instance_ram_gib", "min_benchmark_score", "disk_category", "disk_type",
                               "boot_disk_gb", "data_disk_gb", "use_vm_template", "vm_template_id",
                               "cloud_init_template_id"}
            if (candidate.get("use_vm_template") and candidate.get("vm_template_id")
                    and (role != "rust_builder" or template_fields.intersection(payload))):
                validated_tpl_units = self._validate_vm_template_compatibility(active_tpl, candidate, role)

            qual = qualify_instances(
                candidate,
                self.state["regions_enabled"],
                self.project,
                region_latencies_ms=self.state.get("region_latencies_ms"),
            )

            prev_profiles = copy.deepcopy(self.state["profiles"])
            prev_instances = copy.deepcopy(self.state["instances"])
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
                    # Inventory records follow the new tax settings in this same saved change.
                    self._refresh_instances_pricing(live=True)
                self._persist()
            except Exception:
                self.state["instances"] = prev_instances
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
        recent_within: float | None = None,
        new_capacity_epoch: float | None = None,
    ) -> None:
        """Mark active/unhandled OOM events as handled/acknowledged so a single OOM does not repeatedly trigger scale-ups.

        With ``recent_within`` (seconds), only events still active (unhandled, inside that lookback) are touched, so
        expired incidents are never rewritten as handled."""
        lookback_now = self._clock()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        telem = self.state.setdefault("scaling_telemetry", {})

        def _runner_oom_satisfied(record: dict[str, Any]) -> bool:
            if event_id is not None or current_runners is None:
                return True
            if new_capacity_epoch is not None:
                raw = record.get("failed_at") or record.get("observed_at") or record.get("timestamp")
                try:
                    stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    occurred = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
                except (ValueError, TypeError):
                    return False
                if occurred >= new_capacity_epoch:
                    return False
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
                if not isinstance(run, dict) or not _is_eligible_github_oom(run):
                    continue
                if event_id and str(run.get("id", "")) != event_id and str(run.get("run_id", "")) != event_id:
                    continue
                if recent_within is not None and not _is_recent_unhandled_oom(run, lookback_now, recent_within):
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
            if recent_within is not None and not _is_recent_unhandled_oom(evt, lookback_now, recent_within):
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
            if role == "rust_builder" and isinstance(payload.get("policy"), dict):
                if {"runner_vcpu", "runner_ram_gib"}.intersection(payload["policy"]):
                    raise ValueError("Builder scaling uses container_vcpu/container_ram_gib or builder_vcpu/builder_ram_gib")
                validate_builder_container_limits(payload["policy"])
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
                        self.state["profiles"]["rust_builder"]["min_instances"] = min_inst_val
                        self.state["profiles"]["rust_builder"]["max_instances"] = max_inst_val
                        if {"container_vcpu", "builder_vcpu", "container_ram_gib", "builder_ram_gib"}.intersection(payload["policy"]):
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
                            self.state["profiles"]["rust_builder"]["runner_vcpu"] = _finite_int(
                                pol.get("container_vcpu", pol.get("builder_vcpu", 16)), "container_vcpu", min_val=1
                            )
                            self.state["profiles"]["rust_builder"]["runner_ram_gib"] = builder_ram_ceil
                        self._sync_template_density()
                telem_payload = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else {}
                if self.fleet_source is not None:
                    owned = {"current_runners", "current_instances", "current_builder_instances", "current_builders",
                             "running_instance_hourly_rates", "builder_instance_hourly_rates",
                             "instance_cpu_percentages", "builder_cpu_percentages", "unknown_runner_count_hosts"}
                    telem_payload = {key: value for key, value in telem_payload.items() if key not in owned}
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
                if self.fleet_source is not None:
                    self._sync_fleet_telemetry()
                if payload.get("acknowledge_all_ooms"):
                    self._consume_active_oom_events(role=role if "role" in payload else None)
                if payload.get("acknowledge_oom_id"):
                    self._consume_active_oom_events(
                        role=None, event_id=str(payload["acknowledge_oom_id"]).strip()
                    )
                if "add_github_oom_run" in payload:
                    item = validate_github_oom_run(payload["add_github_oom_run"])
                    cls_info = classify_github_ci_failure(
                        item["error_excerpt"],
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
                running_builder_rates = self._running_role_hourly_rates("rust_builder", usable_only=self.fleet_source is not None)
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
                if self.fleet_source is not None:
                    # With a live inventory, each role's shared cap prices the other role's VMs with its own flags.
                    runner_burn_for_builder = round(
                        sum(self._running_role_hourly_rates("cicd_runner", budget_role="rust_builder")), 4)
                    builder_burn_for_runner = round(
                        sum(self._running_role_hourly_rates("rust_builder", budget_role="cicd_runner")), 4)
                cicd_eval = self._fail_closed_evaluation(evaluate_scaling_decision(
                    self.state["scaling"]["cicd_runner"],
                    self.state["scaling_telemetry"],
                    per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd("cicd_runner"),
                    max_instances=int(
                        self.state["profiles"]["cicd_runner"].get("max_instances", 8)
                    ),
                    other_role_hourly_usd=self._outside_burn(
                        "cicd_runner",
                        shared=bool(global_budget_shared
                                    or self.state["scaling"]["cicd_runner"].get("shared_global_budget")),
                        other_running_burn=builder_burn_for_runner,
                    ) or 0.0,
                ), "cicd_runner")
                builder_eval = self._fail_closed_evaluation(evaluate_builder_scaling_decision(
                    self.state["scaling"]["rust_builder"],
                    self._builder_evaluation_telemetry(),
                    per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd("rust_builder"),
                    other_role_hourly_usd=self._outside_burn(
                        "rust_builder",
                        shared=bool(global_budget_shared
                                    or self.state["scaling"]["rust_builder"].get("shared_global_budget")),
                        other_running_burn=(
                            self._runner_burn_for_builder_budget(cicd_eval) if self.fleet_source is not None
                            else max(runner_burn_for_builder, float(cicd_eval.get("projected_hourly_usd", 0.0)))),
                    ) or 0.0,
                ), "rust_builder")
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
            inst = _select_instance_record(self.state["instances"], instance_id, instance_name)
            if inst is None:
                raise ValueError("Source instance not found for VM template creation")

            if inst.get("shared_attachments") or inst.get("storage_billing_ambiguous"):
                raise ValueError("VMs with shared or ambiguous disk attachments cannot be copied to single-VM templates")
            if inst.get("kind") == "disk":
                raise ValueError(f"{inst['name']} is a disk, not a VM; a VM template needs a VM as its source")
            if inst.get("disk_types_known") is False:
                raise ValueError(
                    f"The disk types of {inst['name']} are not verified (no compute.disks.list), so a VM template "
                    "cannot describe them; create the template from the profile instead"
                )
            if inst.get("regional_disks") or inst.get("unpriced_disk_performance"):
                raise ValueError(
                    f"{inst['name']} has regional disks or provisioned Hyperdisk performance, which a VM template "
                    "cannot describe; create the template from the profile instead"
                )
            if int(inst.get("local_ssd_gb") or 0) > 0:
                raise ValueError(
                    f"{inst['name']} has {int(inst['local_ssd_gb'])} GiB of Local SSD, which a VM template cannot "
                    "describe; create the template from the profile instead"
                )
            if len(inst.get("data_disks") or {}) > 1:
                raise ValueError(
                    f"{inst['name']} has data disks of several types ({', '.join(sorted(inst['data_disks']))}); "
                    "a VM template holds one data-disk type, so create it from the profile instead"
                )
            if inst.get("runner_limits_known") is False:
                raise ValueError(
                    f"Per-runner limits of {inst['name']} are not recorded in its instance metadata; "
                    "set them on the profile instead of copying them from this host"
                )
            if inst.get("role") == "rust_builder" and not builder_shape_compatible(inst):
                raise ValueError("Dedicated builder templates require n4-highcpu-16 / 16 vCPU / 32 GiB")
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
        """Plan only: the dashboard never deletes a VM, and never marks one deleted that still exists.

        Returns the instance and how to retire it with the reviewed lifecycle tools; the record changes only when
        the live inventory no longer lists the VM.
        """
        with self._lock:
            instance_id = str(payload.get("instance_id", "")).strip()
            instance_name = str(payload.get("instance_name", "")).strip()
            inst = _select_instance_record(self.state["instances"], instance_id, instance_name)
            if inst is None:
                raise ValueError("Instance not found")
            if inst.get("status") == "DELETED":
                return {"status": "already_deleted", "applied": False, "instance": copy.deepcopy(inst)}
            if inst.get("role") == "rust_builder":
                next_step = ("Dedicated builders keep their cache disk (auto-delete=no); retire the VM only when no "
                             "build or runtime update holds it, and delete the cache disk separately if intended.")
            else:
                next_step = ("Runner hosts are drained and retired by spot_runner_autoscaler.py so no job loses its "
                             "runner; lower the runner floor or let the autoscaler scale in.")
            return {
                "status": "not_applied",
                "applied": False,
                "instance": copy.deepcopy(inst),
                "message": LIFECYCLE_ACTIONS_DISABLED_MESSAGE,
                "next_step": next_step,
            }

    def scale_region_instances(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Plan only: validate a regional scale change and return what it would launch or retire.

        The dashboard never launches or deletes a VM (see LIFECYCLE_ACTIONS_DISABLED_MESSAGE): the plan is
        computed on a copy of the instance list and nothing in state changes.
        """
        with self._lock:
            instances = copy.deepcopy(self.state["instances"])
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
                for i in instances
                if i["region"] == region and i["status"] == "RUNNING" and i["role"] == role
            ]
            running_other_regions = sum(
                1
                for i in instances
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
            stale_reason = self._inventory_stale_reason()
            if stale_reason:
                raise ValueError(f"{stale_reason}; plan the scale change after the next complete listing")
            pending_hosts = self._transitional_hosts(role)
            if pending_hosts:
                raise ValueError(
                    f"VM(s) {', '.join(pending_hosts[:5])} are starting, stopping, or repairing; plan the "
                    "scale change once their capacity has settled"
                )
            if role == "cicd_runner":
                unknown_counts = [str(i.get("name")) for i in instances
                                  if i.get("role") == role and i.get("status") == "RUNNING"
                                  and i.get("runners_count_known") is False]
                if unknown_counts:
                    raise ValueError(f"Runner count is not recorded on running host(s) {', '.join(unknown_counts[:5])}; "
                                     "the runner total is unknown, so a regional scale plan cannot be checked")
            if role == "rust_builder" and any(
                    inst.get("role") == role and inst.get("status") == "RUNNING"
                    and inst.get("runner_limits_known") is False for inst in instances):
                raise ValueError("Builder capacity is incompatible with the fixed builder shape; a scale plan cannot count it")
            now_ts = time.time()
            would_retire: list[dict[str, Any]] = []
            new_records: list[dict[str, Any]] = []
            projected_fleet_hourly = 0.0
            if target_count < current_count:
                would_retire = running_in_region[target_count:]
            elif target_count > current_count:
                if role == "rust_builder":
                    validate_builder_container_limits(prof)
                    validate_builder_container_limits(pol)
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
                    if role == "rust_builder" and not builder_shape_compatible(vm_tpl):
                        raise ValueError("Dedicated builder templates require n4-highcpu-16 / 16 vCPU / 32 GiB")
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
                    existing_cand = next(
                        # A host with several data-disk types cannot be described by one type and size.
                        (i for i in running_in_region
                         if len(i.get("data_disks") or {}) <= 1 and not int(i.get("local_ssd_gb") or 0)
                         and not i.get("shared_attachments") and not i.get("storage_billing_ambiguous")
                         and not i.get("regional_disks") and not i.get("unpriced_disk_performance")
                         and i.get("disk_types_known") is not False), None)
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

                if role == "rust_builder" and not builder_shape_compatible(source_spec):
                    raise ValueError("Planned dedicated builders require n4-highcpu-16 / 16 vCPU / 32 GiB")
                default_ci_id = "ci-runner-default" if role == "cicd_runner" else "ci-rust-builder-199"
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
                    if role == "rust_builder" and not builder_shape_compatible(created):
                        raise ValueError("Planned dedicated builders require n4-highcpu-16 / 16 vCPU / 32 GiB")
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
                    for i in instances
                    if i["status"] == "RUNNING" and i["role"] == role
                ]
                previous_runner_slots = sum(
                    int(i.get("runners_count", 1)) for i in existing_running_role
                )
                projected_runner_slots = previous_runner_slots
                unknown_counts = [str(i.get("name")) for i in existing_running_role
                                  if i.get("runners_count_known") is False]
                if role == "cicd_runner" and unknown_counts:
                    raise ValueError(
                        f"Runner count is not recorded on running host(s) {', '.join(unknown_counts[:5])}; "
                        "the runner total is unknown, so a scale-up cannot be checked against max_runners"
                    )
                incomplete_costs = self._incomplete_cost_hosts(role)
                if incomplete_costs:
                    raise ValueError(
                        f"Cost of VM(s) {', '.join(incomplete_costs[:5])} is not fully priced (standard VM without "
                        "an on-demand rate, or suspended); a scale-up cannot be checked against the budget"
                    )
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
                        for i in instances
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
                unpriced = [rec for rec in new_records if not _selected_pricing_complete(rec, include_storage=include_disk)]
                if unpriced:
                    raise ValueError(
                        f"The planned {unpriced[0].get('machine_type')} VMs cannot be fully priced (custom or extended "
                        "shape, a standard VM without an on-demand rate, or a disk type that bills provisioned "
                        "performance); the scale-up cannot be checked against the budget"
                    )
                # Stopped inventory VMs bill no compute but keep billing their disks; transitional ones count fully.
                stopped_disk_hourly = sum(
                    _nonrunning_hourly(i, include_disk=include_disk, include_tax=include_tax)
                    for i in instances
                    if is_live_instance_record(i)
                    and (i["status"] not in ("RUNNING", "DELETED") or i.get("disk_liability_pending"))
                    and (i["role"] == role or (shared_global and i["role"] == other_role))
                )
                projected_fleet_hourly = round(
                    other_role_hourly
                    + stopped_disk_hourly
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
            planned_running = [
                i
                for i in new_records + running_in_region
                if not any(i is r for r in would_retire)
            ]
            if role == "rust_builder" and any(not builder_shape_compatible(inst) for inst in planned_running):
                raise ValueError("Planned dedicated builders require n4-highcpu-16 / 16 vCPU / 32 GiB")
            return {
                "status": "plan_only",
                "applied": False,
                "region": region,
                "role": role,
                "previous_count": current_count,
                "current_count": current_count,
                "planned_count": len(planned_running),
                "would_launch": [
                    {k: rec.get(k) for k in ("zone", "machine_type", "vcpu", "ram_gib", "cpu_platform",
                                             "boot_disk_type", "boot_disk_gb", "data_disk_type", "data_disk_gb",
                                             "runners_count", "runner_vcpu", "runner_ram_gib", "pricing",
                                             "vm_template_id", "cloud_init_template_id")}
                    for rec in new_records
                ],
                "would_retire": [i.get("name") for i in would_retire],
                # Budget projection of a scale-up (0 otherwise), including stopped VMs' disks when configured.
                "projected_fleet_hourly_usd": projected_fleet_hourly,
                "planned_instances": planned_running,
                "message": LIFECYCLE_ACTIONS_DISABLED_MESSAGE,
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
            # Aggregates cover only the builds that reported each figure (unreported figures are None).
            # An aggregate with no reported figure behind it is None (unknown), not 0.

            def reported_values(key: str) -> list[Any]:
                return [b[key] for b in completed_builds if b.get(key) is not None]

            hits = reported_values("cache_hits")
            total_hits = sum(hits) if hits else None
            with_crates = [b for b in completed_builds
                           if b.get("cache_hits") is not None and b.get("total_crates") is not None]
            total_crates = sum(b["total_crates"] for b in with_crates)
            if total_crates:
                overall_hit_rate = round(sum(b["cache_hits"] for b in with_crates) / total_crates * 100.0, 1)
            else:
                overall_hit_rate = 0.0 if with_crates else None  # reported zero-crate builds count as 0%
            peaks = reported_values("peak_ram_gib")
            max_peak_ram = max(peaks) if peaks else None
            cpu_times = reported_values("cpu_time_seconds")
            total_cpu_time = round(sum(cpu_times), 2) if cpu_times else None
            points = reported_values("cpu_points")
            total_cpu_points = round(sum(points), 2) if points else None
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

            if payload.get("status") is None and not existing_build.get("status"):
                raise ValueError("A new Rust build needs a status")
            status_str = str(payload["status"] if payload.get("status") is not None else existing_build["status"])
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
            # A build still in progress keeps its reported stage durations across partial updates; once it
            # finishes, its final timing replaces them. (An active build's total is its compile time, re-derived.)
            # A stage duration derived from stored timestamps is not carried as a constraint: it is derived
            # again, so a corrected request or finish time recomputes it.
            keep_stage_durations = not was_non_delivered or (
                is_unfinished and prev_status_norm in UNFINISHED_BUILD_STATUSES
                and not payload.get("finished_at") and not payload.get("delivered_at")
                and (not payload.get("requested_at")
                     or str(payload["requested_at"]) == str(existing_build.get("requested_at")))
                and not existing_build.get("finished_at"))
            raw_delivery = (
                payload.get("delivery_duration_seconds")
                if "delivery_duration_seconds" in payload
                else (existing_build.get("delivery_duration_seconds") if keep_stage_durations else None)
            )
            raw_compile = (
                payload.get("compile_duration_seconds")
                if "compile_duration_seconds" in payload
                else (existing_build.get("compile_duration_seconds") if keep_stage_durations else None)
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
                duration_includes_delivery = explicit_total is not None
                if explicit_total is not None:
                    duration = explicit_total
                elif explicit_compile is not None:
                    duration = explicit_compile
                elif is_unsuccessful and (payload.get("requested_at") or existing_build.get("requested_at")) and (
                        payload.get("finished_at") or existing_build.get("finished_at")
                        or not (existing_build and prev_status_norm in UNSUCCESSFUL_BUILD_STATUSES)):
                    # A failure reported now ended now; a stored failure whose end was never reported stays
                    # unknown on later updates.
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
                    # Keep full precision until inferred timestamps are formed. Rounding elapsed time here can
                    # carry a status-only completion into the next second and hide it from immediate telemetry.
                    duration = max(0.0, fin_epoch_target - req_epoch_existing)
                else:
                    duration = None  # no timing reported: it stays unknown
                delivery_sec = explicit_delivery if explicit_delivery is not None else 0.0
            else:
                if explicit_total is not None:
                    duration = explicit_total
                    if explicit_delivery is not None:
                        delivery_sec = min(explicit_delivery, duration)
                    elif explicit_compile is not None:
                        delivery_sec = max(0.0, duration - min(explicit_compile, duration))
                    else:
                        delivery_sec = None
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
                        delivery_sec = None
                else:
                    delivery_sec = explicit_delivery
                    duration = (explicit_compile + explicit_delivery
                                if explicit_compile is not None and explicit_delivery is not None else None)
            # The compile time, as reported or derived from reported figures (None when it is not known).
            known_compile = (
                explicit_compile if explicit_compile is not None
                else (duration - delivery_sec if duration is not None and delivery_sec is not None else None)
            )

            cache_hits, cache_misses, total_crates, reported_crates = _merged_crate_counts(payload, existing_build)

            if is_non_delivered:
                # Without a reported request time, a build that reports elapsed time started that long ago.
                req_at = (
                    payload.get("requested_at")
                    or existing_build.get("requested_at")
                    # Whole seconds, like every inferred timestamp, so later durations stay consistent with it.
                    or _iso_seconds_ago(0, now_ts - (duration if duration is not None else 0))
                )
                req_epoch = _iso_to_epoch(str(req_at))
                if "finished_at" in payload:
                    fin_at = payload.get("finished_at")
                elif existing_build.get("finished_at"):
                    fin_at = existing_build.get("finished_at")
                elif is_unsuccessful and duration is not None:
                    # Only a total includes delivery; a compile time (or request-to-failure span) is the run itself.
                    run_seconds = max(0.0, duration - delivery_sec) if duration_includes_delivery else duration
                    request_inferred = not payload.get("requested_at") and (
                        not existing_build.get("requested_at") or existing_build.get("requested_at_inferred") is True)
                    if req_epoch + run_seconds > now_ts + 1.0:
                        if not request_inferred:
                            raise ValueError("Inconsistent failure timing: requested_at plus the reported duration "
                                             "is after the report")
                        # An inferred request time (the first report's arrival) can't be right: the build ran for
                        # the reported time and ended by now, so it started that long before now.
                        req_epoch = float(math.floor(now_ts - run_seconds))
                        req_at = _iso_seconds_ago(0, req_epoch)
                    # Request time plus the exact reported run time, at the precision of the resulting time.
                    fin_at = _iso_at_epoch(req_epoch + run_seconds)
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
                    del_at = _iso_at_epoch(max(now_ts, req_epoch + duration))
                    del_epoch = _iso_to_epoch(del_at)
                if payload.get("finished_at"):
                    fin_at = str(payload["finished_at"])
                elif delivery_sec is not None:
                    fin_at = _iso_at_epoch(max(req_epoch, del_epoch - delivery_sec))
                else:
                    fin_at = None  # not reported: the compile/delivery split stays unknown
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
                elif known_compile is not None:
                    fin_at = _iso_at_epoch(req_epoch + max(0.0, known_compile))
                    fin_epoch = _iso_to_epoch(fin_at)
                elif delivery_sec is not None:
                    # Delivered when reported, so it finished the reported delivery time before that.
                    fin_at = _iso_at_epoch(now_ts - delivery_sec)
                    fin_epoch = _iso_to_epoch(fin_at)
                else:
                    fin_epoch = None
                    fin_at = None  # not reported: the compile/delivery split stays unknown
                if (
                    existing_build.get("delivered_at")
                    and not payload.get("requested_at")
                    and "total_duration_seconds" not in payload
                    and "delivery_duration_seconds" not in payload
                ):
                    del_at = str(existing_build["delivered_at"])
                else:
                    if explicit_total is not None:
                        del_at = _iso_at_epoch(req_epoch + duration)
                    elif delivery_sec is not None and fin_epoch is not None:
                        del_at = _iso_at_epoch(fin_epoch + delivery_sec)
                    else:
                        del_at = _iso_at_epoch(now_ts)  # delivered when reported
            else:
                del_at = payload.get("delivered_at") or _iso_at_epoch(now_ts)
                del_epoch = _iso_to_epoch(str(del_at))
                if payload.get("finished_at"):
                    fin_at = payload["finished_at"]
                elif delivery_sec is not None:
                    fin_at = _iso_at_epoch(del_epoch - delivery_sec)
                elif known_compile is not None and (payload.get("requested_at") or existing_build.get("requested_at")):
                    fin_at = _iso_at_epoch(_iso_to_epoch(str(payload.get("requested_at")
                                                             or existing_build["requested_at"]))
                                           + max(0.0, known_compile))
                else:
                    fin_at = None  # not reported: the compile/delivery split stays unknown
                fin_epoch = _iso_to_epoch(str(fin_at)) if fin_at else None
                if payload.get("requested_at"):
                    req_at = payload["requested_at"]
                elif existing_build.get("requested_at"):
                    # A stored request time is evidence: keep it (enrichment rejects timing that contradicts it).
                    req_at = existing_build["requested_at"]
                elif known_compile is not None and fin_epoch is not None:
                    req_at = _iso_at_epoch(fin_epoch - max(0.0, known_compile))
                elif duration is not None:
                    req_at = _iso_at_epoch(del_epoch - max(0.0, duration))
                else:
                    raise ValueError(UNKNOWN_BUILD_TIMING_MESSAGE)

            def given(key: str) -> Any:
                """This report's value, treating a blank string as not reported."""
                value = payload.get(key)
                return None if isinstance(value, str) and not value.strip() else value

            def reported(key: str) -> Any:
                """This report's value, else the stored build's; None when neither reported it."""
                value = given(key)
                return value if value is not None else existing_build.get(key)

            # An explicit benchmark score stands in for the host shape. A stored score is kept unless the report
            # changes an already reported shape value, so a different host is scored again.
            shape_keys = ("cpu_platform", "vcpu", "machine_type")
            def shape_value(value: Any) -> Any:
                """A shape value in comparable form: whole numbers (16, "16", 16.0) as integers, names stripped."""
                text = str(value).strip()
                try:
                    number = float(text)
                except ValueError:
                    return text
                return int(number) if math.isfinite(number) and number.is_integer() else text

            # Only a value that conflicts with a previously reported one means a different host; filling in a
            # field nobody reported before keeps the stored score.
            shape_changed = any(given(k) is not None and existing_build.get(k) is not None
                                and shape_value(given(k)) != shape_value(existing_build[k])
                                for k in shape_keys)
            benchmark_score = (given("instance_benchmark_score")
                               if given("instance_benchmark_score") is not None
                               else (None if shape_changed else existing_build.get("instance_benchmark_score")))

            raw = {
                "build_id": incoming_build_id or f"rb-{uuid.uuid4().hex[:8]}",
                "project_name": reported("project_name"),
                "profile": reported("profile"),
                "target_triple": reported("target_triple"),
                "commit_sha": reported("commit_sha"),
                "status": status_str,
                "requested_at": req_at,
                # Whether no report ever gave the request time (it was inferred from a report's arrival).
                "requested_at_inferred": (False if payload.get("requested_at") else (
                    existing_build.get("requested_at_inferred") is True
                    if existing_build.get("requested_at") and str(req_at) == str(existing_build["requested_at"])
                    else True)),
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
                REPORTED_CRATE_COUNTS_KEY: reported_crates,
                "instance_name": reported("instance_name"),
                "zone": reported("zone"),
                "machine_type": reported("machine_type"),
                "vcpu": reported("vcpu"),
                "cpu_platform": reported("cpu_platform"),
                "instance_benchmark_score": benchmark_score,
                "origin_ip": reported("origin_ip"),
                "origin_hostname": (
                    payload.get("origin_hostname")
                    if "origin_hostname" in payload
                    else existing_build.get("origin_hostname")
                ),
                "peak_ram_gib": reported("peak_ram_gib"),
                "container_ram_cap_gib": _finite_float(
                    payload.get(
                        "container_ram_cap_gib",
                        existing_build.get("container_ram_cap_gib", 28.0),
                    ),
                    "container_ram_cap_gib",
                    min_val=0.1,
                ),
                "cpu_time_seconds": reported("cpu_time_seconds"),
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
            builder_eval = self._fail_closed_evaluation(evaluate_builder_scaling_decision(
                self.state["scaling"]["rust_builder"],
                self._builder_evaluation_telemetry(),
                per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd("rust_builder"),
                now_epoch=now_ts,
                other_role_hourly_usd=self._outside_burn(
                    "rust_builder",
                    shared=bool(self.state["scaling"]["rust_builder"].get("shared_global_budget")
                                or self.state.get("global_budget_enforced")),
                    other_running_burn=self._runner_burn_for_builder_budget(self._fail_closed_evaluation(
                        # The CI evaluation (on a copy, so this read changes no OOM state): runner VMs it would add
                        # count against the shared budget too, as on the overview.
                        evaluate_scaling_decision(
                            self.state["scaling"]["cicd_runner"],
                            copy.deepcopy(self.state["scaling_telemetry"]),
                            per_instance_hourly_usd=self._resolve_scaling_unit_hourly_usd("cicd_runner"),
                            max_instances=int(self.state["profiles"]["cicd_runner"].get("max_instances", 8)),
                            other_role_hourly_usd=self._outside_burn(
                                "cicd_runner",
                                shared=self._role_shares_budget("cicd_runner"),
                                other_running_burn=sum(self._running_role_hourly_rates(
                                    "rust_builder", budget_role="cicd_runner")),
                            ),
                        ), "cicd_runner")),
                ),
            ), "rust_builder")
            return {
                "status": "ok",
                "build": enriched,
                "builder_scaling_evaluation": builder_eval,
            }


CRATE_COUNT_KEYS = ("cache_hits", "cache_misses", "total_crates")
REPORTED_CRATE_COUNTS_KEY = "reported_crate_counts"


def _merged_crate_counts(
    payload: dict[str, Any], stored: dict[str, Any],
) -> tuple[int | None, int | None, int | None, list[str]]:
    """Crate counts of a build update, plus which of them were reported rather than derived.

    This report's counts win over the stored ones, field by field. A stored count that contradicts the new report
    is dropped and derived again from the others, a previously derived count before a reported one (a build stored
    before reported counts were tracked treats every stored count as reported). A count nobody reported stays None.
    """
    reported = {k: payload[k] for k in CRATE_COUNT_KEYS if payload.get(k) is not None}
    stored_reported = stored.get(REPORTED_CRATE_COUNTS_KEY)
    if not isinstance(stored_reported, list):
        stored_reported = [k for k in CRATE_COUNT_KEYS if stored.get(k) is not None]
    # A stored count derived from others is derived again whenever this report changes any count.
    kept = {k: stored[k] for k in CRATE_COUNT_KEYS
            if k not in reported and stored.get(k) is not None and (not reported or k in stored_reported)}

    def counts(values: dict[str, Any]) -> tuple[int | None, int | None, int | None, list[str]]:
        hits, misses, total = _crate_counts(*(values.get(k) for k in CRATE_COUNT_KEYS))
        sources = [k for k in CRATE_COUNT_KEYS if k in reported or (k in values and k in stored_reported)]
        return hits, misses, total, sources

    try:
        return counts({**kept, **reported})
    except ValueError:
        if not kept:
            raise
    order = sorted(("cache_misses", "cache_hits", "total_crates"), key=lambda k: k in stored_reported)
    for dropped in order:
        if dropped in kept:
            try:
                return counts({**{k: v for k, v in kept.items() if k != dropped}, **reported})
            except ValueError:
                continue
    return counts(reported)


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
            if method in MUTATING_METHODS and path not in PLAN_ONLY_POST_PATHS and (
                    store.frozen or not store.reconciled):
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
    fleet_source: Any = None,
) -> tuple[ThreadingHTTPServer, DashboardStore]:
    """Instantiate the ThreadingHTTPServer and DashboardStore, then start hydration.

    ``fleet_source`` (else ``DASHBOARD_FLEET_INVENTORY=gce``) is the read-only live instance listing the
    instance view mirrors; without one the instance list stays empty.

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
    store = DashboardStore(state_path=state_path, project=project, org=org, control_slot=control_slot, kv=kv,
                           fleet_source=fleet_source if fleet_source is not None else fleet_source_from_env(project))
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

