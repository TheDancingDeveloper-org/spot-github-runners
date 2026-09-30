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
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Sequence

from .log_format import error_detail

LOG = logging.getLogger("spot_runner_autoscaler")


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
        result = subprocess.run(args, check=True, text=True, capture_output=True)
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
    org: str = "swack-tools",
    *,
    label: str | None = None,
    gh_binary: str = "gh",
) -> int:
    """Count online organization self-hosted runners, optionally by label."""
    return sum(
        runner.online
        for runner in list_github_runners(org, gh_binary=gh_binary)
        if label is None or label in runner.labels
    )


def list_github_runners(org: str = "swack-tools", *, gh_binary: str = "gh") -> tuple[GitHubRunner, ...]:
    """List all organization self-hosted runners using the authenticated gh command-line tool."""
    command = [
        gh_binary,
        "api",
        "--paginate",
        f"orgs/{org}/actions/runners",
        "--jq",
        ".runners[] | @json",
    ]
    try:
        result = subprocess.run(command, check=True, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {gh_binary}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout).strip()
        raise RuntimeError(f"Unable to list GitHub runners for {org}: {detail}") from exc

    runners: list[GitHubRunner] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        runners.append(
            GitHubRunner(
                runner_id=int(item["id"]),
                name=str(item["name"]),
                status=str(item.get("status", "unknown")),
                busy=bool(item.get("busy", False)),
                labels=tuple(str(label["name"]) for label in item.get("labels", [])),
            )
        )
    return tuple(runners)


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
        disk_rows = _command_json([gcloud_binary, "compute", "disks", "list", f"--project={project}", "--format=json(selfLink,id,type)"])
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
                                 "diskType": disk_inventory[disk["source"]].get("type", "").rsplit("/", 1)[-1]}
                                if disk.get("source") in disk_inventory else {})}
                            for disk in item.get("disks", [])),
                accelerators=tuple(item.get("guestAccelerators", [])),
                instance_id=str(item.get("id", "")),
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
    org: str = "swack-tools",
    project: str = "homelab-424523",
    label: str | None = None,
    vm_name_prefix: str | None = None,
    gh_binary: str = "gh",
    gcloud_binary: str = "gcloud",
) -> FleetSnapshot:
    """Fetch GitHub and GCE state concurrently and return a single snapshot."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        runners_future = pool.submit(list_github_runners, org, gh_binary=gh_binary)
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
    org: str = "swack-tools",
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
    parser.add_argument("--project", default="homelab-424523")
    parser.add_argument("--label", help="Optional runner label to count only a subset")
    parser.add_argument("--vm-name-prefix", help="Optional GCE VM name prefix filter")
    parser.add_argument("--minimum-runners", type=int, default=20)
    parser.add_argument("--runners-per-vm", type=int, default=8)
    parser.add_argument("--gh-binary", default="gh")
    parser.add_argument("--gcloud-binary", default="gcloud")
    args = parser.parse_args(argv)
    try:
        snapshot = observe_fleet(
            org=args.org,
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
