"""Runner floor keeper: at least one minimal Spot runner host per controlled GitHub target.

The control VM watchdog (a Cloud Run service) runs ``src/runner_floor.py tick --apply`` after its control VM step
on every tick. For each target in ``SPOT_RUNNER_FLOOR_TARGETS`` the keeper only guarantees the floor:

* When the target has a managed runner host (``oxidex-runners-*``, Spot, carrying the target's ``spot-target``
  label) that is running or starting, it does nothing.
* When it has none, it provisions one minimal host: 4 vCPU, the smallest shape that holds one runner under the
  4-vCPU-per-runner sizing (``runner_manager.select_sizing``), with one listener, at the cheapest total daily price
  (compute plus boot and data storage) strictly below ``SPOT_RUNNER_FLOOR_MAX_DAILY_USD``.
* When its own recorded floor host is stopped (not preempted: floor hosts use ``--instance-termination-action=DELETE``,
  so a preemption deletes them), it starts it again only if a start can't replay the fresh-host bootstrap and the
  runners kept their registrations and labels; otherwise it provisions a new host.

Scaling up and down beyond the floor, drains, rotations and loss cleanup stay with the autoscaler. The keeper never
drains, never deregisters a working runner, and deletes only a VM that it created itself and that never finished its
bootstrap (identity-checked by instance ID).

A launch is a multi-tick state machine, so no tick waits for a bootstrap: ``creating`` (intent saved before the
registration token exists) -> ``booting`` (VM created; its instance ID recorded) -> done (every expected listener online
on GitHub; then the one-use registration token is removed from the startup metadata). No SSH is needed: GitHub's
listener status is the readiness proof, and ``startup.sh`` itself refuses to register runners unless Docker and
containerd run on the data disk.

State and lease live in Cloud Storage (``SPOT_RUNNER_FLOOR_STATE_URI``), one state object and one lease per target,
written only with generation preconditions (see ``gcs_control_state``). The operator's autoscaler takes the same
per-target lease when ``SPOT_RUNNER_FLOOR_STATE_URI`` is set in its environment, so the two never act on one target
at once.

GitHub credentials are never environment variables: on Cloud Run each account's token is a Secret Manager secret
mounted as a file (``SECRET_ROOT/<secret>/token``); on a workstation the token comes from ``gh auth token --user``.

Standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .gcs_control_state import (AccessToken, GcsAmbiguous, GcsClient, GcsError, GcsLease, GcsPreconditionFailed,
                                LeaseLost, StateLocation, parse_state_uri)
from .https_transport import Transport, TransportError, https_request
from .runner_fleet import MAX_EXTRA_RUNNER_LABELS, GitHubRunner
from .runner_labels import DEFAULT_RUNNER_LABELS
from .target_config import TargetConfig, resolve_target, target_label_value, target_owns_vm_label

LOG = logging.getLogger("spot_runner_autoscaler.floor")

ENV_TARGETS = "SPOT_RUNNER_FLOOR_TARGETS"
ENV_STATE_URI = "SPOT_RUNNER_FLOOR_STATE_URI"
ENV_REGIONS = "SPOT_RUNNER_FLOOR_REGIONS"
ENV_MAX_DAILY_USD = "SPOT_RUNNER_FLOOR_MAX_DAILY_USD"
ENV_TOKEN_SOURCE = "SPOT_RUNNER_FLOOR_TOKEN_SOURCE"
ENV_TICK_DEADLINE = "SPOT_TICK_DEADLINE_EPOCH"  # set by the watchdog server for every tick step
TOKEN_SOURCE_FILES = "secret-files"
TOKEN_SOURCE_GH = "gh"
# Cloud Run mounts each account's secret at SECRET_ROOT/<secret name>/token (one directory per secret).
SECRET_ROOT = "/secrets/runner-floor"
STATE_SCHEMA = "spot-runner-floor-state-v1"
DEFAULT_REGIONS = ("us-central1", "us-east1", "us-west1")
# Compute plus a 40 GiB boot and a 100 GiB data disk, strictly below this per floor host and day.
DEFAULT_MAX_DAILY_USD = Decimal("1.50")

HOST_PREFIX = "oxidex-runners-"  # the autoscaler's runner host names (spot_runner_autoscaler.RUNNER_HOST_PREFIX)
DISK_PREFIX = "runner-data-"     # and their data disks
RUNNER_PREFIX = "gha"
BASE_LABEL = "spot"
FLOOR_VCPU = 4                   # runner_manager sizing: host_vCPU // 4 runners, so 4 vCPU is one runner
DEFAULT_MIN_RAM_GIB = 16
MAX_RAM_GIB = 32
DATA_DISK_GB = 100
MAX_TARGETS = 10

LIVE_STATUSES = frozenset({"PROVISIONING", "STAGING", "RUNNING", "REPAIRING"})
SETTLING_STATUSES = frozenset({"STOPPING", "SUSPENDING"})
STOPPED_STATUSES = frozenset({"TERMINATED", "STOPPED"})

# Timings (seconds). A launch must have every listener online within LAUNCH_DEADLINE of its create; runner_manager
# allows 1200 s from deployment start, and the watchdog only looks every 180 s.
LAUNCH_DEADLINE_SECONDS = 1800
# Longer than a tick (840 s): a VM that never appeared this long after its create attempt is a failed create.
CREATE_CONFIRM_SECONDS = 1200
RESTART_DEADLINE_SECONDS = 900
ZONE_BACKOFF_SECONDS = 6 * 3600
BACKOFF_BASE_SECONDS = 300
BACKOFF_MAX_SECONDS = 3600
# Spread: a zone that no other floor target uses wins when it costs at most this factor of the cheapest offer.
SPREAD_PREMIUM = Decimal("1.5")
# Budget: the least time a step must have left before it starts. A launch prices offers and creates a disk and a VM.
LAUNCH_MIN_SECONDS = 300
STEP_MIN_SECONDS = 120
CREATE_MIN_SECONDS = 180         # the disk and VM create, checked again right before the intent is saved
LATE_CREATE_WATCH_SECONDS = 3600 # a create that failed visibly can still land; its VM is adopted for this long
# An operation a running keeper always ends (finishes or abandons) by its own deadline. Past it plus this grace (five
# ticks), nothing is managing it (its target left SPOT_RUNNER_FLOOR_TARGETS, the floor is off, or the watchdog is
# paused), and an autoscaler stops waiting for it.
ORPHAN_GRACE_SECONDS = 900
TARGET_MIN_SECONDS = 30
GCLOUD_TIMEOUT_SECONDS = 600
DEADLINE_MARGIN_SECONDS = 20
HISTORY_LIMIT = 20

_ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")
# Classic, OAuth, user-to-server, server-to-server, refresh, and fine-grained GitHub token prefixes.
_TOKENISH_RE = re.compile(r"^(gh[pousr]_|github_pat_)")
_OPTION_KEYS = ("secret", "account", "group", "labels", "ram")


class FloorError(RuntimeError):
    """A floor step could not complete; nothing after the failing call ran."""


class BudgetExhausted(FloorError):
    """Not enough of the tick's time budget is left to start this step safely."""


class DeadlineExceeded(BaseException):
    """The hard deadline fired inside a blocking call (``subprocess.run`` kills its child on the way out).

    A BaseException, so no ``except Exception`` in provider code swallows it."""


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FloorTarget:
    name: str                 # an organization or owner/repo
    secret: str               # Secret Manager secret holding the account's GitHub token (a name, never a value)
    account: str = ""         # gh account for workstation runs (``gh auth token --user``)
    group_id: int | None = None  # organization runner group; None registers into the Default group
    labels: tuple[str, ...] = ()  # extra scheduling labels beyond spot, the machine type, and the region
    min_ram_gib: int = DEFAULT_MIN_RAM_GIB

    @property
    def target(self) -> TargetConfig:
        return resolve_target(self.name)

    @property
    def key(self) -> str:
        """Label-safe, collision-resistant key: the value of the hosts' spot-target label."""
        return target_label_value(self.name)

    @property
    def is_repo(self) -> bool:
        return "/" in self.name

    def custom_labels(self, machine_type: str, region: str) -> tuple[str, ...]:
        """The custom labels runner_manager registers (runner label, machine type, region, then the extras), with
        case-only duplicates dropped: GitHub compares labels without case."""
        out: dict[str, str] = {}
        for label in (BASE_LABEL, machine_type, region, *self.labels):
            out.setdefault(label.casefold(), label)
        return tuple(out.values())

    def owns(self, host: "Host") -> bool:
        return target_owns_vm_label(host.target_label or None, self.target)


def _parse_target_name(text: str) -> str:
    name = text.strip()
    if "/" in name:
        owner, _, repo = name.partition("/")
        if not _ORG_RE.match(owner) or not _REPO_PART_RE.match(repo) or repo in (".", ".."):
            raise ValueError(f"floor target {name!r} is not owner/repo")
        return f"{owner}/{repo}"
    if not _ORG_RE.match(name):
        raise ValueError(f"floor target {name!r} is not an organization name or owner/repo")
    return name


def parse_targets(text: str | None) -> tuple[FloorTarget, ...]:
    """Parse ``SPOT_RUNNER_FLOOR_TARGETS``: ``<target>:secret=<name>[,account=<gh user>][,group=<id>]
    [,labels=<a+b>][,ram=<GiB>]`` entries separated by ``;``. Raises ValueError on anything else."""
    targets: list[FloorTarget] = []
    seen: set[str] = set()
    for entry in (part.strip() for part in str(text or "").split(";")):
        if not entry:
            continue
        name_text, sep, options_text = entry.partition(":")
        if not sep:
            raise ValueError(f"floor target entry {entry!r} needs ':secret=<Secret Manager secret>'")
        name = _parse_target_name(name_text)
        if name.lower() in seen:
            raise ValueError(f"floor target {name} is listed twice")
        seen.add(name.lower())
        options: dict[str, str] = {}
        for item in (part.strip() for part in options_text.split(",")):
            key, eq, value = item.partition("=")
            key, value = key.strip(), value.strip()
            if not eq or key not in _OPTION_KEYS or not value:
                raise ValueError(f"floor target {name}: option {item!r} must be one of "
                                 f"{', '.join(k + '=...' for k in _OPTION_KEYS)}")
            if key in options:
                raise ValueError(f"floor target {name}: option {key} is given twice")
            options[key] = value
        secret = options.get("secret", "")
        if _TOKENISH_RE.match(secret):
            raise ValueError(f"floor target {name}: secret= must name a Secret Manager secret, never hold a token")
        if not _SECRET_RE.match(secret):
            raise ValueError(f"floor target {name}: secret= must be a Secret Manager secret name")
        account = options.get("account", "")
        if account and not _ORG_RE.match(account):
            raise ValueError(f"floor target {name}: account= must be a GitHub login")
        group_id = None
        if "group" in options:
            if "/" in name:
                raise ValueError(f"floor target {name}: repositories have no runner groups")
            if not options["group"].isdigit() or int(options["group"]) < 1:
                raise ValueError(f"floor target {name}: group= must be a positive runner group ID")
            group_id = int(options["group"])
        labels: list[str] = []
        for label in (part.strip() for part in options.get("labels", "").split("+")):
            if not label:
                continue
            if not _LABEL_RE.match(label):
                raise ValueError(f"floor target {name}: label {label!r} is not a valid runner label")
            if label.casefold() in folded(DEFAULT_RUNNER_LABELS):
                raise ValueError(f"floor target {name}: {label} is a built-in label GitHub sets itself")
            if label.casefold() == BASE_LABEL.casefold() or _REGION_RE.match(label.casefold()) \
                    or _looks_like_machine_type(label):
                raise ValueError(f"floor target {name}: {label} duplicates a label every floor runner gets "
                                 "(spot, its machine type, and its region)")
            if label.casefold() in folded(labels):
                raise ValueError(f"floor target {name}: label {label} is listed twice (labels ignore case)")
            labels.append(label)
        if len(labels) > MAX_EXTRA_RUNNER_LABELS:  # counted after the case-insensitive duplicate check above
            raise ValueError(f"floor target {name}: at most {MAX_EXTRA_RUNNER_LABELS} extra labels, the most a host's "
                             f"metadata records for later label syncs; {len(labels)} given")
        ram = DEFAULT_MIN_RAM_GIB
        if "ram" in options:
            if not options["ram"].isdigit() or not 4 <= int(options["ram"]) <= MAX_RAM_GIB:
                raise ValueError(f"floor target {name}: ram= must be a whole number of GiB from 4 to {MAX_RAM_GIB}")
            ram = int(options["ram"])
        targets.append(FloorTarget(name, secret, account, group_id, tuple(labels), ram))
    if len(targets) > MAX_TARGETS:
        raise ValueError(f"at most {MAX_TARGETS} floor targets")
    return tuple(targets)


