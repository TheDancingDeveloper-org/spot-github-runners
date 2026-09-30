"""Recover interrupted hosts after verifying their recorded drives are gone."""
from __future__ import annotations

import json
import logging
import re
import subprocess

from .fleet_logging import vm_details, vm_from_record
from .log_format import error_detail
from .runner_fleet import GceVm

LOG = logging.getLogger('spot_runner_autoscaler')
DISK_SOURCE = re.compile(r'(?:https://(?:www.googleapis.com|compute.googleapis.com)/compute/v1/)?projects/([^/]+)/(zones|regions)/([^/]+)/disks/([^/]+)$')


def _list(args: list[str]) -> list[dict]:
    result = subprocess.run(['gcloud', 'compute', *args, '--format=json'],
                            check=True, text=True, capture_output=True, timeout=60)
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError('Provider inventory is not a list')
    return rows


def cleanup_lost_vm(vm: GceVm, project: str) -> bool:
    """Delete only the same stopped host and its recorded auto-delete disks."""
    if not vm.spot or not vm.name.startswith('oxidex-runners-'):
        raise ValueError('Recovery requires a managed Spot VM')
    sources = []
    for disk in vm.disks:
        if disk.get('type') == 'SCRATCH':
            continue  # Local SSDs disappear with the host; no persistent disk remains.
        source = disk.get('source', '')
        match = DISK_SOURCE.fullmatch(source)
        if not match or match[1] != project or not disk.get('autoDelete'):
            LOG.warning('Disk cleanup cannot authorize retained or foreign drive %s for %s; retrying next loop',
                        source or 'unknown', vm_details(vm))
            return False
        sources.append((disk, match[2], match[3], match[4]))
    if not vm.disks:
        LOG.warning('Disk cleanup pending for %s: previous inventory has no disk sources; cannot verify drive deletion',
                    vm_details(vm))
        return False
    instances = _list(['instances', 'list', f'--project={project}', f'--zones={vm.zone}'])
    existing = next((item for item in instances if item.get('name') == vm.name), None)
    if existing:
        if (existing.get('status') != 'TERMINATED' or not vm.instance_id
                or str(existing.get('id')) != vm.instance_id):
            LOG.warning('Disk cleanup waits for %s: host is active or its identity changed', vm_details(vm))
            return False
        recorded = {disk['source'] for disk, *_ in sources}
        attachments = existing.get('disks', [])
        if any(disk.get('type') != 'SCRATCH' and
               (disk.get('source') not in recorded or not disk.get('autoDelete'))
               for disk in attachments):
            LOG.warning('Disk cleanup waits for %s: current attachments differ from recorded auto-delete drives',
                        vm_details(vm))
            return False
        disk_inventory = {item.get('selfLink'): item for item in _list(['disks', 'list', f'--project={project}'])}
        for disk, *_ in sources:
            current = disk_inventory.get(disk['source'])
            if (not current or not disk.get('diskId')
                    or str(current.get('id')) != str(disk['diskId'])):
                LOG.warning('Disk cleanup waits for %s: attached drive identity is unknown or changed', vm_details(vm))
                return False
        subprocess.run(['gcloud', 'compute', 'instances', 'delete', vm.name,
                        f'--zone={vm.zone}', f'--project={project}', '--delete-disks=all', '--quiet'],
                       check=True, text=True, capture_output=True, timeout=180)
        LOG.info('Deleted terminated Spot VM and its attached disks: %s', vm_details(vm))
        if any(item.get('name') == vm.name for item in _list(
                ['instances', 'list', f'--project={project}', f'--zones={vm.zone}'])):
            return False
    # Exact source URLs restrict deletion to drives from the saved attachment inventory.
    rows = _list(['disks', 'list', f'--project={project}'])
    inventory = {item.get('selfLink'): item for item in rows}
    for disk, scope, location, name in sources:
        current = inventory.get(disk['source'])
        if current is None:
            continue
        if (current.get('users') or current.get('status') != 'READY'
                or not disk.get('diskId') or str(current.get('id')) != str(disk['diskId'])):
            LOG.warning('Disk cleanup retains drive %s for %s: attached, not ready, or identity changed',
                        name, vm_details(vm))
            continue
        subprocess.run(['gcloud', 'compute', 'disks', 'delete', name,
                        f"--{'zone' if scope == 'zones' else 'region'}={location}",
                        f'--project={project}', '--quiet'],
                       check=True, text=True, capture_output=True, timeout=180)
        LOG.info('Deleted detached drive %s from %s', name, vm_details(vm))
    remaining = {item.get('selfLink') for item in _list(['disks', 'list', f'--project={project}'])}
    complete = not any(disk['source'] in remaining for disk, *_ in sources)
    if complete:
        LOG.info('Verified all recorded drives are deleted for %s', vm_details(vm))
    return complete



def reconcile_lost_vms(vms, runners, args, state, state_path) -> bool:
    """Share a persisted restoration target with price recovery; launch each deficit once."""
    from .spot_runner_autoscaler import host_runners, reconcile_prices, spot_instances, write_scale_state

    pending = state.get('pending_vm_recovery', {})
    if not pending:
        return False
    new_losses = sum(not record.get('_target_recorded') for record in pending.values())
    if new_losses:
        state['price_replacement_target'] = max(state.get('price_replacement_target', 0),
                                                len(spot_instances(vms)) + new_losses)
        for record in pending.values():
            record['_target_recorded'] = True
        write_scale_state(state_path, state)
    for key, record in list(pending.items()):
        if record.get('_cleanup_complete'):
            continue
        vm = vm_from_record(record)
        try:
            if not cleanup_lost_vm(vm, args.project):
                continue
            for runner in host_runners(vm, runners, [vm, *[other for other in vms if other.name != vm.name]]):
                subprocess.run(['gh', 'api', '-X', 'DELETE',
                                f'orgs/{args.org}/actions/runners/{runner.runner_id}'],
                               check=True, text=True, capture_output=True, timeout=60)
            record['_cleanup_complete'] = True
            write_scale_state(state_path, state)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            LOG.warning('Recovery pending for %s; retrying next loop: %s', vm_details(vm), error_detail(exc))
    # This still checks live prices when cleanup is blocked. The price reconciler
    # holds new launches until all recorded drives are verified gone.
    reconcile_prices(vms, runners, args, state, state_path)
    return True
