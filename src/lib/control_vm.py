"""Lifecycle of the "control" VM that hosts the web dashboard.

The control VM is a Spot VM by default (``SPOT_CONTROL_PROVISIONING=spot``) or the always-on free-tier
e2-micro (``standard``). Exactly one control VM (slot ``a`` or ``b``, named ``spot-control-<slot>``) serves
the dashboard at a time. The Cloudflare Worker routes to whichever slot the KV key ``origin:active`` names; each slot is
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
import dataclasses
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
from . import image_tracking
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

# Provisioning modes (SPOT_CONTROL_PROVISIONING / --provisioning). "spot" picks the cheapest Spot shape under the
# daily cap and rotates for price. "standard" runs the always-on Compute Engine free-tier VM: one non-preemptible
# e2-micro per billing account, only in us-west1, us-central1 or us-east1, with up to 30 GB-months of pd-standard.
PROVISIONING_SPOT = "spot"
PROVISIONING_STANDARD = "standard"
PROVISIONING_MODES = (PROVISIONING_SPOT, PROVISIONING_STANDARD)
ENV_PROVISIONING = "SPOT_CONTROL_PROVISIONING"
STANDARD_MACHINE_TYPES = ("e2-micro",)
FREE_TIER_REGIONS = ("us-central1", "us-east1", "us-west1")
STANDARD_DEFAULT_REGIONS = ("us-central1",)
# 15 GB holds Debian 12 (about 2 GB), docker.io, the 2 GiB swap file, the dashboard and cloudflared images, and
# capped container logs with room to spare. Two such disks (make-before-break, or a retained boot disk) still fit
# the 30 GB-month pd-standard free tier.
STANDARD_BOOT_DISK_GB = 15
STANDARD_BOOT_DISK_TYPE = "pd-standard"
FREE_TIER_DISK_GB = 30
# GCE provisioning models as parsed from the inventory (``scheduling.provisioningModel``); "" when unknown.
MODEL_SPOT = "SPOT"
MODEL_STANDARD = "STANDARD"

DEFAULT_REGIONS = ("us-central1", "us-east1", "us-west1")
DEFAULT_MAX_DAILY_USD = Decimal("0.50")
DEFAULT_DASHBOARD_REPO = "swack-tools/spot-github-runners"
# Published by .github/workflows/dashboard-image.yml for every push to main of DEFAULT_DASHBOARD_REPO.
DEFAULT_DASHBOARD_IMAGE_REPO = "ghcr.io/swack-tools/spot-dashboard"
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
    "dashboard-config-repo",
    "dashboard-controlled-targets",
    "dashboard-global-admins",
)
# Dashboard target policy. Neither is a secret, so both travel as instance metadata and the startup script
# writes them to the dashboard env file on every boot. GCE metadata values can't hold commas in a
# ``--metadata`` list, so the controlled targets travel ``;``-separated and become a comma list on the VM.
ENV_CONFIG_REPO = "SPOT_CONFIG_REPO"
ENV_CONTROLLED_TARGETS = "DASHBOARD_CONTROLLED_TARGETS"
# Global administrators (login:numeric_id, or "none"); unset keeps the dashboard default. Not a secret, so it
# travels as ;-separated instance metadata and becomes DASHBOARD_GLOBAL_ADMINS in the dashboard env file.
ENV_GLOBAL_ADMINS = "DASHBOARD_GLOBAL_ADMINS"
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
FAILED_ZONE_TTL_SECONDS = 1800     # A (machine type, zone) that failed a create is retried after this long.
MAX_STEPS_PER_TICK = 12

STOPPED_STATES = frozenset({"TERMINATED", "STOPPED", "SUSPENDED"})
STOP_RETRY_SECONDS = 120
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
# The commit tag .github/workflows/dashboard-image.yml publishes (name:<40 hex>). Accepted only in standard mode,
# where a digest is preferred. Mirrors dashboard_image_ok in cloud-init/control-vm-startup.sh.
_SHA_TAG_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[0-9a-f]{40}$")


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
    config_repo: str = ""
    controlled_targets: str = ""
    global_admins: str = ""
    provisioning: str = PROVISIONING_SPOT
    # SPOT_DASHBOARD_IMAGE_TRACK: "" (off) or "main" (roll out the published :main image; see image_tracking).
    image_track: str = ""
    image_track_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, project: str, env: Mapping[str, str] | None = None, **overrides: Any) -> ControlConfig:
        env = os.environ if env is None else env
        regions = tuple(r.strip() for r in env.get("SPOT_CONTROL_REGIONS", "").split(",") if r.strip())
        external = env.get("SPOT_CONTROL_EXTERNAL_IP", "1").strip().lower() not in ("0", "false", "no", "off")
        provisioning = overrides.get("provisioning")
        if provisioning is None:
            provisioning = env.get(ENV_PROVISIONING, "")
        # Normalized but never defaulted when set: a misspelt mode is reported by runtime_problems() and
        # pauses every lifecycle change, instead of silently running the other mode.
        provisioning = str(provisioning or "").strip().lower() or PROVISIONING_SPOT
        values: dict[str, Any] = dict(
            project=project,
            # The free-tier VM defaults to one free-tier region; Spot keeps its wider default search.
            regions=regions or (STANDARD_DEFAULT_REGIONS if provisioning == PROVISIONING_STANDARD
                                else DEFAULT_REGIONS),
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
            config_repo=str(env.get(ENV_CONFIG_REPO, "") or "").strip(),
            controlled_targets=str(env.get(ENV_CONTROLLED_TARGETS, "") or "").strip(),
            global_admins=str(env.get(ENV_GLOBAL_ADMINS, "") or "").strip(),
            # Normalized but never defaulted when set, like the provisioning mode: a misspelt value pauses changes.
            image_track=str(env.get(image_tracking.ENV_IMAGE_TRACK, "") or "").strip().lower(),
            image_track_token=str(env.get(image_tracking.ENV_IMAGE_TRACK_TOKEN, "") or "").strip(),
        )
        values.update({key: value for key, value in overrides.items() if value is not None})
        values["provisioning"] = provisioning
        if isinstance(values["regions"], str):
            values["regions"] = tuple(r.strip() for r in values["regions"].split(",") if r.strip())
        values["max_daily_usd"] = Decimal(str(values["max_daily_usd"]))
        return cls(**values)

    @property
    def standard(self) -> bool:
        """True for the always-on free-tier e2-micro mode."""
        return self.provisioning == PROVISIONING_STANDARD

    @property
    def machine_types(self) -> tuple[str, ...]:
        """Control VM shapes this mode may create."""
        return STANDARD_MACHINE_TYPES if self.standard else CONTROL_MACHINE_TYPES

    @property
    def boot_disk(self) -> tuple[int, str]:
        """(size in GB, disk type) of a new control VM's boot disk."""
        return (STANDARD_BOOT_DISK_GB, STANDARD_BOOT_DISK_TYPE) if self.standard else (BOOT_DISK_GB, BOOT_DISK_TYPE)

    def provisioning_problems(self) -> list[str]:
        """Problems with the provisioning mode and, for the free-tier mode, its regions."""
        if self.provisioning not in PROVISIONING_MODES:
            return [f"{ENV_PROVISIONING} must be {' or '.join(PROVISIONING_MODES)}, not {self.provisioning!r}"]
        if not self.standard:
            return []
        outside = [r for r in self.regions if r not in FREE_TIER_REGIONS]
        if outside:
            # A standard e2-micro anywhere else is billed at the full on-demand rate.
            return [f"{ENV_PROVISIONING}=standard runs the free-tier e2-micro, which is free only in "
                    f"{', '.join(FREE_TIER_REGIONS)}; SPOT_CONTROL_REGIONS also lists {', '.join(outside)}"]
        return []

    @property
    def tracking(self) -> bool:
        """True when the published :main dashboard image is rolled out automatically."""
        return self.image_track == image_tracking.TRACK_MAIN

    def tracking_problems(self) -> list[str]:
        """Problems with SPOT_DASHBOARD_IMAGE_TRACK (empty when it is off)."""
        if self.image_track not in image_tracking.TRACK_VALUES:
            return [f"{image_tracking.ENV_IMAGE_TRACK} must be empty or {image_tracking.TRACK_MAIN}, "
                    f"not {self.image_track!r}"]
        if self.tracking and self.dashboard_repo != DEFAULT_DASHBOARD_REPO:
            return [f"{image_tracking.ENV_IMAGE_TRACK}=main follows {DEFAULT_DASHBOARD_IMAGE_REPO}, which only "
                    f"{DEFAULT_DASHBOARD_REPO} publishes; SPOT_DASHBOARD_REPO is {self.dashboard_repo}"]
        return []

    def runtime_problems(self) -> list[str]:
        """Settings without which the manager cannot observe or control the VMs safely."""
        problems = []
        if not self.access_client_id or not self.access_client_secret:
            problems.append("CF_ACCESS_CLIENT_ID/CF_ACCESS_CLIENT_SECRET are not set (health probes need Access)")
        if not self.control_secret:
            problems.append(f"{edge_state.ENV_CONTROL_SECRET} is not set (freeze/import need control tokens)")
        if not self.regions:
            problems.append("no control VM regions are allowed")
        problems.extend(self.provisioning_problems())
        problems.extend(self.tracking_problems())
        return problems

    @property
    def effective_dashboard_image(self) -> str:
        """The dashboard image a new control VM pulls, or "" when it builds from ``dashboard_source_ref``.

        The free-tier e2-micro never builds on the box: without SPOT_DASHBOARD_IMAGE it pulls the commit tag
        that .github/workflows/dashboard-image.yml publishes for SPOT_DASHBOARD_SOURCE_REF, but only for the
        repository that workflow publishes. A digest (SPOT_DASHBOARD_IMAGE) is preferred: a tag can be moved.
        """
        if self.dashboard_image or not self.standard:
            return self.dashboard_image
        if _SHA_RE.match(self.dashboard_source_ref) and self.dashboard_repo == DEFAULT_DASHBOARD_REPO:
            return f"{DEFAULT_DASHBOARD_IMAGE_REPO}:{self.dashboard_source_ref}"
        return ""

    def create_problems(self) -> list[str]:
        """Settings that must be valid before a control VM is created."""
        problems = self.provisioning_problems()
        if self.standard:
            image = self.effective_dashboard_image
            if not image:
                problems.append("the standard (e2-micro) control VM never builds the dashboard image: set "
                                "SPOT_DASHBOARD_IMAGE to the published digest (name@sha256:<64 hex>), or "
                                f"SPOT_DASHBOARD_SOURCE_REF to a commit published as {DEFAULT_DASHBOARD_IMAGE_REPO}:<sha>")
            elif not (_DIGEST_RE.match(image) or _SHA_TAG_IMAGE_RE.match(image)):
                problems.append("the standard control VM's dashboard image must be pinned by digest "
                                "(name@sha256:<64 hex>) or by a 40-hex commit tag (name:<sha>)")
        elif self.dashboard_image:
            if not _DIGEST_RE.match(self.dashboard_image):
                problems.append("dashboard image must be pinned by digest (name@sha256:<64 hex>)")
        elif not _SHA_RE.match(self.dashboard_source_ref):
            problems.append("set a digest-pinned dashboard image or a 40-hex dashboard source commit")
        if not _REPO_RE.match(self.dashboard_repo):
            problems.append("dashboard repository must be owner/name")
        problems.extend(public_url_problems(self.public_url))
        if not _ID_RE.match(self.cloudflare_account_id or ""):
            problems.append(f"{edge_state.ENV_ACCOUNT_ID} is missing or malformed")
        if not _ID_RE.match(self.cloudflare_kv_namespace_id or ""):
            problems.append(f"{edge_state.ENV_NAMESPACE_ID} is missing or malformed")
        if not (_IMAGE_TAG_RE.match(self.cloudflared_image) or _DIGEST_RE.match(self.cloudflared_image)) or self.cloudflared_image.endswith(":latest"):
            problems.append("cloudflared image must be pinned to a version tag or digest")
        if self.service_account and not re.match(r"^[^\s,@]+@[^\s,@]+$", self.service_account):
            problems.append("control VM service account must be an e-mail address")
        problems.extend(session_not_before_problems(self.session_not_before))
        problems.extend(dashboard_target_problems(self.config_repo, self.controlled_targets))
        try:
            global_admins_metadata(self.global_admins)
        except ValueError as exc:
            problems.append(str(exc))
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