@dataclass(frozen=True)
class FloorConfig:
    project: str
    targets: tuple[FloorTarget, ...] = ()
    regions: tuple[str, ...] = DEFAULT_REGIONS
    max_daily_usd: Decimal = DEFAULT_MAX_DAILY_USD
    state_uri: str = ""
    token_source: str = TOKEN_SOURCE_GH

    @classmethod
    def from_env(cls, project: str, env: Mapping[str, str] | None = None) -> "FloorConfig":
        env = os.environ if env is None else env
        targets = parse_targets(env.get(ENV_TARGETS, ""))
        regions = tuple(dict.fromkeys(r.strip() for r in str(env.get(ENV_REGIONS, "") or "").split(",") if r.strip()))
        for region in regions:
            if not _REGION_RE.match(region):
                raise ValueError(f"{ENV_REGIONS} names an invalid region {region!r}")
        raw_cap = str(env.get(ENV_MAX_DAILY_USD, "") or "").strip()
        try:
            cap = Decimal(raw_cap) if raw_cap else DEFAULT_MAX_DAILY_USD
        except InvalidOperation as exc:
            raise ValueError(f"{ENV_MAX_DAILY_USD} must be a number of US dollars") from exc
        if not cap.is_finite() or cap <= 0 or cap > 10:
            raise ValueError(f"{ENV_MAX_DAILY_USD} must be above 0 and at most 10")
        source = str(env.get(ENV_TOKEN_SOURCE, "") or TOKEN_SOURCE_GH).strip()
        if source not in (TOKEN_SOURCE_FILES, TOKEN_SOURCE_GH):
            raise ValueError(f"{ENV_TOKEN_SOURCE} must be {TOKEN_SOURCE_FILES} or {TOKEN_SOURCE_GH}")
        uri = str(env.get(ENV_STATE_URI, "") or "").strip()
        if uri:
            state_location(uri)
        return cls(project, targets, regions or DEFAULT_REGIONS, cap, uri, source)

    def target(self, name: str) -> FloorTarget:
        found = [t for t in self.targets if t.name.lower() == str(name).strip().lower()]
        if not found:
            raise FloorError(f"{name} is not a floor target in {ENV_TARGETS}")
        return found[0]


def state_location(uri: str) -> StateLocation:
    try:
        return parse_state_uri(uri)
    except ValueError as exc:
        raise ValueError(f"{ENV_STATE_URI} must be gs://<bucket>[/<prefix>]: {exc}") from exc


# --------------------------------------------------------------------------------------------
# GitHub credentials and API
# --------------------------------------------------------------------------------------------

class TokenSource:
    """GitHub token per floor target: a mounted Secret Manager file on Cloud Run, or ``gh`` on a workstation.

    Never an environment variable: no code path here reads ``os.environ`` for a token.
    """

    CACHE_SECONDS = 300.0

    def __init__(self, mode: str, *, root: str | Path = SECRET_ROOT, runner: Callable[..., Any] = subprocess.run,
                 monotonic: Callable[[], float] = time.monotonic, account_override: str = "") -> None:
        if mode not in (TOKEN_SOURCE_FILES, TOKEN_SOURCE_GH):
            raise ValueError(f"unknown token source {mode!r}")
        self.mode = mode
        self.root = Path(root)
        self.runner = runner
        self.monotonic = monotonic
        self.account_override = account_override
        self._cache: dict[str, tuple[str, float]] = {}

    def __call__(self, target: FloorTarget) -> str:
        key = target.secret if self.mode == TOKEN_SOURCE_FILES else (self.account_override or target.account)
        cached = self._cache.get(key)
        if cached and self.monotonic() - cached[1] < self.CACHE_SECONDS:
            return cached[0]
        token = self._read_file(target) if self.mode == TOKEN_SOURCE_FILES else self._read_gh(target)
        if not token or any(c.isspace() for c in token) or len(token) > 2048:
            raise FloorError(f"the GitHub token for {target.name} is empty or malformed")
        self._cache[key] = (token, self.monotonic())
        return token

    def _read_file(self, target: FloorTarget) -> str:
        path = self.root / target.secret / "token"
        try:
            with path.open("rb") as handle:
                data = handle.read(4097)
        except OSError as exc:
            raise FloorError(f"the GitHub token secret {target.secret} for {target.name} is not mounted at "
                             f"{path}: {type(exc).__name__}") from exc
        if len(data) > 4096:
            raise FloorError(f"the GitHub token secret {target.secret} is too large")
        return data.decode("utf-8", "replace").strip()

    def _read_gh(self, target: FloorTarget) -> str:
        account = self.account_override or target.account
        if not account:
            raise FloorError(f"floor target {target.name} has no account= for a workstation run; add it or pass "
                             "--github-user")
        try:
            result = self.runner(["gh", "auth", "token", "--user", account], check=False, capture_output=True,
                                 text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FloorError(f"gh auth token --user {account} failed: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise FloorError(f"gh has no token for account {account}")
        return (result.stdout or "").strip()


class GitHubError(FloorError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


GITHUB_API = "https://api.github.com"


class GitHubClient:
    """The runner and runner-group calls the floor needs, over HTTPS with one account's token."""

    def __init__(self, token: Callable[[], str], *, transport: Transport | None = None, timeout: float = 20.0,
                 guard: Callable[[], None] | None = None) -> None:
        self.token = token
        self.transport = transport or https_request
        self.timeout = timeout
        self.guard = guard

    def _request(self, method: str, path: str, body: Any = None, *, ok: Sequence[int] = (200,)) -> tuple[int, Any]:
        if method != "GET" and self.guard is not None:
            self.guard()
        headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "spot-runner-floor/1.0"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            response = self.transport(method, f"{GITHUB_API}/{path.lstrip('/')}", headers, data, self.timeout)
        except TransportError as exc:
            raise GitHubError(f"GitHub {method} {path.split('?')[0]}: {exc}") from exc
        if response.status not in ok:
            raise GitHubError(f"GitHub {method} {path.split('?')[0]} returned HTTP {response.status}",
                              response.status)
        if not response.body:
            return response.status, None
        try:
            return response.status, json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubError(f"GitHub {method} {path.split('?')[0]} returned invalid JSON") from exc

    def list_runners(self, target: TargetConfig) -> list[GitHubRunner]:
        """Every runner of the target; raises unless the paginated inventory is complete and well formed."""
        runners: list[GitHubRunner] = []
        seen: set[int] = set()
        total = None
        for page in range(1, 51):
            _, data = self._request("GET", f"{target.runners_url}?per_page=100&page={page}")
            if not isinstance(data, dict) or type(data.get("total_count")) is not int \
                    or not isinstance(data.get("runners"), list):
                raise GitHubError(f"incomplete runner inventory for {target.name}")
            if total is None:
                total = data["total_count"]
            elif data["total_count"] != total:
                raise GitHubError(f"runner inventory of {target.name} changed while it was read")
            for item in data["runners"]:
                if (not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] in seen
                        or type(item.get("name")) is not str or item.get("status") not in ("online", "offline")
                        or type(item.get("busy")) is not bool or not isinstance(item.get("labels"), list)
                        or any(not isinstance(label, dict) or type(label.get("name")) is not str
                               for label in item["labels"])):
                    raise GitHubError(f"malformed or duplicate runner in the inventory of {target.name}")
                seen.add(item["id"])
                runners.append(GitHubRunner(item["id"], item["name"], item["status"], item["busy"],
                                            tuple(label["name"] for label in item["labels"])))
            if len(runners) >= total or not data["runners"]:
                break
        if total is None or len(runners) != total:
            raise GitHubError(f"partial runner inventory for {target.name}")
        return runners

    def registration_token(self, target: TargetConfig) -> str:
        _, data = self._request("POST", target.registration_token_url, ok=(201,))
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubError(f"GitHub returned no registration token for {target.name}")
        return token

    def runner_group(self, org: str, group_id: int) -> dict:
        _, data = self._request("GET", f"orgs/{org}/actions/runner-groups/{int(group_id)}")
        if not isinstance(data, dict) or data.get("id") != group_id or not isinstance(data.get("name"), str):
            raise GitHubError(f"runner group {group_id} of {org} is malformed")
        return data

    def get_runner(self, target: TargetConfig, runner_id: int) -> dict | None:
        status, data = self._request("GET", f"{target.runners_url}/{int(runner_id)}", ok=(200, 404))
        return data if status == 200 and isinstance(data, dict) else None

    def delete_runner(self, target: TargetConfig, runner_id: int) -> None:
        self._request("DELETE", f"{target.runners_url}/{int(runner_id)}", ok=(204, 404))

    def set_labels(self, target: TargetConfig, runner_id: int, labels: Sequence[str]) -> set[str]:
        _, data = self._request("PUT", f"{target.runners_url}/{int(runner_id)}/labels", {"labels": list(labels)})
        rows = data.get("labels") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise GitHubError("GitHub returned no labels")
        defaults = folded(DEFAULT_RUNNER_LABELS)
        return {row.get("name") for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)
                and row["name"].casefold() not in defaults}


def folded(labels: Any) -> set[str]:
    """Labels compared the way GitHub compares them: without case."""
    return {str(label).casefold() for label in labels}


def custom_labels(runner: GitHubRunner) -> set[str]:
    defaults = folded(DEFAULT_RUNNER_LABELS)
    return {label for label in runner.labels if label.casefold() not in defaults}


def _looks_like_machine_type(label: str) -> bool:
    from .machine_profiles import machine_profile
    return machine_profile(label.casefold()) is not None


# --------------------------------------------------------------------------------------------
# Compute Engine inventory (read-only REST; mutations go through gcloud and runner_manager)
# --------------------------------------------------------------------------------------------

COMPUTE_API = "https://compute.googleapis.com/compute/v1"


@dataclass(frozen=True)
class Host:
    name: str
    zone: str
    status: str
    instance_id: str
    machine_type: str = ""
    spot: bool = True
    target_label: str = ""
    startup_script: bool = False  # the startup-script metadata key (and the one-use token) is still present
    last_start: str = ""
    disks: tuple[dict, ...] = ()

    @property
    def region(self) -> str:
        return self.zone.rsplit("-", 1)[0]

    @property
    def running(self) -> bool:
        return self.status == "RUNNING"

    def describe(self) -> str:
        return f"{self.name} ({self.zone}, {self.machine_type or 'type unknown'}, {self.status})"


def host_from_resource(item: Mapping[str, Any]) -> Host:
    """A Host from a Compute Engine instance resource; raises ValueError on a malformed one."""
    name, zone, status = item.get("name"), str(item.get("zone") or "").rsplit("/", 1)[-1], item.get("status")
    instance_id = str(item.get("id") or "")
    if not isinstance(name, str) or not name or not zone or not isinstance(status, str) or not instance_id.isdigit():
        raise ValueError("instance resource without a name, zone, status, or numeric id")
    scheduling = item.get("scheduling") if isinstance(item.get("scheduling"), dict) else {}
    labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    keys = {row.get("key") for row in metadata.get("items") or [] if isinstance(row, dict)}
    disks = tuple({k: disk[k] for k in ("source", "deviceName", "boot", "autoDelete") if k in disk}
                  for disk in item.get("disks") or [] if isinstance(disk, dict))
    return Host(name=name, zone=zone, status=status, instance_id=instance_id,
                machine_type=str(item.get("machineType") or "").rsplit("/", 1)[-1],
                spot=str(scheduling.get("provisioningModel", "")).upper() == "SPOT"
                or scheduling.get("preemptible") is True,
                target_label=str(labels.get("spot-target") or ""), startup_script="startup-script" in keys,
                last_start=str(item.get("lastStartTimestamp") or ""), disks=disks)


class ComputeInventory:
    """Lists runner hosts with the Compute Engine API; never accepts a partial answer."""

    def __init__(self, project: str, *, token: Callable[[], str] | None = None, transport: Transport | None = None,
                 timeout: float = 30.0) -> None:
        self.project = project
        self.token = token or AccessToken()
        self.transport = transport or https_request
        self.timeout = timeout

    def _get(self, url: str) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.token()}", "User-Agent": "spot-runner-floor/1.0"}
        try:
            response = self.transport("GET", url, headers, None, self.timeout)
        except TransportError as exc:
            raise FloorError(f"Compute Engine inventory failed: {exc}") from exc
        if response.status == 404:
            return 404, None
        if response.status != 200:
            raise FloorError(f"Compute Engine inventory returned HTTP {response.status}")
        try:
            return 200, json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FloorError("Compute Engine inventory returned invalid JSON") from exc

    def list_hosts(self, prefix: str = HOST_PREFIX) -> list[Host]:
        project = urllib.parse.quote(self.project, safe="")
        # No server-side filter (a project has few VMs): the prefix is matched here, so a filter syntax change on
        # the API side can never hide a host.
        query = {"maxResults": "500", "returnPartialSuccess": "false"}
        hosts: list[Host] = []
        token = ""
        for _ in range(100):
            params = dict(query, **({"pageToken": token} if token else {}))
            _, data = self._get(f"{COMPUTE_API}/projects/{project}/aggregated/instances?"
                                + urllib.parse.urlencode(params))
            if not isinstance(data, dict) or not isinstance(data.get("items", {}), dict):
                raise FloorError("Compute Engine inventory is malformed")
            if data.get("unreachables"):
                raise FloorError("Compute Engine inventory is incomplete: unreachable zones")
            for scope, rows in (data.get("items") or {}).items():
                if not isinstance(rows, dict):
                    raise FloorError("Compute Engine inventory is malformed")
                warning = rows.get("warning")
                if isinstance(warning, dict) and warning.get("code") not in (None, "NO_RESULTS_ON_PAGE"):
                    raise FloorError(f"Compute Engine inventory is incomplete in {scope}: {warning.get('code')}")
                for item in rows.get("instances") or []:
                    try:
                        host = host_from_resource(item)
                    except ValueError as exc:
                        raise FloorError(f"Compute Engine inventory is malformed: {exc}") from exc
                    if host.name.startswith(prefix):
                        hosts.append(host)
            token = str(data.get("nextPageToken") or "")
            if not token:
                return hosts
        raise FloorError("Compute Engine inventory has too many pages")

    def describe(self, name: str, zone: str) -> Host | None:
        quote = lambda value: urllib.parse.quote(value, safe="")  # noqa: E731
        status, data = self._get(f"{COMPUTE_API}/projects/{quote(self.project)}/zones/{quote(zone)}/instances/"
                                 f"{quote(name)}")
        if status == 404:
            return None
        try:
            return host_from_resource(data)
        except ValueError as exc:
            raise FloorError(f"instance {name} is malformed: {exc}") from exc


