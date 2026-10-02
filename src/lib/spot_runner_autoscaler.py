"""Maintain an online floor of cost-capped GitHub Actions runners."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace

from .autoscaling_policy import affordable_offers
from .machine_profiles import MINIMUM_RAM_GIB, machine_profile, CpuOffer, platform_preferences, cpu_platform_satisfies
from .compute_api import get_auth
from .environment import load_env
from .job_recovery import JobRecovery
from .fleet_logging import lifecycle_log, observe_vms, reconcile_deletion_identities, vm_details
from .log_format import configure_logging, error_detail
from .vm_recovery import reconcile_lost_vms
from .price_guard import check_running_prices, delete_overpriced_vm
from .project_costs import log_project_costs
from .runner_fleet import GceVm, GitHubRunner, list_gcloud_vms, list_github_runners
from .spot_finder import find
from .storage_selection import storage_rates, cheapest_storage
from .billing_catalog import catalog_skus, clear_catalog_cache, monthly_to_daily
from .launch_candidates import LaunchInventory, candidate_key, launchable_candidates
from .spot_pricing import parse_component_rates

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger("spot_runner_autoscaler")
# Healthy hosts register all runners in 5-8 minutes; slower launches are treated as broken.
LAUNCH_DEADLINE_SECONDS = 600
# One check stops starting new launch attempts after this long so other fleet work still runs.
LAUNCH_SEARCH_BUDGET_SECONDS = 600
SPEND_LOG_INTERVAL_SECONDS = 3600


def now() -> float:
    return time.time()


def count_online_runners(org: str, label: str) -> int:
    return sum(runner.online and runner_is_managed(runner)
               and label in runner.labels
               for runner in list_github_runners(org))


def runner_is_managed(runner: GitHubRunner) -> bool:
    """Return whether a runner belongs to this GCP x64 fleet."""
    current_shape = any('-' in label and machine_profile(label) is not None for label in runner.labels)
    legacy_toronto = {
        "oxidex-spot-7x27", "northamerica-northeast2"
    }.issubset(runner.labels)
    return (current_shape or legacy_toronto) and "X64" in runner.labels


def maximum_runner_name_length(prefix: str, instance_name: str, runner_count: int) -> int:
    """Estimate the longest runner name when GCE's hostname matches its VM name."""
    return max(len(f"{prefix}-{instance_name}-{index}") for index in range(1, runner_count + 1))


def managed_online_runners(runners: list[GitHubRunner] | tuple[GitHubRunner, ...]):
    """Return all online runners on managed GCP x64 machine types."""
    return [runner for runner in runners if runner.online and runner_is_managed(runner)]


def spot_instances(vms: list[GceVm] | tuple[GceVm, ...]) -> list[GceVm]:
    """Return running Spot VMs created for this runner fleet."""
    return [vm for vm in vms if vm.running and vm.spot and vm.name.startswith("oxidex-runners-")]


def utilization_counts(runners: list[GitHubRunner] | tuple[GitHubRunner, ...]) -> tuple[int, int]:
    """Return online and busy runner counts for the managed GCP fleet."""
    online = managed_online_runners(runners)
    return len(online), sum(runner.busy for runner in online)


def host_runners(vm: GceVm, runners: list[GitHubRunner] | tuple[GitHubRunner, ...],
                 vms: list[GceVm] | tuple[GceVm, ...]):
    """Find runners assigned to the longest matching VM name."""
    names = [candidate.name for candidate in vms]
    assigned = []
    for runner in runners:
        matches = [name for name in names if name in runner.name]
        if matches and max(matches, key=len) == vm.name:
            assigned.append(runner)
    return assigned


def fleet_summary(vms, runners, state: dict) -> str:
    """Describe runner capacity, Spot hosts, and any pending host operation in one line."""
    online = [runner for runner in runners if runner.online]
    groups: dict[tuple[str, str, str, str], int] = {}
    fleet = [vm for vm in vms if vm.spot and vm.name.startswith("oxidex-runners-")]
    for vm in fleet:
        group = (vm.status, vm.machine_type, vm.cpu_platform or "CPU platform unknown", vm.zone)
        groups[group] = groups.get(group, 0) + 1
    hosts = ", ".join(f"{count} {status} {machine} {platform} in {zone}"
                      for (status, machine, platform, zone), count in groups.items())
    operation = state.get("host_operation")
    if not operation:
        pending = "no pending host operation"
    else:
        pending = f"pending {operation['kind']} of {operation['vm']['name']}"
        if operation["kind"] == "launch" and "started_at" in operation:
            minutes, seconds = divmod(int(now() - operation["started_at"]), 60)
            pending += f" for {minutes}m {seconds}s of the {LAUNCH_DEADLINE_SECONDS // 60}m limit"
    return (f"Fleet: GitHub runners {len(online)} online ({sum(runner.busy for runner in online)} busy), "
            f"{len(managed_online_runners(runners))} on Spot VMs, {len(runners) - len(online)} offline; "
            f"Spot VMs {len(fleet)}{': ' + hosts if hosts else ''}; {pending}")


def log_hourly_spend(vms, args: argparse.Namespace, state: dict, state_path: Path) -> None:
    """Log the fleet's current spend rate and reported project spend at most once an hour."""
    if now() - state.get("spend_logged_at", 0) < SPEND_LOG_INTERVAL_SECONDS:
        return
    fleet = spot_instances(vms)
    totals: dict[str, Decimal] = {}
    if fleet:
        check_running_prices(fleet, args.project, args.max_daily_usd, totals=totals)
    daily = sum(totals.values(), Decimal(0))
    unpriced = len(fleet) - len(totals)
    LOG.info("Spend rate: %d Spot VMs = $%.2f/day ($%.2f/hour) for compute and storage at current prices%s",
             len(fleet), daily, daily / 24,
             f"; {unpriced} VM price{'s' if unpriced != 1 else ''} unavailable" if unpriced else "")
    log_project_costs(args.project, getattr(args, "billing_export_table", None))
    state["spend_logged_at"] = now()
    write_scale_state(state_path, state)


def removable_spot_vms(vms: list[GceVm], runners: list[GitHubRunner]) -> list[GceVm]:
    """Choose complete, idle Spot VMs for scale-down."""
    candidates = []
    fleet = spot_instances(vms)
    for vm in fleet:
        assigned = host_runners(vm, runners, fleet)
        names = {runner.name for runner in assigned}
        marker = f"-{vm.name}-"
        prefixes = {name.rsplit(marker, 1)[0] for name in names if marker in name}
        complete = any(prefix and {f"{prefix}-{vm.name}-{slot}" for slot in range(1, 9)}.issubset(names)
                       for prefix in prefixes)
        if (not vm.instance_id or not complete
                or any(not runner.online or runner.busy for runner in assigned)):
            continue
        candidates.append(vm)
    return candidates


