"""Maintain an online floor of cost-capped GitHub Actions runners."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace

from .autoscaling_policy import affordable_offers, resource_decision
from .resource_metrics import read_utilization
from .machine_profiles import MINIMUM_RAM_GIB, machine_profile, CpuOffer, platform_preferences, cpu_platform_satisfies
from .compute_api import get_auth
from .environment import load_env
from .job_recovery import JobRecovery
from .fleet_logging import lifecycle_log, observe_vms, reconcile_deletion_identities, vm_details
from .log_format import configure_logging, error_detail
from .vm_recovery import reconcile_lost_vms
from .price_guard import check_running_prices, delete_overpriced_vm
from .runner_drain import delete_vm_and_disks, is_repo_target, target_api_prefix
from .runner_labels import DEFAULT_RUNNER_LABELS
from .project_costs import log_project_costs
from .runner_fleet import GceVm, GitHubRunner, list_gcloud_vms, list_github_runners
from .spot_finder import find
from .storage_selection import storage_rates, cheapest_storage
from .billing_catalog import catalog_skus, clear_catalog_cache, monthly_to_daily
from .launch_candidates import LaunchInventory, candidate_key, launchable_candidates
from .spot_pricing import parse_component_rates
from .runner_tiers import tier_for_shape
from .target_config import DEFAULT_ORG, target_owns_vm_label

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger("spot_runner_autoscaler")
# Healthy hosts register all runners in 5-8 minutes; slower launches are treated as broken.
LAUNCH_DEADLINE_SECONDS = 600
# One check stops starting new launch attempts after this long so other fleet work still runs.
LAUNCH_SEARCH_BUDGET_SECONDS = 600
SPEND_LOG_INTERVAL_SECONDS = 3600


def now() -> float:
    return time.time()


def _active_target(args: Any) -> str:
    """Return configured target or fallback to org."""
    return getattr(args, "target", None) or getattr(args, "org", "swack-tools")


def tracked_job_repositories(args: Any) -> list[str]:
    """Return repositories whose jobs are tracked: explicit ``--job-repository`` values, else the
    repository target itself, else ``<org>/oxidex`` for an organization target."""
    explicit = getattr(args, "job_repository", None)
    if explicit:
        return list(explicit)
    target = _active_target(args)
    return [target] if is_repo_target(target) else [f"{target}/oxidex"]


def owns_vm(vm: GceVm, args: Any) -> bool:
    """Return whether this controller's GitHub target owns a runner host (via its GCE label)."""
    return target_owns_vm_label(getattr(vm, "target_label", ""), _active_target(args),
                                getattr(args, "org", None) or DEFAULT_ORG)


def scope_fleet_to_target(vms, args: Any, state: dict | None = None) -> tuple[GceVm, ...]:
    """Return only the hosts owned by this controller's target.

    GitHub inventory is scoped to one target, but GCE inventory lists every ``oxidex-runners-*``
    host in the project. Scaling, recovery, and rotation must never act on another target's
    hosts, whose runners are invisible to this controller's drain checks. When ``state`` is
    given, saved observations of live foreign hosts are forgotten so their absence from the
    scoped inventory is never mistaken for a loss.
    """
    owned = tuple(vm for vm in vms if owns_vm(vm, args))
    foreign = [vm for vm in vms if not owns_vm(vm, args)]
    if state is not None and foreign:
        forget_foreign_hosts(foreign, state)
    return owned


def forget_foreign_hosts(foreign, state: dict) -> None:
    """Drop controller state that refers to live hosts owned by another target."""
    def matches(record, vm):
        return (isinstance(record, dict) and record.get("name") == vm.name
                and record.get("zone") == vm.zone
                and (not record.get("instance_id") or not vm.instance_id
                     or record.get("instance_id") == vm.instance_id))
    observed = state.get("observed_vms")
    pending = state.get("pending_vm_recovery")
    operation = state.get("host_operation") or {}
    for vm in foreign:
        key = f"{vm.zone}/{vm.name}"
        if isinstance(observed, dict) and key in observed:
            observed.pop(key)
            LOG.warning("Forgetting host owned by another target (label %r): %s",
                        vm.target_label or "<none>", vm_details(vm))
        if isinstance(pending, dict):
            for pending_key in [k for k, record in pending.items()
                                if matches(record, vm) and not record.get("_cleanup_complete")
                                and operation.get("recovery_key") != k]:
                pending.pop(pending_key)
                LOG.warning("Dropping loss recovery for host owned by another target: %s", vm_details(vm))
    rotation = state.get("price_rotation")
    if isinstance(rotation, dict) and any(matches(rotation.get("outgoing_vm"), vm) for vm in foreign):
        # The outgoing host's runners are registered to another target, so this controller can
        # neither cordon nor drain them. Keep any replacement as ordinary owned capacity.
        LOG.error("Abandoning price rotation: outgoing host %s belongs to another target",
                  rotation["outgoing_vm"].get("name"))
        state.pop("price_rotation", None)


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


def _runner_group_memberships(api, org, exclude_group_id=None) -> dict:
    """Map runner id -> runner group id for an organization, from the runner-group APIs."""
    from .runner_drain import pages
    groups = pages(api(org, "GET", f"orgs/{org}/actions/runner-groups", paginate=True), "runner_groups")
    memberships: dict = {}
    for group in groups:
        group_id = group.get("id")
        if type(group_id) is not int or group_id == exclude_group_id:
            continue
        members = pages(api(org, "GET", f"orgs/{org}/actions/runner-groups/{group_id}/runners", paginate=True),
                        "runners")
        for member in members:
            runner_id = member.get("id")
            if type(runner_id) is not int:
                continue
            if memberships.get(runner_id, group_id) != group_id:
                raise RuntimeError(f"runner {runner_id} is reported in more than one runner group")
            memberships[runner_id] = group_id
    return memberships


def _missing_outgoing_runners(out_name, runners, vms, rotation):
    """Runners of an outgoing VM that is no longer in inventory, without touching other hosts.

    Prefer the identities recorded when the host was cordoned. Otherwise accept only runners named
    ``...-<out_name>-<n>`` whose longest matching VM name (among the remaining inventory and the
    outgoing name) is the outgoing host, so ``oxidex-runners-old`` never claims
    ``gha-oxidex-runners-old2-1``.
    """
    records = rotation.get("cordoned_runners") or {}
    if records:
        return [r for r in runners
                if str(r.runner_id) in records and records[str(r.runner_id)].get("name") == r.name]
    pattern = re.compile(r"-" + re.escape(out_name) + r"-\d+$")
    names = [vm.name for vm in vms] + [out_name]
    assigned = []
    for runner in runners:
        if not pattern.search(runner.name):
            continue
        matches = [name for name in names if name in runner.name]
        if max(matches, key=len) == out_name:
            assigned.append(runner)
    return assigned


def protected_heavy_routes(vms,runners,state):
    """Protect planned/live routes; label absence never clears a root-protected VM."""
    protected=state.get('protected_heavy_routes',{})
    if not isinstance(protected,dict) or any(
            not isinstance(identity,str) or not isinstance(row,dict)
            or set(row)!={'name','zone'} or not all(isinstance(row[key],str) and row[key]
                                                   for key in ('name','zone'))
            for identity,row in protected.items()):
        LOG.warning('Fleet changes held: heavy-route protection record is malformed')
        return [vm for vm in vms if vm.spot and vm.status!='TERMINATED'
                and vm.name.startswith('oxidex-runners-')]
    for vm in vms:
        if not (vm.running and vm.spot and vm.name.startswith('oxidex-runners-')
                and vm.instance_id):continue
        if any('spot-heavy32' in runner.labels for runner in host_runners(vm,runners,vms)):
            protected=state.setdefault('protected_heavy_routes',{})
            protected[vm.instance_id]={'name':vm.name,'zone':vm.zone}
    return [vm for vm in vms if vm.status!='TERMINATED' and any(
            row=={'name':vm.name,'zone':vm.zone} for row in protected.values())]


def heavy_route_capacity_hold(state):
    # Known loss permits identity-scoped cleanup, not a generic replacement
    # that would omit the protected route's admission policy and label.
    protected=state.get('protected_heavy_routes',{})
    return not isinstance(protected,dict) or bool(protected)


def runner_inventory_stale(state):
    # Pure legacy callers have no receipt; real controller polls always set it
    # immediately after the strict paginated API call, never from saved history.
    observed=state.get('runner_inventory_observed_at')
    return observed is not None and (type(observed) not in (int,float)
                                     or not 0<=time.time()-observed<=120)


def guard_inventory_fresh(state):
    if runner_inventory_stale(state):
        raise RuntimeError('Fleet mutation held: runner inventory expired')


def guard_fleet_mutation(state, vm=None, is_rotation=False):
    guard_inventory_fresh(state)
    protected = state.get('protected_heavy_routes', {})
    active_rotation = bool(state.get("price_rotation")) or is_rotation
    if vm is None:
        if not active_rotation and heavy_route_capacity_hold(state):
            raise RuntimeError('Automatic launch held: heavy route has no verified replacement')
    elif vm.status != 'TERMINATED':
        retiring_during_rotation = False
        if active_rotation:
            rotation = state.get("price_rotation", {})
            out_vm = rotation.get("outgoing_vm", {})
            repl_vm = rotation.get("replacement_vm", {})
            if out_vm.get("name") == vm.name and repl_vm and rotation.get("readiness", {}).get("runners_online"):
                retiring_during_rotation = True
        if not retiring_during_rotation and (not isinstance(protected, dict) or any(
                not isinstance(row, dict) or row == {'name': vm.name, 'zone': vm.zone}
                for row in protected.values())):
            raise RuntimeError('Retirement held: heavy route has no verified replacement')


def max_allowed_hosts(args: argparse.Namespace, state: dict) -> int:
    """Return maximum allowed hosts, granting +1 overlap allowance during make-before-break rotation."""
    base = getattr(args, "maximum_hosts", 5)
    if state.get("price_rotation") or state.get("migration"):
        return base + 1
    return base


def rotation_spend_allowance(state: dict) -> Decimal:
    """Return spend allowance granted to prevent temporary double billing during rotation overlap."""
    rotation = state.get("price_rotation")
    if not rotation:
        return Decimal(0)
    outgoing_cost = (
        rotation.get("outgoing_vm", {}).get("total_daily_usd", 0)
        or rotation.get("replacement_offer", {}).get("total_daily_usd", 0)
    )
    return Decimal(str(outgoing_cost or 0))


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
            pending += f" for {minutes}m {seconds}s of the {operation.get('deadline_seconds', LAUNCH_DEADLINE_SECONDS) // 60}m limit"
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


def runner_host_floor(fleet: list[GceVm], state: dict, minimum_runners: int) -> int:
    """Return the fewest hosts whose expected listener slots can still reach ``minimum_runners``.

    Each host contributes its recorded deployment slot count (``expected_slots``), so the floor
    follows the actual per-host listener density instead of assuming one runner per host. A
    fleet that cannot reach the runner floor at all keeps every host.
    """
    from .fleet_migration import expected_slots
    total = 0
    slots = sorted((expected_slots(vm, state) for vm in fleet), reverse=True)
    for hosts, count in enumerate(slots, 1):
        total += count
        if total >= minimum_runners:
            return hosts
    return max(1, len(slots))


def record_runner_floor(vms: list[GceVm], args: argparse.Namespace, state: dict) -> None:
    """Persist the runner floor derived from ``--minimum-runners`` for resource and legacy scale-down."""
    minimum = getattr(args, "minimum_runners", None)
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        return
    state["minimum_runners"] = minimum
    state["min_runner_floor"] = runner_host_floor(spot_instances(vms), state, minimum)


def removable_spot_vms(vms: list[GceVm], runners: list[GitHubRunner], state=None) -> list[GceVm]:
    """Choose complete, idle Spot VMs for scale-down.

    Removal must keep at least ``state['min_runner_floor']`` hosts and, when
    ``state['minimum_runners']`` is recorded, the remaining hosts' expected listener slots must
    still reach that runner count.
    """
    from .fleet_migration import expected_slots
    candidates = []
    fleet = spot_instances(vms)
    state = state if isinstance(state, dict) else {}
    min_floor = max(1, int(state.get("min_runner_floor", 1)))
    minimum_runners = int(state.get("minimum_runners", 0) or 0)
    total_slots = sum(expected_slots(vm, state) for vm in fleet)
    for vm in fleet:
        assigned = host_runners(vm, runners, fleet)
        names = {runner.name for runner in assigned}
        marker = f"-{vm.name}-"
        prefixes = {name.rsplit(marker, 1)[0] for name in names if marker in name}
        slots = expected_slots(vm, state)
        complete = any(prefix and {f"{prefix}-{vm.name}-{slot}" for slot in range(1, slots + 1)}.issubset(names)
                       for prefix in prefixes)
        if (not vm.instance_id or not complete
                or any(not runner.online or runner.busy for runner in assigned)):
            continue
        if total_slots - slots < minimum_runners:
            continue  # Removing this host would leave fewer listeners than --minimum-runners.
        candidates.append(vm)
    # Scale-down removes one host at a time, so the floor limits whether any removal is allowed,
    # not which candidate: keep them all so builder quiescence can pick a quiet host.
    return candidates if len(fleet) - 1 >= min_floor else []


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
                        f"{target_api_prefix(org)}/actions/runners/{runner.runner_id}"],
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
    if kind=='recovery':
        guard_inventory_fresh(state)
    else:
        protected_heavy_routes(vms,runners,state)
        guard_fleet_mutation(state,None if kind=='launch' else vm)
    if state.get("host_operation"):
        raise RuntimeError("A previous host operation is still pending")
    state["host_operation"] = {"kind": kind, "vm": asdict(vm),
                               "runner_ids": [runner.runner_id for runner in host_runners(vm, runners, vms)], **metadata}
    if kind == 'launch' and state.get('migration'):
        # Persist the migration/launch link in the same write as provider intent.
        state['migration']['awaiting_launch']=asdict(vm)
    if kind == "delete":
        key = f"{vm.zone}/{vm.name}"
        if key not in state.setdefault("intentional_deletions", []):
            state["intentional_deletions"].append(key)
        state.setdefault("intentional_deletion_ids", {})[key] = vm.instance_id
    write_scale_state(state_path, state)


class ReceiptProviderMismatch(RuntimeError):
    """An otherwise valid receipt belongs to a different current instance."""


def import_deployment_receipt(state, state_path,current=None):
    """Recover the provider identity even when a launch vanishes before inventory."""
    operation = state.get("host_operation", {})
    receipt = operation.get("deployment_receipt")
    if (operation.get("kind") != "launch" or operation.get("receipt_imported")
            or not receipt or not Path(receipt).exists()):
        return False
    from .fleet_logging import vm_from_record
    record = json.loads(Path(receipt).read_text())
    actual = vm_from_record(record)
    planned = vm_from_record(operation["vm"])
    if (not actual.instance_id or not actual.spot or actual.name != planned.name
            or actual.zone != planned.zone or actual.machine_type != planned.machine_type
            or (planned.instance_id and actual.instance_id != planned.instance_id)):
        raise RuntimeError("Deployment receipt does not match the pending host identity")
    if current is not None and (actual.instance_id!=current.instance_id
                                or actual.name!=current.name or actual.zone!=current.zone):
        raise ReceiptProviderMismatch('Deployment receipt differs from current provider identity')
    if record.get('tier'):
        tier = tier_for_shape(actual.vcpu,actual.ram_gib)
        if not tier or tier.name != record['tier'] or not 1 <= record.get('expected_runner_count',0) <= tier.maximum_slots:
            raise RuntimeError('Deployment receipt contains unsafe tier/slot settings')
        operation['expected_runner_count']=record['expected_runner_count']
        rotation=state.get('price_rotation')
        if isinstance(rotation,dict) and any(
                isinstance(row,dict) and row.get('name')==actual.name and row.get('zone')==actual.zone
                for row in (rotation.get('pending_replacement'),rotation.get('replacement_vm'))):
            rotation['expected_runner_count']=record['expected_runner_count']
            rotation['expected_runner_count_source']='deployment_receipt'
        state.setdefault('host_deployments',{})[f'{actual.zone}/{actual.instance_id}']={
            key:record.get(key) for key in ('tier','expected_runner_count','admission_work_gib','runner_memory_gib','price')}
    operation["vm"] = asdict(actual)
    awaiting=state.get('migration',{}).get('awaiting_launch',{})
    if awaiting.get('name')==actual.name and awaiting.get('zone')==actual.zone:
        state['migration']['awaiting_launch']=asdict(actual)
    operation["receipt_imported"] = True
    state.setdefault("observed_vms", {})[f"{actual.zone}/{actual.name}"] = asdict(actual)
    write_scale_state(state_path, state)
    return True