# --------------------------------------------------------------------------------------------
# State and lease in Cloud Storage, one pair of objects per target
# --------------------------------------------------------------------------------------------

def _project_key(project: str) -> str:
    return project.replace(":", "__")


def object_names(location: StateLocation, project: str, target: FloorTarget | str) -> tuple[str, str]:
    """(state object, lease object) of one target. The autoscaler's lease holder uses the same names."""
    key = target.key if isinstance(target, FloorTarget) else target_label_value(target)
    base = location.object_name(f"floor-{_project_key(project)}-{key}")
    return base + ".json", base + ".lock"


class FloorStateConflict(FloorError):
    pass


class TargetStore:
    """One target's floor record. Saves need the lease and the generation they loaded (``ifGenerationMatch``)."""

    def __init__(self, client: GcsClient, location: StateLocation, project: str, target: FloorTarget, *,
                 wall: Callable[[], float] = time.time) -> None:
        self.client = client
        self.location = location
        self.project = project
        self.target = target
        self.object_name, self.lock_name = object_names(location, project, target)
        self.generation = 0
        self.record: dict[str, Any] = {}
        self.wall = wall

    def load(self) -> dict[str, Any]:
        obj = self.client.get(self.object_name)
        if obj is None:
            self.record, self.generation = {}, 0
            return self.record
        try:
            envelope = json.loads(obj.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FloorStateConflict(f"floor state {self.object_name} is not JSON; repair it by hand") from exc
        if (not isinstance(envelope, dict) or envelope.get("schema") != STATE_SCHEMA
                or envelope.get("project") != self.project
                or str(envelope.get("target") or "").lower() != self.target.name.lower()
                or not isinstance(envelope.get("record"), dict)):
            raise FloorStateConflict(f"floor state {self.object_name} is not this project's and target's "
                                     f"{STATE_SCHEMA} object; repair it by hand")
        self.record, self.generation = envelope["record"], obj.generation
        return self.record

    def save(self, lease: GcsLease) -> None:
        lease.ensure()
        write_id = uuid.uuid4().hex
        data = json.dumps({"schema": STATE_SCHEMA, "project": self.project, "target": self.target.name,
                           "writer": lease.owner, "write_id": write_id, "written_at": self.wall(),
                           "record": self.record}, sort_keys=True).encode("utf-8")
        try:
            info = self.client.put(self.object_name, data, if_generation_match=self.generation)
        except GcsPreconditionFailed as exc:
            raise FloorStateConflict(f"floor state of {self.target.name} changed since it was read; another "
                                     "controller acted, so this step stops") from exc
        except GcsAmbiguous as exc:
            obj = self.client.get(self.object_name)
            try:
                if obj is not None and json.loads(obj.data.decode("utf-8")).get("write_id") == write_id:
                    self.generation = obj.generation
                    return
            except (ValueError, UnicodeDecodeError, AttributeError):
                pass
            raise FloorStateConflict(f"could not confirm the floor state write of {self.target.name}: {exc}") from exc
        self.generation = info.generation


def target_lease(client: GcsClient, location: StateLocation, project: str, target: FloorTarget | str,
                 owner: str | None = None) -> GcsLease:
    return GcsLease(client, object_names(location, project, target)[1], owner=owner)


# --------------------------------------------------------------------------------------------
# Time budget
# --------------------------------------------------------------------------------------------

class Deadline:
    """The time left in this tick (wall clock, because the server hands the deadline to a child process)."""

    def __init__(self, epoch: float | None = None, *, wall: Callable[[], float] = time.time) -> None:
        self.epoch = epoch
        self.wall = wall

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, budget_seconds: float | None = None,
                 *, wall: Callable[[], float] = time.time) -> "Deadline":
        env = os.environ if env is None else env
        candidates = []
        raw = str(env.get(ENV_TICK_DEADLINE, "") or "").strip()
        if raw:
            try:
                candidates.append(float(raw) - DEADLINE_MARGIN_SECONDS)
            except ValueError:
                raise ValueError(f"{ENV_TICK_DEADLINE} must be a Unix time") from None
        if budget_seconds is not None:
            candidates.append(wall() + float(budget_seconds))
        return cls(min(candidates) if candidates else None, wall=wall)

    def remaining(self) -> float:
        return float("inf") if self.epoch is None else self.epoch - self.wall()

    def require(self, seconds: float, what: str) -> None:
        left = self.remaining()
        if left < seconds:
            raise BudgetExhausted(f"{what} needs {int(seconds)} s of the tick, {max(int(left), 0)} s are left; "
                                  "it waits for the next tick")

    def cap(self, timeout: float) -> float:
        """A call timeout that ends inside the budget; raises when too little is left to start the call."""
        left = self.remaining()
        if left < 10:
            raise BudgetExhausted("the tick's time budget is used up")
        return min(float(timeout), left)


@contextlib.contextmanager
def hard_deadline(seconds: float) -> Iterator[None]:
    """Raise DeadlineExceeded after ``seconds`` (main thread only; elsewhere a no-op). A ``subprocess.run`` blocked
    when it fires kills its child, so a hung gcloud inside runner_manager can't outlive the budget or the lease."""
    if threading.current_thread() is not threading.main_thread() or seconds == float("inf"):
        yield
        return
    # Nested deadlines: the inner one never outlives the outer one, and the outer one is re-armed afterwards.
    outer, _interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    seconds = min(float(seconds), outer) if outer > 0 else float(seconds)

    def fire(signum: int, frame: Any) -> None:
        raise DeadlineExceeded(f"the step ran past its {int(seconds)} s deadline")

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.01))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if outer > 0:
            signal.setitimer(signal.ITIMER_REAL, max(outer - (time.monotonic() - started), 0.01))


# --------------------------------------------------------------------------------------------
# Planner (pure)
# --------------------------------------------------------------------------------------------

LEASE_ACTIONS = frozenset({"launch", "restart", "confirm_create", "launch_failed", "finish_launch",
                           "abandon_launch", "launch_lost", "finish_restart", "fail_restart", "restart_lost",
                           "adopt_late", "retire_late"})


@dataclass(frozen=True)
class Plan:
    action: str
    reason: str
    host: Host | None = None

    @property
    def idle(self) -> bool:
        """No lease, no write: nothing to do, or only waiting."""
        return self.action not in LEASE_ACTIONS

    def describe(self) -> str:
        return f"{self.action}: {self.reason}"


def managed_hosts(target: FloorTarget, hosts: Sequence[Host]) -> list[Host]:
    return [h for h in hosts if h.spot and h.name.startswith(HOST_PREFIX) and target.owns(h)]


