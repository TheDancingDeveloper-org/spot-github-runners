"""Lifecycle of the Spot "control" VM that hosts the web dashboard.

Exactly one control VM (slot ``a`` or ``b``, named ``spot-control-<slot>``) serves the dashboard at a
time. The Cloudflare Worker routes to whichever slot the KV key ``origin:active`` names; each slot is
reachable only through its Cloudflare Tunnel hostname behind Cloudflare Access.

This module has three layers:

* Pure helpers: shape pricing, the ``gcloud compute instances create`` command, and the planner
  :func:`plan_next`, which maps observations to exactly one next action.
* :class:`ControlVmManager`, the executor. It runs actions through an injectable ``gcloud`` runner,
  Cloudflare KV client, and HTTP requester, and persists every transition step in the controller
  state so a crash at any point resumes safely.
* :func:`autoscaler_tick`, the hook the runner autoscaler calls when ``--manage-control-vm`` is set.

Safety invariants:

* The VM that ``origin:active`` names is never deleted. ``delete_old`` re-reads the origin first.
* A make-before-break rotation cuts over only after the new VM is healthy and has imported the old
  VM's frozen snapshot, and only through a verified ``write_origin``. A failed write unfreezes the old
  VM, restores the origin if needed, deletes the new VM, and keeps the old VM serving.
* Without ``--apply`` (or in autoscaler ``--dry-run``) nothing is created, started, deleted, written
  to KV, sent to a control endpoint, or saved to controller state.

Standard library only.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import edge_state
from .edge_state import (
    CONTROL_SLOTS,
    DEFAULT_ORIGIN_DOMAIN,
    DEFAULT_PUBLIC_URL,
    KV_STATE_KEY,
    CloudflareKv,
    KvConfig,
    KvError,
    OriginRecord,
    control_token,
    control_vm_name,
    origin_url_for_slot,
    other_slot,
    parse_snapshot,
    read_origin,
    write_origin,
)

LOG = logging.getLogger("spot_runner_autoscaler.control_vm")

ROOT = Path(__file__).resolve().parents[2]
STARTUP_SCRIPT_PATH = ROOT / "cloud-init" / "control-vm-startup.sh"

ROLE_LABEL = "spot-role"
ROLE_VALUE = "control"
SLOT_LABEL = "spot-control-slot"

CONTROL_MACHINE_TYPES = ("e2-small", "e2-medium", "t2d-standard-1")
# Billed (vCPU, GiB). Shared-core E2 shapes bill a fraction of a core.
BILLED_SHAPES: dict[str, tuple[Decimal, Decimal]] = {
    "e2-small": (Decimal("0.5"), Decimal("2")),
    "e2-medium": (Decimal("1"), Decimal("4")),
    "t2d-standard-1": (Decimal("1"), Decimal("4")),
}
BOOT_DISK_GB = 20
BOOT_DISK_TYPE = "pd-balanced"
IMAGE_FAMILY = "debian-12"
IMAGE_PROJECT = "debian-cloud"

DEFAULT_REGIONS = ("us-central1", "us-east1", "us-west1")
DEFAULT_MAX_DAILY_USD = Decimal("0.50")
DEFAULT_DASHBOARD_REPO = "swack-tools/spot-github-runners"
DEFAULT_CLOUDFLARED_IMAGE = "cloudflare/cloudflared:2026.9.3@sha256:072c067d25ccbe61d46e18f0d0723255f2bb5304f7317caa95b27031520ff92c"

SECRET_NAMES = (
    "spot-dashboard-session-secret",
    "spot-dashboard-oauth-client-id",
    "spot-dashboard-oauth-client-secret",
    "spot-dashboard-control-secret",
    "spot-dashboard-cloudflare-kv-token",
    "spot-dashboard-github-token",
    "spot-dashboard-tunnel-token-a",
    "spot-dashboard-tunnel-token-b",
)
METADATA_KEYS = (
    "spot-control-slot",
    "dashboard-image",
    "dashboard-repo",
    "dashboard-source-ref",
    "dashboard-public-url",
    "cloudflare-account-id",
    "cloudflare-kv-namespace-id",
    "cloudflared-image",
    "dashboard-session-not-before",
)
# Optional bulk sign-out cut-off (Unix seconds). Not a secret, so it travels as instance metadata; the
# startup script writes it to the dashboard env file as DASHBOARD_SESSION_NOT_BEFORE on every boot.
ENV_SESSION_NOT_BEFORE = "DASHBOARD_SESSION_NOT_BEFORE"
_UNIX_SECONDS_RE = re.compile(r"^[0-9]{1,15}$")
SESSION_NOT_BEFORE_SKEW_SECONDS = 60

STATE_KEY = "control_vm"

# Timings (seconds).
START_GRACE_SECONDS = 600          # Active VM stopped or missing this long -> create the other slot.
START_RETRY_SECONDS = 120          # Minimum spacing between start requests for one VM.
UNHEALTHY_GRACE_SECONDS = 900      # Active VM RUNNING but unhealthy this long -> create the other slot.
HEALTH_TIMEOUT_SECONDS = 1200      # New VM must report healthy within this (first boot may build).
PROPAGATION_SECONDS = 90           # KV propagation wait between cutover and deleting the old VM.
REPLACEMENT_READY_GRACE_SECONDS = 300  # Replacement unready this long after cutover -> fall back to old.
ROTATION_COOLDOWN_SECONDS = 6 * 3600
PRICE_CHECK_INTERVAL_SECONDS = 3600
MIN_SAVINGS_FRACTION = Decimal("0.30")
MIN_SAVINGS_DAILY_USD = Decimal("0.05")
CREATE_ATTEMPTS_PER_TICK = 3
PRICE_CREATE_MAX_FAILURES = 3
STEP_RETRY_LIMIT = 5
FAILED_ZONE_TTL_SECONDS = 1800     # A zone that failed a create is retried after this long.
MAX_STEPS_PER_TICK = 12

STOPPED_STATES = frozenset({"TERMINATED", "STOPPED", "SUSPENDED"})
BOOTING_STATES = frozenset({"PROVISIONING", "STAGING", "RUNNING"})

MODE_BOOTSTRAP = "bootstrap"
MODE_RECOVER = "recover"
MODE_PRICE = "price"
MODE_ACTION = {MODE_BOOTSTRAP: "bootstrap", MODE_RECOVER: "recreate_other_slot", MODE_PRICE: "rotate"}
STEPS = {
    MODE_BOOTSTRAP: ("create", "wait_health", "cutover"),
    MODE_RECOVER: ("create", "wait_health", "freeze", "import", "cutover", "propagate", "delete_old"),
    MODE_PRICE: ("create", "wait_health", "freeze", "import", "cutover", "propagate", "delete_old"),
}
ACTIONS = ("bootstrap", "start", "recreate_other_slot", "rotate", "wait", "noop")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
_IMAGE_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$")


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ControlConfig:
    project: str
    regions: tuple[str, ...] = DEFAULT_REGIONS
    max_daily_usd: Decimal = DEFAULT_MAX_DAILY_USD
    external_ip: bool = True
    service_account: str = ""
    dashboard_image: str = ""
    dashboard_repo: str = DEFAULT_DASHBOARD_REPO
    dashboard_source_ref: str = ""
    public_url: str = DEFAULT_PUBLIC_URL
    cloudflare_account_id: str = ""
    cloudflare_kv_namespace_id: str = ""
    cloudflared_image: str = DEFAULT_CLOUDFLARED_IMAGE
    origin_domain: str = DEFAULT_ORIGIN_DOMAIN
    access_client_id: str = field(default="", repr=False)
    access_client_secret: str = field(default="", repr=False)
    control_secret: str = field(default="", repr=False)
    session_not_before: str = ""

    @classmethod
    def from_env(cls, project: str, env: Mapping[str, str] | None = None, **overrides: Any) -> ControlConfig:
        env = os.environ if env is None else env
        regions = tuple(r.strip() for r in env.get("SPOT_CONTROL_REGIONS", "").split(",") if r.strip())
        external = env.get("SPOT_CONTROL_EXTERNAL_IP", "1").strip().lower() not in ("0", "false", "no", "off")
        values: dict[str, Any] = dict(
            project=project,
            regions=regions or DEFAULT_REGIONS,
            max_daily_usd=Decimal(env.get("SPOT_CONTROL_MAX_DAILY_USD") or DEFAULT_MAX_DAILY_USD),
            external_ip=external,
            service_account=env.get("SPOT_CONTROL_SERVICE_ACCOUNT", ""),
            dashboard_image=env.get("SPOT_DASHBOARD_IMAGE", ""),
            dashboard_repo=env.get("SPOT_DASHBOARD_REPO") or DEFAULT_DASHBOARD_REPO,
            dashboard_source_ref=env.get("SPOT_DASHBOARD_SOURCE_REF", ""),
            public_url=env.get("SPOT_DASHBOARD_PUBLIC_URL") or DEFAULT_PUBLIC_URL,
            cloudflare_account_id=env.get(edge_state.ENV_ACCOUNT_ID, ""),
            cloudflare_kv_namespace_id=env.get(edge_state.ENV_NAMESPACE_ID, ""),
            cloudflared_image=env.get("SPOT_CLOUDFLARED_IMAGE") or DEFAULT_CLOUDFLARED_IMAGE,
            origin_domain=env.get("SPOT_CONTROL_ORIGIN_DOMAIN") or DEFAULT_ORIGIN_DOMAIN,
            access_client_id=env.get("CF_ACCESS_CLIENT_ID", ""),
            access_client_secret=env.get("CF_ACCESS_CLIENT_SECRET", ""),
            control_secret=env.get(edge_state.ENV_CONTROL_SECRET, ""),
            session_not_before=str(env.get(ENV_SESSION_NOT_BEFORE, "") or "").strip(),
        )
        values.update({key: value for key, value in overrides.items() if value is not None})
        if isinstance(values["regions"], str):
            values["regions"] = tuple(r.strip() for r in values["regions"].split(",") if r.strip())
        values["max_daily_usd"] = Decimal(str(values["max_daily_usd"]))
        return cls(**values)

    def runtime_problems(self) -> list[str]:
        """Settings without which the manager cannot observe or control the VMs safely."""
        problems = []
        if not self.access_client_id or not self.access_client_secret:
            problems.append("CF_ACCESS_CLIENT_ID/CF_ACCESS_CLIENT_SECRET are not set (health probes need Access)")
        if not self.control_secret:
            problems.append(f"{edge_state.ENV_CONTROL_SECRET} is not set (freeze/import need control tokens)")
        if not self.regions:
            problems.append("no control VM regions are allowed")
        return problems

    def create_problems(self) -> list[str]:
        """Settings that must be valid before a control VM is created."""
        problems = []
        if self.dashboard_image:
            if not _DIGEST_RE.match(self.dashboard_image):
                problems.append("dashboard image must be pinned by digest (name@sha256:<64 hex>)")
        elif not _SHA_RE.match(self.dashboard_source_ref):
            problems.append("set a digest-pinned dashboard image or a 40-hex dashboard source commit")
        if not _REPO_RE.match(self.dashboard_repo):
            problems.append("dashboard repository must be owner/name")
        if not self.public_url.startswith("https://") or any(c in self.public_url for c in " ,\n\t"):
            problems.append("dashboard public URL must be an https:// URL without spaces or commas")
        if not _ID_RE.match(self.cloudflare_account_id or ""):
            problems.append(f"{edge_state.ENV_ACCOUNT_ID} is missing or malformed")
        if not _ID_RE.match(self.cloudflare_kv_namespace_id or ""):
            problems.append(f"{edge_state.ENV_NAMESPACE_ID} is missing or malformed")
        if not (_IMAGE_TAG_RE.match(self.cloudflared_image) or _DIGEST_RE.match(self.cloudflared_image)) or self.cloudflared_image.endswith(":latest"):
            problems.append("cloudflared image must be pinned to a version tag or digest")
        if self.service_account and not re.match(r"^[^\s,@]+@[^\s,@]+$", self.service_account):
            problems.append("control VM service account must be an e-mail address")
        problems.extend(session_not_before_problems(self.session_not_before))
        return problems


def session_not_before_problems(value: str, now: float | None = None) -> list[str]:
    """Problems with an optional DASHBOARD_SESSION_NOT_BEFORE value (empty means unset).

    The dashboard refuses a cut-off in the future (it would also reject every new session), so a VM created
    with one would never come up; reject it before creating anything. Mirrors ``session_not_before_ok`` in
    cloud-init/control-vm-startup.sh.
    """
    if not value:
        return []
    if not _UNIX_SECONDS_RE.match(value):
        return [f"{ENV_SESSION_NOT_BEFORE} must be a Unix timestamp in seconds"]
    if int(value) > (time.time() if now is None else now) + SESSION_NOT_BEFORE_SKEW_SECONDS:
        return [f"{ENV_SESSION_NOT_BEFORE} must not be in the future"]
    return []


def metadata_items(config: ControlConfig, slot: str) -> dict[str, str]:
    """Non-secret instance metadata; secrets are fetched from Secret Manager at boot."""
    edge_state.validate_slot(slot)
    items = {
        "spot-control-slot": slot,
        "dashboard-image": config.dashboard_image,
        "dashboard-repo": config.dashboard_repo,
        "dashboard-source-ref": config.dashboard_source_ref,
        "dashboard-public-url": config.public_url,
        "cloudflare-account-id": config.cloudflare_account_id,
        "cloudflare-kv-namespace-id": config.cloudflare_kv_namespace_id,
        "cloudflared-image": config.cloudflared_image,
        "dashboard-session-not-before": config.session_not_before,
    }
    items = {key: value for key, value in items.items() if value}
    for key, value in items.items():
        if any(c in value for c in ",\n\r\t ") or "=" in key:
            raise ValueError(f"metadata value for {key} contains a separator character")
    return items


def render_control_startup(path: Path | None = None) -> str:
    """Return the control VM startup script.

    The script is static: every per-VM value is read from instance metadata at boot and every secret
    from Secret Manager, so nothing here is templated and the rendered text never holds a secret.
    """
    return Path(path or STARTUP_SCRIPT_PATH).read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# Observation types
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ControlVm:
    name: str
    zone: str
    status: str
    slot: str
    machine_type: str = ""
    instance_id: str = ""

    @property
    def region(self) -> str:
        return self.zone.rsplit("-", 1)[0]

    @property
    def running(self) -> bool:
        return self.status == "RUNNING"


def parse_control_vms(rows: Any) -> list[ControlVm]:
    """Convert ``gcloud compute instances list --format=json`` rows to labelled control VMs."""
    if not isinstance(rows, list):
        raise RuntimeError("gcloud returned an unexpected instance list")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        labels = row.get("labels") or {}
        slot = labels.get(SLOT_LABEL)
        name = str(row.get("name", ""))
        if labels.get(ROLE_LABEL) != ROLE_VALUE or slot not in CONTROL_SLOTS:
            continue
        if name != control_vm_name(slot):
            LOG.warning("Ignoring control-labelled VM %s: its name does not match slot %s", name, slot)
            continue
        result.append(ControlVm(
            name=name,
            zone=str(row.get("zone", "")).rsplit("/", 1)[-1],
            status=str(row.get("status", "UNKNOWN")).upper(),
            slot=slot,
            machine_type=str(row.get("machineType", "")).rsplit("/", 1)[-1],
            instance_id=str(row.get("id", "")),
        ))
    return result


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    status_code: int | None = None
    state_version: int | None = None
    frozen: bool = False
    hydrated: bool = False
    detail: str = ""
    slot: str | None = None

    @classmethod
    def unknown(cls, detail: str = "not probed") -> HealthResult:
        return cls(ok=False, detail=detail)


@dataclass(frozen=True)
class ControlOffer:
    machine_type: str
    region: str
    zone: str
    daily_usd: Decimal

    def to_dict(self) -> dict[str, str]:
        return {"machine_type": self.machine_type, "region": self.region, "zone": self.zone,
                "daily_usd": str(self.daily_usd)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ControlOffer:
        return cls(str(data["machine_type"]), str(data["region"]), str(data["zone"]),
                   Decimal(str(data["daily_usd"])))


def price_control_offers(regions: Sequence[str], zones_by_shape: Mapping[str, Sequence[str]], *,
                         rates: Callable[[str, str], tuple[Decimal, Decimal]],
                         storage: Callable[[str], Mapping[str, Decimal]],
                         machine_types: Sequence[str] = CONTROL_MACHINE_TYPES) -> list[ControlOffer]:
    """Rank control VM offers by daily compute plus boot-disk cost, cheapest first.

    ``rates(region, family)`` returns the exact Spot (core, GiB) hourly SKU prices (the same source
    the runner price guard uses); ``storage(region)`` returns regional disk rates.
    """
    from .billing_catalog import monthly_to_daily
    from .storage_selection import disk_monthly_cost

    offers = []
    for region in regions:
        try:
            disk_daily = monthly_to_daily(disk_monthly_cost(BOOT_DISK_TYPE, BOOT_DISK_GB, storage(region)))
        except (RuntimeError, KeyError, OSError) as exc:
            LOG.warning("Control VM disk price unavailable in %s: %s", region, exc)
            continue
        for machine_type in machine_types:
            zones = [z for z in zones_by_shape.get(machine_type, ()) if z.rsplit("-", 1)[0] == region]
            if not zones:
                continue
            family = machine_type.split("-", 1)[0]
            try:
                core, ram = rates(region, family)
            except (RuntimeError, KeyError, OSError) as exc:
                LOG.warning("Control VM Spot price unavailable for %s in %s: %s", machine_type, region, exc)
                continue
            vcpu, gib = BILLED_SHAPES[machine_type]
            daily = (Decimal(str(core)) * vcpu + Decimal(str(ram)) * gib) * 24 + disk_daily
            if not daily.is_finite() or daily <= 0:
                continue
            offers.extend(ControlOffer(machine_type, region, zone, daily) for zone in sorted(zones))
    offers.sort(key=lambda o: (o.daily_usd, o.region, o.zone, o.machine_type))
    return offers


def choose_offer(offers: Sequence[ControlOffer] | None, max_daily_usd: Decimal,
                 exclude_zones: Sequence[str] = ()) -> ControlOffer | None:
    """Cheapest offer under the cap, preferring zones outside ``exclude_zones``."""
    affordable = [o for o in offers or () if o.daily_usd <= max_daily_usd]
    preferred = [o for o in affordable if o.zone not in exclude_zones]
    return (preferred or affordable or [None])[0]


def current_price(vm: ControlVm, offers: Sequence[ControlOffer]) -> ControlOffer | None:
    return next((o for o in offers if o.machine_type == vm.machine_type and o.region == vm.region), None)


# --------------------------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    action: str
    reason: str
    slot: str | None = None          # VM acted on (start) or the outgoing slot.
    target_slot: str | None = None   # Slot to create.
    offer: ControlOffer | None = None
    resume: bool = False

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown control VM action {self.action!r}")

    def describe(self) -> str:
        parts = [self.action]
        if self.resume:
            parts.append("(resume)")
        if self.slot:
            parts.append(f"slot={self.slot}")
        if self.target_slot:
            parts.append(f"target={self.target_slot}")
        if self.offer:
            parts.append(f"offer={self.offer.machine_type}@{self.offer.zone} ${self.offer.daily_usd:.4f}/day")
        return " ".join(parts) + f": {self.reason}"


def _wait(reason: str, **kw: Any) -> Plan:
    return Plan("wait", reason, **kw)


def plan_next(*, vms: Sequence[ControlVm], origin: OriginRecord | None, origin_known: bool,
              health: Mapping[str, HealthResult], offers: Sequence[ControlOffer] | None,
              control_state: Mapping[str, Any], now: float, max_daily_usd: Decimal,
              force_rotate: bool = False) -> Plan:
    """Return the single next action for the control VM pair. Pure: no I/O, no mutation."""
    transition = control_state.get("transition")
    if transition:
        mode = transition.get("mode")
        return Plan(MODE_ACTION.get(mode, "wait"), f"resume {mode} at step {transition.get('step')}",
                    slot=transition.get("old_slot"), target_slot=transition.get("new_slot"), resume=True)
    if not origin_known:
        return _wait("origin:active is unreadable; no lifecycle change without KV")

    by_slot = {vm.slot: vm for vm in vms}
    last_start = control_state.get("last_start_at", {})

    def start_or_wait(vm: ControlVm, why: str) -> Plan:
        started = last_start.get(vm.slot)
        if started is not None and now - started < START_RETRY_SECONDS:
            return _wait(f"start of {vm.name} requested {int(now - started)}s ago", slot=vm.slot)
        return Plan("start", why, slot=vm.slot)

    if origin is None:
        if not vms:
            offer = choose_offer(offers, max_daily_usd)
            if offer is None:
                return _wait("no control VM and no priced offer under the cap")
            return Plan("bootstrap", "no control VM and no origin", target_slot="a", offer=offer)
        vm = by_slot.get("a") or by_slot["b"]
        if vm.status in STOPPED_STATES:
            return start_or_wait(vm, f"{vm.name} is {vm.status} and no origin is set")
        if vm.running:
            return Plan("bootstrap", f"adopt existing {vm.name}; no origin is set", target_slot=vm.slot)
        return _wait(f"{vm.name} is {vm.status}; no origin is set")

    slot = origin.slot
    active = by_slot.get(slot)
    other = by_slot.get(other_slot(slot))
    down_since = control_state.get("down_since", {}).get(slot)
    down_for = (now - down_since) if down_since is not None else 0.0

    def recreate(why: str) -> Plan:
        exclude = (active.zone,) if active else ()
        offer = choose_offer(offers, max_daily_usd, exclude) if other is None else None
        if other is None and offer is None:
            return _wait(f"{why}; no priced offer under the cap for the other slot")
        return Plan("recreate_other_slot", why, slot=slot, target_slot=other_slot(slot), offer=offer)

    if active is None:
        if down_for >= START_GRACE_SECONDS:
            return recreate(f"active {control_vm_name(slot)} missing for {int(down_for)}s")
        return _wait(f"active {control_vm_name(slot)} missing; grace {START_GRACE_SECONDS}s", slot=slot)
    if active.status in STOPPED_STATES:
        if down_for >= START_GRACE_SECONDS:
            return recreate(f"{active.name} {active.status} for {int(down_for)}s; start did not restore it")
        return start_or_wait(active, f"{active.name} is {active.status} (preempted)")
    if not active.running:
        if down_for >= START_GRACE_SECONDS:
            return recreate(f"{active.name} stuck {active.status} for {int(down_for)}s")
        return _wait(f"{active.name} is {active.status}", slot=slot)

    result = health.get(slot) or HealthResult.unknown()
    if not result.ok:
        if down_for >= UNHEALTHY_GRACE_SECONDS:
            return recreate(f"{active.name} unhealthy for {int(down_for)}s ({result.detail})")
        return _wait(f"{active.name} running but not healthy ({result.detail})", slot=slot)

    if other is not None:
        return Plan("noop", f"{other.name} exists outside a transition; leaving it for an operator", slot=slot)
    if offers is None:
        return Plan("noop", f"{active.name} healthy; price not checked this tick", slot=slot)
    best = choose_offer(offers, max_daily_usd)
    current = current_price(active, offers)
    if best is None:
        return Plan("noop", f"{active.name} healthy; no offer under the ${max_daily_usd}/day cap", slot=slot)
    if force_rotate:
        return Plan("rotate", "operator requested rotation", slot=slot, target_slot=other_slot(slot), offer=best)
    if current is None:
        return Plan("noop", f"{active.name} healthy; its shape/region has no current price", slot=slot)
    last_end = max(control_state.get("last_transition_end_at") or 0, control_state.get("last_abort_at") or 0)
    if last_end and now - last_end < ROTATION_COOLDOWN_SECONDS:
        return Plan("noop", f"{active.name} healthy; rotation cooldown active", slot=slot)
    savings = current.daily_usd - best.daily_usd
    over_cap = current.daily_usd > max_daily_usd
    worthwhile = savings >= MIN_SAVINGS_DAILY_USD and savings >= current.daily_usd * MIN_SAVINGS_FRACTION
    if savings > 0 and (over_cap or worthwhile):
        why = (f"{active.name} costs ${current.daily_usd:.4f}/day; {best.machine_type}@{best.zone} "
               f"costs ${best.daily_usd:.4f}/day")
        return Plan("rotate", why, slot=slot, target_slot=other_slot(slot), offer=best)
    return Plan("noop", f"{active.name} healthy at ${current.daily_usd:.4f}/day", slot=slot)


def update_tracking(control_state: dict, origin: OriginRecord | None, vms: Sequence[ControlVm],
                    health: Mapping[str, HealthResult], now: float) -> None:
    """Record when the active VM was first seen down or unhealthy; clear it once healthy."""
    if origin is None:
        return
    active = next((vm for vm in vms if vm.slot == origin.slot), None)
    healthy = active is not None and active.running and (health.get(origin.slot) or HealthResult.unknown()).ok
    down = control_state.setdefault("down_since", {})
    if healthy:
        down.pop(origin.slot, None)
        control_state.setdefault("last_start_at", {}).pop(origin.slot, None)
    else:
        down.setdefault(origin.slot, now)


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------

# gcloud create errors that prove no instance was created in that zone (capacity or quota).
_DEFINITIVE_CREATE_ERROR_RE = re.compile(
    r"ZONE_RESOURCE_POOL_EXHAUSTED|QUOTA_EXCEEDED|Quota '[^']*' exceeded|does not have enough resources|"
    r"resource pool exhausted|STOCKOUT", re.IGNORECASE)
_NOT_FOUND_RE = re.compile(r"was not found|notFound|HTTP 404|code: 404|\b404\b", re.IGNORECASE)


def describe_command(name: str, zone: str, project: str) -> list[str]:
    return ["gcloud", "compute", "instances", "describe", name, f"--zone={zone}", f"--project={project}",
            "--format=json(name,zone,status,labels,id,machineType)"]


def duplicate_slot_problems(vms: Sequence[ControlVm]) -> list[str]:
    """One problem per slot that has more than one VM (instance names are only unique per zone)."""
    problems = []
    for slot in CONTROL_SLOTS:
        same = [vm for vm in vms if vm.slot == slot]
        if len(same) > 1:
            where = ", ".join(f"{vm.name} in {vm.zone} ({vm.status}, id {vm.instance_id or '?'})" for vm in same)
            problems.append(f"more than one control VM for slot {slot}: {where}; they share the slot's tunnel "
                            f"and KV writer identity, so nothing changes until an operator deletes the extra")
    return problems


def list_command(project: str) -> list[str]:
    return ["gcloud", "compute", "instances", "list", f"--project={project}",
            f"--filter=labels.{ROLE_LABEL}={ROLE_VALUE}",
            "--format=json(name,zone,status,labels,id,machineType)"]


def zones_command(project: str) -> list[str]:
    return ["gcloud", "compute", "machine-types", "list", f"--project={project}",
            f"--filter=name=({' '.join(CONTROL_MACHINE_TYPES)})", "--format=json(name,zone)"]


def start_command(vm: ControlVm, project: str) -> list[str]:
    return ["gcloud", "compute", "instances", "start", vm.name, f"--zone={vm.zone}",
            f"--project={project}", "--quiet"]


def delete_command(vm: ControlVm, project: str, *, keep_boot_disk: bool = False) -> list[str]:
    """Delete a control VM. ``keep_boot_disk`` retains its boot disk (named like the VM) for recovery."""
    disks = "--keep-disks=boot" if keep_boot_disk else "--delete-disks=all"
    return ["gcloud", "compute", "instances", "delete", vm.name, f"--zone={vm.zone}",
            f"--project={project}", disks, "--quiet"]


def create_command(config: ControlConfig, slot: str, offer: ControlOffer, startup_script_path: str) -> list[str]:
    """``gcloud compute instances create`` for one control slot."""
    problems = config.create_problems()
    if problems:
        raise ValueError("; ".join(problems))
    if offer.machine_type not in CONTROL_MACHINE_TYPES:
        raise ValueError(f"{offer.machine_type} is not an allowed control VM shape")
    metadata = ",".join(f"{key}={value}" for key, value in metadata_items(config, slot).items())
    command = [
        "gcloud", "compute", "instances", "create", control_vm_name(slot),
        f"--project={config.project}", f"--zone={offer.zone}", f"--machine-type={offer.machine_type}",
        "--provisioning-model=SPOT", "--instance-termination-action=STOP",
        f"--image-family={IMAGE_FAMILY}", f"--image-project={IMAGE_PROJECT}",
        f"--boot-disk-size={BOOT_DISK_GB}GB", f"--boot-disk-type={BOOT_DISK_TYPE}", "--boot-disk-auto-delete",
        f"--labels={ROLE_LABEL}={ROLE_VALUE},{SLOT_LABEL}={slot}",
        "--scopes=cloud-platform",
        "--shielded-secure-boot", "--shielded-vtpm", "--shielded-integrity-monitoring",
        f"--metadata-from-file=startup-script={startup_script_path}",
        f"--metadata={metadata}",
    ]
    if config.service_account:
        command.append(f"--service-account={config.service_account}")
    if not config.external_ip:
        command.append("--no-address")
    command += ["--format=json", "--quiet"]
    return command


def default_gcloud(argv: list[str]) -> Any:
    return subprocess.run(argv, check=False, text=True, capture_output=True, timeout=600)


def default_offer_source(config: ControlConfig, gcloud: Callable[[list[str]], Any]) -> list[ControlOffer]:
    """Price the allowed shapes in the allowed regions from the billing catalog (read-only)."""
    from .runner_manager import spot_rates
    from .storage_selection import storage_rates

    result = gcloud(zones_command(config.project))
    if result.returncode != 0:
        raise RuntimeError(f"machine-type inventory failed: {(result.stderr or '').strip()[:300]}")
    zones: dict[str, list[str]] = {}
    for row in json.loads(result.stdout or "[]"):
        zone = str(row.get("zone", "")).rsplit("/", 1)[-1]
        zones.setdefault(str(row.get("name", "")), []).append(zone)
    return price_control_offers(config.regions, zones, rates=spot_rates, storage=storage_rates)


def probe_health(requester: edge_state.Requester, config: ControlConfig, slot: str,
                 timeout: float = 10.0) -> HealthResult:
    url = origin_url_for_slot(slot, config.origin_domain) + "/api/health"
    headers = {**_access_headers(config), "Accept": "application/json"}
    try:
        status, body = requester("GET", url, headers, None, timeout)
    except (KvError, OSError, ValueError) as exc:
        return HealthResult(False, detail=f"unreachable: {exc}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        data = None
    if not isinstance(data, dict):
        return HealthResult(False, status_code=status, detail=f"HTTP {status} without health JSON")
    version = data.get("state_version")
    version = version if isinstance(version, int) and not isinstance(version, bool) else None
    hydrated, frozen = data.get("hydrated") is True, data.get("frozen") is True
    reported_slot = data.get("slot") if data.get("slot") in edge_state.CONTROL_SLOTS else None
    ok = status == 200 and data.get("status") == "ok" and hydrated and not frozen
    return HealthResult(ok, status, version, frozen, hydrated,
                        detail="healthy" if ok else f"HTTP {status} status={data.get('status')!r} "
                                                     f"hydrated={hydrated} frozen={frozen}",
                        slot=reported_slot)


def _access_headers(config: ControlConfig) -> dict[str, str]:
    return {"CF-Access-Client-Id": config.access_client_id,
            "CF-Access-Client-Secret": config.access_client_secret,
            "User-Agent": "spot-control-plane/1.0"}


# --------------------------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------------------------

@dataclass
class Observation:
    vms: list[ControlVm]
    origin: OriginRecord | None
    origin_known: bool
    health: dict[str, HealthResult]
    offers: list[ControlOffer] | None = None
    problems: list[str] = field(default_factory=list)

    def vm(self, slot: str | None) -> ControlVm | None:
        return next((vm for vm in self.vms if vm.slot == slot), None)


class ControlVmManager:
    """Observe, plan, and (with ``apply``) execute one control VM lifecycle step per tick."""

    def __init__(self, config: ControlConfig, state: dict, save: Callable[[], None], *,
                 gcloud: Callable[[list[str]], Any] | None = None,
                 kv: CloudflareKv | None = None,
                 requester: edge_state.Requester | None = None,
                 offer_source: Callable[[], list[ControlOffer]] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        import time as _time
        self.config = config
        self.state = state
        self._save = save
        self.gcloud = gcloud or default_gcloud
        self.kv = kv
        self.requester = requester or edge_state.default_requester
        self.offer_source = offer_source or (lambda: default_offer_source(config, self.gcloud))
        self.clock = clock or _time.time
        self._snapshot: dict[str, Any] | None = None
        self.log: list[str] = []

    # -- state ---------------------------------------------------------------------------
    @property
    def cstate(self) -> dict:
        return self.state.setdefault(STATE_KEY, {})

    def save(self) -> None:
        self.cstate["updated_at"] = self.clock()
        self._save()

    def _note(self, message: str, *args: Any, level: int = logging.INFO) -> None:
        text = message % args if args else message
        self.log.append(text)
        LOG.log(level, "Control VM: %s", text)

    # -- observation ---------------------------------------------------------------------
    def observe(self, *, want_offers: bool | None = None) -> Observation:
        problems = list(self.config.runtime_problems())
        result = self.gcloud(list_command(self.config.project))
        if result.returncode != 0:
            raise RuntimeError(f"control VM inventory failed: {(result.stderr or '').strip()[:300]}")
        vms = parse_control_vms(json.loads(result.stdout or "[]"))
        for problem in duplicate_slot_problems(vms):
            # Split brain risk: never pick one automatically (no origin writes, no deletes); alert and wait.
            self._note(problem, level=logging.ERROR)
            problems.append(problem)
        origin, known = None, False
        if self.kv is None:
            problems.append("Cloudflare KV is not configured (CLOUDFLARE_ACCOUNT_ID, "
                            "CLOUDFLARE_KV_NAMESPACE_ID, CLOUDFLARE_KV_API_TOKEN)")
        else:
            try:
                origin, known = read_origin(self.kv), True
            except KvError as exc:
                self._note("origin:active unreadable: %s", exc, level=logging.WARNING)
        health: dict[str, HealthResult] = {}
        if not problems:
            slots = set()
            if origin is not None:
                slots.add(origin.slot)
            transition = self.cstate.get("transition") or {}
            slots.update(s for s in (transition.get("new_slot"), transition.get("old_slot")) if s)
            for slot in sorted(slots):
                vm = next((v for v in vms if v.slot == slot), None)
                health[slot] = probe_health(self.requester, self.config, slot) if vm and vm.running \
                    else HealthResult.unknown(f"VM is {vm.status if vm else 'missing'}")
        obs = Observation(vms, origin, known, health, problems=problems)
        if want_offers is None:
            want_offers = self._offers_wanted(obs)
        if want_offers and not problems:
            try:
                obs.offers = list(self.offer_source())
            except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                self._note("control VM price scan failed: %s", exc, level=logging.WARNING)
        return obs

    def _offers_wanted(self, obs: Observation) -> bool:
        transition = self.cstate.get("transition")
        if transition:
            return transition.get("step") == "create"
        if obs.origin is None:
            return True
        active = obs.vm(obs.origin.slot)
        if active is None or not active.running or not obs.health.get(obs.origin.slot, HealthResult.unknown()).ok:
            return True
        last = self.cstate.get("last_price_check_at") or 0
        return self.clock() - last >= PRICE_CHECK_INTERVAL_SECONDS

    # -- planning ------------------------------------------------------------------------
    def plan(self, obs: Observation, *, force_rotate: bool = False, control_state: Mapping | None = None) -> Plan:
        if obs.problems:
            return _wait("; ".join(obs.problems))
        if force_rotate and obs.offers is None and not self.cstate.get("transition"):
            return _wait("rotation requested but no price scan is available")
        return plan_next(vms=obs.vms, origin=obs.origin, origin_known=obs.origin_known, health=obs.health,
                         offers=obs.offers, control_state=control_state or self.cstate, now=self.clock(),
                         max_daily_usd=self.config.max_daily_usd, force_rotate=force_rotate)

    def tick(self, *, apply: bool, force_rotate: bool = False) -> Plan:
        """One lifecycle step. Without ``apply`` only reads happen and nothing is saved."""
        if not apply:
            real_state = self.state
            self.state = copy.deepcopy(real_state)  # Previews never touch the caller's state.
            try:
                obs = self.observe(want_offers=True if force_rotate else None)
                update_tracking(self.cstate, obs.origin, obs.vms, obs.health, self.clock())
                plan = self.plan(obs, force_rotate=force_rotate)
            finally:
                self.state = real_state
            self._note("plan (dry run, nothing changed): %s", plan.describe())
            return plan
        obs = self.observe(want_offers=True if force_rotate else None)
        now = self.clock()
        if not obs.problems and obs.origin_known:
            update_tracking(self.cstate, obs.origin, obs.vms, obs.health, now)
        if obs.offers is not None:
            self.cstate["last_price_check_at"] = now
        plan = self.plan(obs, force_rotate=force_rotate)
        self._note("plan: %s", plan.describe())
        self.execute(plan, obs)
        self.save()
        return plan

    # -- execution -----------------------------------------------------------------------
    def execute(self, plan: Plan, obs: Observation) -> None:
        if plan.action in ("wait", "noop"):
            return
        if plan.action == "start":
            self._start(obs.vm(plan.slot))
            return
        if not plan.resume:
            mode = {"bootstrap": MODE_BOOTSTRAP, "recreate_other_slot": MODE_RECOVER, "rotate": MODE_PRICE}[plan.action]
            self._begin(mode, plan, obs)
        self.advance(obs)

    def _start(self, vm: ControlVm | None) -> bool:
        if vm is None:
            return False
        self.cstate.setdefault("last_start_at", {})[vm.slot] = self.clock()
        self.save()
        result = self.gcloud(start_command(vm, self.config.project))
        if result.returncode == 0:
            self._note("started %s in %s", vm.name, vm.zone)
            return True
        failures = self.cstate.setdefault("start_failures", {})
        failures[vm.slot] = failures.get(vm.slot, 0) + 1
        self._note("start of %s failed: %s", vm.name, (result.stderr or "").strip()[:300], level=logging.WARNING)
        return False

    def _begin(self, mode: str, plan: Plan, obs: Observation) -> None:
        now = self.clock()
        new_slot = plan.target_slot
        existing = obs.vm(new_slot)
        old_vm = obs.vm(plan.slot) if mode != MODE_BOOTSTRAP else None
        offer = plan.offer
        transition = {
            "id": uuid.uuid4().hex,
            "mode": mode,
            "old_slot": plan.slot if mode != MODE_BOOTSTRAP else None,
            "new_slot": new_slot,
            "old_vm": {"name": old_vm.name, "zone": old_vm.zone} if old_vm else None,
            "new_vm": {"name": control_vm_name(new_slot),
                       "zone": existing.zone if existing else (offer.zone if offer else ""),
                       "machine_type": existing.machine_type if existing else (offer.machine_type if offer else "")},
            "offer": offer.to_dict() if offer else None,
            "excluded_zones": ([old_vm.zone] if (old_vm and mode == MODE_RECOVER) else [])
                              + self._retained_zones(new_slot),
            "failed_zones": [],
            "failed_zones_at": {},
            "step": "create",
            "started_at": now,
            "step_started_at": now,
            "updated_at": now,
            "attempts": 0,
            "create_failures": 0,
            "freeze_requested": False,
            "frozen_version": None,
            "import_skipped": False,
            "cutover_attempted": False,
            "cutover_at": None,
            "abort_reason": None,
            "error": None,
            "reason": plan.reason,
        }
        self.cstate["transition"] = transition
        self.save()
        self._note("began %s transition %s -> slot %s: %s", mode, plan.slot or "-", new_slot, plan.reason)

    def _retained_zones(self, slot: str) -> list[str]:
        """Zones holding a retained boot disk for ``slot``; creating the VM there would collide on its name."""
        return sorted({str(d.get("zone")) for d in self.cstate.get("retained_disks", [])
                       if d.get("slot") == slot and d.get("zone")})

    def _delete_vm(self, t: dict, vm: ControlVm, *, keep_boot_disk: bool, reason: str) -> bool:
        """Delete ``vm``; with ``keep_boot_disk`` record the retained disk and log it for an operator."""
        result = self.gcloud(delete_command(vm, self.config.project, keep_boot_disk=keep_boot_disk))
        if result.returncode != 0:
            t["error"] = f"delete of {vm.name} failed: {(result.stderr or '').strip()[:300]}"
            self.save()
            return False
        if keep_boot_disk:
            self.cstate.setdefault("retained_disks", []).append(
                {"disk": vm.name, "zone": vm.zone, "slot": vm.slot, "transition": t.get("id"),
                 "reason": reason, "at": self.clock()})
            self.save()
            self._note("deleted control VM %s but KEPT its boot disk %s in %s because it may hold the newest "
                       "dashboard state (%s); recover it or delete it by hand", vm.name, vm.name, vm.zone,
                       reason, level=logging.ERROR)
        else:
            self._note("deleted control VM %s", vm.name)
        return True

    def _next_step(self, t: dict) -> str | None:
        order = STEPS[t["mode"]]
        index = order.index(t["step"])
        return order[index + 1] if index + 1 < len(order) else None

    def _goto(self, t: dict, step: str) -> None:
        now = self.clock()
        t.update(step=step, step_started_at=now, updated_at=now, attempts=0, error=None)
        self.save()

    def _advance_step(self, t: dict) -> str:
        nxt = self._next_step(t)
        if nxt is None:
            return self._finish(t)
        self._goto(t, nxt)
        return "advance"

    def _finish(self, t: dict) -> str:
        self.cstate["transition"] = None
        self.cstate["last_transition_end_at"] = self.clock()
        self.cstate.setdefault("down_since", {}).pop(t["new_slot"], None)
        if t.get("old_slot"):
            self.cstate["down_since"].pop(t["old_slot"], None)
            self.cstate.setdefault("last_start_at", {}).pop(t["old_slot"], None)
        self.cstate.setdefault("history", []).append(
            {"id": t["id"], "mode": t["mode"], "result": "completed", "new_slot": t["new_slot"], "at": self.clock()})
        self.cstate["history"] = self.cstate["history"][-20:]
        self.save()
        self._note("%s transition to slot %s completed", t["mode"], t["new_slot"])
        return "done"

    def _abort(self, t: dict, reason: str) -> str:
        t["abort_reason"] = reason
        self._note("aborting %s transition to slot %s: %s", t["mode"], t["new_slot"], reason, level=logging.WARNING)
        self._goto(t, "abort")
        return "advance"

    def _fail_attempt(self, t: dict, error: str, limit: int = STEP_RETRY_LIMIT) -> bool:
        """Record a failed attempt; True when the retry limit is reached."""
        t["attempts"] = t.get("attempts", 0) + 1
        t["error"] = error
        t["updated_at"] = self.clock()
        self.save()
        self._note("%s step %s attempt %d failed: %s", t["mode"], t["step"], t["attempts"], error,
                   level=logging.WARNING)
        return t["attempts"] >= limit

    def advance(self, obs: Observation) -> None:
        """Run transition steps until one has to wait for the outside world."""
        for _ in range(MAX_STEPS_PER_TICK):
            t = self.cstate.get("transition")
            if not t:
                return
            step = t["step"]
            if obs.problems:
                # Same safety gate as planning, before every step: no create, start, freeze, import,
                # unfreeze, origin write, or delete while the observation is unsafe (for example two VMs
                # for one slot). Everything stays as it is until the problem is resolved.
                t["error"] = "paused: " + "; ".join(obs.problems)
                t["updated_at"] = self.clock()
                self.save()
                self._note("transition paused at step %s: %s", step, "; ".join(obs.problems),
                           level=logging.ERROR)
                return
            handler = getattr(self, f"_step_{step}", None)
            if handler is None:
                self._abort(t, f"unknown step {step!r}")
                continue
            outcome = handler(t, obs)
            if outcome in ("wait", "done"):
                return
            # Steps change provider or origin state; the next step must see fresh observations.
            obs = self.observe(want_offers=False)

    # Each step returns "advance" (continue with fresh observations), "wait", or "done".

    def _reconcile_pending_create(self, t: dict) -> str:
        """Settle a create whose gcloud call did not succeed: "exists", "absent", or "unknown"."""
        pending = t.get("pending_create")
        if not pending:
            return "absent"
        result = self.gcloud(describe_command(pending["name"], pending["zone"], self.config.project))
        if result.returncode == 0:
            self._note("create of %s in %s failed in gcloud but the instance exists; adopting it",
                       pending["name"], pending["zone"], level=logging.WARNING)
            return "exists"
        if _NOT_FOUND_RE.search(result.stderr or ""):
            t.pop("pending_create", None)
            self._mark_zone_failed(t, pending["zone"])
            self.save()
            return "absent"
        t["error"] = (f"cannot confirm whether {pending['name']} exists in {pending['zone']}: "
                      f"{(result.stderr or '').strip()[:200]}")
        self.save()
        self._note(t["error"], level=logging.WARNING)
        return "unknown"

    def _step_create(self, t: dict, obs: Observation) -> str:
        vm = obs.vm(t["new_slot"])
        if vm is None and t.get("pending_create"):
            # A previous create may have landed even though gcloud failed: never try another zone (which
            # could leave two VMs with this slot's name and identity) until that attempt is settled.
            if self._reconcile_pending_create(t) != "absent":
                return "wait"
        if vm is not None:
            t.pop("pending_create", None)
            t["new_vm"].update(zone=vm.zone, machine_type=vm.machine_type)
            if vm.status in STOPPED_STATES:
                self._start(vm)
                return "wait"
            self._note("slot %s VM %s exists (%s); waiting for health", t["new_slot"], vm.name, vm.status)
            return self._advance_step(t)
        offers = obs.offers
        if offers is None:
            try:
                offers = list(self.offer_source())
            except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                self._note("control VM price scan failed: %s", exc, level=logging.WARNING)
                offers = []
        failed = self._failed_zones(t)
        affordable = [o for o in offers if o.daily_usd <= self.config.max_daily_usd]
        candidates = [o for o in affordable if o.zone not in failed]
        preferred = [o for o in candidates if o.zone not in t.get("excluded_zones", [])]
        candidates = preferred or candidates
        if t.get("offer") and not failed:
            planned = ControlOffer.from_dict(t["offer"])
            candidates = [planned] + [o for o in candidates if o != planned]
        if not candidates:
            if affordable:
                # Every available zone failed recently: start over with the full list next tick
                # (stockouts are transient), however few zones there are.
                t["failed_zones"], t["failed_zones_at"] = [], {}
                return self._create_failed(t, "every affordable zone failed; retrying all zones next tick")
            return self._create_failed(t, "no priced offer under the cap")
        script = render_control_startup()
        with tempfile.TemporaryDirectory(prefix="spot-control-") as tmp:
            script_path = Path(tmp) / "control-vm-startup.sh"
            script_path.write_text(script, encoding="utf-8")
            for offer in candidates[:CREATE_ATTEMPTS_PER_TICK]:
                try:
                    command = create_command(self.config, t["new_slot"], offer, str(script_path))
                except ValueError as exc:
                    return self._create_failed(t, f"invalid control VM settings: {exc}")
                # Persist the intended identity and the pending attempt before the provider call, so a
                # crash or an ambiguous failure is reconciled in this zone before any other zone is tried.
                t["new_vm"] = {"name": control_vm_name(t["new_slot"]), "zone": offer.zone,
                               "machine_type": offer.machine_type}
                t["pending_create"] = {"name": control_vm_name(t["new_slot"]), "zone": offer.zone,
                                       "machine_type": offer.machine_type, "at": self.clock()}
                self.save()
                result = self.gcloud(command)
                if result.returncode == 0:
                    t.pop("pending_create", None)
                    self._note("created %s (%s) in %s at $%.4f/day", control_vm_name(t["new_slot"]),
                               offer.machine_type, offer.zone, offer.daily_usd)
                    return self._advance_step(t)
                stderr = (result.stderr or "").strip()
                self._note("create in %s failed: %s", offer.zone, stderr[:300], level=logging.WARNING)
                if _DEFINITIVE_CREATE_ERROR_RE.search(stderr):
                    t.pop("pending_create", None)
                    self._mark_zone_failed(t, offer.zone)
                    self.save()
                    continue
                outcome = self._reconcile_pending_create(t)
                if outcome != "absent":
                    # "exists": wait for the inventory to list it; "unknown": retry the check next tick.
                    return "wait"
        return self._create_failed(t, "every create attempt this tick failed")

    def _mark_zone_failed(self, t: dict, zone: str) -> None:
        zones = t.setdefault("failed_zones", [])
        if zone not in zones:
            zones.append(zone)
        t.setdefault("failed_zones_at", {})[zone] = self.clock()

    def _failed_zones(self, t: dict) -> list[str]:
        """Zones that failed within FAILED_ZONE_TTL_SECONDS; older failures expire so capacity is retried."""
        now = self.clock()
        stamps = t.setdefault("failed_zones_at", {})
        for zone in t.get("failed_zones", []):
            stamps.setdefault(zone, now)  # Records from before timestamps existed start their TTL now.
        fresh = [z for z in t.get("failed_zones", []) if now - stamps[z] < FAILED_ZONE_TTL_SECONDS]
        if fresh != t.get("failed_zones", []):
            t["failed_zones"] = fresh
            t["failed_zones_at"] = {z: stamps[z] for z in fresh}
        return list(fresh)

    def _create_failed(self, t: dict, error: str) -> str:
        t["create_failures"] = t.get("create_failures", 0) + 1
        t["error"] = error
        self.save()
        if t["mode"] == MODE_PRICE and t["create_failures"] >= PRICE_CREATE_MAX_FAILURES:
            return self._abort(t, f"replacement create failed {t['create_failures']} times: {error}")
        if t["mode"] == MODE_RECOVER:
            old = self._old_back(t, None)
            if old:
                return self._abort(t, "old VM is healthy again")
        self._note("create for slot %s waits: %s", t["new_slot"], error, level=logging.WARNING)
        return "wait"

    def _old_back(self, t: dict, obs: Observation | None) -> bool:
        """True when, during recovery, the outgoing VM is RUNNING and healthy again."""
        if t["mode"] != MODE_RECOVER or not t.get("old_slot"):
            return False
        if obs is None:
            obs = self.observe(want_offers=False)
        old = obs.vm(t["old_slot"])
        if old is None or not old.running:
            return False
        result = obs.health.get(t["old_slot"]) or probe_health(self.requester, self.config, t["old_slot"])
        return result.ok

    def _step_wait_health(self, t: dict, obs: Observation) -> str:
        vm = obs.vm(t["new_slot"])
        if vm is None:
            self._note("slot %s VM disappeared before becoming healthy; creating again", t["new_slot"],
                       level=logging.WARNING)
            self._goto(t, "create")
            return "wait"
        if vm.status in STOPPED_STATES:
            self._start(vm)
        else:
            result = obs.health.get(t["new_slot"]) or (
                probe_health(self.requester, self.config, t["new_slot"]) if vm.running else HealthResult.unknown())
            if result.ok:
                self._note("slot %s is healthy (state_version=%s)", t["new_slot"], result.state_version)
                return self._advance_step(t)
            t["error"] = result.detail
        if self._old_back(t, obs):
            return self._abort(t, "old VM is healthy again; keeping it")
        if self.clock() - t["step_started_at"] >= HEALTH_TIMEOUT_SECONDS:
            return self._abort(t, f"slot {t['new_slot']} not healthy after {HEALTH_TIMEOUT_SECONDS}s")
        self.save()
        return "wait"

    def _control(self, slot: str, op: str, method: str, body: Mapping[str, Any] | None = None,
                 timeout: float = 60.0) -> tuple[int | None, Any]:
        url = origin_url_for_slot(slot, self.config.origin_domain) + f"/api/control/{op}"
        headers = {**_access_headers(self.config),
                   "Authorization": f"Bearer {control_token(self.config.control_secret, op, slot, now=self.clock())}",
                   "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self.requester(method, url, headers, data, timeout)
        except (KvError, OSError, ValueError) as exc:
            return None, str(exc)
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            return status, None

    def _step_freeze(self, t: dict, obs: Observation) -> str:
        old = obs.vm(t["old_slot"])
        if t["mode"] == MODE_PRICE and (old is None or old.status in STOPPED_STATES):
            # The outgoing VM was preempted mid-rotation; finish as a recovery instead of aborting
            # and deleting the healthy replacement.
            t["mode"] = MODE_RECOVER
            self._note("old slot %s went down during the price rotation; continuing as recovery", t["old_slot"],
                       level=logging.WARNING)
        if t["mode"] == MODE_RECOVER:
            old_health = obs.health.get(t["old_slot"]) or HealthResult.unknown()
            # A reachable old VM that reports frozen (an earlier freeze whose response was lost, or a
            # controller crash after freezing) still holds the newest state: freeze again (idempotent)
            # and import its snapshot instead of cutting over to older state.
            frozen_reachable = old_health.frozen and old_health.hydrated
            healthy = old is not None and old.running and (old_health.ok or frozen_reachable)
            if frozen_reachable and not old_health.ok:
                self._note("old slot %s is reachable but frozen; taking its snapshot before cutover", t["old_slot"])
            if not healthy:
                t["import_skipped"] = True
                self._note("old slot %s is down; new VM keeps the state it hydrated from KV", t["old_slot"])
                self._goto(t, "cutover")
                return "advance"
        try:
            origin = read_origin(self.kv)
        except KvError as exc:
            t["error"] = f"origin unreadable: {exc}"
            self.save()
            return "wait"
        if origin is None or origin.slot != t["old_slot"]:
            return self._abort(t, f"origin:active changed to {origin.slot if origin else None} during rotation")
        t["freeze_requested"] = True
        self.save()
        status, body = self._control(t["old_slot"], "freeze", "POST")
        version = body.get("state_version") if isinstance(body, dict) else None
        if status == 200 and isinstance(version, int) and not isinstance(version, bool):
            t["frozen_version"] = version
            self._snapshot = parse_snapshot(body.get("snapshot"))
            self._note("froze slot %s at state_version %d", t["old_slot"], version)
            return self._advance_step(t)
        if self._fail_attempt(t, f"freeze returned {status}"):
            if t["mode"] == MODE_RECOVER:
                status, body = self._control(t["old_slot"], "snapshot", "GET")
                snapshot = parse_snapshot(body) if status == 200 and isinstance(body, dict) else None
                if snapshot is not None:
                    self._snapshot = snapshot
                    t["frozen_version"] = snapshot["state_version"]
                    self._note("old slot %s did not freeze; importing its snapshot at state_version %d",
                               t["old_slot"], snapshot["state_version"], level=logging.WARNING)
                    return self._advance_step(t)
                t["import_skipped"] = True
                self._goto(t, "cutover")
                return "advance"
            return self._abort(t, "old VM did not freeze")
        return "wait"

    def _snapshot_for_import(self, t: dict) -> dict[str, Any] | None:
        floor = t.get("frozen_version") or 0
        candidates = [self._snapshot]
        if self._snapshot is None:
            status, body = self._control(t["old_slot"], "snapshot", "GET")
            candidates.append(parse_snapshot(body) if status == 200 and isinstance(body, dict) else None)
            try:
                candidates.append(parse_snapshot(self.kv.get(KV_STATE_KEY)))
            except KvError:
                pass
        for snapshot in candidates:
            if snapshot is not None and snapshot["state_version"] >= floor:
                return snapshot
        return None

    def _step_import(self, t: dict, obs: Observation) -> str:
        snapshot = self._snapshot_for_import(t)
        if snapshot is None:
            if self._fail_attempt(t, "no snapshot at or above the frozen state_version"):
                return self._abort(t, "frozen snapshot unavailable")
            return "wait"
        status, body = self._control(t["new_slot"], "import", "POST", snapshot)
        version = body.get("state_version") if isinstance(body, dict) else None
        floor = max(t.get("frozen_version") or 0, snapshot["state_version"])
        if status == 200 and isinstance(version, int) and not isinstance(version, bool) and version >= floor:
            self._note("slot %s imported state_version %d", t["new_slot"], version)
            return self._advance_step(t)
        if self._fail_attempt(t, f"import returned {status} (state_version={version}, need >= {floor})"):
            return self._abort(t, "new VM did not import the frozen snapshot")
        return "wait"

    def _catch_up_with_kv(self, t: dict) -> bool:
        """Recovery without an import: push the latest KV snapshot into the replacement before cutover.

        The replacement hydrated from KV when it booted, but the old VM may have written newer versions
        before it died. The import applies only when it is >= the replacement's version; the replacement
        must then report at least the KV version. True when the cutover may proceed.
        """
        if t.get("kv_caught_up"):
            return True
        try:
            snapshot = parse_snapshot(self.kv.get(KV_STATE_KEY)) if self.kv is not None else None
        except KvError as exc:
            t["error"] = f"KV snapshot unreadable before recovery cutover: {exc}"
            self.save()
            return False
        if snapshot is not None:
            status, body = self._control(t["new_slot"], "import", "POST", snapshot)
            version = body.get("state_version") if isinstance(body, dict) else None
            if not (status == 200 and isinstance(version, int) and not isinstance(version, bool)
                    and version >= snapshot["state_version"]):
                self._fail_attempt(t, f"replacement did not reach KV state_version {snapshot['state_version']} "
                                      f"(import returned {status}, state_version={version})", limit=10**9)
                return False
            self._note("slot %s caught up with KV at state_version %d before cutover", t["new_slot"], version)
        t["kv_caught_up"] = True
        self.save()
        return True

    def _replacement_ready(self, t: dict, obs: Observation) -> tuple[bool, str]:
        """Fresh readiness of the new slot: RUNNING and passing health right now."""
        vm = obs.vm(t["new_slot"])
        if vm is None:
            return False, f"slot {t['new_slot']} VM is missing"
        if not vm.running:
            return False, f"slot {t['new_slot']} VM is {vm.status}"
        result = obs.health.get(t["new_slot"]) or probe_health(self.requester, self.config, t["new_slot"])
        return (True, "") if result.ok else (False, f"slot {t['new_slot']} not healthy ({result.detail})")

    def _step_cutover(self, t: dict, obs: Observation) -> str:
        # Never point users at a replacement that was preempted or went unhealthy after import, or
        # when resuming a saved cutover step: recheck it immediately before the origin write.
        ready, why = self._replacement_ready(t, obs)
        if not ready:
            if t["mode"] == MODE_PRICE:
                # The old VM is still active (only frozen); abort unfreezes it and keeps it serving.
                return self._abort(t, f"replacement not ready at cutover: {why}")
            if self._old_back(t, obs):
                return self._abort(t, "old VM is healthy again; keeping it")
            self._note("replacement not ready at cutover (%s); waiting for it again", why, level=logging.WARNING)
            self._goto(t, "wait_health")
            return "wait"
        if t["mode"] == MODE_RECOVER and t.get("import_skipped") and not self._catch_up_with_kv(t):
            if self._old_back(t, obs):
                return self._abort(t, "old VM is healthy again; keeping it")
            return "wait"
        t["cutover_attempted"] = True
        self.save()
        record = OriginRecord.for_slot(t["new_slot"], domain=self.config.origin_domain, now=self.clock())
        if write_origin(self.kv, record):
            t["cutover_at"] = self.clock()
            self._note("origin:active now points at slot %s (verified)", t["new_slot"])
            return self._advance_step(t)
        if t["mode"] == MODE_PRICE:
            return self._abort(t, "write_origin was not verified")
        # Bootstrap and recovery have no healthy old VM to fall back to: keep the new VM and retry.
        if self._old_back(t, obs):
            return self._abort(t, "old VM is healthy again; keeping it")
        self._fail_attempt(t, "write_origin was not verified", limit=10**9)
        return "wait"

    def _step_propagate(self, t: dict, obs: Observation) -> str:
        remaining = PROPAGATION_SECONDS - (self.clock() - (t.get("cutover_at") or self.clock()))
        if remaining > 0:
            t["error"] = None
            return "wait"
        return self._advance_step(t)

    def _step_delete_old(self, t: dict, obs: Observation) -> str:
        try:
            origin = read_origin(self.kv)
        except KvError as exc:
            t["error"] = f"origin unreadable: {exc}"
            self.save()
            return "wait"
        if origin is None or origin.slot != t["new_slot"]:
            t["error"] = f"origin:active is {origin.slot if origin else None}; refusing to delete slot {t['old_slot']}"
            self.save()
            self._note(t["error"], level=logging.ERROR)
            return "wait"
        old = obs.vm(t["old_slot"])
        if old is not None:
            ready, detail = self._replacement_ready(t, obs)
            if not ready:
                return self._replacement_not_ready(t, obs, detail)
            t.pop("replacement_unready_since", None)
            # Recovery without an import (the old VM was down) may leave changes on the old disk that never
            # reached KV, so its boot disk is kept.
            if not self._delete_vm(t, old, keep_boot_disk=bool(t.get("import_skipped")),
                                   reason="old VM went down before its state was imported"):
                self._fail_attempt(t, t["error"], limit=10**9)
                return "wait"
        return self._finish(t)

    def _replacement_ready(self, t: dict, obs: Observation) -> tuple[bool, str]:
        """Fresh check that the replacement is RUNNING, healthy, and answers as its own slot."""
        new = obs.vm(t["new_slot"])
        if new is None:
            return False, f"replacement slot {t['new_slot']} VM is missing"
        if new.status != "RUNNING":
            return False, f"replacement {new.name} is {new.status}"
        result = probe_health(self.requester, self.config, t["new_slot"])
        if not result.ok:
            return False, f"replacement {new.name} is not healthy: {result.detail}"
        if result.slot != t["new_slot"]:
            return False, f"replacement hostname answered as slot {result.slot!r}, expected {t['new_slot']!r}"
        return True, "ready"

    def _replacement_not_ready(self, t: dict, obs: Observation, detail: str) -> str:
        """Never delete the old VM for an unready replacement: wait a bounded time, then fall back.

        The fallback is the regular abort path, which carries newer KV state back into the old VM,
        unfreezes it, restores ``origin:active`` to it with a verified write, and deletes the replacement
        only once the origin no longer names it.
        Without a running old VM to fall back to, the transition keeps waiting (nothing is deleted).
        """
        now = self.clock()
        since = t.setdefault("replacement_unready_since", now)
        t["error"] = f"{detail}; refusing to delete slot {t['old_slot']}"
        t["updated_at"] = now
        self.save()
        self._note(t["error"], level=logging.WARNING)
        new = obs.vm(t["new_slot"])
        old = obs.vm(t["old_slot"])
        if new is None:
            return self._abort(t, f"replacement disappeared after cutover: {detail}")
        if new.status in STOPPED_STATES:
            last = self.cstate.get("last_start_at", {}).get(new.slot) or 0
            if now - last >= START_RETRY_SECONDS:
                self._start(new)
        if now - since >= REPLACEMENT_READY_GRACE_SECONDS:
            if old is not None and old.running:
                return self._abort(t, f"replacement not ready {int(now - since)}s after cutover: {detail}")
            self._note("replacement still not ready and old slot %s is %s; waiting", t["old_slot"],
                       old.status if old else "missing", level=logging.ERROR)
        return "wait"

    def _replacement_snapshot(self, t: dict, obs: Observation) -> dict[str, Any] | None:
        """Freeze the replacement (idempotent) and return its snapshot, or None when it is unreachable."""
        new = obs.vm(t["new_slot"])
        if new is None or not new.running:
            return None
        status, body = self._control(t["new_slot"], "freeze", "POST")
        snapshot = parse_snapshot(body.get("snapshot")) if status == 200 and isinstance(body, dict) else None
        if snapshot is None:
            self._note("could not freeze replacement slot %s for rollback (HTTP %s)", t["new_slot"], status,
                       level=logging.WARNING)
        return snapshot

    def _carry_back_newer_state(self, t: dict, obs: Observation) -> bool:
        """Before falling back after cutover, import into the old VM the newest of the replacement's own
        snapshot (taken after freezing it) and the KV snapshot, so changes the replacement accepted are not
        lost even when write-behind lagged. True when the fallback may proceed.

        Sets ``replacement_state_transferred`` only when the replacement's snapshot was captured and the
        old VM confirmed holding at least that version; otherwise the replacement's boot disk is kept.
        """
        if t.get("fallback_import_done"):
            return True
        floor = t.get("frozen_version") or 0
        replacement = self._replacement_snapshot(t, obs)
        candidates = [replacement] if replacement is not None else []
        try:
            kv_snapshot = parse_snapshot(self.kv.get(KV_STATE_KEY)) if self.kv is not None else None
        except KvError as exc:
            if replacement is None:
                return self._fail_attempt(t, f"KV snapshot unreadable before fallback: {exc}")
            kv_snapshot = None
        if kv_snapshot is not None:
            candidates.append(kv_snapshot)
        newest = max(candidates, key=lambda s: s["state_version"], default=None)
        confirmed = newest is None or newest["state_version"] <= floor
        if not confirmed:
            status, body = self._control(t["old_slot"], "import", "POST", newest)
            version = body.get("state_version") if isinstance(body, dict) else None
            confirmed = (status == 200 and isinstance(version, int) and not isinstance(version, bool)
                         and version >= newest["state_version"])
            if confirmed:
                self._note("carried state_version %d back to old slot %s", version, t["old_slot"])
            elif not self._fail_attempt(t, f"fallback import into slot {t['old_slot']} returned {status}"):
                return False  # retry next tick
            else:
                # A reachable old VM with older state beats no dashboard; the replacement's disk is kept.
                self._note("could not confirm state transfer to slot %s; continuing rollback", t["old_slot"],
                           level=logging.ERROR)
        t["replacement_state_transferred"] = bool(confirmed and replacement is not None)
        t["fallback_import_done"] = True
        self.save()
        return True

    def _step_abort(self, t: dict, obs: Observation) -> str:
        old = obs.vm(t.get("old_slot"))
        # After a cutover attempt the replacement may have accepted changes and published them to KV
        # (even if it has since disappeared): import them into the old VM before it serves again.
        if t.get("cutover_attempted") and t.get("old_slot") and old is not None and old.running \
                and not self._carry_back_newer_state(t, obs):
            return "wait"
        if t.get("freeze_requested") and old is not None and old.running and not t.get("unfrozen"):
            status, _ = self._control(t["old_slot"], "unfreeze", "POST")
            if status == 200:
                t["unfrozen"] = True
                self._note("unfroze slot %s", t["old_slot"])
            elif not self._fail_attempt(t, f"unfreeze returned {status}"):
                return "wait"
            else:
                self._note("could not unfreeze slot %s; continuing abort", t["old_slot"], level=logging.ERROR)
        if t.get("cutover_attempted") and t.get("old_slot"):
            try:
                origin = read_origin(self.kv)
            except KvError as exc:
                t["error"] = f"origin unreadable during abort: {exc}"
                self.save()
                return "wait"
            if origin is None or origin.slot == t["new_slot"]:
                restored = OriginRecord.for_slot(t["old_slot"], domain=self.config.origin_domain, now=self.clock())
                if not write_origin(self.kv, restored):
                    t["error"] = "could not restore origin:active to the old slot; keeping the new VM"
                    self.save()
                    self._note(t["error"], level=logging.ERROR)
                    return "wait"
        new = obs.vm(t["new_slot"])
        if new is not None:
            try:
                origin = read_origin(self.kv) if self.kv is not None else None
            except KvError as exc:
                t["error"] = f"origin unreadable during abort: {exc}"
                self.save()
                return "wait"
            if origin is not None and origin.slot == t["new_slot"]:
                t["error"] = "origin:active points at the new slot; refusing to delete it"
                self.save()
                return "wait"
            # After a cutover attempt the replacement may hold changes that never reached the old VM.
            keep = bool(t.get("cutover_attempted")) and not t.get("replacement_state_transferred")
            if not self._delete_vm(t, new, keep_boot_disk=keep,
                                   reason="rollback could not confirm its state reached the old VM"):
                return "wait"
        self.cstate["transition"] = None
        self.cstate["last_abort_at"] = self.clock()
        self.cstate.setdefault("history", []).append(
            {"id": t["id"], "mode": t["mode"], "result": "aborted", "reason": t.get("abort_reason"),
             "new_slot": t["new_slot"], "at": self.clock()})
        self.cstate["history"] = self.cstate["history"][-20:]
        self.save()
        self._note("%s transition aborted: %s", t["mode"], t.get("abort_reason"))
        return "done"

    # -- reporting -----------------------------------------------------------------------
    def status(self, obs: Observation) -> dict[str, Any]:
        return {
            "vms": [vm.__dict__ for vm in obs.vms],
            "origin": json.loads(obs.origin.to_json()) if obs.origin else None,
            "origin_known": obs.origin_known,
            "health": {slot: result.__dict__ for slot, result in obs.health.items()},
            "transition": self.cstate.get("transition"),
            "down_since": self.cstate.get("down_since", {}),
            "last_transition_end_at": self.cstate.get("last_transition_end_at"),
            "last_abort_at": self.cstate.get("last_abort_at"),
            "retained_disks": self.cstate.get("retained_disks", []),
            "problems": obs.problems,
            "offers": [o.to_dict() for o in obs.offers[:5]] if obs.offers else None,
        }


def build_manager(project: str, state: dict, save: Callable[[], None], *,
                  env: Mapping[str, str] | None = None, **overrides: Any) -> ControlVmManager:
    config = ControlConfig.from_env(project, env, **overrides)
    kv_config = KvConfig.from_env(env)
    return ControlVmManager(config, state, save, kv=CloudflareKv(kv_config) if kv_config else None)


def autoscaler_tick(args: Any, state: dict, save: Callable[[], None], *,
                    manager: ControlVmManager | None = None) -> Plan | None:
    """Run one control VM step from the runner autoscaler loop; never raises."""
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        manager = manager or build_manager(args.project, state, save)
        plan = manager.tick(apply=not dry_run)
        if dry_run:
            print(f"Control VM plan (dry run): {plan.describe()}")
        return plan
    except Exception as exc:  # noqa: BLE001 - control VM work must never stop runner management.
        LOG.error("Control VM management failed this check: %s", exc)
        return None


# --------------------------------------------------------------------------------------------
# Command-line interface (src/control_vm.py)
# --------------------------------------------------------------------------------------------

DEFAULT_PROJECT = "homelab-424523"
DEFAULT_STATE_FILE = Path.home() / "Library" / "Application Support" / "Spot Runner Autoscaler" / "state.json"


def args_parser() -> "argparse.ArgumentParser":
    import argparse

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=DEFAULT_PROJECT)
    common.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE,
                        help="Controller state shared with the autoscaler (default: %(default)s)")
    common.add_argument("--regions", help="Comma-separated allowed regions (or SPOT_CONTROL_REGIONS)")
    common.add_argument("--max-daily-usd", type=Decimal,
                        help="Daily compute plus boot-disk cap per control VM (or SPOT_CONTROL_MAX_DAILY_USD)")
    common.add_argument("--no-external-ip", dest="external_ip", action="store_const", const=False, default=None,
                        help="Create without an external IP; requires Cloud NAT for egress")
    common.add_argument("--service-account", help="VM service account e-mail (or SPOT_CONTROL_SERVICE_ACCOUNT)")
    common.add_argument("--dashboard-image", help="Digest-pinned dashboard image (or SPOT_DASHBOARD_IMAGE)")
    common.add_argument("--dashboard-repo", help="Repository built on the VM when no image is set")
    common.add_argument("--dashboard-source-ref", help="40-hex commit to build (or SPOT_DASHBOARD_SOURCE_REF)")
    common.add_argument("--public-url", help="Public dashboard URL (or SPOT_DASHBOARD_PUBLIC_URL)")
    common.add_argument("--cloudflared-image", help="Pinned cloudflared image (or SPOT_CLOUDFLARED_IMAGE)")
    common.add_argument("--origin-domain", help="Domain of the per-slot tunnel hostnames")

    parser = argparse.ArgumentParser(
        description="Inspect and manage the Spot control VM that hosts the web dashboard. "
                    "Nothing is created, started, deleted, or written without --apply.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common], help="Show control VMs, origin:active, health, and transition state")
    sub.add_parser("plan", parents=[common], help="Print the next lifecycle action (read-only)")
    render = sub.add_parser("render-startup", parents=[common], help="Write the control VM startup script")
    render.add_argument("--output", type=Path, required=True)
    for name, text in (("ensure", "Run one lifecycle step (bootstrap, start, recover, or price rotation)"),
                       ("rotate", "Make-before-break rotation to the other slot now")):
        command = sub.add_parser(name, parents=[common], help=text)
        command.add_argument("--apply", action="store_true", help="Execute; without it only print the plan")
    return parser


def _overrides(args: Any) -> dict[str, Any]:
    keys = ("regions", "max_daily_usd", "external_ip", "service_account", "dashboard_image", "dashboard_repo",
            "dashboard_source_ref", "public_url", "cloudflared_image", "origin_domain")
    return {key: getattr(args, key, None) for key in keys}


def main(argv: Sequence[str] | None = None, *, manager_factory: Callable[..., ControlVmManager] | None = None) -> int:
    from .environment import load_env
    from .log_format import configure_logging

    args = args_parser().parse_args(argv)
    configure_logging()
    if args.command == "render-startup":
        args.output.write_text(render_control_startup(), encoding="utf-8")
        args.output.chmod(0o644)
        print(f"Wrote control VM startup script to {args.output}")
        return 0
    load_env(ROOT / ".env")
    from .spot_runner_autoscaler import read_scale_state, write_scale_state

    state_path = args.state_file.expanduser()
    apply = bool(getattr(args, "apply", False))
    factory = manager_factory or build_manager

    def run(state: dict) -> int:
        save = (lambda: write_scale_state(state_path, state)) if apply else (lambda: None)
        manager = factory(args.project, state, save, **_overrides(args))
        if args.command == "status":
            print(json.dumps(manager.status(manager.observe(want_offers=True)), indent=2, sort_keys=True,
                             default=str))
            return 0
        plan = manager.tick(apply=apply, force_rotate=args.command == "rotate")
        print(("Executed: " if apply else "Plan (nothing changed): ") + plan.describe())
        if not apply and plan.action not in ("wait", "noop"):
            print("Re-run with --apply to execute it.")
        return 0

    if not apply:
        # Read-only: work on a copy and never write the state file.
        return run(copy.deepcopy(read_scale_state(state_path)))
    import fcntl

    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(state_path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another controller owns state {state_path}; stop it or wait") from exc
        return run(read_scale_state(state_path))
