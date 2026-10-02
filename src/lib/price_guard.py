"""Check current compute and attached storage prices for Spot runner VMs."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import replace
from decimal import Decimal

from .runner_fleet import GceVm
from .fleet_logging import vm_details
from .log_format import error_detail
from .runner_manager import spot_rates
from .storage_selection import storage_rates, observed_storage_monthly
from .billing_catalog import clear_catalog_cache, monthly_to_daily

LOG = logging.getLogger("spot_runner_autoscaler")


def check_running_prices(vms: list[GceVm], project: str,
                         daily_cap: Decimal, *, raise_on_error: bool = False,
                         totals: dict[str, Decimal] | None = None) -> list[GceVm]:
    """Find overpriced VMs; optionally expose unavailable prices to pending evictions.

    When ``totals`` is given, it receives each priced VM's daily compute plus storage cost by name.
    """
    rates = {}
    disk_rates = {}
    clear_catalog_cache()
    shapes = {}
    overpriced = []
    for vm in vms:
        if not vm.running or not vm.spot or not vm.name.startswith("oxidex-runners-"):
            continue
        region = vm.zone.rsplit("-", 1)[0]
        family = vm.machine_type.split("-", 1)[0]
        rate_key = (region, family)
        shape_key = (vm.zone, vm.machine_type)
        try:
            if rate_key not in rates:
                try:
                    rates[rate_key] = spot_rates(region, family)
                except (RuntimeError, OSError) as exc:
                    rates[rate_key] = exc
            if isinstance(rates[rate_key], Exception):
                raise RuntimeError(str(rates[rate_key]))
            if shape_key not in shapes:
                result = subprocess.run([
                    "gcloud", "compute", "machine-types", "describe", vm.machine_type,
                    f"--zone={vm.zone}", f"--project={project}",
                    "--format=json(guestCpus,memoryMb)",
                ], check=True, text=True, capture_output=True, timeout=60)
                shape = json.loads(result.stdout)
                shapes[shape_key] = (Decimal(str(shape["guestCpus"])),
                                     Decimal(str(shape["memoryMb"])) / 1024)
            cpu, ram = shapes[shape_key]
            core_rate, ram_rate = rates[rate_key]
            compute_daily = (core_rate * cpu + ram_rate * ram) * 24
            if region not in disk_rates:
                disk_rates[region] = storage_rates(region)
            storage_daily = monthly_to_daily(observed_storage_monthly(vm.disks, disk_rates[region]))
            daily = compute_daily + storage_daily
            if not daily.is_finite() or daily <= 0:
                raise ValueError("The provider returned an invalid price")
            LOG.info("Live price: %s costs $%.6f/day compute + $%.6f/day storage = $%.6f/day total; cap $%s/day total",
                     vm_details(replace(vm, vcpu=int(cpu), ram_gib=float(ram))), compute_daily, storage_daily, daily, daily_cap)
            if totals is not None:
                totals[vm.name] = daily
            if daily >= daily_cap:
                overpriced.append(vm)
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            LOG.error("Cannot verify price for %s; keeping VM and retrying next loop: %s",
                      vm_details(vm), error_detail(exc))
            if raise_on_error:
                raise
    return overpriced


def delete_overpriced_vm(vm: GceVm, project: str) -> None:
    """Delete a managed Spot VM and its disks even when jobs are running."""
    if not vm.running or not vm.spot or not vm.name.startswith("oxidex-runners-"):
        raise ValueError("Price eviction requires a running managed Spot VM")
    subprocess.run([
        "gcloud", "compute", "instances", "delete", vm.name,
        f"--zone={vm.zone}", f"--project={project}", "--delete-disks=all", "--quiet",
    ], check=True, text=True, capture_output=True, timeout=180)