_DNS_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_PUBLIC_AUTHORITY_RE = re.compile(rf"({_DNS_LABEL}(?:\.{_DNS_LABEL})*)(?::([1-9][0-9]{{0,4}}))?")


def public_origin(url: str) -> str | None:
    """``https://host[:port]`` for a valid dashboard public URL, else None.

    The URL must be ``https://`` plus a DNS host name, an optional port 1-65535 without leading zeros (the
    origin must equal the browser's ``Origin`` header) and at most a trailing ``/``: no path, query, fragment or
    userinfo. The startup script derives DASHBOARD_PUBLIC_ORIGIN and the OAuth callback from it and refuses
    anything else, so a VM created with another value would never come up. Two independent checks must both
    pass: the parsed URL (``urllib.parse``) and the exact authority shape that ``public_origin_of`` in
    cloud-init/control-vm-startup.sh accepts.
    """
    authority = _public_authority(url)
    return f"https://{authority}" if authority is not None and _public_url_parts_ok(url) else None


def _public_authority(url: Any) -> str | None:
    """The ``host[:port]`` the startup script's ``public_origin_of`` accepts, else None (mirrors it exactly)."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    authority = url[len("https://"):]
    authority = authority[:-1] if authority.endswith("/") else authority
    match = _PUBLIC_AUTHORITY_RE.fullmatch(authority)
    if not match or (match.group(2) is not None and not 1 <= int(match.group(2)) <= 65535):
        return None
    return authority


def _public_url_parts_ok(url: Any) -> bool:
    """The parsed URL is https with a host, an optional valid port and nothing else (no userinfo, path, query,
    or fragment)."""
    import urllib.parse

    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port outside 0-65535 or a non-numeric one
    except ValueError:
        return False
    return (parts.scheme == "https" and bool(parts.hostname) and parts.username is None and parts.password is None
            and parts.port != 0 and parts.path in ("", "/")
            # Literal checks, so an empty query ("?") or fragment ("#") is refused too.
            and "?" not in url and "#" not in url)


def public_url_problems(value: str) -> list[str]:
    """Problems with SPOT_DASHBOARD_PUBLIC_URL, checked before a control VM is created."""
    if public_origin(value) is None:
        return ["dashboard public URL must be https://host[:port] with an optional trailing slash and no path, "
                "query, fragment or user info"]
    return []


def config_repo_problems(value: str) -> list[str]:
    """Problems with an optional SPOT_CONFIG_REPO value (empty means the dashboard default).

    Accepts exactly what the dashboard's ``resolve_config_repo`` accepts: ``owner/repo`` with a GitHub
    login and repository name. Mirrors ``config_repo_ok`` in cloud-init/control-vm-startup.sh.
    """
    if not value:
        return []
    from .dashboard_auth import parse_controlled_target
    try:
        if parse_controlled_target(value).kind == "repo":
            return []
    except ValueError:
        pass
    return [f"{ENV_CONFIG_REPO} must be owner/repo"]


def controlled_target_names(value: str) -> list[str]:
    """Normalized DASHBOARD_CONTROLLED_TARGETS entries, parsed exactly as the dashboard parses them.

    Raises ValueError on any entry that is not an organization or ``owner/repo``.
    """
    from .dashboard_auth import parse_controlled_targets_env
    return [target.name for target in parse_controlled_targets_env(value)]


def dashboard_target_problems(config_repo: str, controlled_targets: str) -> list[str]:
    """Problems with the dashboard target policy forwarded to the control VM."""
    problems = config_repo_problems(config_repo)
    try:
        controlled_target_names(controlled_targets)
    except ValueError as exc:
        problems.append(f"{ENV_CONTROLLED_TARGETS} must list organizations or owner/repo targets: {exc}")
    return problems


def global_admins_metadata(value: str) -> str:
    """Metadata form of DASHBOARD_GLOBAL_ADMINS: "" (dashboard default), "none", or ;-separated login:id.

    Parsed exactly as the dashboard parses it, so an invalid value fails here before any VM is created.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    from .dashboard_auth import parse_global_admins
    admins = parse_global_admins(text)
    if not admins:
        return "none"
    return ";".join(f"{login}:{uid}" for login, uid in sorted(admins))


def metadata_items(config: ControlConfig, slot: str) -> dict[str, str]:
    """Non-secret instance metadata; secrets are fetched from Secret Manager at boot."""
    edge_state.validate_slot(slot)
    items = {
        "spot-control-slot": slot,
        "dashboard-image": config.effective_dashboard_image,
        "dashboard-repo": config.dashboard_repo,
        # Never on the free-tier VM, which only pulls (an empty value is dropped below).
        "dashboard-source-ref": "" if config.standard else config.dashboard_source_ref,
        "dashboard-public-url": config.public_url,
        "cloudflare-account-id": config.cloudflare_account_id,
        "cloudflare-kv-namespace-id": config.cloudflare_kv_namespace_id,
        "cloudflared-image": config.cloudflared_image,
        "dashboard-session-not-before": config.session_not_before,
        "dashboard-config-repo": config.config_repo,
        "dashboard-controlled-targets": ";".join(controlled_target_names(config.controlled_targets)),
        "dashboard-global-admins": global_admins_metadata(config.global_admins),
    }
    problems = config_repo_problems(config.config_repo)
    if problems:
        raise ValueError("; ".join(problems))
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
    provisioning_model: str = ""  # MODEL_SPOT, MODEL_STANDARD, or "" when the inventory did not say
    # scheduling.automaticRestart: True means GCE restarts the VM by itself after a crash or host error, so a
    # TERMINATED status is not a stable fence (see _stable_stop). None when the inventory did not say.
    automatic_restart: bool | None = None

    @property
    def region(self) -> str:
        return self.zone.rsplit("-", 1)[0]

    @property
    def running(self) -> bool:
        return self.status == "RUNNING"

    @property
    def standard(self) -> bool:
        """A known non-preemptible VM: TERMINATED means stopped (never preempted), and price never moves it."""
        return self.provisioning_model == MODEL_STANDARD

    @property
    def spot(self) -> bool:
        return self.provisioning_model == MODEL_SPOT


def provisioning_model_of(row: Mapping[str, Any]) -> str:
    """MODEL_SPOT, MODEL_STANDARD, or "" from an inventory row's ``scheduling``.

    A legacy preemptible VM counts as Spot. Without a ``scheduling`` object the model is unknown and the VM is
    handled the way every control VM was before standard mode existed (as Spot).
    """
    scheduling = row.get("scheduling")
    if not isinstance(scheduling, dict):
        return ""
    model = str(scheduling.get("provisioningModel") or "").upper()
    if model == MODEL_SPOT or scheduling.get("preemptible") is True:
        return MODEL_SPOT
    if model == MODEL_STANDARD or (not model and scheduling.get("preemptible") is False):
        return MODEL_STANDARD
    return ""


def automatic_restart_of(row: Mapping[str, Any]) -> bool | None:
    """``scheduling.automaticRestart`` of an inventory row: True, False, or None when absent."""
    scheduling = row.get("scheduling")
    value = scheduling.get("automaticRestart") if isinstance(scheduling, dict) else None
    return value if isinstance(value, bool) else None


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
            provisioning_model=provisioning_model_of(row),
            automatic_restart=automatic_restart_of(row),
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


def standard_control_offers(regions: Sequence[str],
                            zones_by_shape: Mapping[str, Sequence[str]]) -> list[ControlOffer]:
    """Fixed free-tier offers: e2-micro in every listed zone of the free-tier ``regions``, at $0/day.

    No Spot or billing catalog lookup: the free tier covers the VM and up to 30 GB-months of pd-standard. The
    external IPv4 address (about $0.005/hour) is billed separately and deliberately not counted, because every
    control VM needs it. Ordered by the configured region preference, then zone, so a stockout falls through to
    the next zone of the same region first.
    """
    offers: list[ControlOffer] = []
    for region in dict.fromkeys(regions):  # configured order, duplicates dropped
        if region not in FREE_TIER_REGIONS:
            continue  # never offered: ControlConfig.provisioning_problems() also refuses such a region
        for machine_type in STANDARD_MACHINE_TYPES:
            zones = sorted({z for z in zones_by_shape.get(machine_type, ()) if z.rsplit("-", 1)[0] == region})
            offers.extend(ControlOffer(machine_type, region, zone, Decimal("0")) for zone in zones)
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
              force_rotate: bool = False, provisioning: str = PROVISIONING_SPOT) -> Plan:
    """Return the single next action for the control VM pair. Pure: no I/O, no mutation.

    ``provisioning`` is the configured mode. The observed VM's own provisioning model decides how a stopped
    VM is read: a stopped Spot VM was preempted (start it, then replace it after a grace period), while a
    stopped standard VM is restarted and replaced only when its restart fails. Price never rotates away from
    a standard VM. With ``provisioning`` standard, a healthy active Spot VM is replaced by the free-tier VM
    through the same make-before-break rotation (freeze, import, version floor, fence, cutover) as a price
    rotation.
    """
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
        if active.standard:
            # Not preempted capacity: a standard VM stops only when someone (or a guest shutdown) stopped it,
            # and it keeps its zone. Restart it; replace it only once the restarts themselves fail.
            failed_at = (control_state.get("start_failed_at") or {}).get(slot)
            if down_for >= START_GRACE_SECONDS and failed_at is not None:
                return recreate(f"standard {active.name} {active.status} for {int(down_for)}s and its last "
                                f"start failed")
            return start_or_wait(active, f"standard {active.name} is {active.status} (stopped, not preempted); "
                                         f"restarting it")
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
    last_end = max(control_state.get("last_transition_end_at") or 0, control_state.get("last_abort_at") or 0)
    cooling = bool(last_end and now - last_end < ROTATION_COOLDOWN_SECONDS)
    if provisioning == PROVISIONING_STANDARD and active.spot:
        # Mode switch: only a VM the inventory positively reports as Spot is replaced, so a VM whose model is
        # unknown never rotates in a loop. The cooldown also spaces out retries after an aborted switch.
        if cooling:
            return Plan("noop", f"{active.name} is Spot and {ENV_PROVISIONING}=standard; rotation cooldown "
                                f"active", slot=slot)
        return Plan("rotate", f"{ENV_PROVISIONING}=standard: replace Spot {active.name} with the free-tier "
                              f"{best.machine_type}@{best.zone}", slot=slot, target_slot=other_slot(slot), offer=best)
    if active.standard:
        # Never rotate away from the always-on VM for price, and never automatically back to Spot: switching
        # back is an explicit operator rotation, so controllers with different settings cannot flap.
        why = "standard VMs are never rotated for price"
        if provisioning != PROVISIONING_STANDARD:
            why += f"; {ENV_PROVISIONING} is {provisioning}, run rotate to move back to Spot"
        return Plan("noop", f"{active.name} healthy; {why}", slot=slot)
    if current is None:
        return Plan("noop", f"{active.name} healthy; its shape/region has no current price", slot=slot)
    if cooling:
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
        control_state.get("start_failed_at", {}).pop(origin.slot, None)
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


