"""Deployment support and CPU-generation preference for x86 runner hosts.

Preferences are hardware-generation tiers, not measured Rust build scores.
Within a tier, select the lowest current compute and storage price.
"""
from dataclasses import dataclass
import re
from .compute_api import Offer
from decimal import Decimal
from .billing_catalog import monthly_to_daily

MINIMUM_RAM_GIB = 110


@dataclass(frozen=True)
class MachineProfile:
    cpu_tier: int
    quota_metric: str
    disk_type: str = 'pd-standard'
    disk_interface: str = 'SCSI'


MACHINE_PROFILES = {
    'c4d': MachineProfile(0, 'C4D_CPUS', 'hyperdisk-balanced', 'NVME'),
    'c4': MachineProfile(2, 'C4_CPUS', 'hyperdisk-balanced', 'NVME'),
    'n4d': MachineProfile(1, 'N4D_CPUS', 'hyperdisk-balanced', 'NVME'),
    'n4': MachineProfile(4, 'N4_CPUS', 'hyperdisk-balanced', 'NVME'),
    'c2d': MachineProfile(5, 'C2D_CPUS'),
    'n2': MachineProfile(7, 'N2_CPUS'),
    'n2d': MachineProfile(6, 'N2D_CPUS'),
    'e2': MachineProfile(9, 'E2_CPUS'),
}

# Provisional family/platform preferences; no measured Cargo timings yet.
# E2 remains recognized for existing/manual hosts, but cannot pin a platform.
CPU_PREFERENCES = (
    ('c4d', 'AMD Turin'),
    ('n4d', 'AMD Turin'),
    ('c4', 'Intel Granite Rapids'),
    ('c4', 'Intel Emerald Rapids'),
    ('n4', 'Intel Emerald Rapids'),
    ('c2d', 'AMD Milan'),
    ('n2d', 'AMD Milan'),
    ('n2', 'Intel Ice Lake'),
    ('n2', 'Intel Cascade Lake'),
)

# Google documents all-shape C4 Granite Rapids selection in these zones.
# Checked 2026-09-30; elsewhere small C4 shapes use Emerald Rapids.
# https://docs.cloud.google.com/compute/docs/cpu-platforms#c4-mincpu-regions
C4_GRANITE_MIN_CPU_ZONES = frozenset({
    'asia-south2-a', 'asia-south2-c', 'asia-southeast3-b',
    'europe-north1-b', 'europe-north1-c', 'europe-southwest1-b',
    'europe-west4-ai1a', 'europe-west6-a', 'europe-west8-c',
    'northamerica-northeast1-a', 'southamerica-west1-c', 'us-west2-b', 'us-west3-c',
})


@dataclass(frozen=True)
class CpuOffer(Offer):
    cpu_platform: str
    cpu_rank: int
    boot_disk_type: str = ''
    data_disk_type: str = ''
    storage_monthly_usd: Decimal = Decimal(0)
    zone: str = ''

    @property
    def total_daily_usd(self) -> Decimal:
        return Decimal(str(self.hourly_usd)) * 24 + monthly_to_daily(self.storage_monthly_usd)


def platform_preferences(machine_type: str, zone: str | None = None) -> list[tuple[int, str]]:
    family = machine_type.split('-', 1)[0]
    preferences = [(rank, platform) for rank, (series, platform) in enumerate(CPU_PREFERENCES)
                   if series == family]
    if family == 'c4':
        parts = machine_type.split('-')
        vcpu = int(parts[2] if parts[1] == 'custom' else parts[-1])
        if vcpu in {144, 288}:
            preferences = [(rank, platform) for rank, platform in preferences if platform == 'Intel Granite Rapids']
        elif zone is not None and zone not in C4_GRANITE_MIN_CPU_ZONES:
            preferences = [(rank, platform) for rank, platform in preferences if platform == 'Intel Emerald Rapids']
    return preferences


def configure_cpu_platform(args) -> None:
    """Default to the preferred platform; reject incompatible explicit requests."""
    zone = getattr(args, 'zone', '') or f'{args.region}-a'
    platforms = [platform for _, platform in platform_preferences(args.instance_type, zone)]
    requested = getattr(args, 'min_cpu_platform', None)
    if requested and requested not in platforms:
        raise RuntimeError(f'CPU platform {requested} is unsupported for {args.instance_type}')
    args.min_cpu_platform = requested or (platforms[0] if platforms else None)


def cpu_platform_satisfies(machine_type: str, observed: str, requested: str) -> bool:
    """Accept the requested minimum or a known newer CPU in that family."""
    ranks = {platform: rank for rank, platform in platform_preferences(machine_type)}
    return observed in ranks and requested in ranks and ranks[observed] <= ranks[requested]


def machine_profile(machine_type: str) -> MachineProfile | None:
    """Reject ARM, accelerators, bare metal, and separately billed Local SSD."""
    if not re.fullmatch(r'[a-z0-9]+-(?:(?:standard|highcpu|highmem)-[1-9][0-9]*|custom-[1-9][0-9]*-[1-9][0-9]*(?:-ext)?)', machine_type):
        return None
    return MACHINE_PROFILES.get(machine_type.split('-', 1)[0])


def configure_storage(args) -> MachineProfile:
    """Choose compatible boot/data defaults before any provisioning action."""
    profile = machine_profile(args.instance_type)
    if profile is None:
        raise RuntimeError(f'Unsupported x86 runner machine type: {args.instance_type}')
    if args.disk_type is None:
        args.disk_type = profile.disk_type
    if profile.disk_type == 'hyperdisk-balanced' and args.disk_type not in {'hyperdisk-balanced', 'hyperdisk-throughput'}:
        raise RuntimeError(f'{args.instance_type} requires Hyperdisk Balanced runner storage')
    if args.disk_type == 'hyperdisk-throughput' and args.disk_size_gb < 2048:
        raise RuntimeError('Hyperdisk Throughput requires at least 2048 GiB')
    if args.disk_type == 'hyperdisk-throughput' and args.instance_type.split('-', 1)[0] not in {'c4', 'c4d', 'n4', 'n4d', 'n2', 'n2d'}:
        raise RuntimeError(f'Hyperdisk Throughput is unsupported for {args.instance_type}')
    args.boot_disk_type = getattr(args, 'selected_boot_disk_type', None) or profile.disk_type
    args.disk_interface = 'NVME' if args.disk_type.startswith('hyperdisk-') else profile.disk_interface
    args.boot_disk_interface = 'NVME' if args.boot_disk_type.startswith('hyperdisk-') else profile.disk_interface
    return profile