def terminate_spot_vm(vm: GceVm, project: str, org: str, runners: list[GitHubRunner],
                      vms: list[GceVm], on_deleted=None) -> None:
    """Delete an idle Spot VM and its attached disks, then remove stale runner records."""
    assigned = host_runners(vm, runners, spot_instances(vms))
    if not assigned or any(not runner.online or runner.busy for runner in assigned):
        raise RuntimeError(f"Refusing to delete {vm_details(vm)}: runners are missing, offline, or busy")
    if on_deleted is not None:
        on_deleted()
    subprocess.run(["gcloud", "compute", "instances", "delete", vm.name,
                    f"--zone={vm.zone}", f"--project={project}", "--delete-disks=all", "--quiet"],
                   check=True, text=True, capture_output=True)
    LOG.info("Deleted idle Spot VM %s and its attached disks", vm_details(vm))
    for runner in assigned:
        subprocess.run(["gh", "api", "-X", "DELETE",
                        f"orgs/{org}/actions/runners/{runner.runner_id}"],
                       check=True, text=True, capture_output=True)


def read_scale_state(path: Path) -> dict:
    """Read loop counters that must survive LaunchAgent restarts."""
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {"idle_loops": 0, "scale_up_active": False}
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("Ignoring unreadable autoscaler state at %s: %s", path, exc)
        return {"idle_loops": 0, "scale_up_active": False}
    return {**state, "idle_loops": int(state.get("idle_loops", 0)),
            "scale_up_active": False,
            "price_replacement_target": int(state.get("price_replacement_target", 0)),
            "observed_vms": state.get("observed_vms", {}),
            "job_tracking": state.get("job_tracking", {}),
            "pending_vm_recovery": state.get("pending_vm_recovery", {}),
            "intentional_deletions": state.get("intentional_deletions", [])}