def _offer_key(machine_type: str, zone: str) -> str:
    """Persisted identity of one placement in create-failure records (JSON object keys must be strings)."""
    return f"{machine_type}@{zone}"


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


# stderr lines gcloud prints on a COMPLETE instance listing. Anything else (for example "Some requests did not
# succeed") means the inventory may be partial. The filter-keys warning appears when no instance carries the
# role label at all, which is exactly the empty inventory that bootstrap starts from.
_BENIGN_INVENTORY_LINE_RES = (
    re.compile(r"WARNING: The following filter keys were not present in any resource : labels\.%s"
               % re.escape(ROLE_LABEL)),
)
# gcloud's update notice and the lines that follow it (accepted only after the notice itself).
_UPDATE_NOTICE_RE = re.compile(r"Updates are available for some (?:Google Cloud CLI|Cloud SDK) components\."
                               r"(?:\s+To install them,)?")
_UPDATE_NOTICE_FOLLOW_RES = (re.compile(r"(?:To install them, )?please run:"),
                             re.compile(r"\$ gcloud components update"))


def inventory_stderr_problem(stderr: str) -> str | None:
    """The first stderr line that is not a known-benign gcloud message, or None when every line is."""
    in_update_notice = False
    for raw in (stderr or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(r.fullmatch(line) for r in _BENIGN_INVENTORY_LINE_RES):
            in_update_notice = False
            continue
        if _UPDATE_NOTICE_RE.fullmatch(line):
            in_update_notice = True
            continue
        if in_update_notice and any(r.fullmatch(line) for r in _UPDATE_NOTICE_FOLLOW_RES):
            continue
        return line
    return None


def list_command(project: str) -> list[str]:
    # scheduling tells a Spot VM (TERMINATED means preempted) from a standard one (TERMINATED means stopped).
    return ["gcloud", "compute", "instances", "list", f"--project={project}",
            f"--filter=labels.{ROLE_LABEL}={ROLE_VALUE}",
            "--format=json(name,zone,status,labels,id,machineType,scheduling.provisioningModel,"
            "scheduling.preemptible,scheduling.automaticRestart)"]


def zones_command(project: str, machine_types: Sequence[str] = CONTROL_MACHINE_TYPES) -> list[str]:
    return ["gcloud", "compute", "machine-types", "list", f"--project={project}",
            f"--filter=name=({' '.join(machine_types)})", "--format=json(name,zone)"]


def start_command(vm: ControlVm, project: str) -> list[str]:
    return ["gcloud", "compute", "instances", "start", vm.name, f"--zone={vm.zone}",
            f"--project={project}", "--quiet"]


def no_restart_command(vm: ControlVm, project: str) -> list[str]:
    """Turn off GCE automatic restart, so a TERMINATED control VM stays down until this manager starts it."""
    return ["gcloud", "compute", "instances", "set-scheduling", vm.name, f"--zone={vm.zone}",
            f"--project={project}", "--no-restart-on-failure", "--quiet"]


def metadata_command(vm: ControlVm, project: str) -> list[str]:
    """Read-only: a control VM's id and instance metadata (its pinned ``dashboard-image``)."""
    return ["gcloud", "compute", "instances", "describe", vm.name, f"--zone={vm.zone}", f"--project={project}",
            "--format=json(id,metadata.items)"]


def dashboard_image_of(stdout: str | None) -> tuple[str, str]:
    """(instance id, dashboard-image metadata value) from ``metadata_command`` output; "" when absent."""
    try:
        row = json.loads(stdout or "{}")
    except ValueError:
        return "", ""
    if not isinstance(row, dict):
        return "", ""
    items = (row.get("metadata") or {}).get("items") if isinstance(row.get("metadata"), dict) else None
    image = next((str(i.get("value") or "") for i in items or [] if isinstance(i, dict)
                  and i.get("key") == "dashboard-image"), "")
    return str(row.get("id") or ""), image


def stop_command(vm: ControlVm, project: str) -> list[str]:
    return ["gcloud", "compute", "instances", "stop", vm.name, f"--zone={vm.zone}",
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
    if offer.machine_type not in config.machine_types:
        raise ValueError(f"{offer.machine_type} is not an allowed {config.provisioning} control VM shape")
    zone_region = offer.zone.rsplit("-", 1)[0]
    if config.standard and (zone_region not in FREE_TIER_REGIONS or zone_region not in config.regions):
        raise ValueError(f"zone {offer.zone} is outside the free-tier regions allowed for the standard control VM")
    metadata = ",".join(f"{key}={value}" for key, value in metadata_items(config, slot).items())
    if config.standard:
        # Always-on: live-migrated through host maintenance. --no-restart-on-failure keeps the invariant the
        # rollback and recovery fences rely on: they accept a TERMINATED VM as fenced, and GCE automatic restart
        # would bring a crashed (TERMINATED, not API-stopped) VM back by itself after the fence passed, possibly
        # while origin:active, which is eventually consistent, still names it. A frozen VM would come back frozen
        # (the freeze marker lives on its boot disk), but a VM fenced by stop has no such marker. The manager (the
        # watchdog runs it every 2 minutes) restarts a stopped standard VM instead. See docs/edge-dashboard.md.
        scheduling = ["--provisioning-model=STANDARD", "--maintenance-policy=MIGRATE", "--no-restart-on-failure"]
    else:
        scheduling = ["--provisioning-model=SPOT", "--instance-termination-action=STOP"]
    disk_gb, disk_type = config.boot_disk
    command = [
        "gcloud", "compute", "instances", "create", control_vm_name(slot),
        f"--project={config.project}", f"--zone={offer.zone}", f"--machine-type={offer.machine_type}",
        *scheduling,
        f"--image-family={IMAGE_FAMILY}", f"--image-project={IMAGE_PROJECT}",
        f"--boot-disk-size={disk_gb}GB", f"--boot-disk-type={disk_type}", "--boot-disk-auto-delete",
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


def created_instance_id(stdout: str | None, name: str) -> str:
    """Provider instance id from ``instances create --format=json`` output, or "" when it is absent."""
    try:
        rows = json.loads(stdout or "[]")
    except ValueError:
        return ""
    rows = [rows] if isinstance(rows, dict) else rows
    if not isinstance(rows, list):
        return ""
    return next((str(row["id"]) for row in rows
                 if isinstance(row, dict) and row.get("name") == name and row.get("id")), "")


GCLOUD_TIMEOUT_SECONDS = 600  # the longest single provider call; the lease guard leaves more than this


def default_gcloud(argv: list[str]) -> Any:
    # Never accept partial Compute results (for example a zone that could not be listed): an omitted
    # VM would look missing and could be created again, giving two VMs the same slot identity.
    env = dict(os.environ, CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR="false") if argv[1:2] == ["compute"] else None
    return subprocess.run(argv, check=False, text=True, capture_output=True, timeout=GCLOUD_TIMEOUT_SECONDS, env=env)


def default_offer_source(config: ControlConfig, gcloud: Callable[[list[str]], Any]) -> list[ControlOffer]:
    """Price the allowed shapes in the allowed regions from the billing catalog (read-only).

    The standard mode lists only the zones that offer e2-micro and never reads a price: its offers are fixed.
    """
    result = gcloud(zones_command(config.project, config.machine_types))
    if result.returncode != 0:
        raise RuntimeError(f"machine-type inventory failed: {(result.stderr or '').strip()[:300]}")
    zones: dict[str, list[str]] = {}
    for row in json.loads(result.stdout or "[]"):
        zone = str(row.get("zone", "")).rsplit("/", 1)[-1]
        zones.setdefault(str(row.get("name", "")), []).append(zone)
    if config.standard:
        return standard_control_offers(config.regions, zones)
    from .runner_manager import spot_rates
    from .storage_selection import storage_rates

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
                 clock: Callable[[], float] | None = None,
                 image_tracker: image_tracking.ImageTracker | None = None) -> None:
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
        # (transition id, snapshot) captured when the rollback fence froze the replacement this run.
        self._replacement_freeze: tuple[str, dict[str, Any] | None] | None = None
        self._image_tracker = image_tracker
        self._rollout: image_tracking.RolloutDecision | None = None  # set by plan() for the rotate it returns
        # Previews (tick without apply) record what an applied tick would change, so a caller can tell an idle
        # check (nothing to do, nothing to save) from one that needs the lock.
        self.preview_state: dict | None = None
        self.preview_actions: list[str] = []
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
        if inventory_stderr_problem(result.stderr or ""):
            # gcloud can exit 0 with partial results and diagnostics on stderr; completeness is unknown.
            # Only lines on the known-benign allowlist (see inventory_stderr_problem) are tolerated.
            raise RuntimeError("control VM inventory returned diagnostics; completeness unknown: "
                               f"{result.stderr.strip()[:300]}")
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
        if self.config.standard and active.spot:
            return True  # the switch to the free-tier VM needs its (fixed, price-free) offers now
        last = self.cstate.get("last_price_check_at") or 0
        return self.clock() - last >= PRICE_CHECK_INTERVAL_SECONDS

    # -- planning ------------------------------------------------------------------------
    def plan(self, obs: Observation, *, force_rotate: bool = False, control_state: Mapping | None = None) -> Plan:
        self._rollout = None
        if obs.problems:
            return _wait("; ".join(obs.problems))
        if force_rotate and obs.offers is None and not self.cstate.get("transition"):
            return _wait("rotation requested but no price scan is available")
        plan = plan_next(vms=obs.vms, origin=obs.origin, origin_known=obs.origin_known, health=obs.health,
                         offers=obs.offers, control_state=control_state or self.cstate, now=self.clock(),
                         max_daily_usd=self.config.max_daily_usd, force_rotate=force_rotate,
                         provisioning=self.config.provisioning)
        if self.config.tracking and not force_rotate and control_state is None:
            plan = self._image_rollout(obs, plan) or plan
        if self.config.standard and plan.offer is not None and not plan.resume:
            # Refuse before a transition begins (for example the switch from Spot) rather than after it fails
            # to create: a free-tier VM without a pinned published image would have to build on 1 GB.
            image = (self._rollout.image if self._rollout else self._base_image()) if self.config.tracking else ""
            problems = (dataclasses.replace(self.config, dashboard_image=image) if image
                        else self.config).create_problems()
            if problems:
                return _wait(f"{plan.action} refused: " + "; ".join(problems), slot=plan.slot)
        return plan

    def _prepare(self, obs: Observation, *, apply: bool) -> None:
        """The state changes and maintenance every tick makes before it plans (on the preview copy without apply)."""
        now = self.clock()
        if not obs.problems and obs.origin_known:
            update_tracking(self.cstate, obs.origin, obs.vms, obs.health, now)
        if obs.offers is not None:
            self.cstate["last_price_check_at"] = now
        if not obs.problems:
            self._disable_automatic_restart(obs, apply=apply)

    def tick(self, *, apply: bool, force_rotate: bool = False) -> Plan:
        """One lifecycle step. Without ``apply`` only reads happen and nothing is saved."""
        self.preview_actions = []
        if not apply:
            real_state = self.state
            self.state = copy.deepcopy(real_state)  # Previews never touch the caller's state.
            try:
                obs = self.observe(want_offers=True if force_rotate else None)
                self._prepare(obs, apply=False)
                plan = self.plan(obs, force_rotate=force_rotate)
                self.preview_state = self.state
            finally:
                self.state = real_state
            self._note("plan (dry run, nothing changed): %s", plan.describe())
            return plan
        obs = self.observe(want_offers=True if force_rotate else None)
        self._prepare(obs, apply=True)
        plan = self.plan(obs, force_rotate=force_rotate)
        self._note("plan: %s", plan.describe())
        self.execute(plan, obs)
        self.save()
        return plan

    def preview_is_idle(self, plan: Plan, saved_state: Mapping[str, Any]) -> bool:
        """After ``tick(apply=False)``: True when an applied tick would change nothing. The plan waits or is a
        noop, no maintenance action is pending, and the preview state equals ``saved_state`` apart from the
        ``updated_at`` stamp. Such a check needs neither the project lock nor a state write."""
        if plan.action not in ("wait", "noop") or self.preview_actions or self.preview_state is None:
            return False

        def strip(state: Mapping[str, Any]) -> dict:
            copied = copy.deepcopy(dict(state))
            section = copied.get(STATE_KEY)
            if isinstance(section, dict):
                section.pop("updated_at", None)
            return copied
        return strip(self.preview_state) == strip(saved_state)

    # -- automatic restart -----------------------------------------------------------------
    def _disable_automatic_restart(self, obs: Observation, *, apply: bool) -> None:
        """Turn off GCE automatic restart on every control VM that has it (for example one an operator changed).

        The recovery and rollback fences accept a TERMINATED VM as fenced. With automatic restart, a VM that
        crashed (TERMINATED without an API stop) can come back by itself after the fence passed, so the manager
        keeps the setting off everywhere, ahead of any fence, and restarts stopped standard VMs itself.
        """
        for vm in obs.vms:
            if vm.automatic_restart is not True:
                continue
            if not apply:
                self.preview_actions.append(f"turn off automatic restart on {vm.name}")
                self._note("would turn off GCE automatic restart on %s (dry run)", vm.name)
                continue
            result = self.gcloud(no_restart_command(vm, self.config.project))
            if result.returncode == 0:
                self._note("turned off GCE automatic restart on %s: fences rely on a stopped control VM staying "
                           "stopped, and this manager restarts it instead", vm.name, level=logging.WARNING)
            else:
                self._note("could not turn off GCE automatic restart on %s: %s", vm.name,
                           (result.stderr or "").strip()[:300], level=logging.ERROR)

    def _stable_stop(self, t: dict, role: str, vm: ControlVm) -> bool:
        """True when ``vm``, observed stopped, stays stopped: GCE automatic restart is not on for it.

        Otherwise turn it off (only on the recorded instance) and wait for a fresh observation to confirm it.
        """
        if vm.automatic_restart is not True:
            return True
        problem = self._identity_problem(t, role, vm, adopt=False)
        if problem:
            t["error"] = f"refusing to change scheduling: {problem}"
        else:
            result = self.gcloud(no_restart_command(vm, self.config.project))
            t["error"] = (f"{vm.name} is {vm.status}, but GCE automatic restart is on, so it could restart by "
                          f"itself after this fence; turned it off (exit {getattr(result, 'returncode', None)}) and "
                          f"waiting for the inventory to confirm")
        t["updated_at"] = self.clock()
        self.save()
        self._note(t["error"], level=logging.WARNING)
        return False

    # -- image tracking ------------------------------------------------------------------
    def _tracker(self) -> image_tracking.ImageTracker:
        if self._image_tracker is None:
            token = self.config.image_track_token
            repository = DEFAULT_DASHBOARD_IMAGE_REPO
            self._image_tracker = image_tracking.ImageTracker(
                repository,
                image_tracking.RegistryClient(repository, token) if token else None,
                image_tracking.GitHubClient(self.config.dashboard_repo, token) if token else None,
                self.clock)
        return self._image_tracker

    def _active_image(self, vm: ControlVm) -> str:
        """The ``dashboard-image`` the active VM was created with: from state when it names this instance, else
        read once from the instance metadata (read-only) and recorded."""
        record = (self.cstate.get("image") or {}).get("active")
        if isinstance(record, dict) and record.get("slot") == vm.slot and vm.instance_id \
                and str(record.get("instance_id") or "") == vm.instance_id:
            return str(record.get("image") or "")
        result = self.gcloud(metadata_command(vm, self.config.project))
        if result.returncode != 0:
            self._note("could not read the dashboard image of %s: %s", vm.name, (result.stderr or "").strip()[:200],
                       level=logging.WARNING)
            return ""
        instance_id, image = dashboard_image_of(result.stdout)
        if not instance_id or instance_id != vm.instance_id:
            return ""
        self.cstate.setdefault("image", {})["active"] = {"slot": vm.slot, "instance_id": vm.instance_id,
                                                         "image": image, "at": self.clock(), "source": "metadata"}
        return image

    def _base_image(self) -> str:
        """Image for a new control VM that is not an image rollout while tracking: the active VM's recorded image
        (so a recovery never moves to another image), else the configured one."""
        record = (self.cstate.get("image") or {}).get("active") or {}
        recorded = str(record.get("image") or "")
        digest, commit = image_tracking.parse_image(recorded, DEFAULT_DASHBOARD_IMAGE_REPO)
        if digest:
            return recorded
        if commit:
            # A published commit tag (the standard VM accepts it) is kept as is, never replaced by an older
            # configured image. Spot VMs take digests only, so resolve the tag's digest once and record it.
            if self.config.standard:
                return recorded
            resolved = str(record.get("resolved_image") or "")
            if not resolved:
                registry = self._tracker().registry
                try:
                    resolved = image_tracking.digest_ref(DEFAULT_DASHBOARD_IMAGE_REPO, registry.resolve(commit)) \
                        if registry is not None else ""
                except image_tracking.TrackingError as exc:
                    self._note("could not resolve %s to a digest: %s", recorded, exc, level=logging.WARNING)
                    resolved = ""
                if resolved:
                    self.cstate.setdefault("image", {}).setdefault("active", {})["resolved_image"] = resolved
            # Unresolved: the commit tag itself, which create_problems refuses on Spot, so the step waits instead of
            # moving to another image.
            return resolved or recorded
        return self.config.effective_dashboard_image

    def _image_rollout(self, obs: Observation, plan: Plan) -> Plan | None:
        """While tracking: a rotate plan to a newer published :main image, or the base plan with the reason."""
        if plan.action != "noop" or self.cstate.get("transition") or obs.origin is None:
            return None
        active, other = obs.vm(obs.origin.slot), obs.vm(other_slot(obs.origin.slot))
        if active is not None and not self._same_provisioning(active):
            # A rollout creates the replacement in the configured mode, so it would switch modes implicitly; the
            # switch between standard and Spot is always an explicit operator rotate (or the planner's own switch).
            reason = (f"image rollout held: {active.name} is {active.provisioning_model or 'of unknown model'} but "
                      f"{ENV_PROVISIONING}={self.config.provisioning}; an explicit rotate is required to change "
                      f"modes")
            self._note(reason, level=logging.WARNING)
            return Plan(plan.action, f"{plan.reason}; {reason}", slot=plan.slot, target_slot=plan.target_slot,
                        offer=plan.offer)
        ready = (active is not None and active.running and other is None
                 and (obs.health.get(obs.origin.slot) or HealthResult.unknown()).ok)
        now = self.clock()
        last_end = max(self.cstate.get("last_transition_end_at") or 0, self.cstate.get("last_abort_at") or 0)
        cooling = bool(last_end and now - last_end < ROTATION_COOLDOWN_SECONDS)
        active_image = self._active_image(active) if (ready and not cooling) else ""  # may record it in state
        cache = copy.deepcopy(self.cstate.get("image") or {})
        before = copy.deepcopy(cache)
        decision = self._tracker().evaluate(cache, active_image=active_image, transition_active=False,
                                            active_ready=ready, cooling=cooling, now=now)
        if cache != before:
            self.cstate["image"] = cache
        if not decision.roll:
            return Plan(plan.action, f"{plan.reason}; image tracking: {decision.reason}", slot=plan.slot,
                        target_slot=plan.target_slot, offer=plan.offer)
        offers = obs.offers
        if offers is None:
            try:
                offers = list(self.offer_source())
            except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                return Plan(plan.action, f"{plan.reason}; image rollout waits: offer scan failed: {exc}",
                            slot=plan.slot)
        best = choose_offer(offers, self.config.max_daily_usd)
        if best is None:
            return Plan(plan.action, f"{plan.reason}; image rollout waits: no offer under the cap", slot=plan.slot)
        self._rollout = decision
        return Plan("rotate", f"image rollout: {decision.reason}", slot=active.slot,
                    target_slot=other_slot(active.slot), offer=best)

    def _same_provisioning(self, vm: ControlVm) -> bool:
        """The VM runs the configured mode: standard needs a VM the inventory reports standard; Spot accepts a Spot
        VM or one whose model is unknown (VMs listed before the model was recorded were all Spot)."""
        return vm.standard if self.config.standard else not vm.standard

    def _create_config(self, t: Mapping[str, Any]) -> ControlConfig:
        """The configuration a transition creates its VM with: its recorded image when it pinned one."""
        image = t.get("dashboard_image")
        return dataclasses.replace(self.config, dashboard_image=image) if image else self.config

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
            self.cstate.get("start_failed_at", {}).pop(vm.slot, None)
            down = self.cstate.get("down_since", {})
            if vm.standard and vm.slot in down:
                # A standard VM that started is not lost capacity: its boot gets the full stuck and unhealthy
                # grace periods from now. (A Spot VM keeps the original time, so repeated preemptions still
                # lead to a replacement.)
                down[vm.slot] = self.clock()
            self._note("started %s in %s", vm.name, vm.zone)
            return True
        failures = self.cstate.setdefault("start_failures", {})
        failures[vm.slot] = failures.get(vm.slot, 0) + 1
        # The planner replaces a stopped standard VM only while its latest start failed.
        self.cstate.setdefault("start_failed_at", {})[vm.slot] = self.clock()
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
            # Provider instance ids pin the exact VMs this transition manages: a VM recreated under the
            # same slot name later is a different instance and is never stopped or deleted (see
            # _identity_problem). ``old_vm`` None means the outgoing slot had no VM at the start.
            "old_vm": ({"name": old_vm.name, "zone": old_vm.zone, "instance_id": old_vm.instance_id or None}
                       if old_vm else None),
            "new_vm": {"name": control_vm_name(new_slot),
                       "zone": existing.zone if existing else (offer.zone if offer else ""),
                       "machine_type": existing.machine_type if existing else (offer.machine_type if offer else ""),
                       "instance_id": (existing.instance_id or None) if existing else None},
            "offer": offer.to_dict() if offer else None,
            # Reporting only: every mode runs the same steps, fences, and version checks.
            "provisioning": self.config.provisioning,
            "excluded_zones": ([old_vm.zone] if (old_vm and mode == MODE_RECOVER) else [])
                              + self._retained_zones(new_slot),
            # Create failures are tracked per placement ("machine_type@zone", see _offer_key): a stockout of one
            # shape in a zone says nothing about another shape there.
            "failed_offers": [],
            "failed_offers_at": {},
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
        if self.config.tracking:
            # Pin the image every controller that resumes this transition creates, whatever its own settings.
            rollout = self._rollout if mode == MODE_PRICE else None
            transition["dashboard_image"] = rollout.image if rollout else self._base_image()
            if rollout:
                transition["image_rollout"] = {"image": rollout.image, "digest": rollout.digest,
                                               "revision": rollout.revision, "from_revision": rollout.from_revision}
        self.cstate["transition"] = transition
        self.save()
        self._note("began %s transition %s -> slot %s: %s", mode, plan.slot or "-", new_slot, plan.reason)

    def _retained_zones(self, slot: str) -> list[str]:
        """Zones holding a retained boot disk for ``slot``; creating the VM there would collide on its name."""
        return sorted({str(d.get("zone")) for d in self.cstate.get("retained_disks", [])
                       if d.get("slot") == slot and d.get("zone")})

    # -- instance identity ---------------------------------------------------------------
    def _identity_problem(self, t: dict, role: str, vm: ControlVm, *, adopt: bool) -> str | None:
        """None when ``vm`` is the exact instance this transition manages as ``role`` ("old" or "new").

        Slot names are reused, so a VM recreated under the same name (by an operator or another
        controller) while the transition is pending looks identical by name; only the provider instance
        id tells them apart. With ``adopt``, an id that was never recorded (the replacement before its
        first observation, or a transition saved before ids were recorded) is recorded now, provided the
        VM is in the zone the transition recorded for it.
        """
        label = "outgoing" if role == "old" else "replacement"
        record = t.get(f"{role}_vm")
        if record is None:
            if role == "old":
                return (f"outgoing slot {vm.slot} had no VM when this transition began, but {vm.name} "
                        f"(id {vm.instance_id or '?'}) exists now; it is not the VM this transition manages")
            record = t["new_vm"] = {"name": vm.name, "zone": "", "machine_type": vm.machine_type}
        if not vm.instance_id:
            return f"{label} {vm.name} has no instance id in the inventory, so its identity cannot be confirmed"
        recorded = record.get("instance_id")
        if recorded:
            if str(recorded) == vm.instance_id:
                return None
            return (f"{label} {vm.name} in {vm.zone} is instance {vm.instance_id}, not instance {recorded} "
                    f"recorded by this transition; it was recreated under the same name, so this "
                    f"transition never stops, deletes, or reactivates it; an operator must resolve it")
        if not adopt:
            return f"{label} {vm.name} has no recorded instance id; refusing to act on an unconfirmed VM"
        zone = record.get("zone")
        if zone and zone != vm.zone and not (role == "new" and t.get("step") == "create"):
            return (f"{label} {vm.name} is in {vm.zone}, but this transition recorded zone {zone}; it is "
                    f"not the VM this transition manages")
        record["instance_id"] = vm.instance_id
        self.save()
        self._note("transition %s now tracks %s %s as instance %s", t.get("id"), label, vm.name, vm.instance_id)
        return None

    def _identity_problems(self, t: dict, obs: Observation) -> list[str]:
        """Record unset instance ids and return every observed slot VM that is not the recorded one."""
        problems = []
        for role in ("old", "new"):
            slot = t.get(f"{role}_slot")
            vm = obs.vm(slot) if slot else None
            if vm is not None:
                problem = self._identity_problem(t, role, vm, adopt=True)
                if problem:
                    problems.append(problem)
        return problems

    def _request_stop(self, t: dict, role: str, vm: ControlVm, field_name: str, purpose: str) -> bool:
        """Ask GCE to stop ``vm`` once, and again every STOP_RETRY_SECONDS while it is still not stopped.

        False (and nothing is stopped) when ``vm`` is not the instance this transition recorded.
        """
        problem = self._identity_problem(t, role, vm, adopt=False)
        if problem:
            t["error"] = f"refusing to stop: {problem}"
            self.save()
            self._note(t["error"], level=logging.ERROR)
            return False
        requested = t.get(field_name)
        now = self.clock()
        if requested and now - requested < STOP_RETRY_SECONDS:
            return True
        t[field_name] = now
        self.save()
        result = self.gcloud(stop_command(vm, self.config.project))
        self._note("stopping %s %s (exit %s)", vm.name, purpose, getattr(result, "returncode", None),
                   level=logging.WARNING)
        return True

    def _delete_vm(self, t: dict, vm: ControlVm, *, role: str, keep_boot_disk: bool, reason: str) -> bool:
        """Delete ``vm``; with ``keep_boot_disk`` record the retained disk and log it for an operator.

        Refuses (False) unless ``vm`` is the exact instance this transition recorded as ``role``.
        """
        problem = self._identity_problem(t, role, vm, adopt=False)
        if problem:
            t["error"] = f"refusing to delete: {problem}"
            self.save()
            self._note(t["error"], level=logging.ERROR)
            return False
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
        new_vm = t.get("new_vm") or {}
        if new_vm.get("dashboard_image"):
            # The image the serving VM was created with; image tracking and recoveries start from it.
            self.cstate.setdefault("image", {})["active"] = {
                "slot": t["new_slot"], "instance_id": new_vm.get("instance_id"), "image": new_vm["dashboard_image"],
                "at": self.clock(), "source": f"transition {t['id']}"}
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
            mismatches = self._identity_problems(t, obs)
            if mismatches:
                # A slot VM that is not the recorded instance was recreated under the same name while
                # this transition was pending. Every step would act on it (start, stop, freeze, import,
                # unfreeze, origin writes that depend on it, delete), so hold until an operator resolves it.
                t["error"] = "held: " + "; ".join(mismatches)
                t["identity_hold"] = mismatches
                t["updated_at"] = self.clock()
                self.save()
                self._note("transition held at step %s: %s", step, "; ".join(mismatches), level=logging.ERROR)
                return
            t.pop("identity_hold", None)
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
            self._mark_offer_failed(t, pending.get("machine_type") or "", pending["zone"])
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
        planned_mode = t.get("provisioning")
        if planned_mode and planned_mode != self.config.provisioning:
            # Controllers share the project's transitions. One configured for the other mode must not create
            # the replacement this transition planned (a Spot VM for a switch to the free-tier VM, or the
            # reverse); every controller in the project needs the same SPOT_CONTROL_PROVISIONING.
            return self._create_failed(t, f"this transition creates a {planned_mode} control VM, but this controller "
                                          f"is configured for {ENV_PROVISIONING}={self.config.provisioning}")
        offers = obs.offers
        if offers is None:
            try:
                offers = list(self.offer_source())
            except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                self._note("control VM price scan failed: %s", exc, level=logging.WARNING)
                offers = []
        failed_offers, failed_zones = self._failed_offers(t)
        failed = failed_offers or failed_zones
        affordable = [o for o in offers if o.daily_usd <= self.config.max_daily_usd]
        # Filter by (machine type, zone), so an exhausted cheapest shape never hides an affordable alternative
        # shape in the same zone. Zone-only records from before per-shape tracking still exclude the whole zone
        # (conservatively) until they expire.
        candidates = [o for o in affordable
                      if _offer_key(o.machine_type, o.zone) not in failed_offers and o.zone not in failed_zones]
        preferred = [o for o in candidates if o.zone not in t.get("excluded_zones", [])]
        candidates = preferred or candidates
        if t.get("offer") and not failed:
            # Prefer the planned placement only if a fresh scan still quotes it under the cap, and use
            # that current quote: a saved offer's price is not evidence of today's price.
            planned = ControlOffer.from_dict(t["offer"])
            current = next((o for o in candidates
                            if (o.machine_type, o.zone) == (planned.machine_type, planned.zone)), None)
            if current is not None:
                candidates = [current] + [o for o in candidates if o is not current]
            else:
                self._note("planned offer %s in %s is no longer priced under the cap; choosing from the fresh scan",
                           planned.machine_type, planned.zone, level=logging.WARNING)
        if not candidates:
            if affordable:
                # Every affordable (machine type, zone) failed recently: start over with the full list next
                # tick (stockouts are transient), however few placements there are.
                t["failed_offers"], t["failed_offers_at"] = [], {}
                t.pop("failed_zones", None)
                t.pop("failed_zones_at", None)
                return self._create_failed(t, "every affordable machine type and zone failed; retrying all next tick")
            return self._create_failed(t, "no priced offer under the cap")
        script = render_control_startup()
        create_config = self._create_config(t)
        with tempfile.TemporaryDirectory(prefix="spot-control-") as tmp:
            script_path = Path(tmp) / "control-vm-startup.sh"
            script_path.write_text(script, encoding="utf-8")
            for offer in candidates[:CREATE_ATTEMPTS_PER_TICK]:
                try:
                    command = create_command(create_config, t["new_slot"], offer, str(script_path))
                except ValueError as exc:
                    return self._create_failed(t, f"invalid control VM settings: {exc}")
                # Persist the intended identity and the pending attempt before the provider call, so a
                # crash or an ambiguous failure is reconciled in this zone before any other zone is tried.
                t["new_vm"] = {"name": control_vm_name(t["new_slot"]), "zone": offer.zone,
                               "machine_type": offer.machine_type,
                               "dashboard_image": create_config.effective_dashboard_image or None}
                t["pending_create"] = {"name": control_vm_name(t["new_slot"]), "zone": offer.zone,
                                       "machine_type": offer.machine_type, "at": self.clock()}
                self.save()
                result = self.gcloud(command)
                if result.returncode == 0:
                    t.pop("pending_create", None)
                    t["new_vm"]["instance_id"] = created_instance_id(result.stdout, control_vm_name(t["new_slot"])) \
                        or None  # None: recorded at the first observation instead
                    self._note("created %s (%s) in %s at $%.4f/day", control_vm_name(t["new_slot"]),
                               offer.machine_type, offer.zone, offer.daily_usd)
                    return self._advance_step(t)
                stderr = (result.stderr or "").strip()
                self._note("create of %s in %s failed: %s", offer.machine_type, offer.zone, stderr[:300],
                           level=logging.WARNING)
                if _DEFINITIVE_CREATE_ERROR_RE.search(stderr):
                    t.pop("pending_create", None)
                    self._mark_offer_failed(t, offer.machine_type, offer.zone)
                    self.save()
                    continue
                outcome = self._reconcile_pending_create(t)
                if outcome != "absent":
                    # "exists": wait for the inventory to list it; "unknown": retry the check next tick.
                    return "wait"
        return self._create_failed(t, "every create attempt this tick failed")

    def _mark_offer_failed(self, t: dict, machine_type: str, zone: str) -> None:
        if not machine_type:
            # No shape recorded (a pending create persisted by an older controller): fail the whole zone.
            self._mark_zone_failed(t, zone)
            return
        key = _offer_key(machine_type, zone)
        keys = t.setdefault("failed_offers", [])
        if key not in keys:
            keys.append(key)
        t.setdefault("failed_offers_at", {})[key] = self.clock()

    def _mark_zone_failed(self, t: dict, zone: str) -> None:
        """Legacy zone-wide record: excludes every machine type in ``zone`` until it expires."""
        zones = t.setdefault("failed_zones", [])
        if zone not in zones:
            zones.append(zone)
        t.setdefault("failed_zones_at", {})[zone] = self.clock()

    def _failed_offers(self, t: dict) -> tuple[list[str], list[str]]:
        """Fresh failures: ("machine_type@zone" keys, legacy zone-wide records).

        Records older than FAILED_ZONE_TTL_SECONDS expire so capacity is retried. Zone-only records persisted
        before per-shape tracking cannot say which shape failed, so they keep excluding the whole zone (the
        conservative reading) for the rest of their TTL; nothing new is written in that form except for a
        pending create that names no machine type.
        """
        return (self._fresh(t, "failed_offers", "failed_offers_at"),
                self._fresh(t, "failed_zones", "failed_zones_at"))

    def _fresh(self, t: dict, list_key: str, stamps_key: str) -> list[str]:
        now = self.clock()
        if list_key not in t and stamps_key not in t:
            return []
        entries = list(t.get(list_key) or [])
        stamps = t.setdefault(stamps_key, {})
        for entry in entries:
            stamps.setdefault(entry, now)  # Records from before timestamps existed start their TTL now.
        fresh = [e for e in entries if now - stamps[e] < FAILED_ZONE_TTL_SECONDS]
        if fresh != t.get(list_key):
            t[list_key] = fresh
            t[stamps_key] = {e: stamps[e] for e in fresh}
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
            t["new_vm"]["instance_id"] = None  # the recorded replacement is gone; track the next one
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
            t["old_frozen"] = True
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
                    # The old dashboard was never frozen, so it may accept changes after this snapshot
                    # until it is fenced by stop: the imported state is provisional.
                    self._mark_old_state_unconfirmed(
                        t, f"old slot {t['old_slot']} did not freeze; its unfrozen snapshot at state_version "
                           f"{snapshot['state_version']} was imported, and it may have accepted later changes")
                    return self._advance_step(t)
                t["import_skipped"] = True
                self._goto(t, "cutover")
                return "advance"
            return self._abort(t, "old VM did not freeze")
        return "wait"

    def _snapshot_for_import(self, t: dict, floor: int | None = None) -> dict[str, Any] | None:
        """The first snapshot at or above ``floor`` (default: the frozen version): this run's frozen
        snapshot, then the old VM's snapshot, then the KV snapshot."""
        floor = (t.get("frozen_version") or 0) if floor is None else floor
        if self._snapshot is not None and self._snapshot["state_version"] >= floor:
            return self._snapshot
        candidates = []
        if t.get("old_slot"):
            status, body = self._control(t["old_slot"], "snapshot", "GET")
            candidates.append(parse_snapshot(body) if status == 200 and isinstance(body, dict) else None)
        try:
            candidates.append(parse_snapshot(self.kv.get(KV_STATE_KEY)) if self.kv is not None else None)
        except KvError:
            pass
        for snapshot in candidates:
            if snapshot is not None and snapshot["state_version"] >= floor:
                return snapshot
        return None

    # -- transferred-state floor ---------------------------------------------------------
    @staticmethod
    def _required_version(t: dict) -> int:
        """Lowest state_version the replacement must report: what it confirmed it holds after the import
        or KV catch-up, and never below the old VM's frozen version (also for transitions saved before
        ``required_version`` was recorded)."""
        return max(t.get("required_version") or 0, t.get("frozen_version") or 0)

    def _raise_required_version(self, t: dict, version: int) -> None:
        if version > (t.get("required_version") or 0):
            t["required_version"] = version
            self.save()

    def _reimport_replacement(self, t: dict) -> bool:
        """Import a snapshot at or above the floor into a replacement that reports less (for example after
        it restarted and recovered an older KV snapshot). Bounded by STEP_RETRY_LIMIT per transition;
        True when the replacement confirmed at least the floor (fresh health must still confirm it)."""
        floor = self._required_version(t)
        attempts = t.get("reimport_attempts", 0)
        if attempts >= STEP_RETRY_LIMIT:
            return False
        t["reimport_attempts"] = attempts + 1
        self.save()
        snapshot = self._snapshot_for_import(t, floor)
        if snapshot is None:
            self._note("no snapshot at or above state_version %d to re-import into slot %s", floor, t["new_slot"],
                       level=logging.ERROR)
            return False
        status, body = self._control(t["new_slot"], "import", "POST", snapshot)
        version = body.get("state_version") if isinstance(body, dict) else None
        if status == 200 and isinstance(version, int) and not isinstance(version, bool) and version >= floor:
            self._note("re-imported state_version %d into slot %s, which had fallen below %d", version,
                       t["new_slot"], floor, level=logging.WARNING)
            return True
        self._note("re-import into slot %s returned %s (state_version=%s, need >= %d)", t["new_slot"], status,
                   version, floor, level=logging.WARNING)
        return False

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
            self._raise_required_version(t, version)
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
            self._raise_required_version(t, version)
            self._note("slot %s caught up with KV at state_version %d before cutover", t["new_slot"], version)
        t["kv_caught_up"] = True
        self.save()
        return True

    def _mark_old_state_unconfirmed(self, t: dict, reason: str) -> None:
        """Record that the replacement's state may be older than the old VM's (no confirmed freeze).

        delete_old then keeps the old boot disk (``retained_disks``) so an operator can reconcile it.
        """
        if t.get("old_state_unconfirmed"):
            return
        t["old_state_unconfirmed"] = True
        t["old_state_unconfirmed_reason"] = reason
        self.save()
        self._note("%s; the old boot disk will be KEPT when the old VM is deleted so those changes can be "
                   "reconciled by an operator", reason, level=logging.ERROR)

    @staticmethod
    def _old_state_confirmed(t: dict) -> bool:
        """True only when the old VM confirmed a freeze, so the imported snapshot is its final state."""
        return bool(t.get("old_frozen")) and not t.get("old_state_unconfirmed")

    def _fence_old_writer(self, t: dict, obs: Observation) -> bool:
        """True once the old slot can no longer accept writes: confirmed frozen, or confirmed stopped.

        KV is eventually consistent, so re-reading origin:active cannot give exclusive ownership. A
        recovery that could not freeze the old VM (it was unreachable) must stop it, and see it
        stopped in fresh inventory, before the replacement becomes the writer; otherwise an old VM
        that comes back while KV still names it could accept and publish competing changes.
        """
        if t["mode"] != MODE_RECOVER or not t.get("old_slot") or t.get("old_frozen"):
            return True
        old = obs.vm(t["old_slot"])
        if old is None:
            return True  # nothing left that could write (its boot disk went with it)
        # Fenced by stop rather than freeze: whatever it accepted before it stopped may be only on its disk.
        self._mark_old_state_unconfirmed(
            t, f"old slot {t['old_slot']} was fenced by stopping it, not by a confirmed freeze; changes it "
               f"accepted before it stopped may exist only on its boot disk")
        if old.status in STOPPED_STATES:
            return self._stable_stop(t, "old", old)
        if self._request_stop(t, "old", old, "old_stop_requested_at", "(unreachable old slot) before cutover"):
            t["error"] = f"waiting for old slot {t['old_slot']} ({old.status}) to stop before cutover"
            self.save()
        return False

    def _step_cutover(self, t: dict, obs: Observation) -> str:
        # Never point users at a replacement that was preempted or went unhealthy after import, or
        # when resuming a saved cutover step: recheck it immediately before the origin write.
        ready, why, below_floor = self._replacement_ready(t, obs)
        if not ready and below_floor and self._reimport_replacement(t):
            # Not the writer yet, so re-importing is safe; the next tick's fresh health must confirm it.
            t["error"] = f"{why}; re-imported, waiting for health to confirm"
            self.save()
            return "wait"
        if ready:
            t.pop("reimport_attempts", None)
        if not ready:
            if t["mode"] == MODE_PRICE:
                # The old VM is still active (only frozen); abort unfreezes it and keeps it serving.
                return self._abort(t, f"replacement not ready at cutover: {why}")
            if self._old_back(t, obs):
                return self._abort(t, "old VM is healthy again; keeping it")
            self._note("replacement not ready at cutover (%s); waiting for it again", why, level=logging.WARNING)
            self._goto(t, "wait_health")
            return "wait"
        if not self._fence_old_writer(t, obs):
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
            ready, detail, below_floor = self._replacement_ready(t, obs)
            if not ready:
                # Never delete the old VM (or its disk) while the replacement holds less than was
                # transferred: re-import, and keep the regular grace and rollback rules running meanwhile.
                if below_floor and self._reimport_replacement(t):
                    detail += "; re-imported, waiting for health to confirm"
                return self._replacement_not_ready(t, obs, detail)
            t.pop("replacement_unready_since", None)
            t.pop("reimport_attempts", None)
            # Without a confirmed freeze (recovery without an import, an unfrozen snapshot, or an old VM
            # fenced by stop) the old disk may hold changes that never reached the replacement: keep it.
            keep = t.get("import_skipped") or not self._old_state_confirmed(t)
            if t.get("import_skipped"):
                reason = "old VM went down before its state was imported"
            else:
                reason = t.get("old_state_unconfirmed_reason") or "old VM was never confirmed frozen"
            if not self._delete_vm(t, old, role="old", keep_boot_disk=bool(keep), reason=reason):
                self._fail_attempt(t, t["error"], limit=10**9)
                return "wait"
        return self._finish(t)

    def _replacement_ready(self, t: dict, obs: Observation) -> tuple[bool, str, bool]:
        """Fresh check that the replacement is RUNNING, healthy, answers as its own slot, and still holds
        at least the transferred state. Returns (ready, detail, below_floor)."""
        new = obs.vm(t["new_slot"])
        if new is None:
            return False, f"replacement slot {t['new_slot']} VM is missing", False
        if new.status != "RUNNING":
            return False, f"replacement {new.name} is {new.status}", False
        result = probe_health(self.requester, self.config, t["new_slot"])
        if not result.ok:
            return False, f"replacement {new.name} is not healthy: {result.detail}", False
        if result.slot != t["new_slot"]:
            return (False, f"replacement hostname answered as slot {result.slot!r}, expected {t['new_slot']!r}",
                    False)
        floor = self._required_version(t)
        if floor and (result.state_version is None or result.state_version < floor):
            # For example it restarted after the import and recovered an older KV snapshot.
            return (False, f"replacement {new.name} reports state_version {result.state_version}, below the "
                           f"transferred state_version {floor}", True)
        return True, "ready", False

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
        if self._replacement_freeze is not None and self._replacement_freeze[0] == t.get("id"):
            return self._replacement_freeze[1]  # the rollback fence froze it moments ago
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

        The old VM's *current* state_version comes from fresh health, never from the recorded
        frozen_version: it may have restarted during the rotation and recovered an older KV snapshot.
        Sets ``replacement_state_transferred`` only when the replacement's snapshot was captured and fresh
        health shows the old VM holding at least the newest snapshot (and the frozen version); otherwise
        the replacement's boot disk is kept, with the reason in ``carry_back_unconfirmed``.
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
        target = max(floor, newest["state_version"] if newest else 0)
        current = self._old_state_version(t)
        confirmed = current is not None and current >= target
        importable = newest is not None and newest["state_version"] >= target  # i.e. not below the floor
        problem = None
        if not confirmed and importable:
            # Below the best snapshot (or unknown): import it, then confirm with fresh health.
            status, body = self._control(t["old_slot"], "import", "POST", newest)
            current = self._old_state_version(t)
            confirmed = status == 200 and current is not None and current >= target
            if confirmed:
                self._note("carried state_version %d back to old slot %s", current, t["old_slot"])
            else:
                problem = (f"fallback import into slot {t['old_slot']} returned {status}; fresh health reports "
                           f"state_version {current}, need >= {target}")
        elif not confirmed:
            problem = (f"old slot {t['old_slot']} reports state_version {current}, below {target}, and no "
                       f"snapshot at or above it is available")
        if not confirmed:
            if importable and not self._fail_attempt(t, problem):
                return False  # retry next tick
            # A reachable old VM with older state beats no dashboard; the replacement's disk is kept.
            self._note("could not confirm state transfer to slot %s (%s); continuing rollback and keeping the "
                       "replacement's boot disk", t["old_slot"], problem, level=logging.ERROR)
        if replacement is None and confirmed:
            problem = f"replacement slot {t['new_slot']} could not be frozen for its own snapshot"
        transferred = bool(confirmed and replacement is not None)
        t["replacement_state_transferred"] = transferred
        if not transferred and t.get("cutover_attempted"):
            t["carry_back_unconfirmed"] = (f"rollback could not confirm the replacement's state reached slot "
                                           f"{t['old_slot']}: {problem}")
        t["fallback_import_done"] = True
        self.save()
        return True

    def _old_state_version(self, t: dict) -> int | None:
        """The outgoing VM's current state_version from fresh health (frozen counts), or None if unknown."""
        result = probe_health(self.requester, self.config, t["old_slot"])
        if result.slot != t["old_slot"] or not result.hydrated:
            return None
        return result.state_version

    def _fence_replacement(self, t: dict, obs: Observation) -> bool:
        """True once the replacement can no longer write: confirmed frozen, or confirmed stopped or gone.

        After a cutover attempt ``origin:active`` may have named the replacement (an unverified write can
        still land), so it may be a writer. KV is eventually consistent, so restoring the origin pointer
        cannot take that away by itself; and the delete that follows can fail. The old slot is therefore
        unfrozen and pointed at again only after the replacement answered a freeze with its state_version
        (the freeze marker survives restarts) or fresh inventory shows it stopped or absent. Otherwise the
        replacement is stopped (again every STOP_RETRY_SECONDS) and the rollback waits.
        """
        if not t.get("cutover_attempted") or not t.get("old_slot"):
            return True
        new = obs.vm(t["new_slot"])
        if new is None or t.get("new_frozen"):
            return True
        if new.status in STOPPED_STATES:
            return self._stable_stop(t, "new", new)
        if new.running:
            status, body = self._control(t["new_slot"], "freeze", "POST")
            version = body.get("state_version") if isinstance(body, dict) else None
            if status == 200 and isinstance(version, int) and not isinstance(version, bool):
                t["new_frozen"] = True
                self._replacement_freeze = (t["id"], parse_snapshot(body.get("snapshot")))
                self.save()
                self._note("froze replacement slot %s at state_version %d before rolling back", t["new_slot"],
                           version)
                return True
            self._note("could not freeze replacement slot %s for rollback (HTTP %s); stopping it", t["new_slot"],
                       status, level=logging.WARNING)
        if self._request_stop(t, "new", new, "new_stop_requested_at",
                              f"(replacement) before slot {t['old_slot']} serves again"):
            t["error"] = (f"waiting for replacement slot {t['new_slot']} ({new.status}) to freeze or stop before "
                          f"slot {t['old_slot']} is unfrozen or named by origin:active again")
            self.save()
        return False

    def _step_abort(self, t: dict, obs: Observation) -> str:
        old = obs.vm(t.get("old_slot"))
        # Never two writable slots: fence the replacement before the old slot is reactivated.
        if not self._fence_replacement(t, obs):
            return "wait"
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
            if not self._delete_vm(t, new, role="new", keep_boot_disk=keep,
                                   reason=t.get("carry_back_unconfirmed")
                                   or "rollback could not confirm its state reached the old VM"):
                return "wait"
        rollout = t.get("image_rollout") or {}
        if rollout.get("digest"):
            # Never retry this digest automatically; the next :main digest gets a fresh chance (after the cooldown).
            image_tracking.record_failed(self.cstate.setdefault("image", {}), rollout["digest"])
            self._note("image rollout to %s aborted; backing off that digest until %s:main moves", rollout["digest"],
                       DEFAULT_DASHBOARD_IMAGE_REPO, level=logging.WARNING)
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
            "provisioning": self.config.provisioning,
            "vms": [vm.__dict__ for vm in obs.vms],
            "origin": json.loads(obs.origin.to_json()) if obs.origin else None,
            "origin_known": obs.origin_known,
            "health": {slot: result.__dict__ for slot, result in obs.health.items()},
            "transition": self.cstate.get("transition"),
            "down_since": self.cstate.get("down_since", {}),
            "last_transition_end_at": self.cstate.get("last_transition_end_at"),
            "last_abort_at": self.cstate.get("last_abort_at"),
            "retained_disks": self.cstate.get("retained_disks", []),
            "carry_back_unconfirmed": (self.cstate.get("transition") or {}).get("carry_back_unconfirmed"),
            "old_state_unconfirmed": ((self.cstate.get("transition") or {}).get("old_state_unconfirmed_reason")
                                      if (self.cstate.get("transition") or {}).get("old_state_unconfirmed")
                                      else None),
            "problems": obs.problems,
            "offers": [o.to_dict() for o in obs.offers[:5]] if obs.offers else None,
            "image_track": self.config.image_track or None,
            "image": self.cstate.get("image"),
        }


def build_manager(project: str, state: dict, save: Callable[[], None], *,
                  env: Mapping[str, str] | None = None, guard: Callable[[], None] | None = None,
                  **overrides: Any) -> ControlVmManager:
    """The manager with the real gcloud, KV and HTTP clients.

    ``guard`` (the Cloud Storage lease's ``ensure``) runs before every gcloud call and every HTTP request (KV
    reads and writes, health probes, control calls), so nothing reaches a provider once the lease can't be proven.
    """
    config = ControlConfig.from_env(project, env, **overrides)
    kv_config = KvConfig.from_env(env)
    if guard is None:
        return ControlVmManager(config, state, save, kv=CloudflareKv(kv_config) if kv_config else None)

    def gcloud(argv: list[str]) -> Any:
        guard()
        return default_gcloud(argv)

    def requester(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                  timeout: float) -> tuple[int, bytes]:
        guard()
        return edge_state.default_requester(method, url, headers, body, timeout)

    return ControlVmManager(config, state, save, gcloud=gcloud, requester=requester,
                            kv=CloudflareKv(kv_config, requester=requester) if kv_config else None)


def autoscaler_tick(args: Any, state: dict, save: Callable[[], None], *,
                    manager: ControlVmManager | None = None,
                    guard: Callable[[], None] | None = None) -> Plan | None:
    """Run one control VM step from the runner autoscaler loop; never raises."""
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        # The autoscaler's --config-repo is the same central runner configuration repository the
        # dashboard reads its controlled targets from; when it is unset SPOT_CONFIG_REPO applies.
        manager = manager or build_manager(args.project, state, save, guard=guard,
                                           config_repo=getattr(args, "config_repo", None) or None)
        plan = manager.tick(apply=not dry_run)
        if dry_run:
            print(f"Control VM plan (dry run): {plan.describe()}")
        return plan
    except Exception as exc:  # noqa: BLE001 - control VM work must never stop runner management.
        LOG.error("Control VM management failed this check: %s", exc)
        return None


# --------------------------------------------------------------------------------------------
# Project-scoped control VM state
# --------------------------------------------------------------------------------------------
#
# The control VM slots (spot-control-a/b) are fixed per project, but several runner controllers (one per
# --target, each with its own --state-file) can run --manage-control-vm in one project. Each check therefore
# takes one project-wide lock, and the control VM lifecycle state (transition, retained disks, history) lives in
# one project-scoped file that whichever controller holds the lock reads and writes. A transition that one
# controller started and then crashed in is resumed by the next controller that gets the lock.

ENV_CONTROL_STATE_DIR = "SPOT_CONTROL_STATE_DIR"
# gs://bucket[/prefix]: keep the project state and lock in Cloud Storage (lib.gcs_control_state) instead.
ENV_CONTROL_STATE_URI = "SPOT_CONTROL_STATE_URI"
# A local state file that migrate-state moved to Cloud Storage holds only {CONTROL_STATE_MOVED_KEY: <uri>, ...}.
CONTROL_STATE_MOVED_KEY = "control_vm_state_moved_to"
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9.:-]{0,62}$")