def _runners_by_name(runners: Sequence[GitHubRunner]) -> dict[str, GitHubRunner]:
    return {runner.name: runner for runner in runners}


def restart_problem(host: Host, runners: Sequence[GitHubRunner], record: Mapping[str, Any]) -> str | None:
    """Why starting this stopped host again is unsafe, or None when every guard holds.

    A start runs no bootstrap only when the startup-script metadata is gone (runner_manager removes it after a
    completed bootstrap). The runners must still be registered, offline, idle, and labeled: a drain deregisters
    runners or strips their labels, and the host job-start cordon marker lives in /run, so a restart would silently
    put a host that was being retired back in service.
    """
    own = record.get("host") if isinstance(record.get("host"), dict) else {}
    if not (own and own.get("instance_id") == host.instance_id and own.get("name") == host.name
            and own.get("zone") == host.zone):
        return "it is not this keeper's recorded floor host"
    if host.status not in STOPPED_STATUSES:
        return f"its status is {host.status}"
    if host.startup_script:
        return "its startup-script metadata is still present, so a start would replay the fresh-host bootstrap"
    if host.instance_id in (record.get("restart_failed") or []):
        return "an earlier start did not bring its runners back"
    names = own.get("runner_names") or []
    if not names:
        return "its runner names are not recorded"
    by_name = _runners_by_name(runners)
    wanted = folded(own.get("labels") or [])
    for name in names:
        runner = by_name.get(name)
        if runner is None:
            return f"runner {name} is no longer registered"
        if runner.online or runner.busy:
            return f"runner {name} is online or busy while its host is stopped"
        if not wanted or not wanted <= folded(custom_labels(runner)):
            return f"runner {name} lost its scheduling labels"
    return None


def plan_target(target: FloorTarget, hosts: Sequence[Host], runners: Sequence[GitHubRunner],
                record: Mapping[str, Any], now: float) -> Plan:
    """The one step this target needs now. ``hosts`` is every ``oxidex-runners-*`` VM of the project."""
    operation = record.get("operation")
    if isinstance(operation, dict) and operation:
        return _plan_operation(hosts, runners, operation, now)
    own = managed_hosts(target, hosts)
    for late in record.get("late_creates") or []:
        vm = next((h for h in hosts if h.name == late.get("name") and h.zone == late.get("zone")), None)
        if vm is not None and now < float(late.get("until") or 0):
            others = [h for h in own if h.name != vm.name and h.status in LIVE_STATUSES
                      and host_serving(h, runners, now)]
            if others:
                # Another host already holds the floor: the late VM is excess, never a second floor host.
                return Plan("retire_late", f"{vm.describe()} appeared after its create was given up, but "
                                           f"{others[0].describe()} already serves the floor; retiring the excess VM",
                            vm)
            return Plan("adopt_late", f"{vm.describe()} appeared after its create was given up; adopting it", vm)
    live = [h for h in own if h.status in LIVE_STATUSES]
    serving = [h for h in live if host_serving(h, runners, now)]
    if serving:
        return Plan("noop", f"floor met: {serving[0].describe()}", serving[0])
    settling = [h for h in own if h.status in SETTLING_STATUSES]
    if settling:
        return Plan("wait", f"{settling[0].describe()} is changing state; waiting for it to settle", settling[0])
    next_attempt = float(record.get("next_attempt_at") or 0)
    if now < next_attempt:
        return Plan("wait", f"no running floor host; the next attempt waits {int(next_attempt - now)} s after "
                            f"{int(record.get('failures') or 0)} failure(s)")
    notes = [f"{h.describe()} has had no runner online for {LAUNCH_DEADLINE_SECONDS // 60} minutes; it stays for "
             "the autoscaler or an operator" for h in live]
    for host in own:
        if host.status in STOPPED_STATUSES:
            problem = restart_problem(host, runners, record)
            if problem is None:
                return Plan("restart", f"own floor host {host.describe()} is stopped; starting it", host)
            notes.append(f"{host.name} can't be started again ({problem})")
        elif host.status not in LIVE_STATUSES:
            notes.append(f"{host.describe()} is not usable")
    return Plan("launch", "no running floor host" + (f"; {'; '.join(notes)}" if notes else ""))


def parse_gce_time(text: str) -> float | None:
    try:
        return _dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def runners_of(host: Host, runners: Sequence[GitHubRunner]) -> list[GitHubRunner]:
    """The runners registered for ``host`` (``<prefix>-<host name>-<n>``, the names startup.sh gives them)."""
    pattern = re.compile(rf"-{re.escape(host.name)}-[0-9]+$")
    return [runner for runner in runners if pattern.search(runner.name)]


def host_serving(host: Host, runners: Sequence[GitHubRunner], now: float) -> bool:
    """Whether a live host meets the floor: still starting, booted less than LAUNCH_DEADLINE_SECONDS ago (or at an
    unknown time), or with at least one runner online. A host whose runners all stayed offline longer than that
    doesn't, and the keeper launches another host next to it (it never deletes the broken one)."""
    if host.status != "RUNNING" or any(runner.online for runner in runners_of(host, runners)):
        return True
    started = parse_gce_time(host.last_start)
    return started is None or now - started < LAUNCH_DEADLINE_SECONDS


def _plan_operation(hosts: Sequence[Host], runners: Sequence[GitHubRunner], op: Mapping[str, Any],
                    now: float) -> Plan:
    vm = next((h for h in hosts if h.name == op.get("name") and h.zone == op.get("zone")), None)
    expected = list(op.get("expected_runners") or [])
    by_name = _runners_by_name(runners)
    online = [n for n in expected if n in by_name and by_name[n].online]
    busy = [n for n in expected if n in by_name and by_name[n].busy]
    recorded_id = str(op.get("instance_id") or "")
    if vm is not None and recorded_id and vm.instance_id != recorded_id:
        return Plan("launch_lost" if op.get("kind") == "launch" else "restart_lost",
                    f"{vm.name} was replaced under the same name (instance {vm.instance_id}, not {recorded_id}); "
                    "the keeper leaves that VM alone", vm)
    if op.get("kind") == "restart":
        if vm is None:
            return Plan("restart_lost", f"{op.get('name')} disappeared while it was starting")
        if vm.running and expected and len(online) == len(expected):
            return Plan("finish_restart", f"{vm.describe()} is back with {len(online)} runner(s) online", vm)
        if now - float(op.get("started_at") or 0) >= RESTART_DEADLINE_SECONDS:
            return Plan("fail_restart", f"{vm.describe()} has {len(online)}/{len(expected)} runner(s) online "
                                        f"{RESTART_DEADLINE_SECONDS} s after its start", vm)
        return Plan("wait", f"{vm.describe()} is starting; {len(online)}/{len(expected)} runner(s) online", vm)
    if op.get("phase") == "creating":
        if vm is not None:
            return Plan("confirm_create", f"{vm.describe()} exists; recording its identity", vm)
        if now - float(op.get("started_at") or 0) >= CREATE_CONFIRM_SECONDS:
            return Plan("launch_failed", f"{op.get('name')} never appeared after its create")
        return Plan("wait", f"the create of {op.get('name')} may still be in flight")
    if vm is None:
        return Plan("launch_lost", f"{op.get('name')} disappeared during its bootstrap (preempted or deleted)")
    if vm.running and expected and len(online) == len(expected):
        return Plan("finish_launch", f"{vm.describe()} has every runner online", vm)
    if vm.status in STOPPED_STATUSES or vm.status == "SUSPENDED":
        return Plan("abandon_launch", f"{vm.describe()} stopped during its bootstrap", vm)
    if now - float(op.get("created_at") or op.get("started_at") or 0) >= LAUNCH_DEADLINE_SECONDS:
        if busy:
            return Plan("wait", f"{vm.describe()} missed its deadline, but runner(s) {', '.join(busy)} run a job", vm)
        return Plan("abandon_launch", f"{vm.describe()} has {len(online)}/{len(expected)} runner(s) online "
                                      f"{LAUNCH_DEADLINE_SECONDS} s after its create", vm)
    return Plan("wait", f"{vm.describe()} is bootstrapping; {len(online)}/{len(expected)} runner(s) online", vm)


# --------------------------------------------------------------------------------------------
# Offers (the smallest shape that holds one runner, cheapest total daily price first)
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FloorOffer:
    machine_type: str
    region: str
    zone: str
    ram_gib: int
    hourly_usd: Decimal
    storage_monthly_usd: Decimal

    @property
    def total_daily_usd(self) -> Decimal:
        from .billing_catalog import monthly_to_daily
        return self.hourly_usd * 24 + monthly_to_daily(self.storage_monthly_usd)

    def to_dict(self) -> dict[str, str]:
        return {"machine_type": self.machine_type, "region": self.region, "zone": self.zone,
                "ram_gib": str(self.ram_gib), "hourly_usd": str(self.hourly_usd),
                "total_daily_usd": str(self.total_daily_usd.quantize(Decimal("0.0001")))}


def price_offers(shapes: Sequence[Mapping[str, Any]], regions: Sequence[str], min_ram_gib: int, *,
                 rates: Callable[[str, str], tuple[Decimal, Decimal]],
                 storage: Callable[[str], Mapping[str, Decimal]]) -> list[FloorOffer]:
    """Price every 4-vCPU shape of a supported x86 family in ``regions`` with at least ``min_ram_gib``.

    ``shapes`` are machine-type rows (name, zone, guestCpus, memoryMb); ``rates(region, family)`` returns the exact
    Spot (core, GiB) hourly prices and ``storage(region)`` the disk rates, both from the billing catalog, the same
    sources as runner_manager's price guard. Storage is the cheapest SSD-backed choice runner_manager would make.
    """
    from .machine_profiles import machine_profile
    from .storage_selection import cheapest_storage

    offers = []
    rate_cache: dict[tuple[str, str], tuple[Decimal, Decimal] | None] = {}
    storage_cache: dict[str, Mapping[str, Decimal] | None] = {}
    for row in shapes:
        name, zone = str(row.get("name") or ""), str(row.get("zone") or "").rsplit("/", 1)[-1]
        region = zone.rsplit("-", 1)[0]
        try:
            vcpu, memory_mb = int(row.get("guestCpus")), int(row.get("memoryMb"))
        except (TypeError, ValueError):
            continue
        ram_gib = memory_mb // 1024
        if (region not in regions or vcpu != FLOOR_VCPU or machine_profile(name) is None
                or not min_ram_gib <= ram_gib <= MAX_RAM_GIB or memory_mb % 1024):
            continue
        family = name.split("-", 1)[0]
        if (region, family) not in rate_cache:
            try:
                rate_cache[(region, family)] = rates(region, family)
            except (RuntimeError, KeyError, ValueError) as exc:  # a missing SKU, never a failed catalog read
                LOG.info("Runner floor: no Spot price for %s in %s: %s", family, region, exc)
                rate_cache[(region, family)] = None
        if region not in storage_cache:
            try:
                storage_cache[region] = storage(region)
            except (RuntimeError, KeyError, ValueError) as exc:
                LOG.info("Runner floor: no storage prices in %s: %s", region, exc)
                storage_cache[region] = None
        rate, disk_rates = rate_cache[(region, family)], storage_cache[region]
        if rate is None or disk_rates is None:
            continue
        try:
            _boot, _data, monthly = cheapest_storage(name, DATA_DISK_GB, dict(disk_rates))
        except RuntimeError:
            continue
        hourly = Decimal(str(rate[0])) * vcpu + Decimal(str(rate[1])) * Decimal(memory_mb) / 1024
        if not hourly.is_finite() or hourly <= 0:
            continue
        offers.append(FloorOffer(name, region, zone, ram_gib, hourly, Decimal(str(monthly))))
    offers.sort(key=lambda o: (o.total_daily_usd, o.region, o.zone, o.machine_type))
    return offers