def reconcile_host_operation(vms, runners, args, state, state_path):
    """Hold all fleet changes until a fresh inventory confirms completion."""
    protected=protected_heavy_routes(vms,runners,state)
    operation = state.get("host_operation")
    if not operation:
        return False
    if runner_inventory_stale(state):
        LOG.warning('Host operation held: runner inventory is stale')
        return True
    from .fleet_logging import vm_from_record
    vm=vm_from_record(operation['vm'])
    current=next((item for item in vms if item.zone==vm.zone and item.name==vm.name),None)
    if current and vm.instance_id and current.instance_id!=vm.instance_id:
        LOG.error('Host operation waits: provider identity changed for %s',vm.name)
        return True
    protected_operation=any(item.name==vm.name and item.zone==vm.zone for item in protected)
    if protected_operation and operation.get('kind')!='launch':
        LOG.warning('Retirement held: live heavy route has no verified replacement')
        write_scale_state(state_path,state)
        return True
    try:
        import_deployment_receipt(state,state_path,current)
    except ReceiptProviderMismatch as exc:
        LOG.warning('Host receipt held: %s',error_detail(exc))
        return True
    vm=vm_from_record(operation['vm'])
    if current and vm.instance_id and current.instance_id!=vm.instance_id:
        LOG.warning('Host operation held: receipt identity differs from provider')
        return True
    if current and not vm.instance_id:
        operation["vm"] = asdict(current)
        awaiting=state.get('migration',{}).get('awaiting_launch',{})
        if awaiting.get('name')==current.name and awaiting.get('zone')==current.zone:
            state['migration']['awaiting_launch']=asdict(current)
        vm = current
        write_scale_state(state_path, state)
    assigned = host_runners(vm, runners, [vm, *[item for item in vms if item.name != vm.name]])
    if (operation["kind"] == "delete" and current and current.running
            and not operation.get("price_eviction") and not operation.get("drained")):
        operation.update(kind="drain", runner_names={str(r.runner_id): r.name for r in assigned},
                         drain_mode="label_only")
        operation.setdefault("target", _active_target(args))
        write_scale_state(state_path, state)
    if operation["kind"] == "drain":
        if (getattr(args, 'scaling_mode', 'legacy') in ('resource','tiered')
                and operation.get('drain_mode') == 'label_only'
                and not is_repo_target(operation.get('target') or _active_target(args))):
            operation.update(drain_mode='isolated_group', runners_isolated=False)
            operation.setdefault('drain_group_name', f'spot-drain-{vm.name}-{vm.instance_id}')
            write_scale_state(state_path, state)
        if current and current.running:
            from .fleet_migration import guard_staged_retirement
            if not guard_staged_retirement(vms,runners,args,state,state_path):
                return True
            try:
                from .runner_drain import drain_runners
                fresh = list_github_runners(_active_target(args))
                state['runner_inventory_observed_at']=time.time()
                protected_heavy_routes(vms,fresh,state)
                write_scale_state(state_path,state)
                guard_fleet_mutation(state,vm)
                assigned = host_runners(vm, fresh, [vm, *[item for item in vms if item.name != vm.name]])
                if drain_runners(operation, vm, args, state,
                                 lambda: write_scale_state(state_path, state), assigned,
                                 mutation_guard=lambda:guard_fleet_mutation(state,vm)):
                    if not quiesce_remote_builder(vm, args):
                        return True
                    if not guard_staged_retirement(vms,runners,args,state,state_path):
                        return True
                    def before_delete():
                        guard_fleet_mutation(state,vm)
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
        count = operation.get('expected_runner_count',8)
        expected = {f"gha-{vm.name}-{slot}" for slot in range(1, count+1)}
        online_names = {runner.name for runner in assigned if runner.online}
        requested = operation.get('requested_cpu_platform')
        platform_ok = not requested or (current is not None and cpu_platform_satisfies(
            current.machine_type, current.cpu_platform, requested))
        if current and current.running and current.cpu_platform and not platform_ok:
            operation.setdefault("abandon", f"observed CPU platform {current.cpu_platform} "
                                            f"does not satisfy requested minimum {requested}")
        elif current and current.running and platform_ok and expected.issubset(online_names):
            LOG.info("Host operation complete: all %d runners registered for %s",count,vm_details(current))
            state.pop("host_operation")
            write_scale_state(state_path, state)
            return True
        # A manager-confirmed host keeps its jobs even if one runner briefly restarts later.
        deadline = operation.get('deadline_seconds', LAUNCH_DEADLINE_SECONDS)
        if not operation.get("registered") and now() - operation["started_at"] >= deadline:
            operation.setdefault("abandon", f"all {count} runners did not register within "
                                            f"{deadline // 60} minutes")
        if operation.get("abandon"):
            if protected_operation:
                LOG.warning('Launch abandonment held: running host carries a heavy route')
                write_scale_state(state_path,state)
                return True
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
            LOG.info("Fleet scaling waits for %d online runner slots on %s",count,vm_details(vm))
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
                guard_fleet_mutation(state,current)
                delete_overpriced_vm(current, args.project)
            else:
                if operation.get("drained"):
                    from .fleet_migration import guard_staged_retirement
                    if not guard_staged_retirement(vms,runners,args,state,state_path):
                        return True
                    fresh = list_github_runners(_active_target(args))
                    state['runner_inventory_observed_at']=time.time()
                    protected_heavy_routes(vms,fresh,state)
                    guard_fleet_mutation(state,vm)
                    if host_runners(vm, fresh, [vm, *[item for item in vms if item.name != vm.name]]):
                        LOG.warning("Deletion waits: runner records reappeared for %s", vm_details(vm))
                        return True
                    delete_drained_vm(vm, args,lambda:guard_fleet_mutation(state,vm))
                else:
                    terminate_spot_vm(current, args.project, operation.get('target') or _active_target(args),
                                      runners, vms,lambda:guard_fleet_mutation(state,current))
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
                subprocess.run(["gh", "api", "-X", "DELETE",
                                f"{target_api_prefix(operation.get('target') or _active_target(args))}/actions/runners/{runner.runner_id}"],
                               check=True, text=True, capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError) as exc:
                LOG.warning("Deregistration pending for %s: %s", runner.name, error_detail(exc))
                break
        return True  # Require another inventory proving the records are absent.
    from .fleet_migration import confirm_migration_source_drives
    try:
        if not confirm_migration_source_drives(operation,args,state):
            LOG.warning("Migration completion waits for recorded disk absence: %s", vm_details(vm))
            return True
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        LOG.warning("Migration source disk verification remains pending: %s", error_detail(exc))
        return True
    if operation.get("drain_group_id"):
        from .runner_drain import cleanup_group
        try:
            cleanup_group(operation, _active_target(args),mutation_guard=lambda:guard_inventory_fresh(state))
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            LOG.warning("Drain group cleanup remains pending: %s", error_detail(exc))
            return True
    if operation["kind"] == "recovery":
        record=state["pending_vm_recovery"][operation["recovery_key"]]
        record["_cleanup_complete"] = True
        if operation.get('migration_source_disks_absent') is True:
            record['_disks_absent']=True
    from .fleet_migration import record_retirement_completion
    record_retirement_completion(operation,state)
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
    tiered=getattr(args,'scaling_mode',None)=='tiered'
    observed = subprocess.run(["gcloud", "compute", "instances", "describe", vm.name,
                               f"--zone={vm.zone}", f"--project={args.project}",
                               '--format=json(id,disks)' if tiered else '--format=value(id)'],
                              check=True, text=True, capture_output=True, timeout=60)
    inventory=json.loads(observed.stdout) if tiered else {}
    identity=str(inventory.get('id','')) if tiered else observed.stdout.strip()
    if not vm.instance_id or identity != vm.instance_id:
        raise RuntimeError("Refusing drained deletion: provider instance identity changed")
    if tiered:
        recorded={disk.get('source'):str(disk.get('diskId','')) for disk in vm.disks}
        attached={disk.get('source') for disk in inventory.get('disks',[])}
        if not recorded or None in recorded or attached!=set(recorded) or not all(recorded.values()):
            raise RuntimeError('Refusing drained deletion: attached disk identity changed or unknown')
        result=subprocess.run(['gcloud','compute','disks','list',f'--project={args.project}',
                               '--format=json(selfLink,id)'],check=True,text=True,capture_output=True,timeout=60)
        disks={row['selfLink']:str(row['id']) for row in json.loads(result.stdout)}
        if any(disks.get(source)!=disk_id for source,disk_id in recorded.items()):
            raise RuntimeError('Refusing drained deletion: provider disk identity changed')
    if before_delete is not None:
        before_delete()
    subprocess.run(["gcloud", "compute", "instances", "delete", vm.name,
                    f"--zone={vm.zone}", f"--project={args.project}", "--delete-disks=all", "--quiet"],
                   check=True, text=True, capture_output=True, timeout=180)
    LOG.info("Deleted drained Spot VM %s and its attached disks", vm_details(vm))


