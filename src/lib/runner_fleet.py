#!/usr/bin/env python3
"""Read GitHub/GCE runner-fleet state and plan recovery capacity.

This module uses the authenticated ``gh`` and ``gcloud`` CLIs, has no third
party Python dependencies, and never creates or deletes cloud resources by
itself. Call :func:`reconcile_minimum_runners` with an explicit deployment
callback when automatic scale-out is desired.
"""

from __future__ import annotations

import argparse
import json
import math
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .log_format import error_detail
from .target_config import TARGET_LABEL_KEY

LOG = logging.getLogger("spot_runner_autoscaler")

# Name prefixes of the runner hosts this project provisions, current and earlier fleets. A host created before
# the ``spot-target`` label and patch recording existed is recognizable only by its name.
RUNNER_HOST_NAME_PREFIXES = ("oxidex-runners-", "newznab-spot-runners-", "spot-runners-", "oxidex-spot-runners-")

# Which spot-runner-patch-v1 patch a runner host runs, recorded by runner_manager as instance metadata at
# provisioning and after a confirmed live patch. ``spot-runner-patch`` holds the canonical manifest JSON (omitted
# when larger than MAX_PATCH_METADATA_BYTES), ``spot-runner-patch-sha256`` its sha256 or ``none`` (no patch), and
# ``spot-runner-patch-target`` the owner/repo the patch belongs to. The ``spot-runner-patch`` label carries a sha
# prefix for filtering. Hosts provisioned before this existed have none of these keys.
#
# Those applied keys and the label are written only after the host confirmed the patch: a completed bootstrap
# (runner_manager.main) or LIVE_PATCH_APPLIED. While a startup script is still applying a patch, or after it
# failed, the requested patch sits under the PENDING keys instead, and consumers must treat the host's patch as
# unknown (the applied keys, if any, describe an earlier state and may be stale).
PATCH_METADATA_KEY = "spot-runner-patch"
PATCH_SHA_METADATA_KEY = "spot-runner-patch-sha256"
PATCH_PENDING_METADATA_KEY = "spot-runner-patch-pending"
PATCH_PENDING_SHA_METADATA_KEY = "spot-runner-patch-pending-sha256"
# The GCE ``lastStartTimestamp`` of the boot in which the applied patch was confirmed. A reboot can bring the
# runners back without a live patch's changes, so an applied record whose boot differs from the instance's current
# lastStartTimestamp (or that has no boot) is stale.
PATCH_BOOT_METADATA_KEY = "spot-runner-patch-boot"
PATCH_TARGET_METADATA_KEY = "spot-runner-patch-target"
PATCH_LABEL_KEY = "spot-runner-patch"
# Deployment details apply_live_patch needs to hot-patch a host found by inventory (not secrets).
RUNNER_PREFIX_METADATA_KEY = "spot-runner-prefix"
RUNNER_COUNT_METADATA_KEY = "spot-runner-count"
RUNNER_LABEL_METADATA_KEY = "spot-runner-label"
RUNNER_REGION_METADATA_KEY = "spot-runner-region"
RUNNER_HEAVY_POOL_METADATA_KEY = "spot-runner-heavy-pool"
# The GitHub scope the runners registered with (an org such as swack-tools, or owner/repo) and, for org scopes, the
# runner group; these can differ from spot-runner-patch-target (the patch's repository).
RUNNER_REGISTRATION_TARGET_METADATA_KEY = "spot-runner-registration-target"
RUNNER_GROUP_METADATA_KEY = "spot-runner-group-id"
PATCH_LABEL_SHA_CHARS = 16
NO_PATCH = "none"
# GCE allows 256 KiB per metadata value and 512 KiB in total, and the startup script (with the patch embedded)
# shares that budget; a larger manifest is recorded by sha only.
MAX_PATCH_METADATA_BYTES = 64 * 1024


def gce_metadata_items(item: dict) -> dict[str, str]:
    """``{key: value}`` from a GCE instance's ``metadata.items`` (missing or malformed entries are skipped)."""
    metadata = item.get("metadata") if isinstance(item, dict) else None
    entries = metadata.get("items") if isinstance(metadata, dict) else None
    out: dict[str, str] = {}
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("key"), str) and isinstance(entry.get("value"), str):
            out[entry["key"]] = entry["value"]
    return out