def choose_offer(offers: Sequence[FloorOffer], max_daily_usd: Decimal, *, failed_zones: Sequence[str] = (),
                 occupied_zones: Sequence[str] = (), occupied_regions: Sequence[str] = ()) -> FloorOffer | None:
    """The cheapest offer strictly under the cap outside failed zones, spread away from other floor targets.

    A zone that no other floor target's host uses wins if it costs at most SPREAD_PREMIUM times the cheapest
    affordable offer, and a region that none uses breaks ties after that, so one zonal Spot shortage is less likely
    to take every target's floor at once.
    """
    affordable = [o for o in offers if o.total_daily_usd < max_daily_usd and o.zone not in failed_zones]
    if not affordable:
        return None
    cheapest = min(o.total_daily_usd for o in affordable)
    spread = [o for o in affordable if o.zone not in occupied_zones and o.total_daily_usd <= cheapest * SPREAD_PREMIUM]
    if spread:
        return min(spread, key=lambda o: (o.region in occupied_regions, o.total_daily_usd, o.region, o.zone,
                                          o.machine_type))
    return min(affordable, key=lambda o: (o.total_daily_usd, o.region, o.zone, o.machine_type))


# --------------------------------------------------------------------------------------------
# Provider: gcloud and runner_manager behind the lease guard and the time budget
# --------------------------------------------------------------------------------------------

def manager_argv(project: str, target: FloorTarget, offer_or_op: Mapping[str, Any] | FloorOffer, name: str,
                 disk_name: str, max_daily_usd: Decimal) -> list[str]:
    """runner_manager arguments for a floor host (recorded in the operation so later ticks rebuild the same)."""
    data = offer_or_op.to_dict() if isinstance(offer_or_op, FloorOffer) else dict(offer_or_op)
    argv = ["--project", project, "--target", target.name,
            "--max-cost", str((Decimal(max_daily_usd) / 24).quantize(Decimal("0.000001"))),
            "--max-total-daily-usd", str(max_daily_usd), "--cheapest-storage",
            "--instance-type", str(data["machine_type"]), "--region", str(data["region"]), "--zone", str(data["zone"]),
            "--cpu", str(FLOOR_VCPU), "--ram", str(int(data["ram_gib"])), "--instance-name", name,
            "--disk-device-name", disk_name, "--disk-size-gb", str(DATA_DISK_GB),
            "--runner-prefix", RUNNER_PREFIX, "--runner-label", BASE_LABEL, "--runner-count", "1"]
    if target.group_id:
        argv += ["--runner-group-id", str(target.group_id)]
    return argv


def manager_args(argv: Sequence[str], target: FloorTarget) -> argparse.Namespace:
    """Parse and size runner_manager arguments for a floor host: one runner, no service account."""
    from . import runner_manager

    args = runner_manager.args_parser().parse_args(list(argv))
    # The parser defaults --runner-group-id to the oxidex group; a floor target names its own group or none.
    args.runner_group_id = target.group_id
    args.extra_runner_labels = tuple(target.labels)
    # Floor hosts run with no service account and no scopes: CI jobs reach the metadata server, and the watchdog
    # needs no iam.serviceAccountUser on any runner account.
    args.no_service_account = True
    runner_manager.select_sizing(args)
    if args.runner_count != 1:
        raise FloorError(f"a {FLOOR_VCPU}-vCPU floor host must size to exactly one runner, not {args.runner_count}")
    return args


def expected_runner_names(name: str, count: int = 1) -> list[str]:
    return [f"{RUNNER_PREFIX}-{name}-{index}" for index in range(1, count + 1)]