class ControlStateConflict(RuntimeError):
    """A controller state file holds control VM state that differs from, and can't replace, the project state."""


def default_control_state_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = str(env.get(ENV_CONTROL_STATE_DIR, "") or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_STATE_FILE.parent


def project_control_paths(project: str, directory: Path | str | None = None) -> tuple[Path, Path]:
    """(state file, lock file) of a project's control VM lifecycle."""
    if not isinstance(project, str) or not _PROJECT_ID_RE.match(project):
        raise ValueError(f"invalid GCP project id for control VM state: {project!r}")
    base = Path(directory).expanduser() if directory else default_control_state_dir()
    name = f"control-vm-{project.replace(':', '__')}"
    return base / f"{name}.json", base / f"{name}.lock"


def _must_preserve(cstate: Any) -> bool:
    """Control VM state that can't be dropped: an in-progress transition or boot disks kept for recovery."""
    return isinstance(cstate, dict) and bool(cstate.get("transition") or cstate.get("retained_disks"))


class ProjectControlState:
    """The project's control VM state file, read and written only while the project lock is held."""

    # Called before every provider call while this state is held (the Cloud Storage lease); None for the local
    # file, whose flock holds for as long as the process does.
    guard: Callable[[], None] | None = None

    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: dict[str, Any] = {}

    def load(self) -> "ProjectControlState":
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            backups = sorted(self.path.parent.glob(f"{self.path.name}.migrated-*"))
            if backups:
                # A migrated file whose marker is gone is never empty state: the state lives elsewhere.
                raise ControlStateConflict(f"control VM state {self.path} is missing but {backups[-1].name} shows it "
                                           f"was migrated; set {ENV_CONTROL_STATE_URI}, or restore the file by hand")
            data = {}
        if isinstance(data, dict) and CONTROL_STATE_MOVED_KEY in data:
            # Fail closed: this file was migrated, and acting on it would fork the state.
            raise ControlStateConflict(f"control VM state {self.path} moved to {data.get(CONTROL_STATE_MOVED_KEY)}; "
                                       f"set {ENV_CONTROL_STATE_URI}={data.get(CONTROL_STATE_MOVED_KEY)}")
        if not isinstance(data, dict) or not isinstance(data.get(STATE_KEY, {}), dict):
            # Never start over from defaults: an unreadable record could hide an in-progress transition.
            raise ControlStateConflict(f"control VM state {self.path} is not a JSON object; repair it by hand")
        self.state = data
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False, encoding="utf-8") as temp:
            json.dump(self.state, temp)
            temp.flush()
            os.fsync(temp.fileno())
        Path(temp.name).replace(self.path)

    def adopt(self, controller_state: dict, source: Path | str, *, persist: bool) -> bool:
        """Move control VM state saved in a controller's own state file (before project scoping) into this one.

        Returns True when ``controller_state`` changed and its owner must write it (after this file was written
        when ``persist``). A copy equal to the project state, or one holding nothing that must be kept, is
        dropped. Raises ControlStateConflict when both hold state that must be kept and they differ.
        """
        if STATE_KEY not in controller_state:
            return False
        legacy = controller_state[STATE_KEY]
        current = self.state.get(STATE_KEY)
        if legacy == current or not _must_preserve(legacy):
            controller_state.pop(STATE_KEY)
            if legacy and legacy != current:
                LOG.info("Control VM: dropped idle control VM state from %s; %s is authoritative", source, self.path)
            return True
        if _must_preserve(current):
            raise ControlStateConflict(
                f"{source} and {self.path} both hold an in-progress control VM transition or retained disks, and "
                "they differ; resolve which one is current before this controller manages the control VM")
        self.state[STATE_KEY] = copy.deepcopy(legacy)
        if persist:
            self.save()
        controller_state.pop(STATE_KEY)
        LOG.warning("Control VM: moved control VM state from %s to the project state %s", source, self.path)
        return True


