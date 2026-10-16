"""Expand ranked Spot offers into zone candidates that pass launch preflight."""
from __future__ import annotations

from dataclasses import dataclass, replace

from .machine_profiles import CpuOffer, machine_profile, platform_preferences

BOOT_DISK_GB = 40
DISK_QUOTA_METRICS = {'pd-standard': 'DISKS_TOTAL_GB', 'pd-balanced': 'SSD_TOTAL_GB', 'pd-ssd': 'SSD_TOTAL_GB'}


@dataclass(frozen=True)
class LaunchInventory:
    """Current provider facts that decide whether a zone can accept a host.

    Quotas map each metric to its (limit, usage) pair.
    """
    platforms_by_zone: dict[str, list[str]]
    disk_types_by_zone: dict[str, set[str]]
    region_quotas: dict[str, dict[str, tuple[float, float]]]
    global_quotas: dict[str, tuple[float, float]]


def candidate_key(offer) -> str:
    """Identify one launch attempt target: zone, shape, and requested platform."""
    return (f"{getattr(offer, 'zone', '') or getattr(offer, 'region', '')}/"
            f"{getattr(offer, 'machine_type', '')}/{getattr(offer, 'cpu_platform', '')}")


def _quota_shortfall(
    offer: CpuOffer,
    inventory: LaunchInventory,
    disk_size_gb: int,
    *,
    arch: str = 'x86_64',
) -> str | None:
    quotas = inventory.region_quotas.get(offer.region, {})
    profile = machine_profile(offer.machine_type, arch=arch)
    if profile is None:
        return f'unsupported {arch} machine type'
    preemptible = quotas.get('PREEMPTIBLE_CPUS', (0.0, 0.0))
    # Spot VMs consume preemptible CPU quota when the project has any; otherwise regular CPU quota.
    cpu_metric = 'PREEMPTIBLE_CPUS' if preemptible[0] > 0 else 'CPUS'
    needs = [(cpu_metric, offer.vcpu), (profile.quota_metric, offer.vcpu), ('INSTANCES', 1)]
    disk_needs: dict[str, int] = {}
    for kind, size in ((offer.boot_disk_type, BOOT_DISK_GB), (offer.data_disk_type, disk_size_gb)):
        metric = DISK_QUOTA_METRICS.get(kind)
        if metric:
            disk_needs[metric] = disk_needs.get(metric, 0) + size
    needs.extend(disk_needs.items())
    for metric, amount in needs:
        if metric in quotas:
            limit, usage = quotas[metric]
            if limit - usage < amount:
                return f'regional {metric} quota has {limit - usage:g} free, needs {amount}'
    if 'CPUS_ALL_REGIONS' in inventory.global_quotas:
        limit, usage = inventory.global_quotas['CPUS_ALL_REGIONS']
        if limit - usage < offer.vcpu:
            return f'global CPUS_ALL_REGIONS quota has {limit - usage:g} free, needs {offer.vcpu}'
    return None


def _zone_problem(
    offer: CpuOffer,
    zone: str,
    inventory: LaunchInventory,
    *,
    arch: str = 'x86_64',
    required_isa: tuple[str, ...] | list[str] = (),
) -> str | None:
    if zone not in inventory.platforms_by_zone:
        return 'zone is not UP'
    if offer.cpu_platform not in inventory.platforms_by_zone[zone]:
        return 'zone does not advertise the CPU platform'
    if offer.cpu_platform not in {
        platform for _, platform in platform_preferences(
            offer.machine_type, zone, arch=arch, required_isa=required_isa
        )
    }:
        return 'CPU platform is not selectable for this shape in the zone'
    available = inventory.disk_types_by_zone.get(zone, set())
    for kind in (offer.boot_disk_type, offer.data_disk_type):
        if kind not in available:
            return f'disk type {kind} is not offered'
    return None


def launchable_candidates(
    ranked: list[CpuOffer],
    inventory: LaunchInventory,
    disk_size_gb: int,
    *,
    arch: str = 'x86_64',
    required_isa: tuple[str, ...] | list[str] = (),
) -> tuple[list[CpuOffer], list[str]]:
    """Keep rank order; return zone candidates and why offers had no launchable zone."""
    selected: list[CpuOffer] = []
    skipped: list[str] = []
    for offer in ranked:
        label = f'{offer.machine_type} {offer.cpu_platform} in {offer.region}'
        if not offer.zones:
            skipped.append(f'{label}: machine type is not offered in any zone')
            continue
        shortfall = _quota_shortfall(offer, inventory, disk_size_gb, arch=arch)
        if shortfall:
            skipped.append(f'{label}: {shortfall}')
            continue
        problems = []
        for zone in offer.zones:
            problem = _zone_problem(offer, zone, inventory, arch=arch, required_isa=required_isa)
            if problem:
                problems.append(f'{zone} {problem}')
            else:
                selected.append(replace(offer, zone=zone))
        if len(problems) == len(offer.zones):
            skipped.append(f"{label}: {'; '.join(problems)}")
    return selected, skipped