def quiesce_remote_builder(vm, args, token=None):
    """Hold new builds off a host only after its running builds have finished.

    With ``token`` the drain marker records its owner (only when no drain marker exists yet), so
    :func:`release_remote_builder` can later remove exactly the drain this caller placed.
    """
    if not vm.instance_id:return False
    from . import host_admission
    # The source is embedded here because older supported hosts may not have
    # the latest admission CLI (or any installed admission module). Execute
    # only its definitions and reconciler under the inherited global EX lock.
    source = Path(host_admission.__file__).read_text()
    code = ("namespace={'__name__':'oxidex_drain'}\n"
            "exec("+repr(source)+",namespace)\n"
            "raise SystemExit(75 if namespace['builders_running'](global_lock_held=True) else 0)\n")
    script = ('test "$(curl -fsS -H Metadata-Flavor:Google http://metadata.google.internal/computeMetadata/v1/instance/id)" = '
              +shlex.quote(vm.instance_id)+' || exit 75; '
              '/usr/bin/python3 -I -c '+shlex.quote(code)+
              ' || exit 75; '
              + ('touch /run/oxidex-build-draining' if token is None else
                 'test -e /run/oxidex-build-draining || printf %s '+shlex.quote(token)+' > /run/oxidex-build-draining'))
    command = 'sudo flock -n -E 75 /run/oxidex-build.lock sh -c ' + shlex.quote(script)
    try:
        result = subprocess.run(['gcloud', 'compute', 'ssh', vm.name, f'--zone={vm.zone}',
                                 f'--project={args.project}', '--quiet', '--command='+command],
                                text=True, capture_output=True, timeout=60)
        if result.returncode:
            LOG.info('Drain waits: remote builder active or inaccessible on %s', vm_details(vm))
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def release_remote_builder(vm, args, token) -> bool:
    """Remove a builder drain marker this caller placed (matched by ``token``), under the build lock.

    Leaves a marker owned by anyone else in place. Returns True when no marker with this token
    remains on the expected instance.
    """
    if not vm.instance_id or not token:
        return False
    script = ('test "$(curl -fsS -H Metadata-Flavor:Google http://metadata.google.internal/computeMetadata/v1/instance/id)" = '
              + shlex.quote(vm.instance_id) + ' || exit 75; '
              'if [ -e /run/oxidex-build-draining ] && [ "$(cat /run/oxidex-build-draining)" = '
              + shlex.quote(token) + ' ]; then rm -f /run/oxidex-build-draining; fi')
    command = 'sudo flock -w 30 -E 75 /run/oxidex-build.lock sh -c ' + shlex.quote(script)
    try:
        result = subprocess.run(['gcloud', 'compute', 'ssh', vm.name, f'--zone={vm.zone}',
                                 f'--project={args.project}', '--quiet', '--command='+command],
                                text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _rotation_drain_token(rotation) -> str:
    return "rotation-" + str((rotation.get("outgoing_vm") or {}).get("instance_id") or "unknown")


def _rotation_needs_host_cordon(rotation, target_type) -> bool:
    """Whether retirement must first see an idle host job-start cordon.

    Every repository rotation does (its runners keep their built-in labels and have no runner groups). An org
    rotation does unless its runners were moved into an isolated runner group: ``host`` (group setup failed, so the
    host was cordoned) and ``labels`` (a label-only cordon recorded before that fallback existed) both leave the
    built-in labels schedulable.
    """
    return target_type == "repo" or rotation.get("cordon_mode") in ("host", "labels")


def _rotation_host_cordon(out_vm, assigned, args, state, state_path, rotation) -> str:
    """Ensure the outgoing repository host refuses new jobs at start; return its cordon state.

    The owner token is persisted BEFORE the marker can exist, so recovery can always release exactly the
    marker this rotation placed. Only ``idle`` (cordon already in place, no job on the host) is proof that
    deregistration cannot strand a job; ``placed`` must be observed again.
    """
    from .runner_drain import cordon_runner_host, host_cordon_token, log_cordon_hold
    if not rotation.get("host_cordon_token"):
        rotation["host_cordon_token"] = host_cordon_token("rotation-cordon", rotation["outgoing_vm"])
        write_scale_state(state_path, state)
    names = [runner.name for runner in assigned]
    if not names:
        # No listener of this host is registered, so none can receive a job; deregistration has nothing to do.
        LOG.info("Outgoing host %s has no registered listeners; no job-start cordon is needed", out_vm.name)
        return "idle"
    try:
        status, detail = cordon_runner_host(out_vm, args, rotation["host_cordon_token"], names,
                                            lambda: guard_fleet_mutation(state, out_vm, is_rotation=True))
    except RuntimeError as exc:
        status, detail = "held", {"reason": error_detail(exc)}
    if status != "idle":
        log_cordon_hold(out_vm, status, detail)
    return status


def builder_ready(vm,args):
    """Run a bounded builder canary with the exact installed launcher revision."""
    import hashlib
    from .builder_assets import HOST_LAUNCHER
    digest=hashlib.sha256(HOST_LAUNCHER.encode()).hexdigest()
    setup = """from pathlib import Path
p=Path('/mnt/runner-data/remote-build/sources/admission-canary')
(p/'rust-toolchain.toml').write_text('[toolchain]\\nchannel = "1.97.1"\\n')
(p/'Cargo.toml').write_text('[package]\\nname="oxidex-admission-canary"\\nversion="0.1.0"\\nedition="2021"\\n')
(p/'Cargo.lock').write_text('version = 4\\n\\n[[package]]\\nname="oxidex-admission-canary"\\nversion="0.1.0"\\n')
(p/'src').mkdir(exist_ok=True)
(p/'src'/'main.rs').write_text('fn main() { println!("builder-ready"); }\\n')
"""
    command=(f'test -S /run/gha-admission/socket && test -f /etc/oxidex-remote-build.json && '
        f'test "$(sha256sum /usr/local/bin/oxidex-remote-build | cut -d" " -f1)" = {digest} && '
        'test ! -e /run/oxidex-build-draining && '
        'test "$(command -v docker)" = /usr/local/bin/docker && '
        'docker image inspect oxidex-remote-builder:1.97.1 >/dev/null && '
        '/usr/local/bin/oxidex-remote-build admission-canary prepare >/dev/null && '
        'python3 -c '+shlex.quote(setup)+' && '
        '/usr/local/bin/oxidex-remote-build admission-canary cargo run --locked >/dev/null')
    result=subprocess.run(['gcloud','compute','ssh',vm.name,f'--zone={vm.zone}',
        f'--project={args.project}','--quiet','--command=sudo sh -c '+shlex.quote(command)],
        capture_output=True,text=True,timeout=120)
    return result.returncode==0


def adjust_admission(vm,args,kind='runner'):
    """Raise future reservations, then update only lease-free container caps."""
    if kind not in ('runner','builder') or not vm.instance_id:return False
    program='kind='+repr(kind)+'\n'+'''import fcntl, json, math, os, re, subprocess, sys, time
from pathlib import Path
sys.path.insert(0,'/opt/gha-runners')
from host_admission import observed_envelope, request
GIB=1024**3
def heavy_runner_policy(config):
    """An absent policy preserves the ordinary 15-GiB deployment."""
    if 'heavy_runners' in config:
        names=config['heavy_runners']
        if ('heavy_runner' in config or not isinstance(names,list) or not 2<=len(names)<=3
            or any(not isinstance(name,str) or not name for name in names)
            or len(set(names))!=len(names) or not isinstance(config.get('runner_names'),list)
            or any(not isinstance(name,str) or not name for name in config['runner_names'])
            or len(set(config['runner_names']))!=len(config['runner_names'])
            or set(names)!=set(config['runner_names']) or config.get('role') not in (None,'runner')
            or config.get('runner_gib')!=32 or type(config.get('runner_gib'))is bool
            or type(config.get('work_gib'))not in (int,float)
            or not math.isfinite(config['work_gib']) or config['work_gib']!=32*len(names)):
            raise ValueError('Invalid strict heavy runner pool')
        return dict(names=list(names),memory_gib=32,cpus=8)
    if 'heavy_runner' not in config:return None
    policy=config['heavy_runner']
    if (not isinstance(policy,dict) or set(policy)!={'name','memory_gib','cpus'}
        or type(policy['name']) is not str or not policy['name']
        or type(policy['memory_gib']) is not int or policy['memory_gib']!=32
        or type(policy['cpus']) is not int or policy['cpus']!=8
        or policy['name'] not in config['runner_names']
        or type(config['runner_gib']) not in (int,float)
        or not math.isfinite(config['runner_gib']) or not 0<config['runner_gib']<32
        or type(config['work_gib']) not in (int,float)
        or not math.isfinite(config['work_gib']) or config['work_gib']<32):
        raise ValueError('Invalid single heavy runner policy')
    return policy


changed=[]
pending_runner_ids=[]
ledger_deadline=None
envelope=next_limit=reserved=None
def docker_timeout():
    if ledger_deadline is None:return 5
    remaining=ledger_deadline-time.monotonic()
    if remaining<=0:raise RuntimeError('Budget update slice exhausted; remaining caps deferred')
    return min(5,remaining)
def write_config(path,value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w') as target:
        json.dump(value,target);target.flush();os.fsync(target.fileno())
    temporary.replace(path)
def inspect(identity,name=None):
    rows=json.loads(subprocess.run(['/usr/bin/docker','inspect',identity],
        check=True,capture_output=True,text=True,timeout=docker_timeout()).stdout)
    if not isinstance(rows,list) or len(rows)!=1:raise ValueError('Incomplete runner inventory')
    row=rows[0]
    if (not re.fullmatch('[0-9a-f]{64}',row['Id']) or row['State']['Running'] is not True
        or not row['State']['StartedAt'] or (name is not None and row['Name']!='/'+name)
        or (name is None and row['Id']!=identity)):
        raise ValueError('Runner identity changed')
    if not isinstance(row['HostConfig']['Memory'],int) or row['HostConfig']['Memory']<=0:
        raise ValueError('Runner memory must have an existing finite cap')
    return row
try:
    # Serialize budget changes independently of the lease lock. The service
    # must remain available to finish hooks and to process the config barrier.
    fd=os.open('/run/gha-admission-adjust.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'a') as operation:
        fcntl.flock(operation,fcntl.LOCK_EX|fcntl.LOCK_NB)
        p=Path('/etc/gha-admission.json');config=json.loads(p.read_text())
        heavy=heavy_runner_policy(config)
        if heavy is not None and kind=='builder':
            raise ValueError('Builder cap adjustment held while heavy runner policy is active')
        current=config[kind+'_gib']
        if not math.isfinite(current) or current<=0:raise ValueError('Invalid reservation')
        envelope=observed_envelope(config)
        if not math.isfinite(envelope) or envelope<=0:raise ValueError('Unknown memory envelope')
        ceiling=int(min(config['work_gib'],envelope)*.95)
        builder=Path('/etc/oxidex-remote-build.json')
        if kind=='builder':
            limits=json.loads(builder.read_text())
            existing=limits['memory_gib']
            if not math.isfinite(existing) or existing<=0 or existing>current:
                raise ValueError('Builder limit exceeds its reservation')
            lagging=existing<current
        else:
            names=config['runner_names']
            if not isinstance(names,list) or not names or len(set(names))!=len(names):
                raise ValueError('Invalid runner deployment inventory')
            records={name:inspect(name,name) for name in names}
            if len({row['Id'] for row in records.values()})!=len(names):
                raise ValueError('Runner identities are not unique')
            heavy_names=heavy.get('names',[heavy.get('name')]) if heavy else []
            for name in heavy_names:
                cap=records[name]['HostConfig']
                if (cap['Memory']!=32*GIB or cap['MemorySwap']!=32*GIB
                    or cap['NanoCpus']!=8_000_000_000):
                    raise ValueError('Heavy runner cap differs from policy')
            ordinary={name:row for name,row in records.items() if name not in heavy_names}
            if any(row['HostConfig']['Memory']>current*GIB for row in ordinary.values()):
                raise ValueError('Runner limit exceeds its reservation')
            lagging=any(row['HostConfig']['Memory']<current*GIB for row in ordinary.values())
        next_limit=current if lagging or heavy is not None else min(current*2,ceiling)
        if next_limit>ceiling or (not lagging and next_limit<=current):
            print(json.dumps(dict(kind=kind,status='capacity',reservation_gib=current,observed_work_gib=envelope)))
            raise SystemExit(76)
        if next_limit>current:
            config[kind+'_gib']=next_limit
            write_config(p,config)
        # serve() is single-threaded and reads config before locking leases.
        # Its root release reply guarantees every old-config request has
        # finished. New requests reserve the higher amount before any cap rises.
        if not request('release','builder'):raise RuntimeError('Admission barrier rejected')
        leases=Path('/var/lib/gha-admission/leases.json')
        fd=os.open(str(leases)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as ledger_lock:
            fcntl.flock(ledger_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            # Hooks wait at most30 seconds. Bound this critical section so a
            # large deployment cannot strand releases while Docker is slow.
            ledger_deadline=time.monotonic()+10
            if json.loads(p.read_text())!=config:raise ValueError('Admission config changed')
            rows=json.loads(leases.read_text()) if leases.exists() else {}
            if not isinstance(rows,dict) or any(not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0 for value in rows.values()):
                raise ValueError('Invalid reservation ledger')
            reserved=sum(rows.values())
            if kind=='builder':
                if json.loads(builder.read_text())!=limits:raise ValueError('Builder config changed')
                limits['memory_gib']=next_limit;write_config(builder,limits)
                changed.append('future-builder')
            else:
                for name,before in records.items():
                    if heavy is not None and name in heavy_names:
                        continue  # Never rewrite the dedicated slot to ordinary caps.
                    identity=before['Id']
                    if any(owner.startswith(prefix+identity+':')
                           for owner in rows for prefix in ('container:','runner:')):
                        if before['HostConfig']['Memory']<next_limit*GIB:
                            pending_runner_ids.append(identity)
                        continue  # Preserve active/stale lease amounts and caps.
                    row=inspect(identity,name)
                    if (row['Id']!=identity or row['State']['StartedAt']!=before['State']['StartedAt']
                        or row['HostConfig']['Memory']!=before['HostConfig']['Memory']):
                        raise ValueError('Runner changed during budget update')
                    if row['HostConfig']['Memory']>=next_limit*GIB:continue
                    subprocess.run(['/usr/bin/docker','update','--memory='+str(next_limit)+'g',
                        '--memory-swap='+str(next_limit)+'g',identity],check=True,capture_output=True,timeout=docker_timeout())
                    after=inspect(identity,name)
                    if (after['Id']!=identity or after['State']['StartedAt']!=before['State']['StartedAt']
                        or after['HostConfig']['Memory']!=next_limit*GIB
                        or after['HostConfig']['MemorySwap']!=next_limit*GIB):
                        raise ValueError('Runner limit update was not observed')
                    changed.append(identity)
            print(json.dumps(dict(kind=kind,status='waiting-for-lease-free-runner' if pending_runner_ids or not changed else 'updated',
                reservation_gib=next_limit,observed_work_gib=envelope,
                reserved_gib=reserved,unreserved_gib=max(0,envelope-reserved),updated=changed,pending_runner_ids=pending_runner_ids)))
            # Success clears the controller's already-seen OOM obligation.
            # Keep it pending until every skipped lagging cap has caught up.
            if pending_runner_ids or not changed:raise SystemExit(75)
except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError) as error:
    # Never roll back a higher reservation after a potentially applied Docker
    # update. Unknown/partial updates stay over-reserved for safe retries.
    print(json.dumps(dict(kind=kind,status='deferred',updated=changed,pending_runner_ids=pending_runner_ids,error=str(error)[:200],
        reservation_gib=next_limit,observed_work_gib=envelope,reserved_gib=reserved,
        unreserved_gib=max(0,envelope-reserved) if reserved is not None else None)))
    raise SystemExit(75)
'''
    shell=('test "$(curl -fsS -H Metadata-Flavor:Google http://metadata.google.internal/computeMetadata/v1/instance/id)" = '
           +shlex.quote(vm.instance_id)+' || exit 75; '
           'python3 -c '+shlex.quote(program))
    try:
        result=subprocess.run(['gcloud','compute','ssh',vm.name,f'--zone={vm.zone}',
            f'--project={args.project}','--quiet','--command=sudo sh -c '+shlex.quote(shell)],
            capture_output=True,text=True,timeout=60)
    except (OSError,subprocess.SubprocessError):return False
    if getattr(result,'stdout','').strip():LOG.info('Admission budget update on %s: %s',vm_details(vm),result.stdout.strip())
    return 'capacity' if result.returncode==76 else result.returncode==0


def scale_down(vms: list[GceVm], runners: list[GitHubRunner], args: argparse.Namespace,
               state: dict | None = None, state_path: Path | None = None) -> int:
    """Drain one idle host, then wait for CI completion and deregistration before deletion."""
    state = state if state is not None else read_scale_state(args.state_file.expanduser())
    state_path = state_path or args.state_file.expanduser()
    if state.get("host_operation") or len(spot_instances(vms)) <= 1:
        return 0
    record_runner_floor(vms, args, state)
    candidates = removable_spot_vms(vms, runners, state)
    if not candidates:
        LOG.info("Scale-down waits: no complete idle host can be removed without dropping below "
                 "%s runners (%s-host floor)", state.get("minimum_runners", 1), state.get("min_runner_floor", 1))
        return 0
    # Existing builder presence is independent of future installation settings.
    vm = next((vm for vm in candidates if quiesce_remote_builder(vm, args)), None)
    if vm is None:
        return 0
    assigned = host_runners(vm, runners, vms)
    begin_host_operation("drain", vm, runners, vms, state, state_path,
                         target=_active_target(args),
                         runner_names={str(runner.runner_id): runner.name for runner in assigned},
                         drain_group_name=f'spot-drain-{vm.name}-{vm.instance_id}',
                         drain_mode=('isolated_group' if getattr(args, 'scaling_mode', 'legacy') == 'resource'
                                     else 'label_only'))
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


BOOT_TIMEOUT_SECONDS = 600
BOOT_HYDRATE_TIMEOUT_SECONDS = BOOT_TIMEOUT_SECONDS  # Legacy name kept for callers and saved docs.
READINESS_DEADLINE_SECONDS = 900
DRAIN_DEADLINE_SECONDS = 3600

# Runner-host rotation never touches the dashboard: the dashboard lives only on control VMs
# (``lib.control_vm``), which own health probes, hydration, and the Cloudflare origin cutover.
RUNNER_ROTATION_PHASES = ("select", "provision", "boot", "verify_readiness", "cordon_drain",
                          "deregister", "delete")
# Phases written by the earlier dashboard-coupled rotation map onto the runner-only sequence so a
# saved transaction resumes without probing a dashboard or writing the edge origin.
LEGACY_ROTATION_PHASES = {"boot_hydrate": "boot", "origin_cutover": "cordon_drain"}
# Phases that act on the outgoing host; each step first requires fresh replacement evidence.
RETIREMENT_PHASES = ("cordon_drain", "deregister", "delete")
# Controller-launched org hosts register into runner_manager's default ``--runner-group-id``.
DEFAULT_RUNNER_GROUP_ID = 10


def _offer_record(offer) -> dict:
    return {
        "machine_type": offer.machine_type,
        "zone": getattr(offer, "zone", "") or getattr(offer, "region", ""),
        "region": offer.region,
        "vcpu": offer.vcpu,
        "ram_gib": float(offer.ram_gib),
        "hourly_usd": float(getattr(offer, "hourly_usd", float(offer.total_daily_usd) / 24)),
        "total_daily_usd": float(offer.total_daily_usd),
        "cpu_platform": getattr(offer, "cpu_platform", ""),
    }


def _advance_rotation(rotation: dict, phase: str, state: dict, state_path: Path) -> None:
    timestamp = now()
    rotation["phase"] = phase
    rotation["phase_started_at"] = timestamp
    rotation["updated_at"] = timestamp
    write_scale_state(state_path, state)


def reconcile_prices(vms, runners, args, state, state_path) -> bool:
    """Execute phased make-before-break price rotation or restore deficit hosts."""
    if "price_rotation" in state:
        return step_price_rotation(vms, runners, args, state, state_path)

    if state.get("host_operation"):
        return True

    protected_heavy_routes(vms, runners, state)
    if heavy_route_capacity_hold(state) or runner_inventory_stale(state):
        return True

    fleet = spot_instances(vms)
    overpriced = check_running_prices(fleet, args.project, args.max_daily_usd)
    use_rotation = (
        getattr(args, "make_before_break", False)
        or getattr(args, "target", None) is not None
        or getattr(args, "price_rotation", False)
    )
    if overpriced:
        # A patched host this controller cannot relaunch with its patch is skipped on both paths, not
        # allowed to stall the whole price path: restoration and normal scaling must keep running.
        # Delete-first eviction needs the hold as much as rotation does: its replacement comes from
        # scale_up, which would launch without the host's patch.
        replaceable = []
        for candidate in overpriced:
            why = patched_rotation_hold(candidate, args)
            if why:
                LOG.warning("%s held for %s: %s; keeping it running over the cap",
                            "Make-before-break rotation" if use_rotation else "Delete-first price eviction",
                            vm_details(candidate), why)
            else:
                replaceable.append(candidate)
        overpriced = replaceable
    if overpriced:
        vm = overpriced[0]
        if use_rotation:
            LOG.info("Price exceeds cap for %s ($%s cap); initiating make-before-break rotation",
                     vm_details(vm), args.max_daily_usd)
            return begin_price_rotation(vm, vms, runners, args, state, state_path, reason="price_cap_exceeded")

        state["price_replacement_target"] = max(state.get("price_replacement_target", 0), len(fleet))
        state["idle_loops"] = 0
        guard_fleet_mutation(state, vm)
        begin_host_operation("delete", vm, runners, vms, state, state_path, price_eviction=True)
        LOG.warning("Price exceeds cap: deleting %s with its disks, including any active jobs", vm_details(vm))
        try:
            guard_fleet_mutation(state, vm)
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
        if pending:
            # Completed cleanups with no restoration target (e.g. rotation-owned replacement
            # losses) must not stay pending, or lost-host recovery would block scaling forever.
            retire_completed_recoveries(state, state_path)
        return False
    missing = max(0, target - len(fleet))
    if missing:
        LOG.info("Finding one affordable replacement; %d missing hosts, restoration target %d", missing, target)
        refresh = offer_refresher(args)
        offers = refresh()
        if not offers or not scale_up(1, offers, args, state, state_path, refresh=refresh):
            LOG.warning("Replacement capacity is pending; retrying next loop")
        return True
    for record in pending.values():
        state.get("observed_vms", {}).pop(f"{record['zone']}/{record['name']}", None)
    state["pending_vm_recovery"] = {}
    state["price_replacement_target"] = 0
    state["idle_loops"] = 0
    write_scale_state(state_path, state)
    return True


def retire_completed_recoveries(state, state_path, only=None) -> None:
    """Remove recovery records whose cleanup completed; ``only`` filters which records."""
    pending = state.get("pending_vm_recovery", {})
    done = [key for key, record in pending.items()
            if record.get("_cleanup_complete") and (only is None or only(record))]
    for key in done:
        record = pending.pop(key)
        state.get("observed_vms", {}).pop(f"{record.get('zone')}/{record.get('name')}", None)
    if done:
        LOG.info("Retired %d completed lost-host recovery records", len(done))
        write_scale_state(state_path, state)


def _patch_recorded(value) -> bool:
    """A recorded patch sha (or "invalid"), as opposed to absent or the NO_PATCH sentinel."""
    from .runner_fleet import NO_PATCH
    return bool(value) and str(value).lower() != NO_PATCH


def patched_rotation_hold(outgoing_vm: GceVm, args: argparse.Namespace) -> str:
    """Why a patched host cannot be rotated automatically, or "" when it can.

    A replacement launched without the outgoing host's runner patch would lack its packages,
    files, mounts and extra labels, yet still pass the listener readiness gate, so the patched
    host would be retired for an unpatched one. Rotation needs this controller to launch with the
    same patch source (--patch-repo) the host was recorded with.
    """
    # patch_target alone is recorded for every repository target, patched or not, and "none" is
    # the explicit no-patch record: neither means the host runs a patch.
    patched = (_patch_recorded(getattr(outgoing_vm, "patch_sha256", ""))
               or _patch_recorded(getattr(outgoing_vm, "patch_pending_sha256", "")))
    if not patched:
        return ""
    patch_repo = getattr(args, "patch_repo", None)
    if not patch_repo:
        return "the host runs a runner patch but this controller has no --patch-repo to give its replacement"
    recorded = getattr(outgoing_vm, "patch_target", "")
    if recorded and recorded.lower() != patch_repo.lower():
        return f"the host's patch comes from {recorded}, not this controller's --patch-repo {patch_repo}"
    return ""


def begin_price_rotation(outgoing_vm: GceVm, vms: list[GceVm], runners: list[GitHubRunner],
                         args: argparse.Namespace, state: dict, state_path: Path,
                         reason: str = "price_cap_exceeded", offer=None) -> bool:
    """Initialize a make-before-break runner-capacity rotation transaction.

    The outgoing host keeps running jobs until the replacement registers its runners; only then
    is the outgoing host cordoned, drained, deregistered, and deleted.
    """
    target_name = getattr(args, "target", None) or getattr(args, "org", "swack-tools")
    target_type = "repo" if ("/" in target_name or getattr(args, "target_type", "") == "repo") else "org"
    cordon_mode = "labels" if target_type == "repo" else "isolated_group"

    held = patched_rotation_hold(outgoing_vm, args)
    if held:
        # Defensive: reconcile_prices already skips held hosts. No price work happened.
        LOG.warning("Make-before-break rotation held for %s: %s; keeping running host undisturbed",
                    vm_details(outgoing_vm), held)
        return False

    if offer is None:
        refresh = offer_refresher(args)
        offers = refresh()
        if not offers:
            LOG.warning("Make-before-break rotation deferred for %s: no qualifying replacement offer available below $%s/day; keeping running host undisturbed",
                        vm_details(outgoing_vm), args.max_daily_usd)
            return True
        offer = offers[0]

    timestamp = now()
    state["price_rotation"] = {
        "phase": "provision",
        "target": target_name,
        "target_type": target_type,
        "reason": reason,
        "outgoing_vm": {
            "name": outgoing_vm.name,
            "zone": outgoing_vm.zone,
            "instance_id": outgoing_vm.instance_id,
            "machine_type": outgoing_vm.machine_type,
            "vcpu": outgoing_vm.vcpu,
            "ram_gib": outgoing_vm.ram_gib,
            "total_daily_usd": float(getattr(offer, "total_daily_usd", 0) or 0)
        },
        "replacement_offer": _offer_record(offer),
        # Set only from the name this rotation's own launch persisted; never inferred from inventory.
        "pending_replacement": None,
        "replacement_vm": None,
        "expected_runner_count": launch_slot_count(offer, args),
        "expected_runner_count_source": "launch_sizing",
        "readiness": {"runners_online": False},
        "cordon_mode": cordon_mode,
        "cordon_complete": False,
        "drain_started_at": None,
        "phase_started_at": timestamp,
        "started_at": timestamp,
        "updated_at": timestamp,
        "attempt_count": 0,
        "error": None
    }
    write_scale_state(state_path, state)
    LOG.info("Initiated make-before-break rotation: %s -> replacement %s in %s ($%.2f/day)",
             outgoing_vm.name, offer.machine_type, getattr(offer, "zone", offer.region), float(offer.total_daily_usd))
    return step_price_rotation(vms, runners, args, state, state_path)


def step_price_rotation(vms: list[GceVm], runners: list[GitHubRunner],
                        args: argparse.Namespace, state: dict, state_path: Path) -> bool:
    """Step the runner-only make-before-break rotation state machine."""
    rotation = state.get("price_rotation")
    if not rotation:
        return False

    phase = rotation.get("phase", "select")
    if phase in LEGACY_ROTATION_PHASES:
        mapped = LEGACY_ROTATION_PHASES[phase]
        LOG.warning("Resuming saved rotation phase %s as runner-only phase %s; runner rotations never "
                    "probe the dashboard or change the edge origin", phase, mapped)
        for key in ("origin_switched",):
            rotation.pop(key, None)
        rotation["readiness"] = {"runners_online": bool(rotation.get("readiness", {}).get("runners_online"))}
        _advance_rotation(rotation, mapped, state, state_path)
        phase = mapped
    LOG.debug("Stepping make-before-break rotation for %s: phase=%s", rotation["outgoing_vm"]["name"], phase)

    if rotation.get("recovering") or phase in RETIREMENT_PHASES:
        # Saved readiness is history; a preempted replacement must stop retirement every step.
        failure = rotation.get("recovering") or _replacement_live_failure(vms, runners, rotation)
        if failure:
            return _recover_failed_replacement(vms, runners, args, state, state_path, rotation, failure)

    steps = {
        "select": _step_rotation_select,
        "provision": _step_rotation_provision,
        "boot": _step_rotation_boot,
        "verify_readiness": _step_rotation_verify_readiness,
        "cordon_drain": _step_rotation_cordon_drain,
        "deregister": _step_rotation_deregister,
        "delete": _step_rotation_delete,
    }
    step = steps.get(phase)
    if step is None:
        LOG.error("Unknown price rotation phase: %s; resetting transaction", phase)
        state.pop("price_rotation", None)
        write_scale_state(state_path, state)
        return True
    return step(vms, runners, args, state, state_path, rotation)


def _step_rotation_select(vms, runners, args, state, state_path, rotation):
    """Phase 1: Select replacement Spot VM offer respecting target constraints."""
    refresh = offer_refresher(args)
    offers = refresh()
    if not offers:
        LOG.warning("Rotation select: no qualifying offers available below $%s/day; keeping outgoing host undisturbed",
                    args.max_daily_usd)
        return True
    failed = rotation.setdefault("failed_offer_keys", [])
    untried = [offer for offer in offers if candidate_key(offer) not in failed]
    if not untried:
        # Every current candidate failed; retry the full list, as scale_up does.
        LOG.warning("Rotation select: all %d offers failed; retrying the full list", len(offers))
        failed.clear()
        untried = offers
    rotation["replacement_offer"] = _offer_record(untried[0])
    rotation["attempt_count"] = 0
    _advance_rotation(rotation, "provision", state, state_path)
    return _step_rotation_provision(vms, runners, args, state, state_path, rotation)


def _adopt_replacement(rotation, launched: GceVm | None) -> dict | None:
    """Return the replacement identity this rotation created, or None when it is unknown.

    The name comes from the launch's own persisted intent (``pending_replacement``) or the VM the
    launch returned; any other VM in inventory is never adopted.
    """
    pending = rotation.get("pending_replacement") or {}
    name = pending.get("name") or (launched.name if launched is not None else "")
    zone = pending.get("zone") or (launched.zone if launched is not None else "")
    if not name or (launched is not None and launched.name != name):
        return None
    return {
        "name": name,
        "zone": zone,
        "instance_id": launched.instance_id if launched is not None else "",
        "launched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())),
    }


def _step_rotation_provision(vms, runners, args, state, state_path, rotation):
    """Phase 2: Launch replacement Spot VM. Old VM continues executing jobs."""
    pending = rotation.get("pending_replacement")
    if pending and not rotation.get("replacement_vm"):
        # A previous check persisted launch intent and then stopped (crash or restart).
        operation = state.get("host_operation") or {}
        if operation.get("kind") == "launch" and operation.get("vm", {}).get("name") == pending.get("name"):
            LOG.info("Rotation waits for its pending launch of %s to finish", pending.get("name"))
            return True
        existing = next((vm for vm in vms if vm.name == pending.get("name") and vm.zone == pending.get("zone")), None)
        if existing is not None:
            rotation["replacement_vm"] = _adopt_replacement(rotation, existing)
            _advance_rotation(rotation, "boot", state, state_path)
            LOG.info("Rotation adopted its own launched replacement %s after restart", existing.name)
            return True
        LOG.warning("Rotation launch of %s left no VM; launching again", pending.get("name"))
        rotation["pending_replacement"] = None
        write_scale_state(state_path, state)

    offer_dict = dict(rotation["replacement_offer"])
    if "hourly_usd" not in offer_dict:
        offer_dict["hourly_usd"] = float(offer_dict.get("total_daily_usd", 0)) / 24
    if "total_daily_usd" in offer_dict and isinstance(offer_dict["total_daily_usd"], (int, float)):
        offer_dict["total_daily_usd"] = Decimal(str(offer_dict["total_daily_usd"]))
    if "hourly_usd" in offer_dict and isinstance(offer_dict["hourly_usd"], (int, float)):
        offer_dict["hourly_usd"] = Decimal(str(offer_dict["hourly_usd"]))
    offer = SimpleNamespace(**offer_dict)

    fleet = spot_instances(vms)
    max_hosts = max_allowed_hosts(args, state)
    if len(fleet) >= max_hosts:
        LOG.warning("Rotation launch held: fleet count %d reaches maximum with overlap %d", len(fleet), max_hosts)
        return True

    try:
        guard_fleet_mutation(state, vm=None, is_rotation=True)
        launched = launch_offer(offer, args, state, state_path)
        replacement = _adopt_replacement(rotation, launched if isinstance(launched, GceVm) else None)
        if replacement is None:
            raise RuntimeError("Launch finished without a replacement identity recorded by this rotation")
        rotation["replacement_vm"] = replacement
        rotation["attempt_count"] = 0
        rotation["error"] = None
        _advance_rotation(rotation, "boot", state, state_path)
        LOG.info("Provisioned replacement VM %s; waiting for it to run and register runners", replacement["name"])
        return True
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        attempts = rotation.get("attempt_count", 0) + 1
        rotation["attempt_count"] = attempts
        rotation["error"] = error_detail(exc)
        if attempts >= 3:
            LOG.warning(
                "Provisioning replacement VM for %s failed %d times (%s); resetting to 'select' phase to pick an alternative offer",
                rotation["outgoing_vm"]["name"], attempts, error_detail(exc)
            )
            rotation["phase"] = "select"
            failed_key = candidate_key(SimpleNamespace(**rotation.get("replacement_offer") or {}))
            if failed_key not in rotation.setdefault("failed_offer_keys", []):
                rotation["failed_offer_keys"].append(failed_key)
            rotation.pop("replacement_offer", None)
            rotation["phase_started_at"] = now()
        else:
            LOG.error("Failed to provision replacement VM for %s: %s; retrying next loop (old host continues running)",
                      rotation["outgoing_vm"]["name"], error_detail(exc))
        write_scale_state(state_path, state)
        return True


def _replacement_from_inventory(vms, repl: dict | None) -> GceVm | None:
    """Find the rotation's own replacement by recorded name, zone, and instance ID only."""
    if not repl or not repl.get("name"):
        return None
    for vm in vms:
        if vm.name != repl["name"] or (repl.get("zone") and vm.zone != repl["zone"]):
            continue
        if repl.get("instance_id") and vm.instance_id and vm.instance_id != repl["instance_id"]:
            continue
        return vm
    return None


def _delete_replacement(repl_vm, args, state, state_path, *, target=None, runners=()) -> None:
    """Delete a rotation replacement after persisting it as an intentional deletion.

    Without the record, the next observation would treat the replacement's disappearance as an
    unexpected loss and request another billable restoration host.
    """
    key = f"{repl_vm.zone}/{repl_vm.name}"
    if key not in state.setdefault("intentional_deletions", []):
        state["intentional_deletions"].append(key)
    state.setdefault("intentional_deletion_ids", {})[key] = repl_vm.instance_id
    # The intentional-deletion marker suppresses loss recovery, so persist a cleanup obligation
    # that is retried every check until fresh inventory confirms the VM is gone.
    record = {"name": repl_vm.name, "zone": repl_vm.zone, "instance_id": repl_vm.instance_id}
    if target:
        # Its (offline) runner registrations must be removed too; the intentional-deletion marker
        # suppresses the lost-host path that would otherwise clean them up.
        record["target"] = target
        record["runners"] = {str(r.runner_id): r.name for r in _replacement_runners(runners, repl_vm.name)}
    state.setdefault("replacement_cleanups", {})[key] = record
    write_scale_state(state_path, state)
    try:
        delete_vm_and_disks(repl_vm, args.project)
    except Exception as exc:
        LOG.error("Failed to delete replacement VM %s: %s; cleanup retries next check",
                  repl_vm.name, error_detail(exc))


def _deregister_replacement_runners(record, runners, state, state_path, *, discover=True) -> bool:
    """Remove a deleted replacement's runner registrations; True once none remain.

    Uses this check's fresh runner inventory: recorded runners (and, with ``discover``, any other
    ``gha-<vm>-<n>`` runner that appeared later) are deregistered only while offline and idle,
    matched by id and name. ``discover`` is off when another instance now uses the name, whose
    listeners must never be claimed by name.
    """
    target = record.get("target")
    if not target:
        return True  # Records written before registrations were tracked.
    if runner_inventory_stale(state):
        LOG.warning("Deregistration for deleted replacement %s held: runner inventory is stale", record.get("name"))
        return False
    from .runner_drain import api
    pending = dict(record.get("runners") or {})
    if discover:
        for runner in _replacement_runners(runners, record.get("name")):
            pending.setdefault(str(runner.runner_id), runner.name)
    present = {str(r.runner_id): r for r in runners}
    for rid, name in list(pending.items()):
        runner = present.get(rid)
        if runner is None or runner.name != name:
            pending.pop(rid)  # already gone
            continue
        if runner.online or runner.busy:
            LOG.warning("Runner %s of deleted replacement %s still reports online; waiting", name, record.get("name"))
            continue
        try:
            api(target, "DELETE", f"{target_api_prefix(target)}/actions/runners/{rid}")
            pending.pop(rid)
            LOG.info("Deregistered runner %s of deleted replacement %s", name, record.get("name"))
        except Exception as exc:
            LOG.warning("Could not deregister runner %s: %s; retrying next check", name, error_detail(exc))
    record["runners"] = pending
    write_scale_state(state_path, state)
    return not pending


def _schedule_replacement_drain(repl_vm, rotation, runners, state) -> None:
    """Persist a drain-then-delete obligation for a replacement an abort has to keep.

    Callers persist state afterwards. reconcile_replacement_cleanups deregisters its idle
    listeners, waits for busy ones and builds, deletes the VM, then confirms registrations are gone.
    """
    LOG.warning("Keeping replacement %s until its listeners and builds drain", repl_vm.name)
    key = f"{repl_vm.zone}/{repl_vm.name}"
    state.setdefault("replacement_cleanups", {})[key] = {
        "name": repl_vm.name, "zone": repl_vm.zone, "instance_id": repl_vm.instance_id,
        "target": rotation.get("target"), "drain": True,
        "runners": {str(r.runner_id): r.name for r in _replacement_runners(runners, repl_vm.name)}}


def _drain_replacement_listeners(record, vm, runners, state, state_path) -> None:
    """Deregister an aborted replacement's idle listeners so it can be deleted without losing jobs.

    Busy listeners are left alone until their jobs finish. Each idle listener is re-read from
    GitHub immediately before deregistration, exactly like outgoing-host deregistration.
    """
    from .runner_drain import api
    target = record.get("target")
    record["drain"] = True
    write_scale_state(state_path, state)
    if not target:
        LOG.warning("Aborted replacement %s has listeners but no recorded target; waiting for them to go offline",
                    vm.name)
        return
    prefix = target_api_prefix(target)
    for runner in _replacement_runners(runners, vm.name):
        record.setdefault("runners", {})[str(runner.runner_id)] = runner.name
        if runner.busy or not runner.online:
            continue
        endpoint = f"{prefix}/actions/runners/{runner.runner_id}"
        try:
            fresh = api(target, "GET", endpoint) or {}
            if fresh.get("name") != runner.name or fresh.get("busy") is not False:
                continue
            api(target, "DELETE", endpoint)
            LOG.info("Deregistered idle listener %s of aborted replacement %s", runner.name, vm.name)
        except Exception as exc:
            LOG.warning("Could not deregister listener %s of aborted replacement %s: %s",
                        runner.name, vm.name, error_detail(exc))
    write_scale_state(state_path, state)


def reconcile_replacement_cleanups(vms, runners, args, state, state_path) -> None:
    """Retry deletion of aborted replacements until fresh inventory shows them absent.

    A replacement whose first deletion failed (or that an abort kept because listeners were
    online) may have registered listeners, taken jobs, or started a remote build. Every retry
    re-checks workload safety against this check's fresh inventory. A replacement with online
    listeners is drained here: idle listeners are deregistered after a fresh idle check, busy ones
    are waited for, and the VM is deleted once none are online and builds are quiesced. Keeping
    the obligation here matters: price reconciliation pre-empts normal scaling, so nothing else
    would remove a surplus host left over from an aborted rotation.
    """
    cleanups = state.get("replacement_cleanups")
    if not isinstance(cleanups, dict) or not cleanups:
        return
    for key, record in list(cleanups.items()):
        current = next((vm for vm in vms if vm.name == record.get("name") and vm.zone == record.get("zone")
                        and (not record.get("instance_id") or vm.instance_id == record["instance_id"])), None)
        if current is None:
            # A VM with the recorded name and zone but another instance ID is a recreation, not this
            # replacement: only the registrations recorded for the original instance may be removed.
            recreated = any(vm.name == record.get("name") and vm.zone == record.get("zone")
                            for vm in vms) and bool(record.get("instance_id"))
            if recreated:
                LOG.warning("Aborted replacement %s was recreated under the same name; cleaning up only its "
                            "recorded registrations", record.get("name"))
            if not _deregister_replacement_runners(record, runners, state, state_path, discover=not recreated):
                continue  # keep the obligation until every registration is confirmed gone
            cleanups.pop(key)
            _remember_retired_replacement(record, state)
            LOG.info("Confirmed aborted replacement %s is deleted", record.get("name"))
            write_scale_state(state_path, state)
            continue
        if runner_inventory_stale(state):
            LOG.warning("Retry of aborted replacement %s deletion held: runner inventory is stale", current.name)
            continue
        if any(r.online or r.busy for r in _replacement_runners(runners, current.name)):
            _drain_replacement_listeners(record, current, runners, state, state_path)
            continue
        if not _replacement_safe_to_delete(current, runners, args, require_offline=True,
                                           state=state, target=record.get("target")):
            LOG.info("Aborted replacement %s waits for remote builds before deletion", current.name)
            continue
        key_ids = state.setdefault("intentional_deletion_ids", {})
        if key not in state.setdefault("intentional_deletions", []):
            state["intentional_deletions"].append(key)
        key_ids[key] = current.instance_id
        write_scale_state(state_path, state)
        try:
            delete_vm_and_disks(current, args.project)
            LOG.info("Retried deletion of aborted replacement %s", current.name)
        except Exception as exc:
            LOG.warning("Aborted replacement %s still pending deletion: %s", current.name, error_detail(exc))


def _replacement_safe_to_delete(repl_vm, runners, args, *, require_offline=False, state=None, target=None) -> bool:
    """Return whether an aborted replacement can be deleted without losing work.

    A replacement with a busy listener (or, when ``require_offline``, any online listener) is
    kept. Tracked CI jobs on it must be finished (a listener can look offline or idle while its
    workflow is still in progress), exactly as for outgoing-host retirement. A RUNNING
    replacement must also pass the remote-builder quiescence check, so an active build is never
    deleted.
    """
    assigned = _replacement_runners(runners, repl_vm.name)
    if any(r.busy for r in assigned) or (require_offline and any(r.online for r in assigned)):
        return False
    if state is not None and target:
        from .runner_drain import jobs_finished
        try:
            if not jobs_finished(repl_vm, state, target):
                return False
        except Exception as exc:
            LOG.warning("Cannot confirm tracked CI jobs on %s finished: %s; keeping it", repl_vm.name,
                        error_detail(exc))
            return False
    return not repl_vm.running or quiesce_remote_builder(repl_vm, args)


def _abort_runner_rotation(rotation, vms, args, state, state_path, reason: str, runners=()) -> bool:
    repl_vm = _replacement_from_inventory(vms, rotation.get("replacement_vm"))
    if _outgoing_capacity_lost(rotation):
        # The outgoing host no longer has its listeners, so dropping the transaction would leave
        # no usable capacity. Keep it pending and try another fresh replacement instead.
        LOG.error("%s; outgoing listeners were removed, so the rotation keeps provisioning capacity", reason)
        if repl_vm and _replacement_safe_to_delete(repl_vm, runners, args, require_offline=True,
                                                    state=state, target=rotation.get("target")):
            _delete_replacement(repl_vm, args, state, state_path, target=rotation.get("target"), runners=runners)
        elif repl_vm:
            # Partially ready: keep it, but never abandon it as an uncounted surplus host.
            _schedule_replacement_drain(repl_vm, rotation, runners, state)
        _restart_replacement(rotation, state, state_path)
        return True
    LOG.error("%s; aborting replacement to preserve old capacity", reason)
    # Online listeners stay schedulable during the builder check and deletion, so a replacement with
    # any online listener is kept and drained normally rather than deleted with jobs landing on it.
    if repl_vm and _replacement_safe_to_delete(repl_vm, runners, args, require_offline=True,
                                                    state=state, target=rotation.get("target")):
        _delete_replacement(repl_vm, args, state, state_path, target=rotation.get("target"), runners=runners)
    elif repl_vm:
        # Online, busy or building: never delete it now. Persist a drain-then-delete obligation so
        # the surplus host is removed once its listeners are idle and deregistered and its builds
        # are quiesced (reconcile_replacement_cleanups); otherwise it could outlive the rotation.
        _schedule_replacement_drain(repl_vm, rotation, runners, state)
    state.pop("price_rotation", None)
    write_scale_state(state_path, state)
    return True


def _expected_replacement_listeners(rotation, args=None) -> int:
    """Return how many replacement listeners must be online before retirement.

    Rotations saved before the count was derived from launch sizing recorded a placeholder of 1;
    for those, recompute from the saved replacement offer with launch_offer's own sizing.
    """
    count = int(rotation.get("expected_runner_count", 0) or 0)
    if not rotation.get("expected_runner_count_source"):
        offer = rotation.get("replacement_offer") or {}
        if offer.get("vcpu") and offer.get("ram_gib"):
            count = max(count, launch_slot_count(SimpleNamespace(**offer), args))
        else:
            count = max(count, 8)
    return max(1, count)


RETIRED_REPLACEMENT_TTL_SECONDS = 24 * 3600


def rotation_host_identities(state) -> tuple[dict | None, list[dict]]:
    """Return the active rotation's outgoing host and every replacement identity a rotation owns.

    Replacements stay temporary overlap capacity after their rotation ends: those an abort kept
    under a drain-then-delete obligation (``replacement_cleanups``), and, for a day after that
    obligation finished, recently retired ones. Their loss must never become restoration capacity.
    """
    rotation = state.get("price_rotation")
    replacements = []
    outgoing = None
    if isinstance(rotation, dict):
        replacements += [row for row in (rotation.get("pending_replacement"), rotation.get("replacement_vm"),
                                         *(rotation.get("failed_replacements") or []))
                         if isinstance(row, dict) and row.get("name")]
        outgoing = rotation.get("outgoing_vm") if isinstance(rotation.get("outgoing_vm"), dict) else None
    cleanups = state.get("replacement_cleanups")
    if isinstance(cleanups, dict):
        replacements += [row for row in cleanups.values() if isinstance(row, dict) and row.get("name")]
    now_ts = time.time()
    replacements += [row for row in state.get("retired_replacements") or []
                     if isinstance(row, dict) and row.get("name")
                     and now_ts - float(row.get("retired_at") or 0) < RETIRED_REPLACEMENT_TTL_SECONDS]
    return outgoing, replacements


def _remember_retired_replacement(record, state) -> None:
    """Keep a finished cleanup's identity briefly so a loss observed in the same pass stays non-restorable."""
    now_ts = time.time()
    kept = [row for row in state.get("retired_replacements") or []
            if isinstance(row, dict) and now_ts - float(row.get("retired_at") or 0) < RETIRED_REPLACEMENT_TTL_SECONDS]
    kept.append({"name": record.get("name"), "zone": record.get("zone"),
                 "instance_id": record.get("instance_id"), "retired_at": now_ts})
    state["retired_replacements"] = kept[-50:]


def identity_matches(row, name, zone, instance_id) -> bool:
    return (isinstance(row, dict) and row.get("name") == name and row.get("zone", zone) == zone
            and (not row.get("instance_id") or not instance_id or row.get("instance_id") == instance_id))


def _replacement_runners(runners, name: str):
    """Listeners of exactly this replacement host: ``gha-<name>-<n>``.

    A plain prefix match would also claim ``gha-<name>-extra-1`` from a host whose name extends
    this one, reporting false readiness or deregistering another host's idle listeners.
    """
    if not name:
        return []
    pattern = re.compile(r"gha-" + re.escape(name) + r"-\d+")
    return [r for r in runners if pattern.fullmatch(r.name)]


def _replacement_live_failure(vms, runners, rotation) -> str | None:
    """Return why fresh inventory does not prove the replacement live, or None when it does.

    The replacement must be present with its recorded name, zone, and instance ID, be RUNNING,
    and have at least the expected number of its own runners online in this check's inventory.
    """
    repl = rotation.get("replacement_vm") or {}
    name, zone, instance_id = repl.get("name"), repl.get("zone"), repl.get("instance_id")
    if not name or not instance_id:
        return "replacement identity (name and instance ID) is not recorded"
    vm = next((v for v in vms if v.name == name and (not zone or v.zone == zone)), None)
    if vm is None:
        return f"replacement {name} is absent from fresh inventory"
    if vm.instance_id != instance_id:
        return f"replacement {name} has instance ID {vm.instance_id or 'unknown'}, expected {instance_id}"
    if not vm.running:
        return f"replacement {name} is {vm.status}"
    expected = _expected_replacement_listeners(rotation)
    online = sum(1 for r in _replacement_runners(runners, name) if r.online)
    if online < expected:
        return f"replacement {name} has {online}/{expected} runners online"
    return None


def _record_cordon(rotation, runner, state, state_path, group_id=None) -> None:
    """Persist a runner's pre-cordon labels (and group) once, before any cordon API call."""
    records = rotation.setdefault("cordoned_runners", {})
    key = str(runner.runner_id)
    if key in records:
        if group_id is not None and "group_id" not in records[key]:
            records[key]["group_id"] = group_id
            write_scale_state(state_path, state)
        return
    records[key] = {"name": runner.name,
                    "labels": sorted(set(runner.labels) - DEFAULT_RUNNER_LABELS)}
    if group_id is not None:
        records[key]["group_id"] = group_id
    write_scale_state(state_path, state)


def _uncordon_outgoing(rotation, runners, state, state_path) -> bool:
    """Restore the outgoing host's runner labels and group; return True when nothing remains."""
    from .runner_drain import api
    records = rotation.get("cordoned_runners") or {}
    target = rotation["target"]
    prefix = target_api_prefix(target)
    present = {runner.runner_id: runner for runner in runners}
    for key, record in records.items():
        if record.get("restored"):
            continue
        runner = present.get(int(key))
        if runner is None or runner.name != record.get("name"):
            record["restored"] = "absent"
            write_scale_state(state_path, state)
            continue
        try:
            if record.get("group_id") and not is_repo_target(target):
                api(target, "PUT", f"orgs/{target}/actions/runner-groups/{record['group_id']}/runners/{runner.runner_id}")
            if record.get("labels"):
                api(target, "PUT", f"{prefix}/actions/runners/{runner.runner_id}/labels",
                    {"labels": record["labels"]})
        except Exception as exc:
            LOG.warning("Could not uncordon outgoing runner %s: %s", runner.name, error_detail(exc))
            write_scale_state(state_path, state)
            return False
        record["restored"] = True
        write_scale_state(state_path, state)
        LOG.info("Uncordoned outgoing runner %s", runner.name)
    drain_group_id = rotation.get("drain_group_id")
    if drain_group_id and not is_repo_target(target):
        try:
            api(target, "DELETE", f"orgs/{target}/actions/runner-groups/{drain_group_id}")
            rotation["drain_group_id"] = None
            write_scale_state(state_path, state)
        except Exception as exc:
            LOG.warning("Drain group cleanup pending after uncordon: %s", error_detail(exc))
    return True


def _outgoing_capacity_lost(rotation) -> bool:
    """Return whether any outgoing listener was (or may have been) deregistered."""
    return bool(rotation.get("capacity_lost") or rotation.get("deregistered_runner_ids")
                or rotation.get("deregistering_runner_id") is not None
                or rotation.get("phase") == "delete")


def _restart_replacement(rotation, state, state_path) -> None:
    """Return to offer selection for a fresh replacement; the outgoing host stays cordoned."""
    failed = rotation.setdefault("failed_replacements", [])
    if rotation.get("replacement_vm"):
        failed.append(rotation["replacement_vm"])
    rotation.update(replacement_vm=None, pending_replacement=None, attempt_count=0,
                    readiness={"runners_online": False})
    rotation.pop("recovering", None)
    _advance_rotation(rotation, "select", state, state_path)


# Statuses in which a replacement may still bring its listeners back online.
REPLACEMENT_ALIVE_STATUSES = {"PROVISIONING", "STAGING", "RUNNING", "REPAIRING"}


def _recover_lost_capacity(vms, runners, args, state, state_path, rotation) -> bool:
    """Keep the transaction pending until a replacement's listeners are online again.

    The outgoing host lost listeners to deregistration, so it stays cordoned and out of service
    (bootstrap is never replayed on it). The replacement is never deleted here: the rotation
    resumes retirement once it is live again, or provisions fresh capacity if it is truly gone.
    """
    out_name = rotation["outgoing_vm"]["name"]
    if not rotation.get("capacity_lost"):
        rotation["capacity_lost"] = True
        write_scale_state(state_path, state)
    if _replacement_live_failure(vms, runners, rotation) is None:
        rotation.pop("recovering", None)
        rotation["readiness"] = {"runners_online": True}
        write_scale_state(state_path, state)
        LOG.info("Replacement listeners are online again; resuming retirement of %s", out_name)
        return True
    repl_vm = _replacement_from_inventory(vms, rotation.get("replacement_vm"))
    repl = rotation.get("replacement_vm") or {}
    if (repl_vm is not None and repl.get("instance_id") and repl_vm.instance_id == repl["instance_id"]
            and repl_vm.status.upper() in REPLACEMENT_ALIVE_STATUSES):
        LOG.warning("Rotation waits for replacement %s listeners; %s stays cordoned with removed listeners",
                    repl_vm.name, out_name)
        write_scale_state(state_path, state)
        return True
    LOG.error("Replacement %s is gone after %s lost listeners; provisioning fresh capacity",
              repl.get("name", "unknown"), out_name)
    _restart_replacement(rotation, state, state_path)
    return True


def _recover_failed_replacement(vms, runners, args, state, state_path, rotation, reason: str) -> bool:
    """Stop retiring the outgoing host, restore its scheduling, then abort the rotation."""
    out_name = rotation["outgoing_vm"]["name"]
    if not rotation.get("recovering"):
        rotation["recovering"] = reason
        rotation["readiness"] = {"runners_online": False}
        write_scale_state(state_path, state)
        LOG.error("Retirement of %s stopped: %s; restoring the outgoing host", out_name, reason)
    if _outgoing_capacity_lost(rotation):
        return _recover_lost_capacity(vms, runners, args, state, state_path, rotation)
    if not _uncordon_outgoing(rotation, runners, state, state_path):
        LOG.warning("Rotation recovery waits to uncordon %s; retrying next loop", out_name)
        return True
    records = rotation.get("cordoned_runners") or {}
    present = {r.runner_id: r for r in runners}
    if records and not any(present.get(int(key)) is not None and present[int(key)].online
                           and present[int(key)].name == record.get("name")
                           for key, record in records.items()):
        # Nothing on the outgoing host can take work, so the replacement is the only capacity.
        LOG.error("Outgoing host %s has no online listeners after uncordon", out_name)
        return _recover_lost_capacity(vms, runners, args, state, state_path, rotation)
    cordon_token = rotation.get("host_cordon_token")
    if cordon_token:
        from .runner_drain import release_runner_host_cordon
        out_vm, _ = _outgoing_from_inventory(vms, rotation["outgoing_vm"])
        if out_vm is not None and out_vm.running and not release_runner_host_cordon(out_vm, args, cordon_token):
            # Leaving the marker would refuse every job on the restored host; retry until released.
            LOG.warning("Rotation recovery waits to release the host job-start cordon on %s; retrying next loop",
                        out_name)
            return True
        rotation.pop("host_cordon_token", None)
        write_scale_state(state_path, state)
        LOG.info("Released rotation-owned host job-start cordon on %s", out_name)
    token = rotation.get("builder_drain_token")
    if token:
        out_vm = next((vm for vm in vms if vm.name == out_name
                       and vm.zone == rotation["outgoing_vm"].get("zone")), None)
        if out_vm is not None and out_vm.running and not release_remote_builder(out_vm, args, token):
            # Leaving the marker would block builds on this host indefinitely; retry until released.
            LOG.warning("Rotation recovery waits to release the builder drain on %s; retrying next loop", out_name)
            return True
        rotation.pop("builder_drain_token", None)
        write_scale_state(state_path, state)
        LOG.info("Released rotation-owned builder drain on %s", out_name)
    # The abort path keeps a busy or building replacement and deletes only an idle, quiesced one.
    return _abort_runner_rotation(rotation, vms, args, state, state_path,
                                  f"Rotation for {out_name} aborted: {rotation['recovering']}", runners)


def _step_rotation_boot(vms, runners, args, state, state_path, rotation):
    """Phase 3: Wait for the replacement this rotation launched to reach RUNNING."""
    repl = rotation.get("replacement_vm")
    if not repl:
        _advance_rotation(rotation, "provision", state, state_path)
        return True

    new_vm = _replacement_from_inventory(vms, repl)
    now_ts = now()

    if not new_vm or new_vm.status != "RUNNING":
        if now_ts - rotation.get("phase_started_at", now_ts) >= BOOT_TIMEOUT_SECONDS:
            return _abort_runner_rotation(rotation, vms, args, state, state_path,
                                          f"Boot timed out for {repl['name']}", runners)
        LOG.info("Rotation waits for replacement VM %s to reach RUNNING status (currently %s)",
                 repl["name"], new_vm.status if new_vm else "PROVISIONING")
        return True

    if not repl.get("instance_id") and new_vm.instance_id:
        repl["instance_id"] = new_vm.instance_id

    _advance_rotation(rotation, "verify_readiness", state, state_path)
    LOG.info("Replacement VM %s is RUNNING; waiting for its runners to come online", repl["name"])
    return _step_rotation_verify_readiness(vms, runners, args, state, state_path, rotation)



def _step_rotation_verify_readiness(vms, runners, args, state, state_path, rotation):
    """Phase 4: Gate on the replacement's own runners being registered and online."""
    repl = rotation.get("replacement_vm")
    now_ts = now()
    readiness = rotation.setdefault("readiness", {"runners_online": False})

    repl_name = repl["name"] if repl else ""
    assigned = _replacement_runners(runners, repl_name)
    expected_count = _expected_replacement_listeners(rotation, args)
    online_count = sum(1 for r in assigned if r.online)
    readiness["runners_online"] = bool(repl_name) and online_count >= expected_count

    if readiness["runners_online"]:
        LOG.info("Readiness gate PASSED for %s: %d/%d runners online", repl_name, online_count, expected_count)
        rotation["drain_started_at"] = now_ts
        _advance_rotation(rotation, "cordon_drain", state, state_path)
        return True

    if now_ts - rotation.get("phase_started_at", now_ts) >= READINESS_DEADLINE_SECONDS:
        return _abort_runner_rotation(rotation, vms, args, state, state_path,
                                      f"Readiness gate timed out for replacement {repl_name or 'new'}", runners)

    LOG.info("Rotation waits for runners on %s: %d/%d online", repl_name or "new", online_count, expected_count)
    write_scale_state(state_path, state)
    return True


def _outgoing_from_inventory(vms, out_dict):
    """Return (vm, recreated) for the outgoing host by recorded name, zone, and instance ID.

    ``recreated`` is True when a VM with the recorded name exists in that zone under another
    instance ID: the original host is gone and that VM's runners must never be touched.
    """
    named = [v for v in vms if v.name == out_dict["name"] and v.zone == out_dict.get("zone", v.zone)]
    recorded_id = out_dict.get("instance_id", "")
    match = next((v for v in named if not recorded_id or v.instance_id == recorded_id), None)
    return match, match is None and bool(named)


def _abandon_recreated_outgoing(rotation, state, state_path) -> bool:
    out = rotation["outgoing_vm"]
    LOG.error("Outgoing host %s (instance %s) was recreated under the same name; abandoning its "
              "retirement without touching the new instance", out["name"], out.get("instance_id") or "unknown")
    state.pop("price_rotation", None)
    state["idle_loops"] = 0
    write_scale_state(state_path, state)
    return True


def _step_rotation_cordon_drain(vms, runners, args, state, state_path, rotation):
    """Phase 5: Cordon old runners, wait for in-flight jobs to complete."""
    out_dict = rotation["outgoing_vm"]
    out_name = out_dict["name"]
    out_vm, recreated = _outgoing_from_inventory(vms, out_dict)
    if recreated:
        return _abandon_recreated_outgoing(rotation, state, state_path)
    target = rotation["target"]
    target_type = rotation.get("target_type", "org")
    now_ts = now()

    assigned = host_runners(out_vm, runners, vms) if out_vm else _missing_outgoing_runners(out_name, runners, vms, rotation)

    if not rotation.get("cordon_complete"):
        from .runner_drain import api, isolated_group
        cordon_success = True
        cordon_mode = "labels"
        if target_type == "repo":
            # GitHub keeps a repository runner's built-in labels, so stripping custom labels alone never stops
            # a job that selects only self-hosted/linux/X64. Cordon on the host first; a host that cannot refuse
            # jobs at start keeps its labels and stays in service (retirement held, never forced).
            if out_vm is not None:
                host_state = _rotation_host_cordon(out_vm, assigned, args, state, state_path, rotation)
                if host_state not in ("placed", "busy", "idle"):
                    write_scale_state(state_path, state)
                    return True
            for runner in assigned:
                try:
                    _record_cordon(rotation, runner, state, state_path)
                    api(target, "DELETE", f"repos/{target}/actions/runners/{runner.runner_id}/labels")
                except Exception as exc:
                    cordon_success = False
                    LOG.warning("Could not strip labels from repo runner %s: %s", runner.name, error_detail(exc))
            cordon_mode = "host" if out_vm is not None else "labels"
        else:
            drain_group_name = f"spot-drain-{out_dict['name']}-{out_dict.get('instance_id', '')}"
            operation = {"drain_group_name": drain_group_name, "drain_group_id": rotation.get("drain_group_id")}
            try:
                group_path = isolated_group(operation, target, lambda: None)
                rotation["drain_group_id"] = operation.get("drain_group_id")
                write_scale_state(state_path, state)
                memberships = None
                for runner in assigned:
                    try:
                        if str(runner.runner_id) not in rotation.get("cordoned_runners", {}):
                            # The individual-runner endpoint does not report group membership, so
                            # resolve it from the runner-group APIs; never guess a default group.
                            if memberships is None:
                                memberships = _runner_group_memberships(api, target, rotation.get("drain_group_id"))
                            group_id = memberships.get(runner.runner_id)
                            if type(group_id) is not int:
                                raise RuntimeError(f"runner {runner.name} is in no known runner group")
                            _record_cordon(rotation, runner, state, state_path, group_id)
                        api(target, "DELETE", f"orgs/{target}/actions/runners/{runner.runner_id}/labels")
                        api(target, "PUT", f"{group_path}/runners/{runner.runner_id}")
                    except Exception as exc:
                        cordon_success = False
                        LOG.warning("Could not isolate org runner %s: %s", runner.name, error_detail(exc))
                if cordon_success:
                    cordon_mode = "isolated_group"
            except Exception as exc:
                # Stripping custom labels alone never isolates: the built-in labels (self-hosted, linux, X64) stay,
                # so a job selecting only those could still start before deregistration. Without an isolated group,
                # cordon on the host exactly as for a repository target; a host that cannot refuse jobs at start
                # keeps its labels and holds the rotation (retried next poll, group first), never label-only.
                if out_vm is not None:
                    LOG.warning("Could not set up isolated drain group for %s: %s; cordoning on the host instead",
                                out_name, error_detail(exc))
                    host_state = _rotation_host_cordon(out_vm, assigned, args, state, state_path, rotation)
                    if host_state not in ("placed", "busy", "idle"):
                        LOG.error("Rotation held for %s: neither an isolated runner group nor a host job-start "
                                  "cordon isolates its org runners (host cordon %s); labels and registrations are "
                                  "kept", out_name, host_state)
                        write_scale_state(state_path, state)
                        return True
                else:
                    # The outgoing VM is gone from the provider inventory: no host remains to run a job.
                    LOG.warning("Could not set up isolated drain group: %s; outgoing host %s is absent, stripping "
                                "labels", error_detail(exc), out_name)
                for runner in assigned:
                    try:
                        _record_cordon(rotation, runner, state, state_path)
                        api(target, "DELETE", f"orgs/{target}/actions/runners/{runner.runner_id}/labels")
                    except Exception as e2:
                        cordon_success = False
                        LOG.warning("Could not strip labels from org runner %s: %s", runner.name, error_detail(e2))
                cordon_mode = "host" if out_vm is not None else "labels"

        if not cordon_success:
            rotation["cordon_complete"] = False
            write_scale_state(state_path, state)
            LOG.warning("Cordoning incomplete for outgoing VM %s; will retry next poll before draining", out_dict["name"])
            return True

        rotation["cordon_complete"] = True
        rotation["cordon_mode"] = cordon_mode
        if not rotation.get("drain_started_at"):
            rotation["drain_started_at"] = now_ts
        write_scale_state(state_path, state)
        LOG.info("Cordoned outgoing VM %s (%s mode); waiting for active jobs to complete", out_dict["name"], cordon_mode)
        return True

    busy_runners = [r for r in assigned if r.busy]

    from .runner_drain import jobs_finished
    jobs_done = jobs_finished(out_vm, state, target) if out_vm else True

    if busy_runners or not jobs_done:
        if now_ts - (rotation.get("drain_started_at") or now_ts) >= DRAIN_DEADLINE_SECONDS:
            LOG.warning("Drain deadline exceeded for %s (%d busy runners); forcing advancement to deregistration",
                        out_dict["name"], len(busy_runners))
        else:
            LOG.info("Drain waits for %d busy runners and CI jobs on %s", len(busy_runners), out_dict["name"])
            write_scale_state(state_path, state)
            return True

    if (_rotation_needs_host_cordon(rotation, target_type) and out_vm is not None
            and _rotation_host_cordon(out_vm, assigned, args, state, state_path, rotation) != "idle"):
        # The drain deadline never overrides this: a job running under the cordon must finish first.
        write_scale_state(state_path, state)
        return True

    if out_vm is not None:
        # Record ownership before the marker can exist, so a crash right after placing it still
        # lets rollback release it.
        rotation["builder_drain_token"] = _rotation_drain_token(rotation)
        write_scale_state(state_path, state)
    if out_vm is not None and not quiesce_remote_builder(out_vm, args, token=rotation["builder_drain_token"]):
        # Idle runners do not prove the host is idle: a remote build may still be running. The
        # drain deadline never overrides this; keep the host until builds finish and new ones stop.
        LOG.info("Rotation waits for remote builds to finish on %s before deregistration", out_dict["name"])
        write_scale_state(state_path, state)
        return True

    LOG.info("All CI jobs finished on %s; advancing to runner deregistration", out_dict["name"])
    _advance_rotation(rotation, "deregister", state, state_path)
    return True


def _outgoing_work_active(out_vm, assigned, state, target) -> str | None:
    """Return why the outgoing host may still be running CI work, or None when it is idle."""
    from .runner_drain import jobs_finished
    busy = [r.name for r in assigned if r.busy]
    if busy:
        return f"runners {', '.join(busy)} are busy"
    if out_vm is not None and not jobs_finished(out_vm, state, target):
        return "a tracked CI job is still active on the host"
    return None


def _return_to_drain(rotation, state, state_path, reason: str) -> bool:
    """Send a rotation back to draining instead of deregistering a host that has work."""
    LOG.warning("Deregistration of %s deferred: %s; returning to drain", rotation["outgoing_vm"]["name"], reason)
    rotation["drain_started_at"] = now()
    _advance_rotation(rotation, "cordon_drain", state, state_path)
    return True


def _step_rotation_deregister(vms, runners, args, state, state_path, rotation):
    """Phase 6: Remove old runners from GitHub API."""
    out_dict = rotation["outgoing_vm"]
    out_name = out_dict["name"]
    out_vm, recreated = _outgoing_from_inventory(vms, out_dict)
    if recreated:
        return _abandon_recreated_outgoing(rotation, state, state_path)
    target = rotation["target"]
    target_type = rotation.get("target_type", "org")
    assigned = host_runners(out_vm, runners, vms) if out_vm else _missing_outgoing_runners(out_name, runners, vms, rotation)

    from .runner_drain import api
    busy = _outgoing_work_active(out_vm, assigned, state, target)
    if busy:
        return _return_to_drain(rotation, state, state_path, busy)
    if _rotation_needs_host_cordon(rotation, target_type) and out_vm is not None and assigned:
        host_state = _rotation_host_cordon(out_vm, assigned, args, state, state_path, rotation)
        if host_state != "idle":
            # Never deregister unless the host already refuses new jobs and runs none.
            return _return_to_drain(rotation, state, state_path, f"host cordon is {host_state}")
    for runner in assigned:
        endpoint = f"repos/{target}/actions/runners/{runner.runner_id}" if target_type == "repo" else f"orgs/{target}/actions/runners/{runner.runner_id}"
        try:
            fresh = api(target, "GET", endpoint) or {}
        except Exception as exc:
            LOG.warning("Deregistration waits for fresh state of runner %s: %s", runner.name, error_detail(exc))
            return True
        if fresh.get("name") != runner.name or fresh.get("busy") is not False:
            # Repository runners keep built-in labels, so a job can still land after cordoning.
            return _return_to_drain(rotation, state, state_path, f"runner {runner.name} is not freshly idle")
        # Persist intent first: from here on, rollback can no longer restore this listener.
        rotation["deregistering_runner_id"] = runner.runner_id
        write_scale_state(state_path, state)
        try:
            api(target, "DELETE", endpoint)
            LOG.info("Deregistered old runner %s (%d) from %s", runner.name, runner.runner_id, target)
        except Exception as exc:
            LOG.warning("Deregistration pending for runner %s: %s", runner.name, error_detail(exc))
            return True
        done = rotation.setdefault("deregistered_runner_ids", [])
        if runner.runner_id not in done:
            done.append(runner.runner_id)
        rotation.pop("deregistering_runner_id", None)
        write_scale_state(state_path, state)

    _advance_rotation(rotation, "delete", state, state_path)
    LOG.info("All old runners deregistered for %s; advancing to VM deletion", out_dict["name"])
    return True


def _step_rotation_delete(vms, runners, args, state, state_path, rotation):
    """Phase 7: Delete old VM and auto-deleting disks."""
    out_dict = rotation["outgoing_vm"]
    repl_dict = rotation.get("replacement_vm")
    out_name = out_dict["name"]
    out_id = out_dict.get("instance_id", "")
    out_vm = next((v for v in vms if v.name == out_name and v.zone == out_dict["zone"]
                   and (not out_id or v.instance_id == out_id)), None)

    protected = state.setdefault("protected_heavy_routes", {})
    if out_dict.get("instance_id") in protected and repl_dict:
        protected[repl_dict["instance_id"]] = {"name": repl_dict["name"], "zone": repl_dict["zone"]}
        protected.pop(out_dict["instance_id"], None)
        LOG.info("Transferred protected heavy route from %s to replacement %s", out_dict["name"], repl_dict["name"])

    if out_vm is not None:
        leftover = host_runners(out_vm, runners, vms)
        busy = _outgoing_work_active(out_vm, leftover, state, rotation["target"])
        if busy:
            LOG.warning("Rotation deletion of %s waits: %s", out_name, busy)
            write_scale_state(state_path, state)
            return True

    # A stopped outgoing VM (for example preempted after deletion was first attempted) runs no builds and
    # cannot be reached over SSH; quiescing it would wait forever.
    stopped = out_vm is not None and not out_vm.running
    if out_vm is not None and not stopped and not quiesce_remote_builder(out_vm, args):
        # Re-confirm under the builder lock right before deletion; never delete under a build.
        LOG.info("Rotation deletion waits for remote builds to finish on %s", out_dict["name"])
        write_scale_state(state_path, state)
        return True

    key = f"{out_dict['zone']}/{out_dict['name']}"
    if key not in state.setdefault("intentional_deletions", []):
        state["intentional_deletions"].append(key)
    state.setdefault("intentional_deletion_ids", {})[key] = out_dict.get("instance_id", "")
    write_scale_state(state_path, state)

    if out_vm is None:
        # Fresh inventory has no VM with the recorded identity; never delete by name alone.
        LOG.warning("Outgoing VM %s with instance ID %s is already absent; skipping deletion",
                    out_name, out_id or "unknown")
    else:
        try:
            guard_fleet_mutation(state, out_vm, is_rotation=True)
            if stopped:
                # Identity already verified above; delete_overpriced_vm only accepts running VMs.
                delete_vm_and_disks(out_vm, args.project)
            else:
                delete_overpriced_vm(out_vm, args.project)
            LOG.info("Deleted old Spot VM %s and its attached disks", out_dict["name"])
        except Exception as exc:
            LOG.error("Failed to delete old VM %s: %s; retrying next loop", out_dict["name"], error_detail(exc))
            return True

    drain_group_id = rotation.get("drain_group_id")
    if drain_group_id and rotation.get("target_type") == "org":
        from .runner_drain import api
        try:
            api(rotation["target"], "DELETE", f"orgs/{rotation['target']}/actions/runner-groups/{drain_group_id}")
            LOG.info("Cleaned up temporary drain runner group %s", drain_group_id)
        except Exception as exc:
            LOG.warning("Drain group cleanup pending: %s", error_detail(exc))

    state.pop("price_rotation", None)
    state["idle_loops"] = 0
    write_scale_state(state_path, state)
    LOG.info("Make-before-break runner rotation complete: replaced %s with %s",
             out_dict["name"], repl_dict.get("name") if repl_dict else "new")
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
            return discover_offers(args.project, args.max_daily_usd, disk_size_gb=getattr(args, 'disk_size_gb', 100))
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


def eligible_tier_offers(offers, tier, prices, disk_size_gb=100, job_memory_gib=15):
    from .tier_policy import reservation_fits
    if not reservation_fits((tier.name,),job_memory_gib):
        return []
    candidates = []
    for offer in offers:
        if (offer.vcpu,offer.ram_gib) != (tier.vcpu,tier.ram_gib) or machine_profile(offer.machine_type) is None:
            continue
        try:
            boot,data,monthly = cheapest_storage(offer.machine_type,disk_size_gb,prices.get(offer.region,{}))
        except RuntimeError:
            continue
        if data not in ('pd-balanced','pd-ssd','hyperdisk-balanced'):
            continue  # Tier build caches require SSD-backed random-IO storage.
        for rank,platform in platform_preferences(offer.machine_type):
            candidate = CpuOffer(offer.region,offer.machine_type,offer.vcpu,offer.ram_gib,
                                 offer.hourly_usd,platform,rank,boot,data,monthly,zones=offer.zones)
            if tier.affordable(candidate.total_daily_usd):
                candidates.append(candidate)
    return sorted(candidates,key=lambda row:(row.cpu_rank,row.total_daily_usd,row.region,row.machine_type))


def discover_tier_offers(project,tier,disk_size_gb=100,job_memory_gib=15,*,status=None):
    if status is not None:status['complete']=False
    discovery={}
    clear_catalog_cache()
    offers,_,_,errors = asyncio.run(find(get_auth(project),min_vcpu=tier.vcpu,
        min_ram=tier.ram_gib,top=None,concurrency=200,
        catalog_rates=parse_component_rates(catalog_skus()),status=discovery))
    if errors:
        LOG.warning('Tier discovery has %d incomplete price results',len(errors))
    prices = {region:storage_rates(region) for region in {row.region for row in offers
              if (row.vcpu,row.ram_gib) == (tier.vcpu,tier.ram_gib)}}
    ranked = eligible_tier_offers(offers,tier,prices,disk_size_gb,job_memory_gib)
    candidates,skipped = launchable_candidates(ranked,load_launch_inventory(project),disk_size_gb)
    if status is not None:
        def required(machine,vcpu,ram):
            return ((vcpu,ram)==(tier.vcpu,tier.ram_gib) and machine_profile(machine) is not None
                    and bool(platform_preferences(machine)))
        missing=any(required(row['machine_type'],row['vcpu'],row['ram_gib'])
                    for row in discovery.get('missing_compute_scopes',[]))
        quotes_complete=True
        for offer in offers:
            if not required(offer.machine_type,offer.vcpu,offer.ram_gib):continue
            kinds=('hyperdisk-balanced',) if machine_profile(offer.machine_type).disk_type=='hyperdisk-balanced' else ('pd-standard','pd-balanced','pd-ssd')
            try:
                values=[Decimal(str(prices[offer.region][kind+':capacity'])) for kind in kinds]
                quotes_complete &= all(value.is_finite() and value>=0 for value in values)
            except (KeyError,ValueError,ArithmeticError):quotes_complete=False
        status['complete']=(discovery.get('inventory_complete') is True and not errors and not missing and quotes_complete)
    LOG.info('Tier %s: %d launchable candidates strictly below $%s/day with40+%d GiB disks',
             tier.name,len(candidates),tier.max_daily_usd,disk_size_gb)
    return candidates


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


def launch_slot_count(offer, args) -> int:
    """Return the runner listener count ``launch_offer`` provisions for ``offer``."""
    if getattr(args, 'scaling_mode', 'resource') == 'tiered':
        tier = tier_for_shape(offer.vcpu, offer.ram_gib)
        if tier:
            return int(getattr(args, 'launch_slots', tier.initial_slots))
    return 8


RUNNER_HOST_PREFIX = "oxidex-runners-"
GCE_NAME_MAX_LENGTH = 63  # GCE instance and disk names
# Lowercase RFC 4648 base32: valid in GCE resource names and runner names.
_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
TARGET_TAG_LENGTH = 4
LAUNCH_RANDOM_LENGTH = 5


def target_name_tag(target: str) -> str:
    """Stable short tag of a GitHub target for launched host names (case-insensitive, like GitHub)."""
    import hashlib
    digest = int.from_bytes(hashlib.sha256(str(target).strip().lower().encode("utf-8")).digest()[:4], "big")
    return "".join(_NAME_ALPHABET[(digest >> (5 * index)) & 31] for index in range(TARGET_TAG_LENGTH))


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        value, remainder = divmod(value, 36)
        out = digits[remainder] + out
        if not value:
            return out


def launch_name_suffix(region: str, zone: str, target: str, timestamp: float | None = None,
                       random_part: str | None = None) -> str:
    """Suffix shared by a launched host (``oxidex-runners-<suffix>``) and its data disk (``runner-data-<suffix>``).

    ``<region>-<zone letter>-<target tag>-<base36 seconds><random>``. The target tag keeps controllers of
    different GitHub targets in one project from choosing the same name, and the random part keeps two launches
    in the same second apart. The worst-case region keeps the VM name within GCE's 63 characters and its
    runner names within GitHub's 64.
    """
    import secrets
    random_part = random_part if random_part is not None else "".join(
        secrets.choice(_NAME_ALPHABET) for _ in range(LAUNCH_RANDOM_LENGTH))
    seconds = int(time.time() if timestamp is None else timestamp)
    return (f"{region.replace('-', '')}-{zone.rsplit('-', 1)[-1]}-{target_name_tag(target)}-"
            f"{_base36(seconds)}{random_part}")


def launch_offer(offer, args: argparse.Namespace, state=None, state_path=None) -> GceVm:
    """Try each zone until the manager confirms a complete runner deployment; return the launched VM."""
    state_path = state_path or args.state_file.expanduser()
    state = state if state is not None else read_scale_state(state_path)
    guard_fleet_mutation(state)
    if state.get("host_operation"):
        raise RuntimeError("A previous host operation is still pending")
    preferences = platform_preferences(offer.machine_type)
    if not preferences:
        raise RuntimeError(f'{offer.machine_type} cannot request a CPU platform; refusing automatic launch')
    cpu_platform = getattr(offer, 'cpu_platform', preferences[0][1])
    cpu_rank = next((rank for rank, platform in preferences if platform == cpu_platform), None)
    if cpu_rank is None:
        raise RuntimeError(f'Unsupported CPU platform {cpu_platform} for {offer.machine_type}')
    tier = tier_for_shape(offer.vcpu,offer.ram_gib) if getattr(args,'scaling_mode','resource')=='tiered' else None
    if getattr(args,'scaling_mode','resource')=='tiered':
        if not tier or tier.name != getattr(args,'launch_tier',None):
            raise RuntimeError('Automatic tier launch rejects forbidden or mismatched shape')
        if not tier.affordable(getattr(offer,'total_daily_usd',None)):
            raise RuntimeError('Tier launch requires a strict full-disk quote')
    if tier:
        from math import isfinite
        try:
            job_memory=float(getattr(args,'tier_job_memory_gib',getattr(args,'launch_job_memory_gib',15)))
        except (TypeError,ValueError) as error:
            raise RuntimeError('Tier job memory must be finite and positive') from error
        if not isfinite(job_memory) or job_memory<=0:
            raise RuntimeError('Tier job memory must be finite and positive')
    daily_limit = tier.max_daily_usd if tier else args.max_daily_usd
    slots = launch_slot_count(offer, args)
    hourly_limit = daily_limit / Decimal(24)
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
        suffix = launch_name_suffix(offer.region, zone, _active_target(args))
        instance_name = f"{RUNNER_HOST_PREFIX}{suffix}"
        runner_prefix = "gha"
        # Runner names are the VM name plus at least six characters, so this check also keeps the VM name
        # and its shorter runner-data-<suffix> disk name within GCE_NAME_MAX_LENGTH.
        longest_name = maximum_runner_name_length(runner_prefix, instance_name, slots)
        if longest_name > 64:
            raise RuntimeError(f"Generated GitHub runner name would be {longest_name} characters (maximum 64)")
        command = [
            sys.executable, str(ROOT / "src" / "spot_runner_manager.py"),
            "--project", args.project, "--org", args.org,
            "--max-cost", str(hourly_limit), "--instance-type", offer.machine_type,
            '--max-total-daily-usd', str(daily_limit), '--cheapest-storage',
            "--min-cpu-platform", cpu_platform,
            "--region", offer.region, "--zone", zone,
            "--cpu", str(offer.vcpu), "--ram", str(int(offer.ram_gib)),
            "--instance-name", instance_name,
            "--disk-device-name", f"runner-data-{suffix}",
            "--disk-size-gb", str(args.disk_size_gb),
            "--runner-prefix", runner_prefix,
            "--runner-label", args.label, "--apply",
        ]
        if getattr(args, "target", None):
            command.extend(["--target", args.target])
        # Every host this controller launches gets the same verified effective patch, so a
        # replacement never comes up without the manifest its predecessor ran.
        if getattr(args, "patch_repo", None):
            command.extend(["--patch-repo", args.patch_repo])
        if getattr(args, "config_repo", None):
            command.extend(["--config-repo", args.config_repo])
        if tier:
            command.extend(['--tier',tier.name,'--tier-slots',str(slots),
                            '--tier-job-memory-gib',str(job_memory)])
        if tier and getattr(args, 'remote_builder', False):
            command.append('--remote-builder')
        if getattr(args, 'scaling_mode', 'resource') in ('resource','tiered'):
            command.append('--resource-monitoring')
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
        deadline = 2400 if tier and getattr(args, 'remote_builder', False) else LAUNCH_DEADLINE_SECONDS
        command.extend(["--deployment-receipt", str(receipt),
                        "--registration-timeout-seconds", str(deadline)])
        guard_fleet_mutation(state)
        rotation = state.get("price_rotation")
        if isinstance(rotation, dict) and rotation.get("phase") == "provision":
            # Persist this launch's identity in the same write as the provider intent so the
            # rotation can only ever adopt the VM it created, even across a controller crash.
            rotation["pending_replacement"] = {"name": instance_name, "zone": zone,
                                               "expected_runner_count": slots}
            # Retirement requires every listener this launch provisions to be online.
            rotation["expected_runner_count"] = slots
            rotation["expected_runner_count_source"] = "launch_sizing"
        begin_host_operation("launch", vm, [], [], state, state_path,
                             requested_cpu_platform=cpu_platform, deployment_receipt=str(receipt),
                             candidate_key=f"{zone}/{offer.machine_type}/{cpu_platform}",
                             data_disk_name=f"runner-data-{suffix}", started_at=now(),
                             deadline_seconds=deadline,expected_runner_count=slots)
        started = time.monotonic()
        manager_hung = False
        try:
            # The manager enforces the registration deadline; this backstop covers a hung manager.
            guard_fleet_mutation(state)
            result = subprocess.run(command, check=False, text=True, capture_output=True,
                                    timeout=deadline + 120)
        except subprocess.TimeoutExpired:
            manager_hung = True
            result = SimpleNamespace(returncode=-9, stdout="",
                                     stderr="Deployment manager timed out before reporting an outcome")
        identity_observed = import_deployment_receipt(state, state_path)
        for output in (result.stdout, result.stderr):
            for line in (output or "").splitlines():
                if line.strip():
                    LOG.info("Deployment diagnostics: %s; %s", vm_details(vm), error_detail(line))
        if manager_hung and registered_host(vm, args,slots):
            LOG.warning("Deployment manager hung after all eight runners registered; keeping %s", vm_details(vm))
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
        if result.returncode == 0:
            state["host_operation"]["registered"] = True
            write_scale_state(state_path, state)
            elapsed = time.monotonic() - started
            LOG.info("Instance registered to take jobs in %s runners: %s; %.1f seconds from starting "
                     "deployment to manager confirmation of online runner registration and storage checks",
                     args.org, vm_details(vm), elapsed)
            return vm
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


def registered_host(vm: GceVm, args: argparse.Namespace, count=8) -> bool:
    """Return whether the launched VM is running with all eight runners online."""
    current = next((item for item in list_gcloud_vms(args.project, name_prefix="oxidex-runners-")
                    if item.zone == vm.zone and item.name == vm.name), None)
    if not current or not current.running:
        return False
    target = _active_target(args)
    online = {runner.name for runner in host_runners(vm, list_github_runners(target), [vm]) if runner.online}
    return {f"gha-{vm.name}-{slot}" for slot in range(1, count+1)}.issubset(online)


def abandon_launch(args: argparse.Namespace, state: dict, state_path: Path) -> bool:
    """Delete a failed or stalled launch's VM, disks, and runner records.

    Returns True once fresh inventories confirm all three are absent and the
    host operation is cleared; otherwise cleanup continues on the next check.
    """
    operation = state["host_operation"]
    from .fleet_logging import vm_from_record
    vm = vm_from_record(operation["vm"])
    if runner_inventory_stale(state):raise RuntimeError('Launch cleanup held: stale runner inventory')
    reason = operation["abandon"]
    if operation.get("candidate_key"):
        state.setdefault("failed_launch_candidates", {})[operation["candidate_key"]] = now()
    key = f"{vm.zone}/{vm.name}"
    current = next((item for item in list_gcloud_vms(args.project, name_prefix="oxidex-runners-")
                    if item.zone == vm.zone and item.name == vm.name), None)
    if current and vm.instance_id and current.instance_id!=vm.instance_id:
        LOG.warning('Launch cleanup held: provider identity changed')
        return False
    target = _active_target(args)
    if current:
        fresh=list_github_runners(target)
        state['runner_inventory_observed_at']=time.time()
        protected_heavy_routes([current],fresh,state)
        guard_fleet_mutation(state,current)
    if getattr(args,'scaling_mode','resource')=='tiered':
        if current:
            if not vm.instance_id or current.instance_id!=vm.instance_id:
                LOG.error('Tier launch cleanup holds: missing or changed provider identity')
                return False
            assigned=host_runners(vm,list_github_runners(target),[vm])
            operation.update(kind='drain',runner_ids=[r.runner_id for r in assigned],
                runner_names={str(r.runner_id):r.name for r in assigned},
                drain_mode='isolated_group',drain_group_name=f'spot-drain-{vm.name}-{vm.instance_id}')
            write_scale_state(state_path,state)
            LOG.warning('Failed tier launch will isolate and drain before deleting %s',vm.name)
            return False
        if (operation.get('outcome_uncertain') and now()-operation.get('started_at',now())
                <operation.get('deadline_seconds',LAUNCH_DEADLINE_SECONDS)):
            return False
        # Missing hosts use the same recorded disk-ID cleanup as Spot recovery.
        if not vm.instance_id:
            names={vm.name,operation.get('data_disk_name') or vm.name.replace('oxidex-runners-','runner-data-',1)}
            inventory=subprocess.run(['gcloud','compute','disks','list',f'--project={args.project}',
                f'--zones={vm.zone}','--format=json(name,id)'],check=True,text=True,capture_output=True,timeout=60)
            if (any(row.get('name') in names for row in json.loads(inventory.stdout))
                    or host_runners(vm,list_github_runners(target),[vm])):
                LOG.warning('Tier launch cleanup holds until receipt proves orphan resource identity')
                return False
            state.pop('host_operation')
            write_scale_state(state_path,state)
            return True  # Failed create proven absent; no deletion was authorized.

        operation.update(kind='delete_cleanup')
        write_scale_state(state_path,state)
        return False
    if current:
        if key not in state.setdefault("intentional_deletions", []):
            state["intentional_deletions"].append(key)
        if current.instance_id:
            state.setdefault("intentional_deletion_ids", {})[key] = current.instance_id
        write_scale_state(state_path, state)
        LOG.warning("Deleting launch %s with its disks: %s", vm_details(current), reason)
        guard_fleet_mutation(state,current)
        subprocess.run(["gcloud", "compute", "instances", "delete", vm.name, f"--zone={vm.zone}",
                        f"--project={args.project}", "--delete-disks=all", "--quiet"],
                       check=True, text=True, capture_output=True, timeout=300)
        if any(item.zone == vm.zone and item.name == vm.name
               for item in list_gcloud_vms(args.project, name_prefix="oxidex-runners-")):
            return False
    elif (operation.get("outcome_uncertain")
          and now() - operation.get("started_at", now()) < operation.get('deadline_seconds', LAUNCH_DEADLINE_SECONDS)):
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
        if runner_inventory_stale(state):raise RuntimeError('Disk cleanup held: stale runner inventory')
        subprocess.run(["gcloud", "compute", "disks", "delete", disk["name"], f"--zone={vm.zone}",
                        f"--project={args.project}", "--quiet"],
                       check=True, text=True, capture_output=True, timeout=120)
        LOG.info("Deleted disk %s left by launch %s", disk["name"], vm.name)
    for runner in host_runners(vm, list_github_runners(target), [vm]):
        subprocess.run(["gh", "api", "-X", "DELETE", f"{target_api_prefix(target)}/actions/runners/{runner.runner_id}"],
                       check=True, text=True, capture_output=True, timeout=60)
    if host_runners(vm, list_github_runners(target), [vm]):
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
    plist['ProgramArguments'].extend(['--scaling-mode', args.scaling_mode, '--maximum-hosts', str(args.maximum_hosts)])
    plist['ProgramArguments'].extend(['--tier-job-memory-gib',str(getattr(args,'tier_job_memory_gib',15))])
    plist['ProgramArguments'].append('--remote-builder' if args.remote_builder else '--no-remote-builder')
    if getattr(args,'github_user',None):plist['ProgramArguments'].extend(['--github-user',args.github_user])
    if getattr(args,'migrate_oversized',False):plist['ProgramArguments'].append('--migrate-oversized')
    if getattr(args, 'billing_export_table', None):
        plist['ProgramArguments'].extend(['--billing-export-table', args.billing_export_table])
    if getattr(args, 'manage_control_vm', False):
        plist['ProgramArguments'].append('--manage-control-vm')
    if getattr(args, 'target', None):
        plist['ProgramArguments'].extend(['--target', args.target])
    if getattr(args, 'patch_repo', None):
        plist['ProgramArguments'].extend(['--patch-repo', args.patch_repo])
    if getattr(args, 'config_repo', None):
        plist['ProgramArguments'].extend(['--config-repo', args.config_repo])
    if getattr(args, 'make_before_break', False):
        plist['ProgramArguments'].append('--make-before-break')
    plist_path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], check=False)
    return plist_path


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="swack-tools")
    parser.add_argument("--target", default=None, help="Target repository (owner/repo) or organization (org)")
    parser.add_argument("--make-before-break", action=argparse.BooleanOptionalAction, default=False,
                        help="Execute zero-downtime make-before-break rotation on price spikes")
    parser.add_argument("--github-user",help="Select an existing gh account without persisting its credential")
    parser.add_argument("--project", default="homelab-424523")
    parser.add_argument("--minimum-runners", type=int, default=1)
    parser.add_argument("--label", default="spot")
    parser.add_argument("--max-daily-usd", type=Decimal, default=Decimal("10"),
                        help='Maximum daily compute plus boot/data storage cost per VM')
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument('--scaling-mode', choices=('tiered','resource', 'legacy'), default='resource',
                        help='Resource: two 5-minute checks above/below 75%% CPU/memory; legacy: runner occupancy')
    parser.add_argument('--maximum-hosts', type=int, default=5)
    def job_memory(value):
        from math import isfinite
        value=float(value)
        if not isfinite(value) or value<=0:
            raise argparse.ArgumentTypeError('Job memory must be finite and positive')
        return value
    parser.add_argument('--tier-job-memory-gib',type=job_memory,default=15,
                        help='Per-job cap/reservation; default 15 GiB is a verified compatible workload cap, not a measured peak')
    parser.add_argument('--dry-run',action='store_true',help='Read-only tier decision; never mutate providers or controller state')
    parser.add_argument('--manage-control-vm', action='store_true', default=False,
                        help='Also manage the Spot control VM that hosts the dashboard (bootstrap, preemption '
                             'recovery, make-before-break price rotation); with --dry-run only print its plan')
    parser.add_argument('--migrate-oversized',action='store_true',help='Gradually replace existing oversized owned hosts')
    parser.add_argument('--remote-builder', action=argparse.BooleanOptionalAction, default=True,
                        help='Install dedicated remote Rust builders on new nodes (default enabled)')
    parser.add_argument("--disk-size-gb", type=int, default=100)
    parser.add_argument('--billing-export-table', help='Billing export project.dataset.table for launch cost logs (or GCP_BILLING_EXPORT_TABLE)')
    parser.add_argument("--log-file", type=Path, default=Path.home() / "spot-ghrunner.log")
    parser.add_argument("--state-file", type=Path,
                        default=Path.home() / "Library" / "Application Support"
                        / "Spot Runner Autoscaler" / "state.json")
    parser.add_argument("--install-launch-agent", action="store_true")
    parser.add_argument("--patch-repo", default=None,
                        help="OWNER/REPO whose spot-runner-patch manifest every launched host gets (required to "
                             "rotate hosts that run a patch)")
    parser.add_argument("--config-repo", default=None,
                        help="Central runner configuration repository (default <org>/spot-runners-config)")
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
    if (args.maximum_hosts < 1 or (args.scaling_mode in ('resource','tiered') and args.interval_seconds != 300)
            or (args.scaling_mode=='tiered' and args.maximum_hosts>5)):
        raise ValueError('Resource mode requires 300-second checks and a positive maximum host count')
    load_env(ROOT / ".env")
    if args.github_user:
        secret=subprocess.check_output(["gh","auth","token","--user",args.github_user],text=True).strip()
        if not secret:raise RuntimeError("Missing requested GitHub account credential")
        os.environ["GH_TOKEN"]=secret
    args.billing_export_table = args.billing_export_table or os.environ.get('GCP_BILLING_EXPORT_TABLE')
    if args.install_launch_agent:
        print(f"Installed LaunchAgent at {install_launch_agent(args)}")
        return 0

    with lifecycle_log(args.log_file):
        LOG.info("Checking fleet every %d seconds; total compute/storage cap $%s/day per VM",
                 args.interval_seconds, args.max_daily_usd)
        return run_controller(args)