class ProjectControlLock:
    """Non-blocking exclusive project lock; ``__enter__`` returns the loaded ProjectControlState, or None when
    another controller holds the lock."""

    def __init__(self, project: str, directory: Path | str | None = None) -> None:
        self.state_path, self.lock_path = project_control_paths(project, directory)
        self._handle: Any = None

    def __enter__(self) -> ProjectControlState | None:
        import fcntl

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        self._handle = handle
        try:
            return ProjectControlState(self.state_path).load()
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc: Any) -> None:
        if self._handle is not None:
            self._handle.close()  # Closing the descriptor releases the flock.
            self._handle = None


def control_state_uri(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return str(env.get(ENV_CONTROL_STATE_URI, "") or "").strip()


def open_project_control_lock(project: str, directory: Path | str | None = None,
                              env: Mapping[str, str] | None = None) -> Any:
    """The project lock for the configured backend: the Cloud Storage lease when ``SPOT_CONTROL_STATE_URI`` is
    set, else the local flock. Both return the loaded state from ``__enter__``, or None when another controller
    holds the lock."""
    uri = control_state_uri(env)
    if uri:
        from .gcs_control_state import GcsProjectControlLock
        return GcsProjectControlLock(project, uri)
    return ProjectControlLock(project, directory)


def load_project_control_state(project: str, directory: Path | str | None = None,
                               env: Mapping[str, str] | None = None) -> ProjectControlState:
    """Read the project state without taking the lock (previews and status)."""
    uri = control_state_uri(env)
    if uri:
        from .gcs_control_state import load_state
        return load_state(project, uri)
    state_path, _lock = project_control_paths(project, directory)
    return ProjectControlState(state_path).load()


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
                        help="Autoscaler state file whose control VM state, saved before project scoping, is "
                             "moved to the project state file (default: %(default)s)")
    common.add_argument("--control-state-dir", type=Path, default=None,
                        help=f"Directory of the project-scoped control VM state and lock (or "
                             f"{ENV_CONTROL_STATE_DIR}; default: {DEFAULT_STATE_FILE.parent})")
    common.add_argument("--provisioning", choices=PROVISIONING_MODES, default=None,
                        help=f"spot: cheapest Spot shape under the daily cap; standard: the always-on free-tier "
                             f"e2-micro in {', '.join(FREE_TIER_REGIONS)} (or {ENV_PROVISIONING}; default: spot)")
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
    common.add_argument("--config-repo",
                        help=f"Dashboard runner configuration repository, owner/repo (or {ENV_CONFIG_REPO})")
    common.add_argument("--controlled-targets",
                        help=f"Comma-separated orgs and owner/repo targets the dashboard requires admin on "
                             f"(or {ENV_CONTROLLED_TARGETS})")
    common.add_argument("--global-admins",
                        help=f"login:numeric_id entries that skip per-target admin checks, or none "
                             f"(or {ENV_GLOBAL_ADMINS}; default: the dashboard default)")

    parser = argparse.ArgumentParser(
        description="Inspect and manage the control VM (Spot or free-tier e2-micro) that hosts the web dashboard. "
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
        command.add_argument("--skip-if-locked", action="store_true",
                             help="Exit 0 instead of failing when another controller holds the project lock")
        if name == "ensure":
            command.add_argument("--skip-if-idle", action="store_true",
                                 help="With --apply: first plan read-only, and take the lock and save state only "
                                      "when the step would change something (the watchdog's steady state)")
    migrate = sub.add_parser("migrate-state", parents=[common],
                             help=f"Upload the local project state to {ENV_CONTROL_STATE_URI} once; refuses when "
                                  "both exist and differ")
    migrate.add_argument("--apply", action="store_true", help="Upload; without it only report what would happen")
    return parser


def _overrides(args: Any) -> dict[str, Any]:
    keys = ("regions", "max_daily_usd", "external_ip", "service_account", "dashboard_image", "dashboard_repo",
            "dashboard_source_ref", "public_url", "cloudflared_image", "origin_domain", "config_repo",
            "controlled_targets", "global_admins", "provisioning")
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

    legacy_path = args.state_file.expanduser()
    apply = bool(getattr(args, "apply", False))
    factory = manager_factory or build_manager
    if args.command == "migrate-state":
        uri = control_state_uri()
        if not uri:
            raise RuntimeError(f"set {ENV_CONTROL_STATE_URI}=gs://<bucket>[/<prefix>] to migrate the control VM state")
        from .gcs_control_state import migrate_local_state
        code, message = migrate_local_state(args.project, args.control_state_dir, uri, apply=apply)
        print(message)
        return code

    def run(state: dict, save: Callable[[], None], guard: Callable[[], None] | None = None) -> int:
        manager = factory(args.project, state, save, **_overrides(args), **({"guard": guard} if guard else {}))
        if args.command == "status":
            print(json.dumps(manager.status(manager.observe(want_offers=True)), indent=2, sort_keys=True,
                             default=str))
            return 0
        plan = manager.tick(apply=apply, force_rotate=args.command == "rotate")
        print(("Executed: " if apply else "Plan (nothing changed): ") + plan.describe())
        if not apply and plan.action not in ("wait", "noop"):
            print("Re-run with --apply to execute it.")
        return 0

    if not apply or getattr(args, "skip_if_idle", False):
        # Read-only: plan on a copy that also folds in unmigrated controller state, and write nothing.
        project = load_project_control_state(args.project, args.control_state_dir)
        saved = copy.deepcopy(project.state)
        project.adopt(copy.deepcopy(read_scale_state(legacy_path)), legacy_path, persist=False)
        if not apply:
            return run(copy.deepcopy(project.state), lambda: None)
        preview = factory(args.project, copy.deepcopy(project.state), lambda: None, **_overrides(args))
        plan = preview.tick(apply=False)
        if preview.preview_is_idle(plan, saved):
            # Nothing to do and nothing to save: no lock, no write (the watchdog's steady state).
            print(f"Idle (no lock taken, nothing changed): {plan.describe()}")
            return 0
    import fcntl

    lock = open_project_control_lock(args.project, args.control_state_dir)
    with lock as project:
        if project is None:
            message = f"Another controller is managing the control VM of project {args.project}; wait for its check " \
                      "to finish"
            if hasattr(lock, "describe_holder"):
                message += f" (lease held by {lock.describe_holder()})"
            if getattr(args, "skip_if_locked", False):
                print(f"Skipped: {message}")
                return 0
            raise RuntimeError(message)
        legacy = read_scale_state(legacy_path) if legacy_path.exists() else {}
        if STATE_KEY in legacy:
            # Moving state out of a controller's file needs that controller's lock; a running autoscaler keeps
            # it and moves the state itself on its next check.
            with legacy_path.with_suffix(legacy_path.suffix + ".lock").open("a") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    if _must_preserve(legacy.get(STATE_KEY)) and legacy.get(STATE_KEY) != project.state.get(STATE_KEY):
                        raise RuntimeError(f"{legacy_path} still holds control VM state that its running controller "
                                           "moves to the project state on its next check; wait for it") from exc
                else:
                    legacy = read_scale_state(legacy_path)
                    if project.adopt(legacy, legacy_path, persist=True):
                        write_scale_state(legacy_path, legacy)
        return run(project.state, project.save, project.guard)
