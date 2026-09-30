"""Deployment support and CPU-generation preference for x86 runner hosts.

Preferences are hardware-generation tiers, not measured Rust build scores.
Within a tier, select the lowest current Spot compute price.
"""
from dataclasses import dataclass
import re

MINIMUM_RAM_GIB = 110


@dataclass(frozen=True)
class MachineProfile:
    cpu_tier: int
    quota_metric: str
    disk_type: str = 'pd-balanced'
    disk_interface: str = 'SCSI'


MACHINE_PROFILES = {
    'c4d': MachineProfile(0, 'C4D_CPUS', 'hyperdisk-balanced', 'NVME'),
    # Exact 32-vCPU C4 uses Emerald Rapids, as does N4. C4D/N4D use Turin.
    'c4': MachineProfile(1, 'C4_CPUS', 'hyperdisk-balanced', 'NVME'),
    'n4d': MachineProfile(0, 'N4D_CPUS', 'hyperdisk-balanced', 'NVME'),
    'n4': MachineProfile(1, 'N4_CPUS', 'hyperdisk-balanced', 'NVME'),
    'c2d': MachineProfile(3, 'C2D_CPUS'),
    'n2': MachineProfile(3, 'N2_CPUS'),
    'n2d': MachineProfile(3, 'N2D_CPUS'),
    'e2': MachineProfile(4, 'E2_CPUS'),
}


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
    if profile.disk_type == 'hyperdisk-balanced' and args.disk_type != 'hyperdisk-balanced':
        raise RuntimeError(f'{args.instance_type} requires Hyperdisk Balanced runner storage')
    args.boot_disk_type = profile.disk_type
    args.disk_interface = 'NVME' if args.disk_type.startswith('hyperdisk-') else profile.disk_interface
    return profile
