"""Append controller lifecycle events and VM observations to a local log."""

from __future__ import annotations

import logging
import os
import signal
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from pathlib import Path

from .log_format import SingleLineFormatter
from .runner_fleet import GceVm

LOG = logging.getLogger("spot_runner_autoscaler")


@contextmanager
def lifecycle_log(path: Path):
    """Append timestamped events and record normal or signal-driven shutdowns."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(SingleLineFormatter("%(asctime)s %(levelname)s %(message)s"))
    previous_level = LOG.level
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    previous_signals = {}

    def stop(signum, frame):
        LOG.info("Controller received %s; shutting down", signal.Signals(signum).name)
        raise SystemExit(0)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_signals[signum] = signal.signal(signum, stop)
        LOG.info("Controller started: PID %d; append log %s", os.getpid(), path)
        yield
    except Exception as exc:
        LOG.error("Controller stopped after an error of type %s; see service diagnostics",
                  type(exc).__name__)
        raise
    finally:
        LOG.info("Controller shutdown: PID %d", os.getpid())
        for signum, previous in previous_signals.items():
            signal.signal(signum, previous)
        LOG.removeHandler(handler)
        handler.close()
        LOG.setLevel(previous_level)


def vm_details(vm: GceVm) -> str:
    """Describe observed hardware; never infer sizes from a machine name."""
    cpu = f"{vm.vcpu:g} vCPU" if vm.vcpu is not None else "vCPU unknown"
    ram = f"{vm.ram_gib:g} GiB RAM" if vm.ram_gib is not None else "RAM unknown"
    specs = [vm.machine_type or "machine type unknown", cpu, ram]
    specs.append(f"CPU platform: {vm.cpu_platform or 'unknown'}")
    if vm.requested_cpu_platform:
        specs.append(f"requested CPU platform: {vm.requested_cpu_platform}")
    for disk in vm.disks:
        size = disk.get("diskSizeGb", "unknown")
        kind = disk.get("diskType") or disk.get("type", "storage type unknown")
        interface = disk.get("interface", "interface unknown")
        role = "root" if disk.get("boot") else "data"
        specs.append(f"{size} GiB {role} disk {kind} {interface}".strip())
    if not vm.disks:
        specs.append("disk specifications unknown")
    for accelerator in vm.accelerators:
        kind = accelerator.get("acceleratorType", "GPU unknown").rsplit("/", 1)[-1]
        specs.append(f"{accelerator.get('acceleratorCount', 'unknown')} GPU {kind}")
    suffix = "; specs: last known" if vm.specs_last_known else ""
    return f"instance type: {', '.join(specs)}; zone: {vm.zone}; instance name: {vm.name}{suffix}"


def vm_from_record(record: dict) -> GceVm:
    allowed = {field.name for field in fields(GceVm)}
    return GceVm(**{key: value for key, value in record.items() if key in allowed})



def reconcile_deletion_identities(vms, state: dict) -> None:
    """Remove old-instance intent before either observer considers a new identity."""
    intentional = state.setdefault('intentional_deletions', [])
    ids = state.get('intentional_deletion_ids', {})
    for vm in vms:
        key = f'{vm.zone}/{vm.name}'
        if key in intentional and ids.get(key) and vm.instance_id and ids[key] != vm.instance_id:
            intentional.remove(key)
            ids.pop(key, None)
            LOG.warning('Intentional deletion record belongs to a previous instance identity: %s', vm_details(vm))


def recovery_key(key: str, vm: GceVm, pending: dict) -> str:
    """Keep independent cleanup authorizations for reused VM names."""
    if key in pending and pending[key].get('instance_id') != vm.instance_id:
        return f"{key}#{vm.instance_id or 'unknown'}"
    return key

def observe_vms(vms, state: dict) -> None:
    """Persist hardware and recovery work before the controller changes resources."""
    reconcile_deletion_identities(vms, state)
    previous = state.get("observed_vms", {})
    current = {}
    pending = state.setdefault("pending_vm_recovery", {})
    intentional = state.setdefault("intentional_deletions", [])
    for vm in vms:
        if not vm.spot or not vm.name.startswith("oxidex-runners-"):
            continue
        key = f"{vm.zone}/{vm.name}"
        old = previous.get(key)
        if (isinstance(old, dict) and vm.instance_id and old.get('instance_id') == vm.instance_id):
            saved_disks = {disk.get('source'): disk for disk in old.get('disks', [])}
            disks = []
            last_known = False
            for disk in vm.disks:
                saved = saved_disks.get(disk.get('source'), {})
                if saved and (not disk.get('diskId') or str(disk.get('diskId')) == str(saved.get('diskId'))):
                    merged = {**saved, **disk}
                    last_known |= merged != disk
                    disks.append(merged)
                else:
                    disks.append(disk)
            same_shape = old.get('machine_type') == vm.machine_type
            cpu = old.get('vcpu') if same_shape and vm.vcpu is None else vm.vcpu
            ram = old.get('ram_gib') if same_shape and vm.ram_gib is None else vm.ram_gib
            platform = vm.cpu_platform or (old.get('cpu_platform', '') if same_shape else '')
            requested = vm.requested_cpu_platform or (old.get('requested_cpu_platform', '') if same_shape else '')
            last_known |= cpu != vm.vcpu or ram != vm.ram_gib or platform != vm.cpu_platform
            vm = replace(vm, vcpu=cpu, ram_gib=ram, disks=tuple(disks), specs_last_known=last_known,
                         cpu_platform=platform, requested_cpu_platform=requested)
        current[key] = asdict(vm)
        old_status = old.get("status") if isinstance(old, dict) else old
        if key not in previous:
            LOG.info("Spot VM observed: %s; status %s", vm_details(vm), vm.status)
        elif old_status != vm.status:
            LOG.warning("Spot VM status changed: %s; %s -> %s", vm_details(vm), old_status, vm.status)
        if vm.status.upper() == "TERMINATED" and key not in intentional:
            pending_key = recovery_key(key, vm, pending)
            if pending_key not in pending:
                LOG.warning("Spot VM is terminated: %s; reason: termination cause is unconfirmed", vm_details(vm))
                LOG.info("Searching for a new VM to replace the old VM; ensuring all drives from the previous instance are deleted: %s", vm_details(vm))
            pending[pending_key] = {**pending.get(pending_key, {}), **asdict(vm)}
    for key in previous.keys() - current.keys():
        old = previous[key]
        if isinstance(old, dict):
            vm = vm_from_record(old)
        else:
            zone, name = key.split("/", 1)
            vm = GceVm(name, zone, str(old), "", True)
        LOG.warning("Spot VM no longer present: %s; reason: consult deletion events for controller actions; otherwise the cause is unconfirmed", vm_details(vm))
        if key not in intentional:
            pending.setdefault(recovery_key(key, vm, pending), asdict(vm))
            LOG.info("Searching for a new VM to replace the old VM; ensuring all drives from the previous instance are deleted: %s", vm_details(vm))
    state["observed_vms"] = current
    state["intentional_deletions"] = [key for key in intentional if key in current]
    state["intentional_deletion_ids"] = {key: value for key, value in state.get("intentional_deletion_ids", {}).items() if key in current}