def manage_control_vm(args: argparse.Namespace, state: dict, state_path: Path):
    """Run one control VM lifecycle step; its failures never stop runner management.

    Control VMs (``spot-control-*``) are separate from runner hosts: runner inventory, price
    rotation, and scaling never list, rotate, or delete them.

    Several --target controllers can manage the one control VM pair of a project, so each check takes the
    project-wide control VM lock (skipping the check while another controller holds it) and works on the
    project-scoped control VM state, never on this controller's own state file. Control VM state saved in
    this controller's state file before project scoping is moved to the project state first.
    """
    from .control_vm import ControlStateConflict, ProjectControlLock, autoscaler_tick
    dry_run = getattr(args, 'dry_run', False)
    try:
        with ProjectControlLock(args.project, getattr(args, 'control_state_dir', None)) as project:
            if project is None:
                LOG.info('Control VM management skipped this check: another controller holds the %s control VM '
                         'lock', args.project)
                return None
            if dry_run:
                project.adopt(copy.deepcopy(state), state_path, persist=False)
                return autoscaler_tick(args, copy.deepcopy(project.state), lambda: None)
            if project.adopt(state, state_path, persist=True):
                write_scale_state(state_path, state)
            return autoscaler_tick(args, project.state, project.save)
    except (ControlStateConflict, OSError, ValueError) as exc:
        # Control VM failures never stop runner management.
        LOG.error('Control VM management failed this check: %s', exc)
        return None


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