def write_scale_state(path: Path, state: dict) -> None:
    """Write autoscaler loop counters atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False) as temp:
        json.dump(state, temp)
        temp.flush()
        os.fsync(temp.fileno())
        temporary_path = Path(temp.name)
    temporary_path.replace(path)


def begin_host_operation(kind, vm, runners, vms, state, state_path, **metadata):
    """Persist the single host identity before making any cloud change."""
    if state.get("host_operation"):
        raise RuntimeError("A previous host operation is still pending")
    state["host_operation"] = {"kind": kind, "vm": asdict(vm),
                               "runner_ids": [runner.runner_id for runner in host_runners(vm, runners, vms)], **metadata}
    if kind == "delete":
        key = f"{vm.zone}/{vm.name}"
        if key not in state.setdefault("intentional_deletions", []):
            state["intentional_deletions"].append(key)
        state.setdefault("intentional_deletion_ids", {})[key] = vm.instance_id
    write_scale_state(state_path, state)


def import_deployment_receipt(state, state_path):
    """Recover the provider identity even when a launch vanishes before inventory."""
    operation = state.get("host_operation", {})
    receipt = operation.get("deployment_receipt")
    if (operation.get("kind") != "launch" or operation.get("receipt_imported")
            or not receipt or not Path(receipt).exists()):
        return False
    from .fleet_logging import vm_from_record
    actual = vm_from_record(json.loads(Path(receipt).read_text()))
    planned = vm_from_record(operation["vm"])
    if (not actual.instance_id or not actual.spot or actual.name != planned.name
            or actual.zone != planned.zone or actual.machine_type != planned.machine_type
            or (planned.instance_id and actual.instance_id != planned.instance_id)):
        raise RuntimeError("Deployment receipt does not match the pending host identity")
    operation["vm"] = asdict(actual)
    operation["receipt_imported"] = True
    state.setdefault("observed_vms", {})[f"{actual.zone}/{actual.name}"] = asdict(actual)
    write_scale_state(state_path, state)
    return True


def reconcile_host_operation(vms, runners, args, state, state_path):
    """Hold all fleet changes until a fresh inventory confirms completion."""
    operation = state.get("host_operation")
    if not operation:
        return False
    import_deployment_receipt(state, state_path)
    from .fleet_logging import vm_from_record
    vm = vm_from_record(operation["vm"])
    current = next((item for item in vms if item.zone == vm.zone and item.name == vm.name), None)
    if current and vm.instance_id and current.instance_id != vm.instance_id:
        LOG.error("Host operation waits: instance identity changed for %s", vm_details(vm))
        return True
    if current and not vm.instance_id:
        operation["vm"] = asdict(current)
        vm = current
        write_scale_state(state_path, state)
    assigned = host_runners(vm, runners, [vm, *[item for item in vms if item.name != vm.name]])
    if (operation["kind"] == "delete" and current and current.running
            and not operation.get("price_eviction") and not operation.get("drained")):
        operation.update(kind="drain", runner_names={str(r.runner_id): r.name for r in assigned},
                         drain_mode="label_only")
        write_scale_state(state_path, state)
    if operation["kind"] == "drain":
        if current and current.running:
            try:
                from .runner_drain import drain_runners
                fresh = list_github_runners(args.org)
                assigned = host_runners(vm, fresh, [vm, *[item for item in vms if item.name != vm.name]])
                if drain_runners(operation, vm, args, state,
                                 lambda: write_scale_state(state_path, state), assigned):
                    def before_delete():
                        operation.update(kind="delete", drained=True)
                        key = f"{vm.zone}/{vm.name}"
                        if key not in state.setdefault("intentional_deletions", []):
                            state["intentional_deletions"].append(key)
                        state.setdefault("intentional_deletion_ids", {})[key] = vm.instance_id
                        write_scale_state(state_path, state)
                    delete_drained_vm(vm, args, before_delete)
            except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
                clear_rejected_deletion(vm, exc, state, state_path)
                LOG.warning("Drain remains pending for %s: %s", vm_details(vm), error_detail(exc))
            return True
        if f"{vm.zone}/{vm.name}" in state.get("intentional_deletions", []):
            # An older controller may already have submitted an asynchronous delete.
            operation.update(kind="delete_cleanup")
            write_scale_state(state_path, state)
            return True
        # A real preemption during drain remains unexpected until a provider delete is requested.
        from .fleet_logging import recovery_key
        from .vm_recovery import record_loss_restoration_target
        losses = state.setdefault("pending_vm_recovery", {})
        key = recovery_key(f"{vm.zone}/{vm.name}", vm, losses)
        losses.setdefault(key, asdict(vm))
        record_loss_restoration_target(vms, state, state_path)
        operation.update(kind="recovery", recovery_key=key)
        write_scale_state(state_path, state)
    if operation["kind"] == "delete" and current and current.status.upper() == "TERMINATED":
        # A timed-out intentional delete can be followed by a stopped host.
        # Finish only this identity's disk cleanup without creating a loss target.
        # Keep the saved drive IDs as cleanup authorization. A partial current
        # inventory must not discard IDs, or authorize newly attached drives.
        vm = replace(vm, status=current.status)
        operation.update(kind="delete_cleanup", vm=asdict(vm))
        write_scale_state(state_path, state)
    if operation["kind"] == "launch":
        if "started_at" not in operation:
            # Launches recorded before deadlines existed expire relative to this restart.
            operation["started_at"] = now()
            write_scale_state(state_path, state)
        expected = {f"gha-{vm.name}-{slot}" for slot in range(1, 9)}
        online_names = {runner.name for runner in assigned if runner.online}
        requested = operation.get('requested_cpu_platform')
        platform_ok = not requested or (current is not None and cpu_platform_satisfies(
            current.machine_type, current.cpu_platform, requested))
        if current and current.running and current.cpu_platform and not platform_ok:
            operation.setdefault("abandon", f"observed CPU platform {current.cpu_platform} "
                                            f"does not satisfy requested minimum {requested}")
        elif current and current.running and platform_ok and expected.issubset(online_names):
            LOG.info("Host operation complete: all eight runners registered for %s", vm_details(current))
            state.pop("host_operation")
            write_scale_state(state_path, state)
            return True
        # A manager-confirmed host keeps its jobs even if one runner briefly restarts later.
        if not operation.get("registered") and now() - operation["started_at"] >= LAUNCH_DEADLINE_SECONDS:
            operation.setdefault("abandon", f"all eight runners did not register within "
                                            f"{LAUNCH_DEADLINE_SECONDS // 60} minutes")
        if operation.get("abandon"):
            try:
                abandon_launch(args, state, state_path)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                LOG.warning("Launch cleanup pending for %s: %s", vm_details(vm), error_detail(exc))
        else:
            losses = state.setdefault("pending_vm_recovery", {})
            if current is None and vm.instance_id:
                # The manager observed a real host; its disappearance is a loss
                # even when it happened before the controller's next full poll.
                from .fleet_logging import recovery_key
                key = recovery_key(f"{vm.zone}/{vm.name}", vm, losses)
                if key not in losses:
                    losses[key] = asdict(vm)
                    LOG.warning("In-flight launch VM no longer present: %s; cause unconfirmed", vm_details(vm))
            lost = next(((key, record) for key, record in losses.items()
                         if record.get("name") == vm.name and record.get("zone") == vm.zone
                         and (not vm.instance_id or record.get("instance_id") == vm.instance_id)), None)
            if lost:
                key, record = lost
                from .vm_recovery import record_loss_restoration_target
                record_loss_restoration_target(vms, state, state_path)
                operation.update(kind="recovery", vm=record, recovery_key=key)
                write_scale_state(state_path, state)
            LOG.info("Fleet scaling waits for eight online runner slots on %s", vm_details(vm))
        return True
    if operation["kind"] in ("recovery", "delete_cleanup") and not operation.get("drives_deleted"):
        from .vm_recovery import cleanup_lost_vm
        try:
            if cleanup_lost_vm(vm, args.project):
                operation["drives_deleted"] = True
                write_scale_state(state_path, state)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            LOG.warning("Recovery pending for %s: %s", vm_details(vm), error_detail(exc))
        return True
    if current and operation["kind"] in ("recovery", "delete_cleanup"):
        LOG.info("Recovery waits for fresh VM absence confirmation: %s", vm_details(vm))
        return True
    if current:
        LOG.info("Retrying deletion of the same pending host: %s", vm_details(vm))
        try:
            if operation.get("price_eviction"):
                if not current.running:
                    LOG.info("Price eviction waits for VM deletion confirmation: %s", vm_details(current))
                    return True
                overpriced = check_running_prices([current], args.project, args.max_daily_usd, raise_on_error=True)
                if not overpriced:
                    # The previous delete may still complete asynchronously.
                    # Pause retries but retain its identity, intent and fleet barrier.
                    LOG.info("Paused pending price eviction: fresh price is within cap; previous deletion outcome remains pending for %s",
                             vm_details(current))
                    return True
                delete_overpriced_vm(current, args.project)
            else:
                if operation.get("drained"):
                    fresh = list_github_runners(args.org)
                    if host_runners(vm, fresh, [vm, *[item for item in vms if item.name != vm.name]]):
                        LOG.warning("Deletion waits: runner records reappeared for %s", vm_details(vm))
                        return True
                    delete_drained_vm(current, args)
                else:
                    terminate_spot_vm(current, args.project, args.org, runners, vms)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            clear_rejected_deletion(vm, exc, state, state_path)
            LOG.warning("Deletion remains pending for %s: %s", vm_details(vm), error_detail(exc))
        return True
    recorded_ids = set(operation.get("runner_ids", []))
    stale = [runner for runner in runners if runner.runner_id in recorded_ids or runner in assigned]
    if any(runner.online or runner.busy for runner in stale):
        LOG.info("Fleet scaling waits for deleted host runners to become offline and idle: %s", vm_details(vm))
        return True
    if stale:
        for runner in stale:
            try:
                subprocess.run(["gh", "api", "-X", "DELETE", f"orgs/{args.org}/actions/runners/{runner.runner_id}"],
                               check=True, text=True, capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                LOG.warning("Deregistration pending for %s: %s", runner.name, error_detail(exc))
                break
        return True  # Require another inventory proving the records are absent.
    if operation["kind"] == "recovery":
        state["pending_vm_recovery"][operation["recovery_key"]]["_cleanup_complete"] = True
    if operation.get("drain_group_id"):
        from .runner_drain import cleanup_group
        try:
            cleanup_group(operation, args.org)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            LOG.warning("Drain group cleanup remains pending: %s", error_detail(exc))
            return True
    state.pop("host_operation")
    write_scale_state(state_path, state)
    LOG.info("Host operation complete: VM and assigned runner records absent for %s", vm_details(vm))
    return True


def clear_rejected_deletion(vm: GceVm, exc: Exception, state: dict | None, state_path: Path) -> None:
    """Retire intent only when gcloud confirms the delete request was rejected."""
    if state is None or not isinstance(exc, subprocess.CalledProcessError):
        return
    command = exc.cmd if isinstance(exc.cmd, (list, tuple)) else []
    if (not command or Path(str(command[0])).name != 'gcloud'
            or list(command[1:4]) != ['compute', 'instances', 'delete']):
        return  # A subsequent runner deregistration failure does not undo VM deletion.
    detail = error_detail(exc).lower()
    rejected = (re.search(r'http (400|401|403)\b', detail)
                or 'permission_denied' in detail or 'permission denied' in detail
                or ('required' in detail and 'permission' in detail)
                or 'invalid_argument' in detail)
    if not rejected:
        return  # Timeouts and unknown operation outcomes must retain intent.
    key = f'{vm.zone}/{vm.name}'
    state['intentional_deletions'] = [item for item in state.get('intentional_deletions', []) if item != key]
    state.get('intentional_deletion_ids', {}).pop(key, None)
    operation = state.get('host_operation', {})
    if operation.get('drained'):
        operation['kind'] = 'drain'
    else:
        state.pop('host_operation', None)
    write_scale_state(state_path, state)
    LOG.warning('Cleared deletion intent after provider rejection for %s: %s', vm_details(vm), error_detail(exc))


def delete_drained_vm(vm, args, before_delete=None):
    observed = subprocess.run(["gcloud", "compute", "instances", "describe", vm.name,
                               f"--zone={vm.zone}", f"--project={args.project}", "--format=value(id)"],
                              check=True, text=True, capture_output=True, timeout=60)
    if not vm.instance_id or observed.stdout.strip() != vm.instance_id:
        raise RuntimeError("Refusing drained deletion: provider instance identity changed")
    if before_delete is not None:
        before_delete()
    subprocess.run(["gcloud", "compute", "instances", "delete", vm.name,
                    f"--zone={vm.zone}", f"--project={args.project}", "--delete-disks=all", "--quiet"],
                   check=True, text=True, capture_output=True, timeout=180)
    LOG.info("Deleted drained Spot VM %s and its attached disks", vm_details(vm))


def scale_down(vms: list[GceVm], runners: list[GitHubRunner], args: argparse.Namespace,
               state: dict | None = None, state_path: Path | None = None) -> int:
    """Drain one idle host, then wait for CI completion and deregistration before deletion."""
    state = state if state is not None else read_scale_state(args.state_file.expanduser())
    state_path = state_path or args.state_file.expanduser()
    if state.get("host_operation") or len(spot_instances(vms)) <= 1:
        return 0
    candidates = removable_spot_vms(vms, runners)
    if not candidates:
        LOG.info("Scale-down waits: no complete idle host is available")
        return 0
    vm = candidates[0]
    assigned = host_runners(vm, runners, vms)
    begin_host_operation("drain", vm, runners, vms, state, state_path,
                         runner_names={str(runner.runner_id): runner.name for runner in assigned},
                         drain_mode="label_only")
    reconcile_host_operation(vms, runners, args, state, state_path)
    return 0  # Host removal is counted only after the drain completes.


def scale_up(additional_hosts: int, offers, args: argparse.Namespace, state=None, state_path=None,
             refresh=None) -> int:
    """Launch at most one host, trying each ranked candidate once per search.

    A failed candidate is skipped until the list is exhausted or a launch succeeds.
    After each failure, ``refresh`` supplies a fresh price scan when provided.
    """
    state_path = state_path or args.state_file.expanduser()
    state = state if state is not None else read_scale_state(state_path)
    if additional_hosts <= 0 or state.get("host_operation"):
        return 0
    if hasattr(args, 'cpu_platforms_by_zone') and args.cpu_platforms_by_zone is None:
        LOG.warning('Capacity launch waits for a current CPU platform zone map')
        return 0
    failed = state.setdefault("failed_launch_candidates", {})
    search_started = now()
    while True:
        untried = [offer for offer in offers if candidate_key(offer) not in failed]
        if not untried:
            if offers:
                LOG.warning("Launch candidates exhausted: all %d failed in this search; "
                            "retrying the full list on the next check", len(failed))
                failed.clear()
                write_scale_state(state_path, state)
            return 0
        offer = untried[0]
        try:
            launch_offer(offer, args, state, state_path)
            failed.clear()
            write_scale_state(state_path, state)
            return 1
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            failed[candidate_key(offer)] = now()
            write_scale_state(state_path, state)
            LOG.warning("Could not deploy %s requesting CPU platform %s in %s: %s", offer.machine_type,
                        getattr(offer, 'cpu_platform', 'preferred family platform'),
                        getattr(offer, 'zone', '') or offer.region, error_detail(exc))
            if state.get("host_operation"):
                return 0
            if now() - search_started >= LAUNCH_SEARCH_BUDGET_SECONDS:
                LOG.info("Launch search paused after %d minutes; the next check continues with "
                         "the next untried candidate", LAUNCH_SEARCH_BUDGET_SECONDS // 60)
                return 0
            if refresh is not None:
                LOG.info("Refreshing Spot prices and launch preflight before the next candidate")
                offers = refresh()


def reconcile_prices(vms, runners, args, state, state_path) -> bool:
    """Evict or restore one host, preserving the restoration target across polls."""
    if state.get("host_operation"):
        return True
    fleet = spot_instances(vms)
    overpriced = check_running_prices(fleet, args.project, args.max_daily_usd)
    if overpriced:
        state["price_replacement_target"] = max(state.get("price_replacement_target", 0), len(fleet))
        state["idle_loops"] = 0
        vm = overpriced[0]
        begin_host_operation("delete", vm, runners, vms, state, state_path, price_eviction=True)
        LOG.warning("Price exceeds cap: deleting %s with its disks, including any active jobs", vm_details(vm))
        try:
            delete_overpriced_vm(vm, args.project)
            LOG.info("Deleted overpriced Spot VM %s and its attached disks", vm_details(vm))
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            clear_rejected_deletion(vm, exc, state, state_path)
            LOG.error("Price eviction failed for %s; retrying next loop: %s", vm_details(vm), error_detail(exc))
        return True
    target = state.get("price_replacement_target", 0)
    pending = state.get("pending_vm_recovery", {})
    if any(not record.get("_cleanup_complete") for record in pending.values()):
        LOG.warning("Replacement waits until previous drives and runner records have been verified deleted")
        return True
    if not target:
        return False
    missing = max(0, target - len(fleet))
    if missing:
        LOG.info("Finding one affordable replacement; %d missing hosts, restoration target %d", missing, target)
        refresh = offer_refresher(args)
        offers = refresh()
        if not offers or not scale_up(1, offers, args, state, state_path, refresh=refresh):
            LOG.warning("Replacement capacity is pending; retrying next loop")
        return True  # Only fresh observations may clear the restoration target.
    for record in pending.values():
        state.get("observed_vms", {}).pop(f"{record['zone']}/{record['name']}", None)
    state["pending_vm_recovery"] = {}
    state["price_replacement_target"] = 0
    state["idle_loops"] = 0
    write_scale_state(state_path, state)
    return True


def scaling_decision(online: int, busy: int, vm_count: int, idle_loops: int,
                     scale_up_active: bool, target_workers: int) -> tuple[str, int, int, bool]:
    """Choose a single host change using only current utilization."""
    if online > 0 and online <= target_workers and busy * 2 > online:
        return "up", 1, 0, False
    if vm_count > 1 and online > 0 and busy * 2 < online:
        loops = idle_loops + 1
        if loops >= 2:
            return "down", 1, 0, False
        return "wait", 0, loops, False
    return "wait", 0, 0, False


CANDIDATE_LOG_LIMIT = 10


def offer_refresher(args: argparse.Namespace):
    """Return a scan function that logs provider failures instead of stopping the controller."""
    def refresh():
        try:
            return discover_offers(args.project, args.max_daily_usd, disk_size_gb=args.disk_size_gb)
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
            LOG.error("Spot scan failed; launches wait for the next check: %s", error_detail(exc))
            return []
    return refresh


def discover_offers(project: str, max_daily_usd: Decimal, disk_size_gb: int = 100):
    """Return fresh zone candidates under the total cap that pass launch preflight, best first."""
    clear_catalog_cache()
    auth = get_auth(project)
    # The authenticated catalog is the same price source the manager and price guard use.
    offers, _, _, errors = asyncio.run(
        find(auth, min_vcpu=32, min_ram=MINIMUM_RAM_GIB, top=None, concurrency=200,
             catalog_rates=parse_component_rates(catalog_skus()))
    )
    if errors:
        LOG.warning("Spot scan is incomplete: %d Spot price lookup(s) failed; first: %s",
                    len(errors), error_detail(errors[0]))
    prices = {region: storage_rates(region) for region in {offer.region for offer in offers}}
    ranked = eligible_offers(offers, max_daily_usd, storage_rates=prices, disk_size_gb=disk_size_gb)
    candidates, skipped = launchable_candidates(ranked, load_launch_inventory(project), disk_size_gb)
    LOG.info("Spot scan: %d priced shapes; %d platform offers under $%s/day total; "
             "%d launchable zone candidates; %d offers have no launchable zone",
             len(offers), len(ranked), max_daily_usd, len(candidates), len(skipped))
    for index, candidate in enumerate(candidates[:CANDIDATE_LOG_LIMIT], start=1):
        LOG.info("Candidate #%d %s %s %s: CPU preference rank %d; $%.2f/day total",
                 index, candidate.zone, candidate.machine_type, candidate.cpu_platform,
                 candidate.cpu_rank + 1, candidate.total_daily_usd)
    for reason in skipped:
        LOG.info("Not launchable: %s", reason)
    return candidates


def load_launch_inventory(project: str) -> LaunchInventory:
    """Read zone, disk-type, and quota facts that decide whether a launch can succeed."""
    def gcloud_json(*command):
        result = subprocess.run(['gcloud', 'compute', *command, f'--project={project}'],
                                check=True, text=True, capture_output=True, timeout=60)
        return json.loads(result.stdout)

    def quotas(rows):
        return {row['metric']: (float(row['limit']), float(row['usage'])) for row in rows or []}

    disk_types: dict[str, set[str]] = {}
    for row in gcloud_json('disk-types', 'list', '--format=json(name,zone)'):
        if row.get('zone'):  # Regional disk types cannot back a zonal runner VM.
            disk_types.setdefault(row['zone'].rsplit('/', 1)[-1], set()).add(row['name'])
    return LaunchInventory(
        platforms_by_zone=load_cpu_platform_map(project),
        disk_types_by_zone=disk_types,
        region_quotas={row['name']: quotas(row.get('quotas'))
                       for row in gcloud_json('regions', 'list', '--format=json(name,quotas)')},
        global_quotas=quotas(gcloud_json('project-info', 'describe', '--format=json(quotas)').get('quotas')))


def eligible_offers(offers, max_daily_usd: Decimal, *, storage_rates=None, disk_size_gb: int = 100):
    """Rank explicitly requested platforms, then regional compute price."""
    shapes = [offer for offer in offers if offer.vcpu == 32 and offer.ram_gib >= MINIMUM_RAM_GIB
              and machine_profile(offer.machine_type) is not None]
    if storage_rates is None:
        raise RuntimeError('Regional storage prices are required to rank total VM cost')
    candidates = []
    for offer in affordable_offers(shapes, max_daily_usd):
        try:
            boot, data, monthly = cheapest_storage(offer.machine_type, disk_size_gb,
                                                   storage_rates.get(offer.region, {}))
        except RuntimeError as exc:
            LOG.warning('Skipping %s in %s: %s', offer.machine_type, offer.region, error_detail(exc))
            continue
        for rank, platform in platform_preferences(offer.machine_type):
            candidate = CpuOffer(offer.region, offer.machine_type, offer.vcpu, offer.ram_gib,
                                 offer.hourly_usd, platform, rank, boot, data, monthly, zones=offer.zones)
            if candidate.total_daily_usd < max_daily_usd:
                candidates.append(candidate)
    return sorted(candidates, key=lambda offer: (offer.cpu_rank, offer.total_daily_usd,
                                                offer.region, offer.machine_type))


def load_cpu_platform_map(project: str) -> dict[str, list[str]]:
    result = subprocess.run(['gcloud', 'compute', 'zones', 'list', f'--project={project}',
                             '--format=json(name,status,availableCpuPlatforms)'],
                            check=True, text=True, capture_output=True, timeout=60)
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise RuntimeError('CPU platform zone inventory is invalid')
    platforms = {}
    for row in rows:
        if row.get('status') != 'UP':
            continue
        advertised = row.get('availableCpuPlatforms')
        if not row.get('name') or not isinstance(advertised, list) or not advertised or not all(isinstance(p, str) for p in advertised):
            raise RuntimeError('CPU platform zone inventory is incomplete')
        platforms[row['name']] = advertised
    if not platforms:
        raise RuntimeError('CPU platform zone inventory is empty')
    return platforms


def runner_capacity(vcpu: int) -> int:
    if vcpu < 4:
        return 0
    return vcpu // 4


def launch_offer(offer, args: argparse.Namespace, state=None, state_path=None) -> None:
    """Try each zone until the manager confirms a complete runner deployment."""
    state_path = state_path or args.state_file.expanduser()
    state = state if state is not None else read_scale_state(state_path)
    if state.get("host_operation"):
        raise RuntimeError("A previous host operation is still pending")
    preferences = platform_preferences(offer.machine_type)
    if not preferences:
        raise RuntimeError(f'{offer.machine_type} cannot request a CPU platform; refusing automatic launch')
    cpu_platform = getattr(offer, 'cpu_platform', preferences[0][1])
    cpu_rank = next((rank for rank, platform in preferences if platform == cpu_platform), None)
    if cpu_rank is None:
        raise RuntimeError(f'Unsupported CPU platform {cpu_platform} for {offer.machine_type}')
    hourly_limit = args.max_daily_usd / Decimal(24)
    preflighted_zone = getattr(offer, 'zone', '')
    if preflighted_zone:
        zones = [preflighted_zone]
    else:
        result = subprocess.run(
            ["gcloud", "compute", "zones", "list", f"--project={args.project}",
             f"--filter=name~'^{offer.region}-' AND status=UP", "--format=value(name)"],
            check=True, text=True, capture_output=True,
        )
        zones = result.stdout.split()
    if not zones:
        raise RuntimeError(f"No active Compute Engine zones found in {offer.region}")
    for zone in zones:
        platform_map = getattr(args, 'cpu_platforms_by_zone', None)
        if preflighted_zone:
            pass  # Discovery checked a fresh zone inventory moments ago.
        elif platform_map is not None and cpu_platform not in platform_map.get(zone, []):
            LOG.info('Skipping %s in %s: requested platform %s is not advertised by the startup CPU map',
                     offer.machine_type, zone, cpu_platform)
            continue
        if cpu_platform not in {platform for _, platform in platform_preferences(offer.machine_type, zone)}:
            LOG.info('Skipping %s in %s: requested CPU platform %s is unsupported for this shape/zone',
                     offer.machine_type, zone, cpu_platform)
            continue
        letter = zone.rsplit("-", 1)[-1]
        suffix = f"{offer.region.replace('-', '')}-{letter}-{int(time.time())}"
        instance_name = f"oxidex-runners-{suffix}"
        runner_prefix = "gha"
        longest_name = maximum_runner_name_length(runner_prefix, instance_name, 8)
        if longest_name > 64:
            raise RuntimeError(f"Generated GitHub runner name would be {longest_name} characters (maximum 64)")
        command = [
            sys.executable, str(ROOT / "src" / "spot_runner_manager.py"),
            "--project", args.project, "--org", args.org,
            "--max-cost", str(hourly_limit), "--instance-type", offer.machine_type,
            '--max-total-daily-usd', str(args.max_daily_usd), '--cheapest-storage',
            "--min-cpu-platform", cpu_platform,
            "--region", offer.region, "--zone", zone,
            "--cpu", str(offer.vcpu), "--ram", str(int(offer.ram_gib)),
            "--instance-name", instance_name,
            "--disk-device-name", f"runner-data-{suffix}",
            "--disk-size-gb", str(args.disk_size_gb),
            "--runner-prefix", runner_prefix,
            "--runner-label", args.label, "--apply",
        ]
        if getattr(args, 'billing_export_table', None):
            command.extend(['--billing-export-table', args.billing_export_table])
        profile = machine_profile(offer.machine_type)
        if profile is None:
            raise RuntimeError(f'Unsupported runner machine type: {offer.machine_type}')
        vm = GceVm(instance_name, zone, "PROVISIONING", offer.machine_type, True,
                   vcpu=offer.vcpu, ram_gib=offer.ram_gib, requested_cpu_platform=cpu_platform,
                   disks=({"boot": True, "diskSizeGb": 40,
                           "diskType": getattr(offer, 'boot_disk_type', '') or profile.disk_type,
                           "interface": 'NVME' if (getattr(offer, 'boot_disk_type', '') or profile.disk_type).startswith('hyperdisk-') else 'SCSI'},
                          {"boot": False, "diskSizeGb": args.disk_size_gb,
                           "diskType": getattr(offer, 'data_disk_type', '') or profile.disk_type,
                           "interface": 'NVME' if (getattr(offer, 'data_disk_type', '') or profile.disk_type).startswith('hyperdisk-') else 'SCSI'}))
        storage_daily = monthly_to_daily(getattr(offer, 'storage_monthly_usd', Decimal(0)))
        LOG.info("Found instance in region %s: %s; CPU preference rank %d (hardware estimate, "
                 "Rust performance unmeasured); $%.6f/day compute + $%.6f/day storage = $%.6f/day total; attempting to launch it now",
                 offer.region, vm_details(vm), cpu_rank + 1, offer.hourly_usd * 24,
                 storage_daily, Decimal(str(offer.hourly_usd)) * 24 + storage_daily)
        receipt = state_path.parent / f"{instance_name}-{uuid.uuid4().hex}-deployment.json"
        command.extend(["--deployment-receipt", str(receipt),
                        "--registration-timeout-seconds", str(LAUNCH_DEADLINE_SECONDS)])
        begin_host_operation("launch", vm, [], [], state, state_path,
                             requested_cpu_platform=cpu_platform, deployment_receipt=str(receipt),
                             candidate_key=f"{zone}/{offer.machine_type}/{cpu_platform}",
                             data_disk_name=f"runner-data-{suffix}", started_at=now())
        started = time.monotonic()
        manager_hung = False
        try:
            # The manager enforces the registration deadline; this backstop covers a hung manager.
            result = subprocess.run(command, check=False, text=True, capture_output=True,
                                    timeout=LAUNCH_DEADLINE_SECONDS + 120)
        except subprocess.TimeoutExpired:
            manager_hung = True
            result = SimpleNamespace(returncode=-9, stdout="",
                                     stderr="Deployment manager timed out before reporting an outcome")
        identity_observed = import_deployment_receipt(state, state_path)
        for output in (result.stdout, result.stderr):
            for line in (output or "").splitlines():
                if line.strip():
                    LOG.info("Deployment diagnostics: %s; %s", vm_details(vm), error_detail(line))
        if manager_hung and registered_host(vm, args):
            LOG.warning("Deployment manager hung after all eight runners registered; keeping %s", vm_details(vm))
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
        if result.returncode == 0:
            state["host_operation"]["registered"] = True
            write_scale_state(state_path, state)
            elapsed = time.monotonic() - started
            LOG.info("Instance registered to take jobs in %s runners: %s; %.1f seconds from starting "
                     "deployment to manager confirmation of online runner registration and storage checks",
                     args.org, vm_details(vm), elapsed)
            return
        diagnostics = error_detail(result.stderr or result.stdout or "no diagnostics")
        if identity_observed and not any(item.zone == zone and item.name == instance_name
                                         for item in list_gcloud_vms(args.project, name_prefix="oxidex-runners-")):
            raise RuntimeError(f"Deployment VM {instance_name} disappeared; waiting for loss recovery")
        failure = diagnostics.lower()
        uncertain = result.returncode < 0 or any(fragment in failure for fragment in
                    ("timeout", "timed out", "deadline exceeded", "connection reset", "connection aborted"))
        state["host_operation"]["abandon"] = f"deployment failed (exit {result.returncode}): {diagnostics}"
        # An unconfirmed create may still be in flight; absence is trusted only after the deadline.
        state["host_operation"]["outcome_uncertain"] = uncertain
        write_scale_state(state_path, state)
        if not abandon_launch(args, state, state_path):
            raise RuntimeError(f"Deployment of {instance_name} failed; cleanup is pending before the next candidate")
        LOG.warning("Deployment in %s failed; resources removed before the next candidate. Tried to deploy %s; exit %d; %s",
                    zone, vm_details(vm), result.returncode, diagnostics)
    raise RuntimeError(f"No zone accepted {offer.machine_type} in {offer.region}")


def registered_host(vm: GceVm, args: argparse.Namespace) -> bool:
    """Return whether the launched VM is running with all eight runners online."""
    current = next((item for item in list_gcloud_vms(args.project, name_prefix="oxidex-runners-")
                    if item.zone == vm.zone and item.name == vm.name), None)
    if not current or not current.running:
        return False
    online = {runner.name for runner in host_runners(vm, list_github_runners(args.org), [vm]) if runner.online}
    return {f"gha-{vm.name}-{slot}" for slot in range(1, 9)}.issubset(online)


def abandon_launch(args: argparse.Namespace, state: dict, state_path: Path) -> bool:
    """Delete a failed or stalled launch's VM, disks, and runner records.

    Returns True once fresh inventories confirm all three are absent and the
    host operation is cleared; otherwise cleanup continues on the next check.
    """
    operation = state["host_operation"]
    from .fleet_logging import vm_from_record
    vm = vm_from_record(operation["vm"])
    reason = operation["abandon"]
    if operation.get("candidate_key"):
        state.setdefault("failed_launch_candidates", {})[operation["candidate_key"]] = now()
    key = f"{vm.zone}/{vm.name}"
    current = next((item for item in list_gcloud_vms(args.project, name_prefix="oxidex-runners-")
                    if item.zone == vm.zone and item.name == vm.name), None)
    if current:
        if key not in state.setdefault("intentional_deletions", []):
            state["intentional_deletions"].append(key)
        if current.instance_id:
            state.setdefault("intentional_deletion_ids", {})[key] = current.instance_id
        write_scale_state(state_path, state)
        LOG.warning("Deleting launch %s with its disks: %s", vm_details(current), reason)
        subprocess.run(["gcloud", "compute", "instances", "delete", vm.name, f"--zone={vm.zone}",
                        f"--project={args.project}", "--delete-disks=all", "--quiet"],
                       check=True, text=True, capture_output=True, timeout=300)
        if any(item.zone == vm.zone and item.name == vm.name
               for item in list_gcloud_vms(args.project, name_prefix="oxidex-runners-")):
            return False
    elif (operation.get("outcome_uncertain")
          and now() - operation.get("started_at", now()) < LAUNCH_DEADLINE_SECONDS):
        LOG.info("Launch cleanup waits until the deadline confirms %s was never created: %s",
                 vm.name, reason)
        write_scale_state(state_path, state)
        return False
    names = {vm.name, operation.get("data_disk_name") or vm.name.replace("oxidex-runners-", "runner-data-", 1)}
    disks = subprocess.run(["gcloud", "compute", "disks", "list", f"--project={args.project}",
                            f"--zones={vm.zone}", "--format=json(name,users)"],
                           check=True, text=True, capture_output=True, timeout=60)
    leftovers = [disk for disk in json.loads(disks.stdout) if disk.get("name") in names]
    if any(disk.get("users") for disk in leftovers):
        LOG.warning("Launch cleanup waits: a disk of %s is still attached", vm.name)
        write_scale_state(state_path, state)
        return False
    for disk in leftovers:
        subprocess.run(["gcloud", "compute", "disks", "delete", disk["name"], f"--zone={vm.zone}",
                        f"--project={args.project}", "--quiet"],
                       check=True, text=True, capture_output=True, timeout=120)
        LOG.info("Deleted disk %s left by launch %s", disk["name"], vm.name)
    for runner in host_runners(vm, list_github_runners(args.org), [vm]):
        subprocess.run(["gh", "api", "-X", "DELETE", f"orgs/{args.org}/actions/runners/{runner.runner_id}"],
                       check=True, text=True, capture_output=True, timeout=60)
    if host_runners(vm, list_github_runners(args.org), [vm]):
        write_scale_state(state_path, state)
        return False
    state.pop("host_operation")
    write_scale_state(state_path, state)
    LOG.warning("Abandoned launch %s; its VM, disks, and runner records are absent: %s",
                vm_details(vm), reason)
    return True


def install_launch_agent(args: argparse.Namespace) -> Path:
    """Install a per-user LaunchAgent that restarts the autoscaler after login."""
    import plistlib

    home = Path.home()
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / "com.swacktools.spot-runner-autoscaler.plist"
    plist = {
        "Label": "com.swacktools.spot-runner-autoscaler",
        "ProgramArguments": [sys.executable, str(ROOT / "src" / "spot_runner_autoscaler.py"),
                             "--max-daily-usd", str(args.max_daily_usd),
                             "--project", args.project, "--org", args.org,
                             "--interval-seconds", str(args.interval_seconds),
                             "--minimum-runners", str(args.minimum_runners),
                             "--disk-size-gb", str(args.disk_size_gb),
                             "--label", args.label, "--state-file", str(args.state_file.expanduser()),
                             "--log-file", str(args.log_file.expanduser())],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": str(home / "Library" / "Logs" / "spot-runner-autoscaler.log"),
        "StandardErrorPath": str(home / "Library" / "Logs" / "spot-runner-autoscaler.log"),
    }
    for repo in args.job_repository or []:
        plist["ProgramArguments"].extend(["--job-repository", repo])
    if getattr(args, 'billing_export_table', None):
        plist['ProgramArguments'].extend(['--billing-export-table', args.billing_export_table])
    plist_path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], check=False)
    return plist_path


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="swack-tools")
    parser.add_argument("--project", default="homelab-424523")
    parser.add_argument("--minimum-runners", type=int, default=25)
    parser.add_argument("--label", default="spot")
    parser.add_argument("--max-daily-usd", type=Decimal, default=Decimal("10"),
                        help='Maximum daily compute plus boot/data storage cost per VM')
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--disk-size-gb", type=int, default=100)
    parser.add_argument('--billing-export-table', help='Billing export project.dataset.table for launch cost logs (or GCP_BILLING_EXPORT_TABLE)')
    parser.add_argument("--log-file", type=Path, default=Path.home() / "spot-ghrunner.log")
    parser.add_argument("--state-file", type=Path,
                        default=Path.home() / "Library" / "Application Support"
                        / "Spot Runner Autoscaler" / "state.json")
    parser.add_argument("--install-launch-agent", action="store_true")
    parser.add_argument("--job-repository", action="append", default=None,
                        help="Repository whose crash jobs are tracked/rerun; repeat for multiple repos (default: ORG/oxidex)")
    parser.add_argument("--once", action="store_true", help="Check and scale once, then exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = args_parser().parse_args(argv)
    configure_logging()
    if args.minimum_runners < 1 or args.interval_seconds < 1 or args.disk_size_gb < 1:
        raise ValueError("runner floors, interval, and disk size must be positive")
    if args.max_daily_usd <= 0:
        raise ValueError("max daily VM cost must be positive")
    load_env(ROOT / ".env")
    args.billing_export_table = args.billing_export_table or os.environ.get('GCP_BILLING_EXPORT_TABLE')
    if args.install_launch_agent:
        print(f"Installed LaunchAgent at {install_launch_agent(args)}")
        return 0

    with lifecycle_log(args.log_file):
        LOG.info("Checking fleet every %d seconds; total compute/storage cap $%s/day per VM",
                 args.interval_seconds, args.max_daily_usd)
        return run_controller(args)


def run_controller(args: argparse.Namespace) -> int:
    """Hold one controller lock so concurrent checks cannot duplicate reruns."""
    import fcntl

    state_path = args.state_file.expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(state_path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another controller owns state {state_path}") from exc
        return _run_controller(args)


def _run_controller(args: argparse.Namespace) -> int:
    """Run fleet checks until interrupted or a single check completes."""
    state_path = args.state_file.expanduser()
    state = read_scale_state(state_path)
    jobs = JobRecovery(state, lambda: write_scale_state(state_path, state),
                       args.job_repository or [f"{args.org}/oxidex"])
    args.cpu_platforms_by_zone = None
    while True:
        clear_catalog_cache()
        if args.cpu_platforms_by_zone is None:
            try:
                args.cpu_platforms_by_zone = load_cpu_platform_map(args.project)
                state['cpu_platforms_by_zone'] = args.cpu_platforms_by_zone
                state['cpu_platform_map_updated_at'] = time.time()
                write_scale_state(state_path, state)
                LOG.info('Loaded current CPU platform map for %d zones from GCP; refresh on controller restart',
                         len(args.cpu_platforms_by_zone))
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                LOG.error('CPU platform map unavailable; capacity launches paused until fresh inventory succeeds: %s', error_detail(exc))
        runners = list_github_runners(args.org)
        vms = list_gcloud_vms(args.project, name_prefix="oxidex-runners-")
        reconcile_deletion_identities(vms, state)
        jobs.poll(vms)
        observe_vms(vms, state)
        write_scale_state(state_path, state)
        LOG.info(fleet_summary(vms, runners, state))
        log_hourly_spend(vms, args, state, state_path)
        operation_work = reconcile_host_operation(vms, runners, args, state, state_path)
        recovery_work = False if operation_work else reconcile_lost_vms(vms, runners, args, state, state_path)
        price_work = False if recovery_work or operation_work else reconcile_prices(vms, runners, args, state, state_path)
        if operation_work or price_work or recovery_work:
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
            continue
        fleet_vms = spot_instances(vms)
        current, busy = utilization_counts(runners)
        LOG.info("Managed GCP runners: %d online, %d busy; %d Spot VMs",
                 current, busy, len(fleet_vms))
        action, host_count, idle_loops, scale_up_active = scaling_decision(
            current, busy, len(fleet_vms), state["idle_loops"],
            state["scale_up_active"], args.minimum_runners)
        if state["scale_up_active"] and not scale_up_active and action == "hold":
            LOG.info("Scale-up complete: online capacity is above %d runners", args.minimum_runners)
        state["scale_up_active"] = scale_up_active

        if action == "up":
            state["idle_loops"] = idle_loops
            if host_count:
                LOG.info("High usage: launching %d host, then checking fresh demand", host_count)
                refresh = offer_refresher(args)
                offers = refresh()
                launched = scale_up(host_count, offers, args, state, state_path, refresh=refresh) if offers else 0
                if not launched:
                    LOG.error("Could not launch Spot capacity below $%s/day", args.max_daily_usd)
        elif action == "down":
            state["idle_loops"] = idle_loops
            LOG.info("Low usage persisted for two checks; removing up to %d Spot hosts", host_count)
            removed = scale_down(vms, runners, args, state=state, state_path=state_path)
            LOG.info("Scale-down step removed %d hosts", removed)
        else:
            state["idle_loops"] = idle_loops
            if action == "wait" and idle_loops:
                LOG.info("Low usage observed in loop %d of 2", idle_loops)
        write_scale_state(state_path, state)
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