def _pending_patch_sha(metadata: dict[str, str]) -> str:
    """The pending (requested, unconfirmed) patch sha; ``invalid`` when a pending key is present but malformed."""
    if PATCH_PENDING_SHA_METADATA_KEY not in metadata and PATCH_PENDING_METADATA_KEY not in metadata:
        return ""
    sha = metadata.get(PATCH_PENDING_SHA_METADATA_KEY, "").strip().lower()
    if sha == NO_PATCH or (len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
        return sha
    return "invalid"  # Still pending: a damaged pending record never reads as confirmed.


def gce_timestamp(value: Any) -> str:
    """A GCE RFC 3339 timestamp such as ``lastStartTimestamp``, or an empty string when absent or malformed."""
    text = str(value or "").strip()
    return text if 0 < len(text) <= 40 and all(c.isdigit() or c in "TZ:.+-" for c in text) else ""


def patch_record_from_gce_item(item: dict) -> dict[str, str]:
    """The recorded runner patch of a GCE instance resource: ``patch_sha256``, ``patch_manifest_json``,
    ``patch_target`` (empty strings when not recorded), and ``patch_pending_sha256`` (non-empty while a
    requested patch is unconfirmed). The values are not trusted further than their shape; consumers verify the
    manifest against its sha before using it."""
    metadata = gce_metadata_items(item)
    pending = _pending_patch_sha(metadata)
    sha = metadata.get(PATCH_SHA_METADATA_KEY, "").strip().lower()
    if sha != NO_PATCH and not (len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
        sha = ""
    manifest = metadata.get(PATCH_METADATA_KEY, "") if sha and sha != NO_PATCH else ""
    if len(manifest.encode("utf-8")) > MAX_PATCH_METADATA_BYTES:
        manifest = ""
    target = metadata.get(PATCH_TARGET_METADATA_KEY, "").strip() if sha or pending else ""
    boot = gce_timestamp(metadata.get(PATCH_BOOT_METADATA_KEY)) if sha else ""
    return {"patch_sha256": sha, "patch_manifest_json": manifest, "patch_target": target,
            "patch_pending_sha256": pending, "patch_boot": boot}


def runner_deployment_from_gce_item(item: dict) -> dict[str, Any]:
    """Recorded ``runner_prefix`` and ``runners_count`` of a runner host (absent keys are omitted)."""
    metadata = gce_metadata_items(item)
    out: dict[str, Any] = {}
    prefix = metadata.get(RUNNER_PREFIX_METADATA_KEY, "").strip()
    if prefix and all(c.isalnum() or c in "-_" for c in prefix):
        out["runner_prefix"] = prefix
    count = metadata.get(RUNNER_COUNT_METADATA_KEY, "").strip()
    if count.isdigit() and 0 < int(count) <= 64:
        out["runners_count"] = int(count)
    label = metadata.get(RUNNER_LABEL_METADATA_KEY, "").strip()
    if label and len(label) <= 64 and all(c.isalnum() or c in "._-" for c in label):
        out["runner_label"] = label
    region = metadata.get(RUNNER_REGION_METADATA_KEY, "").strip()
    if region and len(region) <= 40 and all(c.islower() or c.isdigit() or c == "-" for c in region):
        out["runner_region"] = region
    heavy = metadata.get(RUNNER_HEAVY_POOL_METADATA_KEY, "").strip()
    if heavy in ("2", "3"):
        out["heavy_runner_pool"] = int(heavy)
    registration = metadata.get(RUNNER_REGISTRATION_TARGET_METADATA_KEY, "").strip()
    parts = registration.split("/")
    if registration and len(parts) <= 2 and all(p and all(c.isalnum() or c in "._-" for c in p) for p in parts):
        out["registration_target"] = registration
    group = metadata.get(RUNNER_GROUP_METADATA_KEY, "").strip()
    if group.isdigit() and int(group) > 0:
        out["runner_group_id"] = int(group)
    return out


@dataclass(frozen=True)
class GitHubRunner:
    runner_id: int
    name: str
    status: str
    busy: bool
    labels: tuple[str, ...]

    @property
    def online(self) -> bool:
        return self.status.lower() == "online"


@dataclass(frozen=True)
class GceVm:
    name: str
    zone: str
    status: str
    machine_type: str
    spot: bool
    vcpu: int | None = None
    ram_gib: float | None = None
    disks: tuple[dict, ...] = ()
    accelerators: tuple[dict, ...] = ()
    instance_id: str = ""
    specs_last_known: bool = False
    cpu_platform: str = ""
    requested_cpu_platform: str = ""
    # Value of the GCE ``spot-target`` label; empty for hosts launched before target labels.
    target_label: str = ""
    # Recorded runner patch (see PATCH_*_METADATA_KEY); empty for hosts provisioned before patch recording.
    patch_sha256: str = ""
    patch_manifest_json: str = ""
    patch_target: str = ""
    # Requested at provisioning but not yet confirmed by a completed bootstrap; the patch is unknown while set.
    patch_pending_sha256: str = ""
    # The boot the applied record was confirmed in, and the instance's current GCE lastStartTimestamp.
    patch_boot: str = ""
    last_start_timestamp: str = ""

    @property
    def running(self) -> bool:
        return self.status.upper() == "RUNNING"


@dataclass(frozen=True)
class FleetSnapshot:
    runners: tuple[GitHubRunner, ...]
    vms: tuple[GceVm, ...]

    @property
    def online_runners(self) -> tuple[GitHubRunner, ...]:
        return tuple(runner for runner in self.runners if runner.online)

    @property
    def running_vms(self) -> tuple[GceVm, ...]:
        return tuple(vm for vm in self.vms if vm.running)

    @property
    def running_spot_vms(self) -> tuple[GceVm, ...]:
        return tuple(vm for vm in self.running_vms if vm.spot)

    @property
    def stopped_spot_vms(self) -> tuple[GceVm, ...]:
        return tuple(vm for vm in self.vms if vm.spot and not vm.running)


@dataclass(frozen=True)
class ScaleOutPlan:
    online_runners: int
    minimum_online_runners: int
    runners_per_vm: int
    additional_vms: int
    planned_runner_capacity: int

    @property
    def needed(self) -> bool:
        return self.additional_vms > 0


def _command_json(args: Sequence[str]) -> object:
    try:
        strict={'env':dict(os.environ,CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR='false')} if len(args)>1 and args[1]=='compute' else {}
        result = subprocess.run(args, check=True, text=True, capture_output=True,timeout=60,**strict)
        if strict and result.stderr.strip():
            raise RuntimeError('Provider inventory returned diagnostics; completeness unknown')
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout).strip()
        raise RuntimeError(f"Command failed: {args[0]}: {error_detail(exc)}") from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Command returned invalid JSON: {' '.join(args)}") from exc


def count_active_github_runners(
    org: str | Any = "swack-tools",
    *,
    target: str | Any | None = None,
    label: str | None = None,
    gh_binary: str = "gh",
) -> int:
    """Count online organization or repository self-hosted runners, optionally by label."""
    return sum(
        runner.online
        for runner in list_github_runners(org, target=target, gh_binary=gh_binary)
        if label is None or label in runner.labels
    )


def list_github_runners(
    org: str | Any = "swack-tools",
    *,
    target: str | Any | None = None,
    gh_binary: str = "gh",
) -> tuple[GitHubRunner, ...]:
    """List all organization or repository self-hosted runners using the authenticated gh command-line tool."""
    target_val = target if target is not None else org
    if hasattr(target_val, "runners_url"):
        endpoint = f"{target_val.runners_url}?per_page=100"
        target_name = getattr(target_val, "name", str(target_val))
    elif hasattr(target_val, "api_path"):
        endpoint = f"{target_val.api_path}/actions/runners?per_page=100"
        target_name = getattr(target_val, "name", str(target_val))
    else:
        target_name = str(target_val).strip()
        if target_name.startswith(("repos/", "orgs/")):
            if target_name.endswith("/actions/runners"):
                endpoint = f"{target_name}?per_page=100"
            else:
                endpoint = f"{target_name}/actions/runners?per_page=100"
        elif "/" in target_name:
            endpoint = f"repos/{target_name}/actions/runners?per_page=100"
        else:
            endpoint = f"orgs/{target_name}/actions/runners?per_page=100"

    command = [gh_binary, "api", "--paginate", "--slurp", endpoint]
    try:
        result = subprocess.run(command, check=True, text=True, capture_output=True, timeout=60)
        if result.stderr.strip(): raise ValueError("GitHub inventory diagnostic")
        pages = json.loads(result.stdout)
        if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
            raise ValueError("Missing runner pages")
        total = pages[0].get('total_count')
        if type(total) is not int or total < 0: raise ValueError("Invalid runner total")
        items = []
        for page in pages:
            if (not isinstance(page, dict) or page.get('total_count') != total
                or not isinstance(page.get('runners'), list)):
                raise ValueError("Incomplete runner page")
            items.extend(page['runners'])
        if (len(items) != total or len(pages) != max(1, (total + 99) // 100)
            or any(len(page['runners']) != min(100, max(0, total - index * 100))
                   for index, page in enumerate(pages))):
            raise ValueError("Partial runner inventory")
        runners = []; seen = set()
        for item in items:
            if (not isinstance(item, dict) or type(item.get('id')) is not int
                or item['id'] in seen or type(item.get('name')) is not str
                or item.get('status') not in ('online', 'offline')
                or type(item.get('busy')) is not bool or not isinstance(item.get('labels'), list)
                or any(not isinstance(label, dict) or type(label.get('name')) is not str
                       for label in item['labels'])):
                raise ValueError("Malformed or duplicate runner")
            seen.add(item['id'])
            runners.append(GitHubRunner(item['id'], item['name'], item['status'], item['busy'],
                                        tuple(label['name'] for label in item['labels'])))
        return tuple(runners)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"Complete GitHub runner inventory unavailable for {target_name}") from exc



def list_gcloud_vms(
    project: str = "homelab-424523",
    *,
    name_prefix: str | None = None,
    gcloud_binary: str = "gcloud",
) -> tuple[GceVm, ...]:
    """List Compute Engine VMs and identify Spot/legacy preemptible instances."""
    rows = _command_json(
        [gcloud_binary, "compute", "instances", "list", "--project", project, "--format=json"]
    )
    if not isinstance(rows, list):
        raise RuntimeError("gcloud returned an unexpected instance list")

    try:
        disk_rows = _command_json([gcloud_binary, "compute", "disks", "list", f"--project={project}", "--format=json(selfLink,id,type,sizeGb,provisionedIops,provisionedThroughput)"])
        disk_inventory = {disk["selfLink"]: disk for disk in disk_rows}
    except (RuntimeError, KeyError, TypeError) as exc:
        disk_inventory = {}
        LOG.warning("Disk type and identity inventory unavailable: %s", error_detail(exc))
    vms: list[GceVm] = []
    shapes: dict[tuple[str, str], dict] = {}
    for item in rows:
        name = str(item.get("name", ""))
        if name_prefix and not name.startswith(name_prefix):
            continue
        zone = str(item.get("zone", "")).rsplit("/", 1)[-1]
        machine_type = str(item.get("machineType", "")).rsplit("/", 1)[-1]
        key = (zone, machine_type)
        if key not in shapes:
            try:
                shapes[key] = _command_json([
                    gcloud_binary, "compute", "machine-types", "describe", machine_type,
                    f"--zone={zone}", f"--project={project}", "--format=json(guestCpus,memoryMb)",
                ])
            except RuntimeError as exc:
                shapes[key] = {}
                LOG.warning("Machine specifications unavailable for %s in %s: %s",
                            machine_type, zone, error_detail(exc))
        shape = shapes[key]
        scheduling = item.get("scheduling", {})
        spot = (
            str(scheduling.get("provisioningModel", "")).upper() == "SPOT"
            or bool(scheduling.get("preemptible", False))
        )
        vms.append(
            GceVm(
                name=name,
                zone=str(item.get("zone", "")).rsplit("/", 1)[-1],
                status=str(item.get("status", "UNKNOWN")),
                machine_type=str(item.get("machineType", "")).rsplit("/", 1)[-1],
                spot=spot,
                vcpu=int(shape["guestCpus"]) if shape.get("guestCpus") is not None else None,
                ram_gib=float(shape["memoryMb"]) / 1024 if shape.get("memoryMb") is not None else None,
                disks=tuple({**disk,
                             **({"diskId": disk_inventory[disk["source"]].get("id"),
                                 "diskType": disk_inventory[disk["source"]].get("type", "").rsplit("/", 1)[-1],
                                 **({'diskSizeGb': disk_inventory[disk['source']]['sizeGb']} if 'sizeGb' in disk_inventory[disk['source']] else {}),
                                 **{key: disk_inventory[disk['source']][key] for key in ('provisionedIops', 'provisionedThroughput') if key in disk_inventory[disk['source']]}}
                                if disk.get("source") in disk_inventory else {})}
                            for disk in item.get("disks", [])),
                accelerators=tuple(item.get("guestAccelerators", [])),
                instance_id=str(item.get("id", "")),
                cpu_platform=str(item.get("cpuPlatform", "")),
                requested_cpu_platform=str(item.get("minCpuPlatform", "")),
                target_label=str((item.get("labels") or {}).get(TARGET_LABEL_KEY, "")
                                 if isinstance(item.get("labels"), dict) else ""),
                **patch_record_from_gce_item(item),
                last_start_timestamp=gce_timestamp(item.get("lastStartTimestamp")),
            )
        )
    return tuple(vms)


def count_running_gcloud_vms(
    project: str = "homelab-424523",
    *,
    name_prefix: str | None = None,
    gcloud_binary: str = "gcloud",
) -> int:
    """Count running VMs in a GCP project, optionally limited by name prefix."""
    return sum(vm.running for vm in list_gcloud_vms(project, name_prefix=name_prefix, gcloud_binary=gcloud_binary))


def observe_fleet(
    *,
    org: str | Any = "swack-tools",
    target: str | Any | None = None,
    project: str = "homelab-424523",
    label: str | None = None,
    vm_name_prefix: str | None = None,
    gh_binary: str = "gh",
    gcloud_binary: str = "gcloud",
) -> FleetSnapshot:
    """Fetch GitHub and GCE state concurrently and return a single snapshot."""
    target_val = target if target is not None else org
    with ThreadPoolExecutor(max_workers=2) as pool:
        runners_future = pool.submit(list_github_runners, target_val, gh_binary=gh_binary)
        vms_future = pool.submit(list_gcloud_vms, project, name_prefix=vm_name_prefix, gcloud_binary=gcloud_binary)
        runners = runners_future.result()
        vms = vms_future.result()
    if label is not None:
        runners = tuple(runner for runner in runners if label in runner.labels)
    return FleetSnapshot(runners=runners, vms=vms)


def plan_scale_out(
    snapshot: FleetSnapshot,
    *,
    minimum_online_runners: int = 20,
    runners_per_vm: int = 8,
) -> ScaleOutPlan:
    """Calculate whole-host scale-out needed to restore the online-runner floor."""
    if minimum_online_runners < 1:
        raise ValueError("minimum_online_runners must be positive")
    if runners_per_vm < 1:
        raise ValueError("runners_per_vm must be positive")
    current = len(snapshot.online_runners)
    deficit = max(0, minimum_online_runners - current)
    hosts = math.ceil(deficit / runners_per_vm)
    return ScaleOutPlan(
        online_runners=current,
        minimum_online_runners=minimum_online_runners,
        runners_per_vm=runners_per_vm,
        additional_vms=hosts,
        planned_runner_capacity=hosts * runners_per_vm,
    )


def reconcile_minimum_runners(
    launch_hosts: Callable[[int], None],
    *,
    org: str | Any = "swack-tools",
    target: str | Any | None = None,
    project: str = "homelab-424523",
    label: str | None = None,
    vm_name_prefix: str | None = None,
    minimum_online_runners: int = 20,
    runners_per_vm: int = 8,
    poll_interval_seconds: int = 20,
    startup_timeout_seconds: int = 1200,
    gh_binary: str = "gh",
    gcloud_binary: str = "gcloud",
) -> FleetSnapshot:
    """Launch the planned number of hosts once, then wait for the runner floor.

    ``launch_hosts(count)`` is supplied by the caller (for example, a wrapper
    around ``spot_runner_manager.py``). This function never launches resources
    unless that callback is explicitly provided and called. Within this call,
    it launches one batch and then waits; callers should serialize concurrent
    invocations to avoid overlapping batches.
    """
    snapshot = observe_fleet(
        org=org,
        target=target,
        project=project,
        label=label,
        vm_name_prefix=vm_name_prefix,
        gh_binary=gh_binary,
        gcloud_binary=gcloud_binary,
    )
    plan = plan_scale_out(
        snapshot,
        minimum_online_runners=minimum_online_runners,
        runners_per_vm=runners_per_vm,
    )
    if not plan.needed:
        return snapshot

    if poll_interval_seconds < 1:
        raise ValueError("poll_interval_seconds must be positive")
    if startup_timeout_seconds < 1:
        raise ValueError("startup_timeout_seconds must be positive")

    launch_hosts(plan.additional_vms)
    deadline = time.monotonic() + startup_timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_interval_seconds)
        snapshot = observe_fleet(
            org=org,
            target=target,
            project=project,
            label=label,
            vm_name_prefix=vm_name_prefix,
            gh_binary=gh_binary,
            gcloud_binary=gcloud_binary,
        )
        if len(snapshot.online_runners) >= minimum_online_runners:
            return snapshot
    raise TimeoutError(
        f"Runner floor not restored: {len(snapshot.online_runners)}/{minimum_online_runners} online "
        f"after launching {plan.additional_vms} VM(s)"
    )