class GcloudProvider:
    """Real provider calls. Every call runs the lease guard first and ends inside the tick's budget."""

    def __init__(self, project: str, *, guard: Callable[[], None], deadline: Deadline,
                 inventory: ComputeInventory | None = None,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.project = project
        self.guard = guard
        self.deadline = deadline
        self.inventory = inventory or ComputeInventory(project)
        self.runner = runner

    def gcloud(self, argv: Sequence[str], *, timeout: float = GCLOUD_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
        self.guard()
        env = dict(os.environ, CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR="false", CLOUDSDK_CORE_DISABLE_PROMPTS="1")
        return self.runner(["gcloud", *argv, f"--project={self.project}"], check=False, capture_output=True,
                           text=True, timeout=self.deadline.cap(timeout), env=env)

    def _checked(self, argv: Sequence[str], what: str, *, timeout: float = GCLOUD_TIMEOUT_SECONDS) -> str:
        result = self.gcloud(argv, timeout=timeout)
        if result.returncode != 0:
            raise FloorError(f"{what} failed: {(result.stderr or '').strip()[-400:]}")
        return result.stdout or ""

    @contextlib.contextmanager
    def manager(self) -> Iterator[Any]:
        """runner_manager with this project, under the guard and a hard deadline (it sets no call timeouts)."""
        from . import runner_manager

        self.guard()
        previous = runner_manager.PROJECT
        runner_manager.PROJECT = self.project
        try:
            with hard_deadline(self.deadline.cap(GCLOUD_TIMEOUT_SECONDS)):
                yield runner_manager
        finally:
            runner_manager.PROJECT = previous

    def list_hosts(self) -> list[Host]:
        return self.inventory.list_hosts()

    def describe(self, name: str, zone: str) -> Host | None:
        return self.inventory.describe(name, zone)

    def offers(self, config: FloorConfig, target: FloorTarget) -> list[FloorOffer]:
        from .runner_manager import spot_rates
        from .storage_selection import storage_rates

        pattern = "|".join(config.regions)  # each matches _REGION_RE: letters, digits, and hyphens only
        out = self._checked(["compute", "machine-types", "list",
                             f"--filter=guestCpus={FLOOR_VCPU} AND zone~^({pattern})-",
                             "--format=json(name,zone,guestCpus,memoryMb)"], "machine-type inventory", timeout=120)
        rows = json.loads(out or "[]")
        from .billing_catalog import catalog_skus
        with hard_deadline(self.deadline.cap(300)):
            try:
                catalog_skus()  # cached for spot_rates and storage_rates below
            except (OSError, ValueError) as exc:
                raise FloorError(f"the billing catalog is unavailable, so no floor offer can be priced: {exc}") from exc
            return price_offers(rows if isinstance(rows, list) else [], config.regions, target.min_ram_gib,
                                rates=spot_rates, storage=storage_rates)

    def preflight(self, args: argparse.Namespace, zone: str) -> None:
        """runner_manager's live price, storage, and quota checks; raises when the offer is over the cap."""
        with self.manager() as runner_manager:
            runner_manager.verify_price_and_capacity(args, zone)

    def create(self, args: argparse.Namespace, zone: str, script: str) -> None:
        with self.manager() as runner_manager:
            runner_manager.apply_startup(args, zone, script)

    def record_no_patch(self, args: argparse.Namespace, zone: str, boot: str) -> None:
        with self.manager() as runner_manager:
            runner_manager.record_patch_metadata(args, zone, None, boot=boot, clear_pending=True)

    def remove_startup_script(self, host: Host) -> None:
        self._checked(["compute", "instances", "remove-metadata", host.name, f"--zone={host.zone}",
                       "--keys=startup-script", "--quiet"], f"removing the startup script of {host.name}", timeout=180)

    def start(self, host: Host) -> None:
        self._checked(["compute", "instances", "start", host.name, f"--zone={host.zone}", "--quiet"],
                      f"starting {host.name}", timeout=300)

    def delete_instance(self, host: Host, *, all_disks: bool = True) -> None:
        """Delete the VM with all attached disks, or (``all_disks=False``) with only its auto-delete disks."""
        self._checked(["compute", "instances", "delete", host.name, f"--zone={host.zone}",
                       *(["--delete-disks=all"] if all_disks else []), "--quiet"], f"deleting {host.name}", timeout=300)

    def delete_orphan_disk(self, name: str, zone: str) -> bool:
        """Delete the floor host's own data disk only when it is unattached, READY, and the manager's."""
        result = self.gcloud(["compute", "disks", "describe", name, f"--zone={zone}",
                              "--format=json(name,status,users,labels)"], timeout=60)
        if result.returncode != 0:
            return False  # absent (or unreadable: left for the manager's own six-hour orphan cleanup)
        disk = json.loads(result.stdout or "{}")
        if (disk.get("users") or disk.get("status") != "READY"
                or (disk.get("labels") or {}).get("managed-by") != "spot-runner-manager"):
            return False
        self._checked(["compute", "disks", "delete", name, f"--zone={zone}", "--quiet"],
                      f"deleting the orphan disk {name}", timeout=180)
        return True


# --------------------------------------------------------------------------------------------
# Executor: one step per target per tick
# --------------------------------------------------------------------------------------------

class FloorKeeper:
    """Executes a Plan for one target. ``save`` persists ``record`` under the lease before each provider action."""

    def __init__(self, config: FloorConfig, target: FloorTarget, record: dict[str, Any], save: Callable[[], None], *,
                 provider: Any, github: GitHubClient, deadline: Deadline | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.target = target
        self.record = record
        self._save = save
        self.provider = provider
        self.github = github
        self.deadline = deadline or Deadline()
        self.clock = clock

    # -- bookkeeping ---------------------------------------------------------------------
    def save(self) -> None:
        self.record["updated_at"] = self.clock()
        self._save()

    def _saved(self, event: str, detail: str) -> str:
        """Record a terminal event in the history, then save: the event is part of the write that ends the step."""
        text = self.note(event, detail)
        self.save()
        return text

    def note(self, event: str, detail: str) -> str:
        history = self.record.setdefault("history", [])
        history.append({"at": self.clock(), "event": event, "detail": detail[:500]})
        del history[:-HISTORY_LIMIT]
        LOG.info("Runner floor %s: %s: %s", self.target.name, event, detail)
        return f"{event}: {detail}"

    def _failed_zones(self) -> list[str]:
        now = self.clock()
        zones = {z: until for z, until in (self.record.get("failed_zones") or {}).items() if float(until) > now}
        self.record["failed_zones"] = zones
        return sorted(zones)

    def _record_failure(self, zone: str | None) -> None:
        failures = int(self.record.get("failures") or 0) + 1
        self.record["failures"] = failures
        self.record["next_attempt_at"] = self.clock() + min(BACKOFF_MAX_SECONDS,
                                                            BACKOFF_BASE_SECONDS * 2 ** min(failures - 1, 6))
        if zone:
            self.record.setdefault("failed_zones", {})[zone] = self.clock() + ZONE_BACKOFF_SECONDS

    # -- dispatch ------------------------------------------------------------------------
    def execute(self, plan: Plan, hosts: Sequence[Host]) -> str:
        handler = getattr(self, f"_do_{plan.action}", None)
        if handler is None or plan.idle:
            return plan.describe()
        return handler(plan, hosts)

    # -- launch --------------------------------------------------------------------------
    def _do_launch(self, plan: Plan, hosts: Sequence[Host]) -> str:
        from . import runner_manager
        from .spot_runner_autoscaler import launch_name_suffix

        self.deadline.require(LAUNCH_MIN_SECONDS, f"a floor launch for {self.target.name}")
        self._prune_late_creates()
        others = [h for h in hosts if h.status in LIVE_STATUSES and h.name.startswith(HOST_PREFIX)
                  and any(t.owns(h) for t in self.config.targets if t.name.lower() != self.target.name.lower())]
        offers = self.provider.offers(self.config, self.target)
        offer = choose_offer(offers, self.config.max_daily_usd, failed_zones=self._failed_zones(),
                             occupied_zones=sorted({h.zone for h in others}),
                             occupied_regions=sorted({h.region for h in others}))
        if offer is None:
            self._record_failure(None)
            return self._saved("launch_held", f"no 4-vCPU Spot offer with at least {self.target.min_ram_gib} GiB costs "
                                            f"less than ${self.config.max_daily_usd}/day in "
                                            f"{', '.join(self.config.regions)} outside failed zones")
        suffix = launch_name_suffix(offer.region, offer.zone, self.target.name)
        name, disk_name = HOST_PREFIX + suffix, DISK_PREFIX + suffix
        argv = manager_argv(self.config.project, self.target, offer, name, disk_name, self.config.max_daily_usd)
        args = manager_args(argv, self.target)
        self.deadline.require(CREATE_MIN_SECONDS + 60, f"the floor host preflight for {self.target.name}")
        try:
            self.provider.preflight(args, offer.zone)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            self._record_failure(offer.zone)
            return self._saved("launch_held", f"{offer.machine_type} in {offer.zone} failed the price and quota "
                                            f"preflight: {exc}")
        group_name = ""
        if self.target.group_id and not self.target.is_repo:
            group = self.github.runner_group(self.target.name, self.target.group_id)
            if group.get("visibility") != "selected" or group.get("default"):
                why = (f"runner group {self.target.group_id} of {self.target.name} must be a selected-repository "
                       "group, not the Default group or one open to all repositories")
                self._record_failure(None)
                self._saved("launch_held", why)
                raise FloorError(why)
            group_name = group["name"]
        # Pricing and the preflight took time: the create itself must still fit, or nothing is saved or created.
        self.deadline.require(CREATE_MIN_SECONDS, f"the floor host create for {self.target.name}")
        operation = {"kind": "launch", "phase": "creating", "name": name, "zone": offer.zone,
                     "region": offer.region, "disk_name": disk_name, "machine_type": offer.machine_type,
                     "expected_runners": expected_runner_names(name, args.runner_count),
                     "labels": list(self.target.custom_labels(offer.machine_type, offer.region)),
                     "manager_argv": argv, "offer": offer.to_dict(), "deployment_id": uuid.uuid4().hex,
                     "started_at": self.clock(), "reason": plan.reason[:300]}
        # The intent is durable before the registration token exists or anything is created.
        self.record["operation"] = operation
        self.note("launch", f"creating {name} ({offer.machine_type}, {offer.zone}, "
                            f"${offer.total_daily_usd.quantize(Decimal('0.01'))}/day): {plan.reason}")
        self.save()
        try:
            token = self.github.registration_token(self.target.target)
            script = runner_manager.render_startup(args, token, group_name, operation["deployment_id"])
        except (RuntimeError, ValueError, OSError) as exc:
            # Nothing exists yet, so the launch ends now rather than after the create-confirm window.
            return self._launch_failed(f"no registration token or startup script for {name}: {exc}",
                                       zone_failure=False)
        token = ""
        try:
            self.provider.create(args, offer.zone, script)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, DeadlineExceeded) as exc:
            script = ""
            created = self.provider.describe(name, offer.zone)
            if created is None:
                return self._launch_failed(f"the create of {name} failed: {exc}")
            LOG.warning("Runner floor %s: the create of %s reported %s, but the VM exists", self.target.name, name, exc)
        script = ""
        created = self.provider.describe(name, offer.zone)
        if created is None:
            return self.note("launch", f"{name} was created but is not visible yet; the next tick confirms it")
        return self._booting(created)

    def _booting(self, vm: Host) -> str:
        operation = self.record["operation"]
        operation.update(phase="booting", instance_id=vm.instance_id,
                         created_at=operation.get("created_at") or self.clock())
        return self._saved("booting", f"{vm.describe()} created as instance {vm.instance_id}; waiting for its runners")

    def _do_confirm_create(self, plan: Plan, hosts: Sequence[Host]) -> str:
        return self._booting(plan.host)

    def _do_retire_late(self, plan: Plan, hosts: Sequence[Host]) -> str:
        """Delete an excess late VM with the keeper's own deletion rules: its own VM (pinned by instance ID), no
        runner online or busy, its registrations removed first. Otherwise it holds, logged, and after the watch
        window the VM is left for the autoscaler or an operator."""
        vm = plan.host
        entry = next(e for e in self.record.get("late_creates") or []
                     if e.get("name") == vm.name and e.get("zone") == vm.zone)
        if not entry.get("instance_id"):
            entry["instance_id"] = vm.instance_id  # the identity every later check must match
            self.save()
        elif entry["instance_id"] != vm.instance_id:
            self.record["late_creates"] = [e for e in self.record["late_creates"] if e is not entry]
            return self._saved("late_create_left", f"{vm.name} is now instance {vm.instance_id}, not the "
                                                 f"{entry['instance_id']} first seen; it isn't this keeper's to delete")
        names = list((entry.get("operation") or {}).get("expected_runners") or expected_runner_names(vm.name))
        runners = {r.name: r for r in self.github.list_runners(self.target.target)}
        active = [n for n in names if n in runners and (runners[n].online or runners[n].busy)]
        if active:
            return self.note("late_create_held", f"excess {vm.describe()} can't be deleted safely: runner(s) "
                                                 f"{', '.join(active)} are online or busy. It is retried until "
                                                 "the watch ends, then left for the autoscaler or an operator")
        for name in names:
            if name in runners:
                self.github.delete_runner(self.target.target, runners[name].runner_id)
        fresh = self.provider.describe(vm.name, vm.zone)
        if fresh is None or fresh.instance_id != entry["instance_id"]:
            self.record["late_creates"] = [e for e in self.record["late_creates"] if e is not entry]
            return self._saved("late_create_gone" if fresh is None else "late_create_left",
                               f"{vm.name} is gone" if fresh is None else f"{vm.name} is now another instance; "
                                                                           "left alone")
        if fresh.startup_script and fresh.status not in STOPPED_STATUSES:
            self.provider.remove_startup_script(fresh)
        in_use = self._runners_in_use(names)  # GitHub again, immediately before the delete
        if in_use:
            return self.note("late_create_held", f"{in_use} on {fresh.name} right before its delete; holding")
        self.provider.delete_instance(fresh)
        self.record["late_creates"] = [e for e in self.record["late_creates"] if e is not entry]
        return self._saved("late_create_retired", f"deleted excess {fresh.describe()}: {plan.reason}")

    def _do_adopt_late(self, plan: Plan, hosts: Sequence[Host]) -> str:
        entry = next(e for e in self.record.get("late_creates") or []
                     if e.get("name") == plan.host.name and e.get("zone") == plan.host.zone)
        self.record["late_creates"] = [e for e in self.record["late_creates"] if e is not entry]
        operation = dict(entry["operation"], phase="booting")
        operation.pop("instance_id", None)
        operation.pop("created_at", None)
        self.record["operation"] = operation
        return self._booting(plan.host)

    def _cleanup_registrations(self, names: Sequence[str]) -> list[str]:
        """Delete the offline, idle registrations of this launch's own runner names (never another host's)."""
        removed = []
        wanted = set(names)
        for runner in self.github.list_runners(self.target.target):
            if runner.name in wanted and not runner.online and not runner.busy:
                self.github.delete_runner(self.target.target, runner.runner_id)
                removed.append(runner.name)
        return removed

    def _prune_late_creates(self) -> None:
        now = self.clock()
        self.record["late_creates"] = [entry for entry in self.record.get("late_creates") or []
                                       if float(entry.get("until") or 0) > now]

    def _launch_failed(self, why: str, *, zone_failure: bool = True) -> str:
        operation = self.record.get("operation") or {}
        zone = operation.get("zone")
        if operation.get("phase") == "creating" and operation.get("name"):
            # The create may still land after this (a timed-out call, a killed tick): adopt its VM if it appears.
            self._prune_late_creates()
            self.record["late_creates"].append({"name": operation["name"], "zone": zone,
                                                "until": self.clock() + LATE_CREATE_WATCH_SECONDS,
                                                "operation": dict(operation)})
        try:
            if operation.get("disk_name") and zone:
                self.provider.delete_orphan_disk(operation["disk_name"], zone)
        except (FloorError, OSError, subprocess.SubprocessError, ValueError) as exc:
            LOG.warning("Runner floor %s: orphan disk cleanup failed (the manager's own cleanup retries): %s",
                        self.target.name, exc)
        self.record.pop("operation", None)
        self._record_failure(zone if zone_failure else None)
        return self._saved("launch_failed", why)

    def _do_launch_failed(self, plan: Plan, hosts: Sequence[Host]) -> str:
        return self._launch_failed(plan.reason)

    def _do_launch_lost(self, plan: Plan, hosts: Sequence[Host]) -> str:
        operation = self.record.get("operation") or {}
        if plan.host is None:  # gone: its own registrations are stale
            self._cleanup_registrations(operation.get("expected_runners") or [])
        return self._launch_failed(plan.reason)

    def _do_finish_launch(self, plan: Plan, hosts: Sequence[Host]) -> str:
        operation = self.record["operation"]
        vm = self.provider.describe(operation["name"], operation["zone"])
        if vm is None or vm.instance_id != operation.get("instance_id") or not vm.running:
            return self.note("finish_waits", f"{operation['name']} is not the running instance it recorded")
        # The one-use registration token leaves the metadata first.
        if vm.startup_script:
            self.provider.remove_startup_script(vm)
        wanted = list(operation.get("labels") or [])
        runners = {r.name: r for r in self.github.list_runners(self.target.target)}
        for name in operation["expected_runners"]:
            runner = runners.get(name)
            if runner is None or not runner.online:
                return self.note("finish_waits", f"runner {name} is no longer online")
            if folded(custom_labels(runner)) != folded(wanted):
                actual = self.github.set_labels(self.target.target, runner.runner_id, wanted)
                if folded(actual) != folded(wanted):
                    raise FloorError(f"runner {name} has labels {sorted(actual)}, expected {sorted(wanted)}")
        args = manager_args(operation["manager_argv"], self.target)
        self.provider.record_no_patch(args, vm.zone, vm.last_start)
        self.record["host"] = {"name": vm.name, "zone": vm.zone, "instance_id": vm.instance_id,
                               "machine_type": vm.machine_type, "region": vm.region,
                               "runner_names": list(operation["expected_runners"]), "labels": wanted,
                               "created_at": operation.get("created_at"), "ready_at": self.clock()}
        self.record.pop("operation", None)
        self.record["failures"] = 0
        self.record["next_attempt_at"] = 0
        return self._saved("ready", f"{vm.describe()} serves {self.target.name} with "
                                  f"{len(operation['expected_runners'])} runner(s)")

    def _runners_in_use(self, names: Sequence[str]) -> str | None:
        """A fresh GitHub read: why the named runners forbid a delete (one runs a job or is online), or None."""
        runners = {r.name: r for r in self.github.list_runners(self.target.target)}
        busy = [n for n in names if n in runners and runners[n].busy]
        if busy:
            return f"runner(s) {', '.join(busy)} run a job"
        online = [n for n in names if n in runners and runners[n].online]
        if online:
            return f"runner(s) {', '.join(online)} came online"
        return None

    def _do_abandon_launch(self, plan: Plan, hosts: Sequence[Host]) -> str:
        operation = self.record["operation"]
        names = operation.get("expected_runners") or []
        in_use = self._runners_in_use(names)
        if in_use:
            return self.note("abandon_waits", f"{in_use} on {operation['name']}; the next tick finishes or waits")
        vm = self.provider.describe(operation["name"], operation["zone"])
        if vm is not None:
            if vm.instance_id != operation.get("instance_id"):
                return self._launch_failed(f"{plan.reason}; {vm.name} is now another instance, left alone")
            if vm.startup_script and vm.status not in STOPPED_STATUSES:
                self.provider.remove_startup_script(vm)
            # GitHub again, immediately before the delete: a runner may have come online meanwhile.
            in_use = self._runners_in_use(names)
            if in_use:
                return self.note("abandon_waits", f"{in_use} on {vm.name} right before its delete; holding")
            self.provider.delete_instance(vm)
        self._cleanup_registrations(names)
        return self._launch_failed(f"abandoned: {plan.reason}")

    # -- restart -------------------------------------------------------------------------
    def _do_restart(self, plan: Plan, hosts: Sequence[Host]) -> str:
        self.deadline.require(STEP_MIN_SECONDS, f"a floor host start for {self.target.name}")
        host = plan.host
        fresh = self.provider.describe(host.name, host.zone)
        if fresh is None or fresh.instance_id != host.instance_id or fresh.status not in STOPPED_STATUSES \
                or fresh.startup_script:
            return self.note("restart_held", f"{host.name} changed before its start; planning again next tick")
        own = self.record.get("host") or {}
        self.record["operation"] = {"kind": "restart", "phase": "starting", "name": host.name, "zone": host.zone,
                                    "instance_id": host.instance_id,
                                    "expected_runners": list(own.get("runner_names") or []),
                                    "started_at": self.clock()}
        self.note("restart", plan.reason)
        self.save()
        self.provider.start(fresh)
        return f"restart: started {fresh.name}"

    def _do_finish_restart(self, plan: Plan, hosts: Sequence[Host]) -> str:
        self.record.pop("operation", None)
        self.record["failures"] = 0
        return self._saved("ready", plan.reason)

    def _do_fail_restart(self, plan: Plan, hosts: Sequence[Host]) -> str:
        operation = self.record.pop("operation", None) or {}
        failed = [i for i in self.record.get("restart_failed") or [] if i != operation.get("instance_id")]
        self.record["restart_failed"] = (failed + [operation.get("instance_id")])[-HISTORY_LIMIT:]
        self._record_failure(None)
        return self._saved("restart_failed", plan.reason)

    def _do_restart_lost(self, plan: Plan, hosts: Sequence[Host]) -> str:
        self.record.pop("operation", None)
        return self._saved("restart_lost", plan.reason)


# --------------------------------------------------------------------------------------------
# Tick
# --------------------------------------------------------------------------------------------

@dataclass
class Services:
    """Injectable dependencies of a tick (tests replace every one)."""

    inventory: Any
    github_for: Callable[[FloorTarget, Callable[[], None] | None], GitHubClient]
    provider_for: Callable[[Callable[[], None], Deadline], Any]
    client: GcsClient
    clock: Callable[[], float] = time.time
    lease_factory: Callable[[str], GcsLease] | None = None


def default_services(config: FloorConfig, *, tokens: TokenSource | None = None) -> Services:
    location = state_location(config.state_uri)
    client = GcsClient(location.bucket)
    inventory = ComputeInventory(config.project)
    tokens = tokens or TokenSource(config.token_source)

    def github_for(target: FloorTarget, guard: Callable[[], None] | None) -> GitHubClient:
        return GitHubClient(lambda: tokens(target), guard=guard)

    def provider_for(guard: Callable[[], None], deadline: Deadline) -> GcloudProvider:
        return GcloudProvider(config.project, guard=guard, deadline=deadline, inventory=inventory)

    return Services(inventory, github_for, provider_for, client)


def run_tick(config: FloorConfig, services: Services, *, apply: bool, deadline: Deadline | None = None,
             only: Sequence[str] = ()) -> tuple[int, list[dict[str, Any]]]:
    """One floor step for each target. Returns (exit code, per-target results); a target's failure never stops
    the others. Without ``apply`` nothing takes a lease or writes."""
    if not config.state_uri:
        raise FloorError(f"{ENV_STATE_URI} is not set")
    deadline = deadline or Deadline()
    location = state_location(config.state_uri)
    hosts = services.inventory.list_hosts()
    results: list[dict[str, Any]] = []
    code = 0
    for target in config.targets:
        if only and target.name.lower() not in {o.lower() for o in only}:
            continue
        result: dict[str, Any] = {"target": target.name}
        results.append(result)
        if deadline.remaining() < TARGET_MIN_SECONDS:
            result.update(status="skipped", detail="the tick's time budget is used up")
            continue
        try:
            store = TargetStore(services.client, location, config.project, target, wall=services.clock)
            record = store.load()
            github = services.github_for(target, None)
            runners = github.list_runners(target.target)
            plan = plan_target(target, hosts, runners, record, services.clock())
            result.update(action=plan.action, detail=plan.reason)
            if plan.idle or not apply:
                result["status"] = "idle" if plan.idle else "planned"
                continue
            lease = (services.lease_factory(store.lock_name) if services.lease_factory
                     else GcsLease(services.client, store.lock_name))
            if not lease.acquire():
                holder = lease.holder.describe() if lease.holder else "another controller"
                result.update(status="locked", detail=f"the target lease is held by {holder}")
                continue
            try:
                record = store.load()
                fresh_hosts = services.inventory.list_hosts()
                github = services.github_for(target, lease.ensure)
                runners = github.list_runners(target.target)
                plan = plan_target(target, fresh_hosts, runners, record, services.clock())
                result.update(action=plan.action, detail=plan.reason)
                if plan.idle:
                    result["status"] = "idle"
                    continue
                provider = services.provider_for(lease.ensure, deadline)
                keeper = FloorKeeper(config, target, store.record, lambda: store.save(lease), provider=provider,
                                     github=github, deadline=deadline, clock=services.clock)
                result.update(status="acted", detail=keeper.execute(plan, fresh_hosts))
            finally:
                lease.release()
        except BudgetExhausted as exc:
            result.update(status="skipped", detail=str(exc))
        except (FloorError, GcsError, LeaseLost, RuntimeError, ValueError, OSError,
                subprocess.SubprocessError, DeadlineExceeded) as exc:
            code = 1
            result.update(status="error", detail=f"{type(exc).__name__}: {exc}"[:600])
            LOG.error("Runner floor %s failed this tick: %s", target.name, exc)
    return code, results


def status_report(config: FloorConfig, services: Services) -> list[dict[str, Any]]:
    """Read-only: every target's plan, record, and hosts."""
    location = state_location(config.state_uri)
    hosts = services.inventory.list_hosts()
    report = []
    for target in config.targets:
        row: dict[str, Any] = {"target": target.name, "key": target.key}
        try:
            store = TargetStore(services.client, location, config.project, target)
            record = store.load()
            runners = services.github_for(target, None).list_runners(target.target)
            plan = plan_target(target, hosts, runners, record, services.clock())
            own = managed_hosts(target, hosts)
            names = {n for h in own for n in expected_runner_names(h.name)}
            prefix = f"{RUNNER_PREFIX}-{HOST_PREFIX}"
            row.update(plan=plan.describe(), hosts=[asdict(h) for h in own],
                       runners=[{"name": r.name, "status": r.status, "busy": r.busy, "labels": list(r.labels)}
                                for r in runners if r.name in names or r.name.startswith(prefix)],
                       record={k: v for k, v in record.items() if k != "history"},
                       history=(record.get("history") or [])[-5:])
            live = [h for h in own if h.running]
            row["degraded"] = bool(live) and not any(r.online for r in runners if r.name in names)
        except (FloorError, GcsError, RuntimeError, ValueError, OSError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        report.append(row)
    return report


# --------------------------------------------------------------------------------------------
# The operator autoscaler's per-target lease (opt-in: SPOT_RUNNER_FLOOR_STATE_URI in its environment)
# --------------------------------------------------------------------------------------------

def operation_deadline(operation: Mapping[str, Any]) -> float:
    """When a running keeper has certainly finished or abandoned ``operation``, plus ORPHAN_GRACE_SECONDS."""
    def at(key: str) -> float:
        try:
            return float(operation.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
    if operation.get("kind") == "restart":
        end = at("started_at") + RESTART_DEADLINE_SECONDS
    elif operation.get("phase") == "creating":
        end = at("started_at") + CREATE_CONFIRM_SECONDS
    else:
        end = (at("created_at") or at("started_at")) + LAUNCH_DEADLINE_SECONDS
    return end + ORPHAN_GRACE_SECONDS


def operation_orphaned(operation: Mapping[str, Any], now: float) -> bool:
    return now >= operation_deadline(operation)


def scrub_orphaned_token(project: str, operation: Mapping[str, Any],
                         run: Callable[..., Any] = subprocess.run) -> str:
    """Remove the one-use registration token from an orphaned launch's VM: only the recorded instance, only the
    startup-script metadata key. Deletes nothing, never raises (the token also expires an hour after its creation)."""
    name, zone, instance_id = (str(operation.get(k) or "") for k in ("name", "zone", "instance_id"))
    if operation.get("kind") != "launch" or not (name and zone and instance_id):
        return "nothing to scrub: no confirmed instance"
    try:
        found = run(["gcloud", "compute", "instances", "describe", name, f"--zone={zone}", f"--project={project}",
                     "--format=json(id,metadata.items[].key)"], check=False, capture_output=True, text=True,
                    timeout=60)
        if found.returncode != 0:
            return f"{name} is not readable or gone; nothing scrubbed"
        item = json.loads(found.stdout or "{}")
        if str(item.get("id") or "") != instance_id:
            return f"{name} is now another instance; left alone"
        keys = {row.get("key") for row in (item.get("metadata") or {}).get("items") or [] if isinstance(row, dict)}
        if "startup-script" not in keys:
            return f"{name} carries no startup-script"
        removed = run(["gcloud", "compute", "instances", "remove-metadata", name, f"--zone={zone}",
                       f"--project={project}", "--keys=startup-script", "--quiet"], check=False, capture_output=True,
                      text=True, timeout=120)
        return (f"removed the leftover registration token from {name}" if removed.returncode == 0
                else f"could not remove the token from {name}: {(removed.stderr or '').strip()[:200]}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return f"could not check {name}: {type(exc).__name__}"


def kill_process_group(pid: int, *, grace: float = 2.0, killpg: Callable[[int, int], None] = os.killpg,
                       sleep: Callable[[float], None] = time.sleep,
                       clock: Callable[[], float] = time.monotonic) -> bool:
    """SIGTERM a child's whole process group, wait up to ``grace``, then SIGKILL it. Never reaps (the thread that
    waits on the child does), so it is safe from any thread. True when the group is gone."""
    def send(sig: int) -> bool:
        try:
            killpg(pid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    if not send(signal.SIGTERM):
        return True
    deadline = clock() + grace
    while clock() < deadline:
        if not send(0):
            return True
        sleep(0.05)
    send(signal.SIGKILL)
    return not send(0)


def run_fenced(command: Sequence[str], *, timeout: float, holder: "AutoscalerTargetLease | None",
               popen: Callable[..., Any] = subprocess.Popen) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a long child that mutates providers, fenced by the floor lease: it runs in its own
    process group (with its gcloud grandchildren), and ``holder`` kills that whole group the moment the lease can
    no longer be proven held, instead of letting it act until its own deadline."""
    process = popen(list(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    if holder is not None:
        holder.fence(process)
    try:
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_group(process.pid)
            try:
                out, err = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            raise subprocess.TimeoutExpired(list(command), timeout, output=out, stderr=err) from None
    finally:
        if holder is not None:
            holder.unfence(process)
    return subprocess.CompletedProcess(list(command), process.returncode, out, err)


# --------------------------------------------------------------------------------------------
# Process-wide mutation fence (an audit hook), active while an autoscaler holds its target's floor lease
# --------------------------------------------------------------------------------------------

# Read-only gcloud verbs; every other gcloud command (and ssh, scp, the deployment manager, and any gh call that
# isn't a plain GET) counts as a mutation. Unknown commands are mutations: the fence denies by default.
GCLOUD_READ_VERBS = frozenset({"list", "describe", "get-serial-port-output", "print-access-token",
                               "print-identity-token", "get-iam-policy", "get-guest-attributes"})
_GH_BODY_FLAGS = ("--input", "-f", "-F", "--field", "--raw-field")
_MUTATION_GUARD: Callable[[], None] | None = None
_FENCE_HOOK_INSTALLED = False


def is_mutating_command(argv: Sequence[Any] | str) -> bool:
    """Whether a child process may change a provider: VMs, disks, metadata, labels, or runner registrations."""
    import shlex

    words = shlex.split(argv) if isinstance(argv, str) else [str(a) for a in argv or ()]
    if not words:
        return False
    if any(w.endswith("spot_runner_manager.py") for w in words):
        return True
    base = os.path.basename(words[0])
    if base in ("ssh", "scp"):
        return True
    positional = [w for w in words[1:] if not w.startswith("-")]
    if base == "gcloud":
        if any(w in ("ssh", "scp") for w in positional[:3]):
            return True
        return not any(w in GCLOUD_READ_VERBS for w in positional[:5])
    if base == "gh":
        if positional[:2] in (["auth", "token"], ["auth", "status"]):
            return False
        if positional[:1] != ["api"]:
            return True
        method = "GET"
        for index, word in enumerate(words):
            if word in ("-X", "--method") and index + 1 < len(words):
                method = words[index + 1]
            elif word.startswith("--method="):
                method = word.split("=", 1)[1]
            elif word.startswith("-X") and len(word) > 2:
                method = word[2:]
            elif word in _GH_BODY_FLAGS or word.startswith(("--field=", "--raw-field=", "--input=")):
                return True  # gh api sends a body with POST unless told otherwise
        return method.upper() != "GET"
    return False


def _fence_hook(event: str, args: tuple) -> None:
    guard = _MUTATION_GUARD
    if event == "subprocess.Popen" and guard is not None and is_mutating_command(args[1]):
        guard()  # raises before the child exists


def install_mutation_fence(guard: Callable[[], None] | None) -> None:
    """While ``guard`` is set, every mutating child process this interpreter starts, from any module or thread,
    first proves the floor lease (``guard`` raises when it can't). ``None`` turns the fence off."""
    global _MUTATION_GUARD, _FENCE_HOOK_INSTALLED
    if not _FENCE_HOOK_INSTALLED:
        sys.addaudithook(_fence_hook)  # audit hooks can't be removed; the guard variable switches it
        _FENCE_HOOK_INSTALLED = True
    _MUTATION_GUARD = guard


class AutoscalerTargetLease:
    """Holds the floor lease of the autoscaler's target for the controller's lifetime.

    A heartbeat thread renews it; ``guard`` (hooked into the autoscaler's fleet mutation guard) raises once it can
    no longer be proven held, so the autoscaler and the floor keeper never act on one target at once. While the
    autoscaler holds it, the keeper skips the target.

    Children that act on providers for a long time (the deployment manager) are fenced: on the first failed proof,
    from the heartbeat or any guard call, their process groups are killed at once. The lease keeps at least 660 s
    left while it is proven (``GcsLease.ensure``), and nobody can break it until 60 s after it expires, so a fenced
    child is gone long before another controller may act. The heartbeat checks every few seconds, so a workstation
    waking from sleep notices a lease taken over within that time.
    """

    HEARTBEAT_SECONDS = 5.0

    def __init__(self, lease: GcsLease, *, location: StateLocation | None = None, project: str = "",
                 target: str = "") -> None:
        self.lease = lease
        self.location, self.project, self.target = location, project, target
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost: BaseException | None = None
        self._children: set[Any] = set()
        self.orphaned: dict | None = None

    @classmethod
    def for_target(cls, project: str, target: str, uri: str, *, client: GcsClient | None = None) -> \
            "AutoscalerTargetLease":
        location = state_location(uri)
        client = client or GcsClient(location.bucket)
        owner = f"autoscaler:{socket.gethostname()}:{os.getpid()}"
        name = resolve_target(target).name
        return cls(target_lease(client, location, project, name, owner=owner), location=location, project=project,
                   target=name)

    def acquire(self) -> bool:
        with self._lock:
            self.lost = None
            return self.lease.acquire()

    def holder(self) -> str:
        return self.lease.holder.describe() if self.lease.holder else "another controller"

    def guard(self) -> None:
        with self._lock:
            if self.lost is None:
                try:
                    self.lease.ensure()
                    return
                except LeaseLost as exc:
                    self.lost = exc
            lost, children = self.lost, list(self._children)
            self._children.clear()
        for child in children:
            LOG.error("Runner floor lease lost (%s); killing the process group of fenced child %s", lost, child.pid)
            kill_process_group(child.pid)
        raise RuntimeError(f"Fleet mutation held: the runner floor lease was lost ({lost})")

    def fence(self, process: Any) -> None:
        """Kill ``process``'s group the moment the lease is lost (at once when it already is)."""
        with self._lock:
            if self.lost is None:
                self._children.add(process)
                return
        kill_process_group(process.pid)

    def unfence(self, process: Any) -> None:
        with self._lock:
            self._children.discard(process)

    def _beat(self) -> None:
        while not self._stop.wait(self.HEARTBEAT_SECONDS):
            try:
                self.guard()
            except RuntimeError as exc:
                LOG.error("%s", exc)
                return

    def pending_operation(self, now: float | None = None) -> dict | None:
        """The floor keeper's launch or start in progress on this target, if any. An orphaned one (see
        ``operation_orphaned``) doesn't count: it is kept in ``orphaned`` and the autoscaler goes ahead."""
        record = TargetStore(self.lease.client, self.location, self.project,
                             FloorTarget(self.target, secret="-")).load()
        operation = record.get("operation")
        if not isinstance(operation, dict) or not operation:
            return None
        if operation_orphaned(operation, time.time() if now is None else now):
            self.orphaned = operation
            LOG.warning("Runner floor %s of %s on %s passed its deadline with nobody managing it (its target left "
                        "the floor, the floor is off, or the watchdog is paused); the autoscaler manages that host as "
                        "a normal managed host", operation.get("kind"), operation.get("name"), self.target)
            return None
        return operation

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._beat, name="runner-floor-lease", daemon=True)
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
        with self._lock:
            self.lease.release()


# --------------------------------------------------------------------------------------------
# Command-line interface
# --------------------------------------------------------------------------------------------

def args_parser() -> argparse.ArgumentParser:
    from .control_vm import DEFAULT_PROJECT

    parser = argparse.ArgumentParser(
        description="Keep at least one minimal Spot runner host per target in SPOT_RUNNER_FLOOR_TARGETS. Only "
                    "`tick --apply` and `replace-unmanaged --apply` change resources.")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Print each target's floor plan, record, and hosts (read-only)")
    status.add_argument("--github-user", default="", help="gh account for every target (workstation runs)")
    status.add_argument("--offers", action="store_true",
                        help="Also price the cheapest floor host offers for each target (reads the billing catalog)")
    tick = commands.add_parser("tick", help="Plan one floor step per target; with --apply, run it")
    tick.add_argument("--apply", action="store_true", help="Take each acting target's lease and run its step")
    tick.add_argument("--target", action="append", default=[], help="Only this target (repeatable)")
    tick.add_argument("--budget-seconds", type=float, default=None,
                      help="Time budget of this tick (the watchdog passes its deadline in SPOT_TICK_DEADLINE_EPOCH)")
    tick.add_argument("--github-user", default="", help="gh account for every target (workstation runs)")
    from .runner_floor_replace import add_parser as add_replace_parser
    add_replace_parser(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from .environment import load_env
    from .log_format import configure_logging

    configure_logging()
    args = args_parser().parse_args(argv)
    if not os.environ.get("K_SERVICE"):  # a workstation: read the repository .env like the other entry points
        load_env(Path(__file__).resolve().parents[2] / ".env")
    config = FloorConfig.from_env(args.project)
    if not config.targets:
        print(f"No floor targets: {ENV_TARGETS} is empty.")
        return 0
    if not config.state_uri:
        raise FloorError(f"{ENV_STATE_URI} is not set; the floor keeps its state and lease in Cloud Storage only")
    if args.command == "replace-unmanaged":
        from .runner_floor_replace import run_replace
        return run_replace(args, config)
    if args.command == "tick":
        deadline = Deadline.from_env(budget_seconds=args.budget_seconds)
        if deadline.remaining() < TARGET_MIN_SECONDS:
            # The control VM step used the tick: nothing starts, and that is not a failure.
            print(json.dumps({"floor": "skipped: the tick's time budget is used up"}))
            return 0
    tokens = TokenSource(config.token_source, account_override=getattr(args, "github_user", "") or "")
    services = default_services(config, tokens=tokens)
    if args.command == "status":
        report = status_report(config, services)
        if args.offers:
            provider = services.provider_for(lambda: None, Deadline())
            for row, target in zip(report, config.targets):
                offers = [o for o in provider.offers(config, target) if o.total_daily_usd < config.max_daily_usd]
                row["offers"] = [o.to_dict() for o in offers[:5]]
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    try:
        with hard_deadline(deadline.remaining()):
            code, results = run_tick(config, services, apply=args.apply, deadline=deadline, only=args.target)
    except DeadlineExceeded as exc:
        LOG.error("Runner floor tick stopped: %s", exc)
        print(json.dumps({"floor": "deadline"}))
        return 1
    for row in results:
        LOG.info("Runner floor %s: %s (%s)", row["target"], row.get("status"), row.get("detail", ""))
    summary = {"floor": [{k: row.get(k) for k in ("target", "status", "action")} for row in results]}
    print(json.dumps(summary, sort_keys=True))
    return code