def bind_state_to_target(state: dict, args: Any) -> bool:
    """Bind controller state to its GitHub target; refuse state saved by another target.

    Saved observations, recovery obligations, rotations and host operations all assume one target.
    Reusing a state file under another --target would turn the old target's absent hosts into
    "lost" capacity and replace or clean them up under the wrong target. State saved before
    binding existed belongs to the default organization target. Returns True when the binding
    was just added (the caller persists it).
    """
    active = str(_active_target(args)).lower()
    bound = state.get("controller_target")
    if isinstance(bound, str) and bound:
        if bound.lower() != active:
            raise RuntimeError(f"Controller state belongs to target {bound!r}, not {active!r}; "
                               "use a separate --state-file for each target")
        return False
    legacy = any(state.get(key) for key in ("observed_vms", "pending_vm_recovery", "price_rotation",
                                            "host_operation", "replacement_cleanups"))
    default_org = str(getattr(args, "org", None) or DEFAULT_ORG).lower()
    if legacy and active != default_org:
        raise RuntimeError(f"Controller state predates target binding and belongs to {default_org!r}; "
                           f"use a separate --state-file for target {active!r}")
    state["controller_target"] = active
    return True


def _run_controller(args: argparse.Namespace) -> int:
    """Run fleet checks until interrupted or a single check completes."""
    state_path = args.state_file.expanduser()
    state = read_scale_state(state_path)
    if bind_state_to_target(state, args) and not getattr(args, 'dry_run', False):
        write_scale_state(state_path, state)
    if getattr(args,'dry_run',False):
        if getattr(args, 'manage_control_vm', False):
            # Plans only: the control VM manager reads inventory, KV, and health but never mutates.
            manage_control_vm(args, state, state_path)
        from .tier_controller import dry_run
        return dry_run(args,state)
    jobs = JobRecovery(state, lambda: write_scale_state(state_path, state),
                       tracked_job_repositories(args))
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
        runners = list_github_runners(_active_target(args))
        state['runner_inventory_observed_at']=time.time()
        vms = scope_fleet_to_target(list_gcloud_vms(args.project, name_prefix="oxidex-runners-"), args, state)
        reconcile_deletion_identities(vms, state)
        jobs.poll(vms)
        observe_vms(vms, state)
        write_scale_state(state_path, state)
        reconcile_replacement_cleanups(vms, runners, args, state, state_path)
        LOG.info(fleet_summary(vms, runners, state))
        log_hourly_spend(vms, args, state, state_path)
        if getattr(args, 'manage_control_vm', False):
            manage_control_vm(args, state, state_path)
        if runner_inventory_stale(state):
            LOG.warning('Fleet changes held: strict runner inventory expired during observation')
            from .tier_controller import check_tiers
            check_tiers(vms,runners,args,state,state_path)
            if args.once:return 0
            time.sleep(args.interval_seconds)
            continue
        operation_work = reconcile_host_operation(vms, runners, args, state, state_path)
        recovery_work = False if operation_work else reconcile_lost_vms(vms, runners, args, state, state_path)
        protected=bool(protected_heavy_routes(vms,runners,state)) or heavy_route_capacity_hold(state)
        price_work = False if protected or recovery_work or operation_work or args.scaling_mode=='tiered' else reconcile_prices(vms, runners, args, state, state_path)
        if protected:
            from .tier_controller import check_tiers
            check_tiers(vms,runners,args,state,state_path)
            if args.once:return 0
            time.sleep(args.interval_seconds)
            continue
        if operation_work or price_work or recovery_work:
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
            continue
        fleet_vms = spot_instances(vms)
        if args.scaling_mode != 'tiered':
            record_runner_floor(vms, args, state)
        current, busy = utilization_counts(runners)
        LOG.info("Managed GCP runners: %d online, %d busy; %d Spot VMs",
                 current, busy, len(fleet_vms))
        if args.scaling_mode=='tiered':
            from .tier_controller import check_tiers
            from .poll_cadence import tier_poll_delay
            check_started_at = time.time()
            check_tiers(vms,runners,args,state,state_path)
            if args.once:return 0
            time.sleep(tier_poll_delay(state,args.interval_seconds,check_started_at,time.time()))
            continue
        if args.scaling_mode == 'resource':
            roster = sorted(f'{vm.zone}/{vm.instance_id}' for vm in fleet_vms)
            if state.get('resource_roster') != roster:
                state['resource_evidence'] = {}
                state['resource_roster'] = roster
            try:
                observed = read_utilization(args.project, fleet_vms)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                LOG.warning('Resource metrics unavailable; optimization waits: %s', error_detail(exc))
                observed = None
            action = resource_decision(observed, state.setdefault('resource_evidence', {}), time.time())
            LOG.info('Resource check: CPU/memory fractions=%s; decision=%s; threshold=75%%, two 5-minute checks', observed, action)
            if action == 'up' and len(fleet_vms) >= args.maximum_hosts:
                action = 'wait'
            if action == 'down' and len(fleet_vms) <= state.get('min_runner_floor', 1):
                LOG.info('Resource scale-down holds at the %d-host floor for %d runners',
                         state.get('min_runner_floor', 1), args.minimum_runners)
                action = 'wait'
            host_count, idle_loops, scale_up_active = (1 if action in ('up', 'down') else 0), 0, False
        else:
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
            LOG.info("Low usage persisted for two checks; draining up to %d Spot hosts", host_count)
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