def _print_status(snapshot: FleetSnapshot, *, floor: int, runners_per_vm: int) -> None:
    plan = plan_scale_out(snapshot, minimum_online_runners=floor, runners_per_vm=runners_per_vm)
    busy = sum(runner.online and runner.busy for runner in snapshot.runners)
    print(f"GitHub self-hosted runners: {len(snapshot.online_runners)} online / {len(snapshot.runners)} registered ({busy} busy)")
    print(f"GCE VMs: {len(snapshot.running_vms)} running / {len(snapshot.vms)} listed")
    print(f"GCE Spot VMs: {len(snapshot.running_spot_vms)} running / {len(snapshot.stopped_spot_vms)} stopped or non-running")
    if plan.needed:
        print(f"Scale-out plan: launch {plan.additional_vms} VM(s) for up to {plan.planned_runner_capacity} runners to restore the {floor}+ online floor")
    else:
        print(f"Scale-out plan: none; online runner floor of {floor} is met")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="swack-tools")
    parser.add_argument("--target", help="Optional target repository (owner/repo) or organization")
    parser.add_argument("--project", default="homelab-424523")
    parser.add_argument("--label", help="Optional runner label to count only a subset")
    parser.add_argument("--vm-name-prefix", help="Optional GCE VM name prefix filter")
    parser.add_argument("--minimum-runners", type=int, default=1)
    parser.add_argument("--runners-per-vm", type=int, default=8)
    parser.add_argument("--gh-binary", default="gh")
    parser.add_argument("--gcloud-binary", default="gcloud")
    args = parser.parse_args(argv)
    try:
        target = args.target or args.org
        snapshot = observe_fleet(
            target=target,
            project=args.project,
            label=args.label,
            vm_name_prefix=args.vm_name_prefix,
            gh_binary=args.gh_binary,
            gcloud_binary=args.gcloud_binary,
        )
        _print_status(snapshot, floor=args.minimum_runners, runners_per_vm=args.runners_per_vm)
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
