#!/usr/bin/env python3
"""Provision GCE Spot hosts and register persistent swack-tools/oxidex runners."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from typing import Any
from datetime import datetime, timezone
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from .environment import load_env
from .machine_profiles import configure_storage, configure_cpu_platform, cpu_platform_satisfies, _requested_arch_and_isa
from .billing_catalog import catalog_skus, unit_price, monthly_to_daily
from .storage_selection import storage_rates, cheapest_storage, throughput_floor, observed_storage_monthly, disk_monthly_cost
from .project_costs import log_project_costs
from .fleet_logging import vm_details
from .runner_fleet import (
    MAX_PATCH_METADATA_BYTES,
    NO_PATCH,
    PATCH_BOOT_METADATA_KEY,
    PATCH_LABEL_KEY,
    PATCH_LABEL_SHA_CHARS,
    PATCH_METADATA_KEY,
    PATCH_PENDING_METADATA_KEY,
    PATCH_PENDING_SHA_METADATA_KEY,
    PATCH_SHA_METADATA_KEY,
    PATCH_TARGET_METADATA_KEY,
    RUNNER_COUNT_METADATA_KEY,
    RUNNER_HEAVY_POOL_METADATA_KEY,
    RUNNER_LABEL_METADATA_KEY,
    RUNNER_EXTRA_LABELS_METADATA_KEY,
    MAX_EXTRA_RUNNER_LABELS,
    RUNNER_GROUP_METADATA_KEY,
    RUNNER_PREFIX_METADATA_KEY,
    RUNNER_REGION_METADATA_KEY,
    RUNNER_REGISTRATION_TARGET_METADATA_KEY,
    GceVm,
    gce_metadata_items,
    gce_timestamp,
    list_gcloud_vms,
    runner_deployment_from_gce_item,
)
from .log_format import configure_logging, error_detail, single_line
from .runner_labels import DEFAULT_RUNNER_LABELS
from .runner_assets import (CLEAN_TARGET, DOCKER_WRAPPER, JOB_COMPLETED,
                            RUNNER_DOCKERFILE, RUNNER_ENTRYPOINT, STARTUP_SCRIPT)
from .builder_assets import BUILDER_DOCKERFILE, BUILDER_ENTRYPOINT, HOST_LAUNCHER
from .target_config import (DEFAULT_ORG, TARGET_LABEL_KEY, TargetConfig, resolve_target, target_label_value,
                            target_owns_vm_label)
from .runner_patch import (
    RunnerPatch,
    classify_patch_diff,
    destructive_patch_changes,
    fetch_patch_from_config_repo,
    host_capacity_problems,
    PATCH_RECEIPT_READ_COMMAND,
    inject_startup_patch,
    load_patch_from_file,
    parse_patch_receipt,
    render_dockerfile,
    render_live_hot_patch_script,
    validate_patch_dict,
)

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "homelab-424523"
ORG = "swack-tools"
RUNNER_VERSION = "2.337.0"
LOG = logging.getLogger("spot_runner_autoscaler")


def run(args: list[str], *, quiet: bool = False, input_data: str | None = None) -> str:
    result = subprocess.run(args, text=True, capture_output=True, input=input_data)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {shlex.join(args)}; {error_detail(result.stderr or result.stdout)}")
    output = result.stdout.strip()
    if output and not quiet:
        LOG.info("Command output: %s", single_line(output))
    return output


def gcloud(args: list[str], *, quiet: bool = False) -> str:
    return run(["gcloud", *args, "--project", PROJECT], quiet=quiet)


def spot_rates(region: str, family: str) -> tuple[Decimal, Decimal]:
    """Read the exact regional Spot Preemptible core and RAM SKU prices."""
    rates: dict[str, Decimal] = {}
    for sku in catalog_skus():
        desc = sku.get('description', '')
        if region not in sku.get('serviceRegions', []) or not desc.lower().startswith(f'spot preemptible {family.lower()} '):
            continue
        key = 'cpu' if 'Instance Core running' in desc else 'ram' if 'Instance Ram running' in desc else None
        amount = unit_price(sku, 'h')
        if key and amount is not None and amount > 0:
            rates[key] = amount
    if set(rates) != {"cpu", "ram"}:
        raise RuntimeError(f"Exact Spot {family.upper()} core/RAM SKUs not found for {region}; refusing deployment")
    return rates["cpu"], rates["ram"]


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--org", default=ORG)
    parser.add_argument("--target", default=None,
                        help="Target repository (owner/repo) or organization (org); overrides --org")
    parser.add_argument("--max-cost", type=Decimal, required=True, help="Maximum compute USD/hour for this VM")
    parser.add_argument('--max-total-daily-usd', type=Decimal, help='Maximum compute plus boot/data storage USD/day')
    parser.add_argument('--cheapest-storage', action='store_true', help='Choose compatible storage using current regional prices')
    parser.add_argument('--billing-export-table', default=os.environ.get('GCP_BILLING_EXPORT_TABLE'),
                        help='Billing export project.dataset.table for project-wide cost logs')
    parser.add_argument("--instance-type", default="n2-standard-32")
    parser.add_argument("--min-cpu-platform", help="Explicit CPU platform; defaults to the preferred platform for this family")
    parser.add_argument("--region", default="asia-south2")
    parser.add_argument("--zone", default="")
    parser.add_argument("--ram", type=int, default=128, help="Expected VM RAM GiB")
    parser.add_argument("--cpu", type=int, default=32, help="Expected VM vCPU")
    parser.add_argument("--instance-name", default="oxidex-spot-runners-asia")
    parser.add_argument("--disk-size-gb", type=int, default=100)
    parser.add_argument("--disk-type", default=None,
                        help="Data disk type; defaults to the machine family's compatible storage")
    parser.add_argument("--disk-device-name", default="runner-data")
    parser.add_argument("--heavy-runner-pool", type=int, choices=(2,3), help="Strict CI-only pool: each runner 8 CPU/32 GiB, no extra swap")
    parser.add_argument("--runner-count", type=int, default=4)
    parser.add_argument("--runner-cpu", type=int, default=4)
    parser.add_argument("--runner-memory-gb", type=int, default=15)
    parser.add_argument("--fixed-runner-sizing", action="store_true", help="Use the explicit runner sizing values")
    parser.add_argument("--runner-label", default="oxidex-spot-7x27")
    parser.add_argument("--runner-group-id", type=int, default=10)
    parser.add_argument("--runner-prefix", default="oxidex-asia")
    parser.add_argument("--existing-instance", action="store_true", help="Attach/bootstrap an existing Spot VM")
    parser.add_argument("--deployment-receipt", type=Path,
                        help="Persist the created VM identity before waiting for runner registration")
    parser.add_argument("--registration-timeout-seconds", type=int, default=1200,
                        help="Fail when bootstrap has not finished this long after deployment starts")
    parser.add_argument("--apply", action="store_true", help="Run provisioning and startup-script deployment")
    parser.add_argument('--remote-builder', action='store_true', help='Install dedicated Rust 1.97.1 remote builder and CI tools')
    parser.add_argument('--resource-monitoring', action='store_true', help='Install metrics-only Ops Agent memory monitoring')
    parser.add_argument('--tier', choices=('small','starting','expanded','large','bulky'))
    parser.add_argument('--tier-slots',type=int)
    parser.add_argument('--tier-job-memory-gib',type=float,default=15,
                        help='Per-job cap/reservation; default 15 GiB is a verified compatible workload cap, not a measured peak')
    parser.add_argument('--host-overhead-gib',type=float,default=2)
    parser.add_argument('--config-repo', help='Override central runner configuration repository (defaults to <org>/spot-runners-config)')
    parser.add_argument('--patch-repo', help='Repository OWNER/REPO whose patch manifest should be loaded from the central config repo')
    parser.add_argument('--patch-file', type=Path, help='Local path to a spot-runner-patch-v1 YAML/JSON manifest')
    parser.add_argument('--patch-live', action='store_true', help='Hot-patch an already running Spot VM and its active runner containers in-place')
    return parser


def resolve_runner_patch(args: argparse.Namespace) -> RunnerPatch | None:
    patch = getattr(args, 'runner_patch', None)
    if isinstance(patch, RunnerPatch):
        return patch
    patch_file = getattr(args, 'patch_file', None)
    patch_repo = getattr(args, 'patch_repo', None)
    if patch_file and patch_repo:
        raise RuntimeError('Specify at most one of --patch-file or --patch-repo')
    if patch_file:
        patch = load_patch_from_file(Path(patch_file))
    elif patch_repo:
        patch = fetch_patch_from_config_repo(
            patch_repo,
            config_repo=getattr(args, 'config_repo', None),
            org=getattr(args, 'org', ORG),
        )
    if patch is not None:
        args.runner_patch = patch
    return patch


def strict_pool_settings(args):
    count=getattr(args,'heavy_runner_pool',None)
    if count is None:return None
    if type(count)is not int or count not in (2,3):raise RuntimeError('Strict pool must contain two or three runners')
    if getattr(args,'remote_builder',False):raise RuntimeError('Strict CI pool cannot host remote builders')
    if getattr(args,'tier',None):raise RuntimeError('Strict pool is an explicit fixed mode, not a tier override')
    # Strict pools are verified through an organization runner group; reject a repository target
    # here, before any VM is provisioned or listener registered, instead of failing afterwards.
    target=resolve_target(getattr(args,'target',None) or getattr(args,'org',ORG))
    if target.is_repo:raise RuntimeError(f'Strict pools need an organization runner group; repository target {target.name} is not supported')
    if not getattr(args,'runner_group_id',None):raise RuntimeError('Strict pool requires --runner-group-id')
    if args.cpu < count*8+2 or args.ram < count*32+16:raise RuntimeError('Strict pool needs 2 CPU and 16 GiB host headroom')
    return count


def select_sizing(args: argparse.Namespace) -> None:
    if getattr(args,'remote_builder',False) and not getattr(args,'tier',None):
        raise RuntimeError('Remote builders require a tier host with shared memory admission')
    patch = resolve_runner_patch(args)
    if patch is not None:
        if patch.hardware.min_vcpu is not None and args.cpu < patch.hardware.min_vcpu:
            raise RuntimeError(f'Host vCPU {args.cpu} is below patch minimum {patch.hardware.min_vcpu}')
        if patch.hardware.min_ram_gib is not None and args.ram < patch.hardware.min_ram_gib:
            raise RuntimeError(f'Host RAM {args.ram} GiB is below patch minimum {patch.hardware.min_ram_gib} GiB')
    configure_storage(args)
    configure_cpu_platform(args)
    strict=strict_pool_settings(args)
    if strict:
        args.runner_count=strict
        args.runner_cpu=8
        args.runner_memory_gb=32
        args.fixed_runner_sizing=True
        args.admission_work_gib=32*strict
        args.builder_memory_gib=28
        args.runner_label='spot-heavy32'
        if len(f'{args.runner_prefix}-{args.instance_name}-{strict}')>64:raise RuntimeError('GitHub runner name exceeds64 characters')
        return
    if getattr(args,'tier',None):
        from .runner_tiers import TIERS
        from .host_admission import admission_budget
        tier = TIERS[args.tier]
        if (args.cpu,args.ram) != (tier.vcpu,tier.ram_gib):
            raise RuntimeError('Tier shape does not match requested host')
        args.runner_count = args.tier_slots or tier.initial_slots
        budget = admission_budget(tier,args.host_overhead_gib,args.tier_job_memory_gib,slots=args.runner_count)
        args.admission_work_gib = budget['work_gib']
        args.runner_memory_gb = args.tier_job_memory_gib
        args.runner_cpu = 0
        args.fixed_runner_sizing = False
        args.builder_memory_gib = min(int(args.ram*.4),int(budget['work_gib']))
        if args.max_total_daily_usd is None or args.max_total_daily_usd > tier.max_daily_usd:
            args.max_total_daily_usd = tier.max_daily_usd
        if len(f'{args.runner_prefix}-{args.instance_name}-{args.runner_count}') > 64:
            raise RuntimeError('GitHub runner name exceeds64 characters')
        return
    host_overhead = int(getattr(args, 'host_overhead_gib', 2)) if args.ram <= 8 else (4 if args.ram <= 16 else 8)

    if not args.fixed_runner_sizing:
        if args.cpu < 2:
            raise RuntimeError("At least 2 host vCPUs are required for one runner")
        if args.ram < 4:
            raise RuntimeError("At least 4 GiB host RAM is required for one runner")
        if getattr(args, "runner_count", None) == 1 or args.cpu < 4 or args.ram <= 8:
            args.runner_count = 1
            args.runner_cpu = min(4, args.cpu)
            args.runner_memory_gb = max(2, args.ram - host_overhead)
        else:
            args.runner_count = args.cpu // 4
            args.runner_cpu = 4
            args.runner_memory_gb = (args.ram - host_overhead) // args.runner_count
            if args.runner_memory_gb < 2:
                args.runner_count = 1
                args.runner_cpu = min(4, args.cpu)
                args.runner_memory_gb = max(2, args.ram - host_overhead)

    if args.runner_count < 1 or args.runner_cpu * args.runner_count > args.cpu:
        raise RuntimeError("Runner count or CPU limits exceed the requested host capacity")
    if args.runner_memory_gb < 2:
        raise RuntimeError(f"At least 2 GiB runner RAM is required; allocated {args.runner_memory_gb} GiB")
    if args.runner_memory_gb * args.runner_count > args.ram - host_overhead:
        raise RuntimeError(f"Runner RAM exceeds (host RAM - {host_overhead} GiB); lower runner count/memory")
    longest_name = len(f"{args.runner_prefix}-{args.instance_name}-{args.runner_count}")
    if longest_name > 64:
        raise RuntimeError(f"Runner name would be {longest_name} characters; GitHub allows at most 64")


def verify_price_and_capacity(args: argparse.Namespace, zone: str) -> Decimal:
    family = args.instance_type.split("-", 1)[0]
    profile = configure_storage(args)
    machine = json.loads(gcloud(["compute", "machine-types", "describe", args.instance_type, "--zone", zone, "--format=json"], quiet=True))
    cpu, ram = int(machine["guestCpus"]), int(machine["memoryMb"]) // 1024
    priced_ram = Decimal(str(machine["memoryMb"])) / 1024
    if (cpu, ram) != (args.cpu, args.ram):
        raise RuntimeError(f"GCE reports {cpu} vCPU/{ram} GiB; request was {args.cpu}/{args.ram}")
    core_rate, ram_rate = spot_rates(args.region, family)
    hourly = core_rate * cpu + ram_rate * priced_ram
    if args.cheapest_storage or args.max_total_daily_usd is not None:
        arch, _ = _requested_arch_and_isa(args)
        rates = storage_rates(args.region)
        boot, data = args.boot_disk_type, args.disk_type
        if args.cheapest_storage:
            boot, data, monthly = cheapest_storage(args.instance_type, args.disk_size_gb, rates, arch=arch)
        else:
            monthly = disk_monthly_cost(boot, 40, rates) + disk_monthly_cost(data, args.disk_size_gb, rates,
                throughput=throughput_floor(args.disk_size_gb) if data == 'hyperdisk-throughput' else 140)
        if args.existing_instance:
            vm = args.deployment_vm
            boots = [disk for disk in vm.disks if disk.get('boot')]
            if len(boots) != 1:
                raise RuntimeError('Existing boot disk specifications are unavailable')
            boot = boots[0].get('diskType', '')
            retained_data = next((disk for disk in vm.disks if disk.get('deviceName') == args.disk_device_name), None)
            monthly = observed_storage_monthly(vm.disks, rates)
            if retained_data:
                data = retained_data.get('diskType', '')
            else:
                monthly += disk_monthly_cost(data, args.disk_size_gb, rates,
                    throughput=throughput_floor(args.disk_size_gb) if data == 'hyperdisk-throughput' else 140)
        args.selected_boot_disk_type, args.disk_type = boot, data
        configure_storage(args)
        args.storage_monthly_usd = monthly
        total_daily = hourly * 24 + monthly_to_daily(monthly)
        args.verified_price=dict(compute_daily_usd=str(hourly*24),storage_daily_usd=str(monthly_to_daily(monthly)),
                                total_daily_usd=str(total_daily),quoted_at=time.time(),
                                boot_disk_type=boot,data_disk_type=data,boot_gib=40,data_gib=args.disk_size_gb)
        LOG.info('Live total estimate: %s; $%.6f/day compute + $%.6f/day storage = $%.6f/day total',
                 deployment_details(args), hourly * 24, monthly_to_daily(monthly), total_daily)
        if args.max_total_daily_usd is not None and total_daily >= args.max_total_daily_usd:
            raise RuntimeError(f'Total compute/storage price ${total_daily:.6f}/day is not below ${args.max_total_daily_usd}; no resources changed')
    LOG.info(f"Live Spot {family.upper()} rates ({args.region}): ${core_rate}/vCPU-hour, ${ram_rate}/GiB-hour")
    LOG.info(f"Compute estimate: ${hourly:.6f}/hour per VM. {args.disk_size_gb} GiB {args.disk_type} disk is billed separately.")
    if hourly >= args.max_cost:
        raise RuntimeError(f"Spot compute price ${hourly:.6f}/hour is not below ${args.max_cost}; no resources changed")
    if not args.existing_instance:
        regional = json.loads(gcloud(["compute", "regions", "describe", args.region, "--format=json"], quiet=True)).get("quotas", [])
        regional_family = next((q for q in regional if q.get("metric") == profile.quota_metric), None)
        if regional_family and float(regional_family["limit"]) - float(regional_family["usage"]) < cpu:
            raise RuntimeError(f"Regional {family.upper()} CPU quota is insufficient: {regional_family}")
        global_quotas = json.loads(gcloud(["compute", "project-info", "describe", "--format=json"], quiet=True)).get("quotas", [])
        global_cpu = next((q for q in global_quotas if q.get("metric") == "CPUS_ALL_REGIONS"), None)
        if global_cpu and float(global_cpu["limit"]) - float(global_cpu["usage"]) < cpu:
            raise RuntimeError(f"Global CPU quota is insufficient before disk/VM creation: {global_cpu}")
    return hourly


def verify_runner_group(args: argparse.Namespace) -> dict:
    target = resolve_target(getattr(args, "target", None) or getattr(args, "org", ORG))
    if target.is_repo:
        raise RuntimeError(f"Runner groups are not supported for personal repository targets: {target.name}")
    org = target.name
    group = json.loads(run(["gh", "api", f"orgs/{org}/actions/runner-groups/{args.runner_group_id}"], quiet=True))
    if group.get("visibility") != "selected":
        raise RuntimeError("Runner group must be repository-scoped")
    repos = json.loads(run(["gh", "api", f"orgs/{org}/actions/runner-groups/{args.runner_group_id}/repositories"], quiet=True))
    if not any(repo.get("full_name") == f"{org}/oxidex" for repo in repos.get("repositories", [])):
        raise RuntimeError(f"Runner group {args.runner_group_id} is not enabled for {org}/oxidex")
    if getattr(args, 'heavy_runner_pool', None) and group.get('allows_public_repositories') is not True:
        raise RuntimeError('Strict runner group must permit the public OxiDex repository')
    if getattr(args, 'heavy_runner_pool', None) and [repo.get('full_name') for repo in repos.get('repositories', [])] != [f'{org}/oxidex']:
        raise RuntimeError('Strict runner group must select only OxiDex')
    return group


def generate_registration_token(target: TargetConfig) -> str:
    token = run(["gh", "api", "--method", "POST", target.registration_token_url, "--jq", ".token"], quiet=True)
    if not token:
        raise RuntimeError(f"GitHub returned an empty {target.target_type} runner registration token for {target.name}")
    return token


def cleanup_orphan_runner_disks(zone: str) -> list[str]:
    """Delete only old, unattached persistent disks owned by this manager."""
    output = gcloud([
        "compute", "disks", "list",
        f"--filter=zone:{zone} AND labels.managed-by=spot-runner-manager",
        "--format=json(name,status,users,labels,creationTimestamp)",
    ], quiet=True)
    disks = json.loads(output) if output else []
    deleted: list[str] = []
    now = datetime.now(timezone.utc)
    for disk in disks:
        labels = disk.get("labels", {})
        created = disk.get("creationTimestamp")
        if not created:
            continue
        age = now - datetime.fromisoformat(created.replace("Z", "+00:00"))
        if (age.total_seconds() < 6 * 60 * 60 or disk.get("status") != "READY"
                or disk.get("users") or not disk.get("name")
                or labels.get("managed-by") != "spot-runner-manager"):
            continue
        name = disk["name"]
        gcloud(["compute", "disks", "delete", name, f"--zone={zone}", "--quiet"])
        deleted.append(name)
    return deleted


def _desired_runner_labels(args: argparse.Namespace) -> list[str]:
    patch = resolve_runner_patch(args)
    extra = list(patch.runner_labels()) if patch is not None else []
    # Extra scheduling labels of a runner floor host (runner_floor), recorded in its metadata for later label syncs.
    floor_extra = list(getattr(args, "extra_runner_labels", ()) or ())
    return list(dict.fromkeys([args.runner_label, args.instance_type, args.region, *floor_extra, *extra]))


def render_startup(args: argparse.Namespace, token: str, group_name: str, deployment_id: str) -> str:
    if getattr(args,'remote_builder',False) and not getattr(args,'tier',None):
        raise RuntimeError('Remote builders require a tier host with shared memory admission')
    patch = resolve_runner_patch(args)
    dockerfile_text = render_dockerfile(RUNNER_DOCKERFILE, patch)
    labels_csv = ",".join(_desired_runner_labels(args))
    source = STARTUP_SCRIPT
    strict=strict_pool_settings(args)
    if strict and (args.runner_count!=strict or args.runner_memory_gb!=32 or args.runner_cpu!=8 or args.admission_work_gib!=32*strict or args.runner_label!='spot-heavy32'):raise RuntimeError('Strict pool sizing has not been selected')
    tiered = bool(getattr(args,'tier',None) or strict)
    admission_source = Path(__file__).with_name('host_admission.py').read_bytes()
    evidence_source = Path(__file__).with_name('host_evidence.py').read_bytes()
    replacements = {
        "DISK_DEVICE": f"/dev/disk/by-id/google-{args.disk_device_name}",
        "DOCKERFILE_B64": base64.b64encode(dockerfile_text.encode()).decode("ascii"),
        "ENTRYPOINT_B64": base64.b64encode(RUNNER_ENTRYPOINT.encode()).decode("ascii"),
        "JOB_COMPLETED_B64": base64.b64encode(JOB_COMPLETED.encode()).decode("ascii"),
        "CLEAN_TARGET_B64": base64.b64encode(CLEAN_TARGET.encode()).decode("ascii"),
        "DOCKER_WRAPPER_B64": base64.b64encode(DOCKER_WRAPPER.encode()).decode("ascii"),
        "RUNNER_VERSION": "2.337.0",
        "ORG": getattr(args, "target", None) or getattr(args, "org", None) or ORG,
        "RUNNER_LABELS": labels_csv,
        "RUNNER_GROUP": group_name,
        "RUNNER_PREFIX": args.runner_prefix,
        "REGISTRATION_TOKEN": shlex.quote(token),
        "RUNNER_COUNT": str(args.runner_count),
        "RUNNER_MEMORY_GB": str(args.runner_memory_gb),
        "RUNNER_CPU_LIMIT": str(args.runner_cpu) if getattr(args, "fixed_runner_sizing", False) else "",
        "INSTANCE_NAME": args.instance_name,
        "DEPLOYMENT_ID": deployment_id,
        "REMOTE_BUILDER": '1' if getattr(args, 'remote_builder', False) else '0',
        "RESOURCE_MONITORING": '1' if getattr(args, 'resource_monitoring', False) else '0',
        "BUILDER_DOCKERFILE_B64": base64.b64encode(BUILDER_DOCKERFILE.encode()).decode('ascii'),
        "BUILDER_ENTRYPOINT_B64": base64.b64encode(BUILDER_ENTRYPOINT.encode()).decode('ascii'),
        "HOST_LAUNCHER_B64": base64.b64encode(HOST_LAUNCHER.encode()).decode('ascii'),
        "BUILDER_CPUS": str(max(1, getattr(args, 'cpu', 32) - 4)),
        "BUILDER_MEMORY_GIB": str(getattr(args,'builder_memory_gib',max(1, getattr(args, 'ram', 128) - 24))),
        "ADMISSION_B64":base64.b64encode(admission_source).decode(),
        "DOCKER_ADMISSION_B64":base64.b64encode((ROOT/'builder/docker-admission.py').read_bytes()).decode(),
        "EVIDENCE_B64":base64.b64encode(evidence_source).decode(),
        "JOB_STARTED_B64":base64.b64encode((ROOT/'runner/job-started.sh').read_bytes()).decode(),
        "TIERED":'1' if tiered else '0',
        "STRICT_HEAVY_POOL":'1' if strict else '0',
        "ADMISSION_WORK_GIB":str(getattr(args,'admission_work_gib',0)),
    }
    for key, value in replacements.items():
        source = source.replace("{{" + key + "}}", value)
    source = inject_startup_patch(source, patch)
    if "{{" in source:
        raise RuntimeError("Unexpanded value in GCE startup script")
    return source


def write_deployment_receipt(args: argparse.Namespace, vm: GceVm) -> None:
    """Save safe provider identity independently of controller and registration lifetimes."""
    if not getattr(args, "deployment_receipt", None):
        return
    if not vm.instance_id or vm.name != args.instance_name:
        raise RuntimeError("Cannot persist an unidentified deployment")
    path = args.deployment_receipt
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as temp:
        record = asdict(vm)
        record.update(expected_runner_count=args.runner_count,tier=getattr(args,'tier',None),
                      admission_work_gib=getattr(args,'admission_work_gib',None),
                      runner_memory_gib=args.runner_memory_gb,price=getattr(args,'verified_price',None))
        json.dump(record, temp)
        temp.flush()
        os.fsync(temp.fileno())
        temporary = Path(temp.name)
    temporary.replace(path)


def launch_target_label(args: argparse.Namespace) -> str:
    """Return the ownership label value that scopes this host to its GitHub target's controller."""
    return target_label_value(resolve_target(getattr(args, "target", None) or getattr(args, "org", ORG)))


def runner_patch_record(args: argparse.Namespace, patch: RunnerPatch | None) -> tuple[dict[str, str], str | None, str]:
    """(plain metadata, canonical manifest JSON or None when too large, label value) recording ``patch``.

    Nothing here is secret: the manifest is the reviewed config-repo patch, never the registration token.
    """
    target = (patch.repository if patch is not None and patch.repository else "") or str(getattr(args, "target", "") or "")
    plain: dict[str, str] = {}
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target):
        plain[PATCH_TARGET_METADATA_KEY] = target
    prefix = str(getattr(args, "runner_prefix", "") or "")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,63}", prefix):
        plain[RUNNER_PREFIX_METADATA_KEY] = prefix
    count = getattr(args, "runner_count", None)
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        plain[RUNNER_COUNT_METADATA_KEY] = str(count)
    # What sync_runner_labels needs to keep the host's real GitHub scheduling labels after a live patch.
    runner_label = str(getattr(args, "runner_label", "") or "")
    if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", runner_label):
        plain[RUNNER_LABEL_METADATA_KEY] = runner_label
    region = str(getattr(args, "region", "") or "")
    if re.fullmatch(r"[a-z0-9-]{1,40}", region):
        plain[RUNNER_REGION_METADATA_KEY] = region
    extra_labels = [str(label) for label in getattr(args, "extra_runner_labels", ()) or ()]
    if len(extra_labels) > MAX_EXTRA_RUNNER_LABELS:
        raise RuntimeError(f"At most {MAX_EXTRA_RUNNER_LABELS} extra runner labels can be recorded; "
                           f"{len(extra_labels)} were requested")
    if extra_labels and all(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", label) for label in extra_labels):
        plain[RUNNER_EXTRA_LABELS_METADATA_KEY] = "+".join(extra_labels)
    heavy = getattr(args, "heavy_runner_pool", None)
    if heavy in (2, 3):
        plain[RUNNER_HEAVY_POOL_METADATA_KEY] = str(heavy)
    # The scope the runners registered with (org or repo) and its runner group, which label sync needs.
    registration = resolve_target(getattr(args, "target", None) or getattr(args, "org", ORG))
    plain[RUNNER_REGISTRATION_TARGET_METADATA_KEY] = registration.name
    group = getattr(args, "runner_group_id", None)
    if registration.is_org and isinstance(group, int) and not isinstance(group, bool) and group > 0:
        plain[RUNNER_GROUP_METADATA_KEY] = str(group)
    if patch is None:
        plain[PATCH_SHA_METADATA_KEY] = NO_PATCH
        return plain, None, NO_PATCH
    plain[PATCH_SHA_METADATA_KEY] = patch.patch_sha256
    manifest = json.dumps(patch.to_canonical_dict(), sort_keys=True, separators=(",", ":"))
    if len(manifest.encode("utf-8")) > MAX_PATCH_METADATA_BYTES:
        LOG.warning("Patch %s manifest is %d bytes; recording its sha256 only", patch.patch_sha256[:12],
                    len(manifest.encode("utf-8")))
        manifest = None
    return plain, manifest, patch.patch_sha256[:PATCH_LABEL_SHA_CHARS]


def pending_patch_record(args: argparse.Namespace, patch: RunnerPatch | None) -> tuple[dict[str, str], str | None]:
    """(plain metadata, canonical manifest JSON or None) announcing ``patch`` as requested but not yet applied.

    The deployment keys match runner_patch_record; the patch sha goes under the PENDING key (and the manifest
    under PATCH_PENDING_METADATA_KEY), never the applied keys or the label. Those wait for
    record_patch_metadata, after the bootstrap confirms the patch.
    """
    plain, manifest, _label = runner_patch_record(args, patch)
    plain[PATCH_PENDING_SHA_METADATA_KEY] = plain.pop(PATCH_SHA_METADATA_KEY)
    return plain, manifest


def record_pending_patch_metadata(args: argparse.Namespace, zone: str, patch: RunnerPatch | None) -> None:
    """Announce ``patch`` as in progress (PENDING keys only) before anything on the host changes.

    A live patch that fails part-way must not leave the previous applied record looking current: the
    preview treats any pending host as unknown until record_patch_metadata(clear_pending=True) runs. Only the
    two PENDING keys are written, so a failed attempt changes nothing else; an oversized manifest is replaced by
    a placeholder naming its sha so no earlier attempt's pending manifest survives.
    """
    plain, manifest = pending_patch_record(args, patch)
    sha = plain[PATCH_PENDING_SHA_METADATA_KEY]
    if manifest is None:
        manifest = json.dumps({"patch_sha256": sha, "manifest_omitted": "larger than the metadata limit"},
                              sort_keys=True, separators=(",", ":"))
    manifest_path = None
    try:
        with tempfile.NamedTemporaryFile("w", prefix="gha-patch-pending-", suffix=".json", delete=False) as temp:
            temp.write(manifest)
            manifest_path = temp.name
        gcloud(["compute", "instances", "add-metadata", args.instance_name, f"--zone={zone}",
                _plain_metadata_flag({PATCH_PENDING_SHA_METADATA_KEY: sha}),
                f"--metadata-from-file={PATCH_PENDING_METADATA_KEY}={manifest_path}"])
    finally:
        if manifest_path:
            Path(manifest_path).unlink(missing_ok=True)


def _plain_metadata_flag(plain: dict[str, str]) -> str:
    for key, value in plain.items():
        if any(c in value for c in ",=\n\r") or any(c in key for c in ",=\n\r"):
            raise RuntimeError(f"Refusing metadata value with a separator for {key}")
    return "--metadata=" + ",".join(f"{k}={v}" for k, v in plain.items())


def record_patch_metadata(args: argparse.Namespace, zone: str, patch: RunnerPatch | None, *, boot: str,
                          existing_keys: set[str] | None = None, clear_pending: bool = False) -> None:
    """Record the patch a running host now runs (instance metadata plus a sha-prefix label).

    Call it only after the host confirmed ``patch`` (a completed bootstrap or LIVE_PATCH_APPLIED). ``boot`` is
    the GCE lastStartTimestamp of the boot that confirmation belongs to (see confirmed_boot); a later boot makes
    the record stale. ``clear_pending`` then removes the PENDING keys, last, so the host never looks
    confirmed before the applied record is complete.
    """
    if not gce_timestamp(boot):
        raise RuntimeError(f"Refusing to record a patch for {args.instance_name} without the boot it was confirmed in")
    plain, manifest, label = runner_patch_record(args, patch)
    plain[PATCH_BOOT_METADATA_KEY] = gce_timestamp(boot)
    manifest_path = None
    try:
        command = ["compute", "instances", "add-metadata", args.instance_name, f"--zone={zone}",
                   _plain_metadata_flag(plain)]
        if manifest is not None:
            with tempfile.NamedTemporaryFile("w", prefix="gha-patch-", suffix=".json", delete=False) as temp:
                temp.write(manifest)
                manifest_path = temp.name
            command.append(f"--metadata-from-file={PATCH_METADATA_KEY}={manifest_path}")
        gcloud(command)
        if manifest is None and (existing_keys is None or PATCH_METADATA_KEY in existing_keys):
            gcloud(["compute", "instances", "remove-metadata", args.instance_name, f"--zone={zone}",
                    f"--keys={PATCH_METADATA_KEY}"])
        gcloud(["compute", "instances", "add-labels", args.instance_name, f"--zone={zone}",
                f"--labels={PATCH_LABEL_KEY}={label}"])
        if clear_pending:
            gcloud(["compute", "instances", "remove-metadata", args.instance_name, f"--zone={zone}",
                    f"--keys={PATCH_PENDING_SHA_METADATA_KEY},{PATCH_PENDING_METADATA_KEY}"])
    finally:
        if manifest_path:
            Path(manifest_path).unlink(missing_ok=True)


def boot_change_problem(args: argparse.Namespace, before: GceVm, now: GceVm) -> str | None:
    """Why ``now`` is not the same instance and boot (GCE lastStartTimestamp) as ``before``; None when it is."""
    first, then = gce_timestamp(before.last_start_timestamp), gce_timestamp(now.last_start_timestamp)
    if not first or not then:
        return (f"Cannot bind the confirmed patch of {args.instance_name} to a boot: lastStartTimestamp is "
                "unknown; the applied patch is not recorded")
    if first != then or (before.instance_id and now.instance_id != before.instance_id):
        return (f"{args.instance_name} restarted while its patch was being confirmed (boot {first}, now "
                f"{then}); the applied patch is not recorded")
    return None


def confirmed_boot(args: argparse.Namespace, zone: str, observed: GceVm) -> str:
    """The boot (GCE lastStartTimestamp) a patch confirmation belongs to.

    ``observed`` is the VM as seen BEFORE the confirmation was read (bootstrap marker or LIVE_PATCH_APPLIED);
    never pass an observation taken after it, which may already show a later boot. A fresh observation must
    show the same instance and boot; otherwise it restarted mid-confirmation and nothing is recorded.
    """
    problem = boot_change_problem(args, observed, observed_deployment_vm(args, zone))
    if problem:
        raise RuntimeError(problem)
    return gce_timestamp(observed.last_start_timestamp)


def registered_runner_rows(target: TargetConfig) -> list[dict]:
    """Every runner registered to ``target`` on GitHub (raises when the scope can't be listed)."""
    output = run(["gh", "api", "--paginate", target.runners_url, "--jq", ".runners[] | @json"], quiet=True)
    return [json.loads(row) for row in output.splitlines() if row.strip()]


def runners_named_for_vm(rows: list[dict], vm_name: str) -> list[str]:
    """Names of the runners startup.sh registers for ``vm_name`` (``<prefix>-<vm>-<n>``), with any prefix."""
    pattern = re.compile(rf"[A-Za-z0-9._-]+-{re.escape(vm_name)}-[0-9]+")
    return sorted(str(row.get("name")) for row in rows
                  if isinstance(row, dict) and pattern.fullmatch(str(row.get("name") or "")))


def verify_adoption_registrations(args: argparse.Namespace, vm: dict) -> None:
    """Refuse to relabel an unlabeled VM whose runners are registered to another GitHub scope.

    The runner data disk keeps each runner's ``.runner`` registration and entrypoint.sh skips configuration when
    it exists, so relabeling would scope the VM to the new target while its listeners stay registered elsewhere.
    The new target, the default organization (which owns unlabeled hosts), and any registration scope recorded on
    the VM are listed; a scope that can't be listed refuses the adoption rather than guessing.
    """
    new = resolve_target(args.target)
    recorded = gce_metadata_items(vm).get(RUNNER_REGISTRATION_TARGET_METADATA_KEY, "").strip()
    if recorded and runner_deployment_from_gce_item(vm).get("registration_target") != recorded:
        raise RuntimeError(f"Cannot verify which GitHub scope the runners of {args.instance_name} are registered to "
                           f"(recorded scope {recorded!r} is malformed); refusing to adopt it for {new.name}")
    candidates = [args.target, DEFAULT_ORG, ORG, recorded]
    elsewhere: dict[str, list[str]] = {}
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or str(candidate).lower() in seen:
            continue
        seen.add(str(candidate).lower())
        try:
            scope = resolve_target(candidate)
            names = runners_named_for_vm(registered_runner_rows(scope), args.instance_name)
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError(f"Cannot verify which GitHub scope the runners of {args.instance_name} are registered "
                               f"to ({candidate}: {exc}); refusing to adopt it for {new.name}") from exc
        if names and scope.name.lower() != new.name.lower():
            elsewhere[scope.name] = names
    if elsewhere:
        found = "; ".join(f"{scope}: {', '.join(names)}" for scope, names in sorted(elsewhere.items()))
        raise RuntimeError(f"VM {args.instance_name} has runners registered to another target ({found}). Their "
                           f"persisted registrations would keep serving that target after it is labeled for "
                           f"{new.name}; drain the host and remove those registrations (or reprovision it) first")


def adoption_preflight(args: argparse.Namespace, zone: str) -> None:
    """Ownership and registration checks for --existing-instance, before anything (even a start) changes the VM."""
    describe = subprocess.run(["gcloud", "compute", "instances", "describe", args.instance_name,
                               "--zone", zone, "--project", PROJECT, "--format=json"], text=True, capture_output=True)
    if describe.returncode:
        raise RuntimeError(f"Existing VM {args.instance_name} was not found")
    adoption_target_label(args, json.loads(describe.stdout))


def adoption_target_label(args: argparse.Namespace, vm: dict) -> str | None:
    """Ownership check for adopting an existing VM; returns the target label to set first, or None.

    A VM labeled for another target is refused. An unlabeled VM (owned by the default org, see
    target_owns_vm_label) is labeled only when the operator passed --target explicitly; without --target
    it may be adopted only by the default-org target that already owns it.
    """
    labels = vm.get("labels") if isinstance(vm.get("labels"), dict) else {}
    current = str(labels.get(TARGET_LABEL_KEY) or "")
    expected = launch_target_label(args)
    target = resolve_target(getattr(args, "target", None) or getattr(args, "org", ORG))
    if current:
        if current != expected:
            raise RuntimeError(f"VM {args.instance_name} is owned by another target ({TARGET_LABEL_KEY}={current}); "
                               f"refusing to adopt it for {target.name}")
        return None
    if getattr(args, "target", None):
        verify_adoption_registrations(args, vm)  # Never relabel a host whose registrations belong elsewhere.
        return expected
    if target_owns_vm_label(None, target):
        return None
    raise RuntimeError(f"Unlabeled VM {args.instance_name} belongs to the default organization; pass "
                       f"--target {target.name} to adopt it explicitly")


def apply_startup(args: argparse.Namespace, zone: str, script: str) -> None:
    configure_storage(args)
    configure_cpu_platform(args)
    arch, required_isa = _requested_arch_and_isa(args)
    cleanup_orphan_runner_disks(zone)
    describe = subprocess.run(["gcloud", "compute", "instances", "describe", args.instance_name,
                               "--zone", zone, "--project", PROJECT, "--format=json"], text=True, capture_output=True)
    vm = json.loads(describe.stdout) if describe.returncode == 0 else None
    if vm and not args.existing_instance:
        raise RuntimeError(f"VM {args.instance_name} already exists; use --existing-instance to adopt")
    if vm and vm.get("scheduling", {}).get("provisioningModel") != "SPOT":
        raise RuntimeError("Existing VM is not Spot; refusing to use it")
    if args.existing_instance and not vm:
        raise RuntimeError(f"Existing VM {args.instance_name} was not found")
    if vm:
        actual_type = vm.get("machineType", "").rsplit("/", 1)[-1]
        if actual_type != args.instance_type:
            raise RuntimeError(f"Existing VM type {actual_type} does not match {args.instance_type}")
        # Ownership before any change to the VM (and long before its runners register).
        adopt_label = adoption_target_label(args, vm)
        if vm.get('status') == 'RUNNING' and args.min_cpu_platform and not cpu_platform_satisfies(
                args.instance_type, vm.get('cpuPlatform', ''), args.min_cpu_platform, arch=arch, required_isa=required_isa):
            raise RuntimeError(f"Existing CPU platform {vm.get('cpuPlatform', 'unknown')} does not match requested {args.min_cpu_platform}")
        if args.min_cpu_platform and vm.get('status') in {'STOPPED', 'TERMINATED'}:
            gcloud(['compute', 'instances', 'update', args.instance_name,
                    f'--zone={zone}', f'--min-cpu-platform={args.min_cpu_platform}', '--quiet'])

    disk = subprocess.run(["gcloud", "compute", "disks", "describe", args.disk_device_name,
                           "--zone", zone, "--project", PROJECT, "--format=json"], text=True, capture_output=True)
    created_disk = False
    if not disk.stdout:
        performance = (["--provisioned-iops=3000", "--provisioned-throughput=140"]
                       if args.disk_type == 'hyperdisk-balanced' else [])
        if args.disk_type == 'hyperdisk-throughput':
            performance = [f'--provisioned-throughput={throughput_floor(args.disk_size_gb)}']
        gcloud(["compute", "disks", "create", args.disk_device_name, f"--zone={zone}",
                f"--size={args.disk_size_gb}GB", f"--type={args.disk_type}",
                "--labels=managed-by=spot-runner-manager", *performance, "--quiet"])
        created_disk = True
    else:
        disk_info = json.loads(disk.stdout)
        actual_disk_type = disk_info.get('type', '').rsplit('/', 1)[-1]
        if actual_disk_type != args.disk_type:
            raise RuntimeError(f'Existing disk type {actual_disk_type or "unknown"} differs from {args.disk_type}')
        attached_to_vm = vm and any(disk.get('deviceName') == args.disk_device_name for disk in vm.get('disks', []))
        if not attached_to_vm:
            if int(disk_info.get('sizeGb', 0)) != args.disk_size_gb:
                raise RuntimeError('Existing unattached disk size differs from the priced deployment request')
            if args.disk_type == 'hyperdisk-balanced' and (int(disk_info.get('provisionedIops', 0)) != 3000
                    or int(disk_info.get('provisionedThroughput', 0)) != 140):
                raise RuntimeError('Existing unattached disk performance differs from the priced baseline')
            if args.disk_type == 'hyperdisk-throughput' and int(disk_info.get('provisionedThroughput', 0)) != throughput_floor(args.disk_size_gb):
                raise RuntimeError('Existing unattached disk performance differs from the priced baseline')
        if disk_info.get("users") and not vm:
            raise RuntimeError(f"Disk {args.disk_device_name} is already attached; refusing to create another VM")

    with tempfile.NamedTemporaryFile("w", prefix="gha-startup-", delete=False) as temp:
        temp.write(script)
        script_path = temp.name
    # Announce the patch this startup script applies as PENDING only. A VM is RUNNING long before its bootstrap
    # finishes and stays running when the package install or patch step fails, so the applied keys and label
    # (which runner_fleet and the dashboard trust) wait for main() to confirm the completed bootstrap.
    startup_patch = resolve_runner_patch(args)
    args.startup_patch = startup_patch
    patch_plain, patch_manifest = pending_patch_record(args, startup_patch)
    patch_path = None
    if patch_manifest is not None:
        with tempfile.NamedTemporaryFile("w", prefix="gha-patch-", suffix=".json", delete=False) as temp:
            temp.write(patch_manifest)
            patch_path = temp.name
    metadata_files = f"startup-script={script_path}" + (f",{PATCH_PENDING_METADATA_KEY}={patch_path}" if patch_path else "")
    try:
        if vm:
            if adopt_label:
                # Same ownership label as a new instance gets, set before the startup script registers anything.
                gcloud(["compute", "instances", "add-labels", args.instance_name, f"--zone={zone}",
                        f"--labels={TARGET_LABEL_KEY}={adopt_label}"])
            for boot_disk in vm.get("disks", []):
                if boot_disk.get("boot"):
                    gcloud(["compute", "instances", "set-disk-auto-delete", args.instance_name,
                            f"--zone={zone}", f"--device-name={boot_disk['deviceName']}",
                            "--auto-delete", "--quiet"])
            attached = {disk.get("deviceName") for disk in vm.get("disks", [])}
            if args.disk_device_name not in attached:
                gcloud(["compute", "instances", "attach-disk", args.instance_name, f"--zone={zone}",
                        f"--disk={args.disk_device_name}", f"--device-name={args.disk_device_name}", "--mode=rw",
                        f"--interface={args.disk_interface}", "--auto-delete", "--quiet"])
            else:
                gcloud(["compute", "instances", "set-disk-auto-delete", args.instance_name,
                        f"--zone={zone}", f"--disk={args.disk_device_name}", "--auto-delete", "--quiet"])
            gcloud(["compute", "instances", "add-metadata", args.instance_name, f"--zone={zone}",
                    f"--metadata-from-file={metadata_files}", _plain_metadata_flag(patch_plain)])
            if patch_path is None and PATCH_PENDING_METADATA_KEY in gce_metadata_items(vm):
                # A pending manifest left by an earlier, unconfirmed attempt must not describe this one.
                gcloud(["compute", "instances", "remove-metadata", args.instance_name, f"--zone={zone}",
                        f"--keys={PATCH_PENDING_METADATA_KEY}"])
            # The applied keys and label keep their previous value; the pending keys mark them unconfirmed.
            if vm.get("status") == "RUNNING":
                gcloud(["compute", "instances", "reset", args.instance_name, f"--zone={zone}", "--quiet"])
            elif vm.get("status") in {"STOPPED", "TERMINATED"}:
                gcloud(["compute", "instances", "start", args.instance_name, f"--zone={zone}", "--quiet"])
            else:
                raise RuntimeError(f"Cannot run startup bootstrap for VM status {vm.get('status')}")
        else:
            try:
                boot_performance = (["--boot-disk-provisioned-iops=3000",
                                     "--boot-disk-provisioned-throughput=140"]
                                    if args.boot_disk_type == 'hyperdisk-balanced' else [])
                network = ("--network-interface=network=default,nic-type=GVNIC"
                           if args.boot_disk_type == 'hyperdisk-balanced' else "--network=default")
                arch, _ = _requested_arch_and_isa(args)
                image_family = "--image-family=debian-13-arm64" if arch == "arm64" else "--image-family=debian-13"
                created = gcloud(["compute", "instances", "create", args.instance_name, f"--zone={zone}",
                        f"--machine-type={args.instance_type}", "--provisioning-model=SPOT",
                        *([f'--min-cpu-platform={args.min_cpu_platform}'] if args.min_cpu_platform else []),
                        "--instance-termination-action=DELETE", "--maintenance-policy=TERMINATE",
                        f"--boot-disk-type={args.boot_disk_type}", "--boot-disk-size=40GB",
                        "--boot-disk-auto-delete",
                        f"--boot-disk-interface={args.boot_disk_interface}", *boot_performance,
                        image_family, "--image-project=debian-cloud",
                        f"--disk=name={args.disk_device_name},device-name={args.disk_device_name},mode=rw,auto-delete=yes,interface={args.disk_interface}",
                        network, f"--metadata-from-file={metadata_files}", _plain_metadata_flag(patch_plain),
                        # A runner floor host runs with no service account: CI jobs can reach the metadata server.
                        *(["--no-service-account", "--no-scopes"] if getattr(args, "no_service_account", False)
                          else []),
                        f"--labels={TARGET_LABEL_KEY}={launch_target_label(args)}",
                        "--format=json(id,name,zone,status,disks,cpuPlatform,minCpuPlatform)",
                        "--quiet"], quiet=True)
                if getattr(args, "deployment_receipt", None):
                    rows = json.loads(created)
                    row = rows[0] if isinstance(rows, list) and len(rows) == 1 else rows
                    if (not isinstance(row, dict) or row.get("name") != args.instance_name
                            or row.get("zone", "").rsplit("/", 1)[-1] != zone):
                        raise RuntimeError("Create response does not identify the requested VM")
                    write_deployment_receipt(args, GceVm(
                        row["name"], zone, row.get("status", "PROVISIONING"), args.instance_type, True,
                        disks=tuple({key: disk[key] for key in
                                     ("boot", "source", "autoDelete", "deviceName", "interface", "diskSizeGb")
                                     if key in disk} for disk in row.get("disks", [])),
                        instance_id=str(row.get("id") or ""),
                        vcpu=args.cpu, ram_gib=args.ram,
                        cpu_platform=row.get('cpuPlatform', ''),
                        requested_cpu_platform=args.min_cpu_platform or ''))
            except RuntimeError:
                if created_disk:
                    vm_check = subprocess.run(
                        ["gcloud", "compute", "instances", "describe", args.instance_name,
                         "--zone", zone, "--project", PROJECT, "--format=json"],
                        text=True, capture_output=True,
                    )
                    if vm_check.returncode:
                        for _ in range(6):
                            disk_check = subprocess.run(
                                ["gcloud", "compute", "disks", "describe", args.disk_device_name,
                                 "--zone", zone, "--project", PROJECT, "--format=json"],
                                text=True, capture_output=True,
                            )
                            if disk_check.returncode == 0:
                                users = json.loads(disk_check.stdout).get("users", [])
                                if not users:
                                    gcloud(["compute", "disks", "delete", args.disk_device_name,
                                            f"--zone={zone}", "--quiet"])
                                break
                            time.sleep(2)
                raise
    finally:
        Path(script_path).unlink(missing_ok=True)
        if patch_path:
            Path(patch_path).unlink(missing_ok=True)


def online_runners(args: argparse.Namespace) -> list[str]:
    target = resolve_target(getattr(args, "target", None) or getattr(args, "org", ORG))
    expected_names = {f"{args.runner_prefix}-{args.instance_name}-{n}" for n in range(1, args.runner_count + 1)}
    online: list[str] = []
    for _ in range(120):
        output = run(["gh", "api", "--paginate", target.runners_url, "--jq", ".runners[] | @json"], quiet=True)
        runners = [json.loads(row) for row in output.splitlines() if row.strip()]
        matches = [runner for runner in runners if runner.get("name") in expected_names]
        online = [runner["name"] for runner in matches if runner.get("status") == "online"]
        if online and hasattr(args, "deployment_started_at") and not getattr(args, "registration_logged", False):
            LOG.info("Instance first seen online in %s runners, registered to take jobs: %s; %.1f seconds "
                     "from starting VM deployment; %d/%d runners online", target.name, deployment_details(args),
                     time.monotonic() - args.deployment_started_at, len(online), args.runner_count)
            args.registration_logged = True
        if len(online) == args.runner_count:
            return online
        time.sleep(10)
    return online


def verify_strict_runner_membership(args) -> dict[str, int] | None:
    if not getattr(args, 'heavy_runner_pool', None): return None
    target = resolve_target(getattr(args, "target", None) or getattr(args, "org", ORG))
    expected = {f'{args.runner_prefix}-{args.instance_name}-{n}' for n in range(1, args.runner_count + 1)}
    output = run(['gh', 'api', '--paginate', f'orgs/{target.name}/actions/runner-groups/{args.runner_group_id}/runners', '--jq', '.runners[] | @json'], quiet=True)
    rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    selected = [row for row in rows if row.get('name') in expected]
    if (len(selected) != len(expected) or {row.get('name') for row in selected} != expected
        or any(type(row.get('id')) is not int for row in selected)
        or len({row['id'] for row in selected}) != len(expected)):
        raise RuntimeError('Registered strict runners are not in the verified OxiDex group')
    return {row['name']: row['id'] for row in selected}


def start_retained_strict_host(args, zone):
    if not (args.existing_instance and getattr(args,'heavy_runner_pool',None)):return
    vm=args.deployment_vm
    if not vm.instance_id or not vm.spot or vm.machine_type!=args.instance_type:
        raise RuntimeError('Retained strict host identity/type/Spot policy differs')
    if vm.running:return
    if vm.status not in {'STOPPED','TERMINATED'}:raise RuntimeError('Retained strict host is transitioning')
    if getattr(args,'min_cpu_platform',None):
        gcloud(['compute','instances','update',args.instance_name,f'--zone={zone}',f'--min-cpu-platform={args.min_cpu_platform}','--quiet'])
    gcloud(['compute','instances','start',args.instance_name,f'--zone={zone}','--quiet'])
    deadline=time.monotonic()+min(120,args.registration_timeout_seconds)
    while time.monotonic()<deadline:
        actual=observed_deployment_vm(args,zone)
        if actual.instance_id!=vm.instance_id or not actual.spot or actual.machine_type!=vm.machine_type:
            raise RuntimeError('Retained strict host changed during start')
        args.deployment_vm=actual
        if actual.running:return
        time.sleep(1)
    raise RuntimeError('Retained strict host did not become running; no bootstrap applied')


def read_cached_runner_ids(args):
    expected=[f'{args.runner_prefix}-{args.instance_name}-{n}' for n in range(1,args.runner_count+1)]
    vm_id=getattr(getattr(args,'deployment_vm',None),'instance_id',None)
    if not isinstance(vm_id,str)or not vm_id.isdigit():raise RuntimeError('Observed VM identity required for cached runner verification')
    prefix="import urllib.request;actual=urllib.request.urlopen(urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/instance/id',headers={'Metadata-Flavor':'Google'}),timeout=3).read().decode();assert actual=="+repr(vm_id)+"\n"
    script=prefix+"import json,os,stat;from pathlib import Path;names="+repr(expected)+";rows={}\nfor name in names:\n p=Path('/mnt/runner-data/instances')/name/'.runner'\n if any(x.is_symlink() for x in [*p.parents,p]):raise ValueError('Symlinked cached runner')\n fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)\n with os.fdopen(fd,'rb')as f:\n  if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise ValueError('Nonregular cached runner')\n  raw=f.read(65537)\n if len(raw)>65536:raise ValueError('Oversized cached runner')\n row=json.loads(raw)\n if row.get('agentName')!=name or type(row.get('agentId'))is not int:raise ValueError('Cached identity invalid')\n rows[name]=row['agentId']\nprint(json.dumps(rows))"
    output=gcloud(['compute','ssh',args.instance_name,f'--zone={args.zone or args.region+"-a"}',f'--project={args.project}','--quiet','--command='+shlex.join(['sudo','-n','python3','-I','-B','-c',script])],quiet=True)
    return json.loads(output)


def validate_strict_identity_join(group_ids,cached_ids,org_rows):
    expected=set(group_ids)
    selected=[row for row in org_rows if row.get('name')in expected]
    if len(selected)!=len(expected) or any(type(row.get('id'))is not int for row in selected):raise RuntimeError('Ambiguous org runner identities')
    targets={row['name']:row['id']for row in selected}
    if set(targets)!=expected or cached_ids!=group_ids or targets!=group_ids:raise RuntimeError('Cached/group/label runner identities differ')


def verify_strict_identities(args,org_rows=None):
    if not getattr(args,'heavy_runner_pool',None):return
    group_ids=verify_strict_runner_membership(args)
    cached=read_cached_runner_ids(args)
    if org_rows is None:
        target = resolve_target(getattr(args, "target", None) or getattr(args, "org", ORG))
        output=run(['gh','api','--paginate',f'orgs/{target.name}/actions/runners','--jq','.runners[] | @json'],quiet=True)
        org_rows=[json.loads(line)for line in output.splitlines()if line.strip()]
    validate_strict_identity_join(group_ids,cached,org_rows)


def runner_label_sync_plan(args: argparse.Namespace) -> tuple[Any, list[dict], list[str], dict[str, int]]:
    """Every read-only check label sync depends on: group and strict-pool verification, and that GitHub lists
    every expected runner. Returns (target, runner rows, desired labels, expected names); raises otherwise."""
    target = resolve_target(getattr(args, "target", None) or getattr(args, "org", ORG))
    if getattr(args, 'heavy_runner_pool', None):
        verify_runner_group(args)
        verify_strict_runner_membership(args)
    expected_names = {f"{args.runner_prefix}-{args.instance_name}-{n}": n
                      for n in range(1, args.runner_count + 1)}
    output = run(["gh", "api", "--paginate", target.runners_url,
                  "--jq", ".runners[] | @json"], quiet=True)
    rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    if getattr(args, 'heavy_runner_pool', None):
        verify_strict_identities(args, rows)
    missing = sorted(set(expected_names) - {row.get("name") for row in rows})
    if missing:
        raise RuntimeError(f"GitHub did not list all expected runners for label sync: {missing}")
    return target, rows, _desired_runner_labels(args), expected_names


def sync_runner_labels(args: argparse.Namespace) -> None:
    """Ensure every Spot VM runner has the common scheduling labels."""
    target, rows, desired, expected_names = runner_label_sync_plan(args)
    seen: set[str] = set()
    for runner in rows:
        name = runner.get("name")
        if name not in expected_names:
            continue
        seen.add(name)
        current = {label.get("name") for label in runner.get("labels", [])} - DEFAULT_RUNNER_LABELS
        if current != set(desired):
            response = run(["gh", "api", "--method", "PUT",
                            f"{target.runners_url}/{runner['id']}/labels", "--input", "-"],
                           quiet=True, input_data=json.dumps({"labels": desired}))
            actual = {label.get("name") for label in json.loads(response).get("labels", [])} - DEFAULT_RUNNER_LABELS
            if actual != set(desired):
                raise RuntimeError(f"GitHub labels for {name} are {sorted(actual)}, expected {sorted(desired)}")
    if seen != set(expected_names):
        missing = sorted(set(expected_names) - seen)
        raise RuntimeError(f"GitHub did not list all expected runners for label sync: {missing}")


def apply_live_patch(args: argparse.Namespace, zone: str) -> str:
    """Apply a compatible RunnerPatch live to a running VM and its active runner containers."""
    patch = resolve_runner_patch(args)
    if patch is None:
        raise RuntimeError("Live patching requires --patch-repo or --patch-file")
    vm = observed_deployment_vm(args, zone)
    args.deployment_vm = vm  # Strict pools verify cached runner identities against this VM.
    if not vm.running:
        raise RuntimeError(f"Cannot hot-patch non-running VM: {vm_details(vm)}")
    if vm.machine_type and vm.machine_type != args.instance_type:
        raise RuntimeError(f"Existing VM type {vm.machine_type} does not match {args.instance_type}")
    # Every hardware constraint (min vCPU/RAM, arch, ISA) against the observed VM, before anything changes.
    violations, unknowns = host_capacity_problems(patch, machine_type=vm.machine_type or args.instance_type,
                                                  vcpu=vm.vcpu, ram_gib=vm.ram_gib, cpu_platform=vm.cpu_platform)
    if violations or unknowns:
        raise RuntimeError(f"Patch {patch.patch_sha256[:12]} cannot be live-applied to {args.instance_name}: "
                           + "; ".join(violations + unknowns))
    receipt = parse_patch_receipt(gcloud(
        ["compute", "ssh", args.instance_name, f"--zone={zone}", "--quiet", f"--command={PATCH_RECEIPT_READ_COMMAND}"],
        quiet=True,
    ))
    # An unfinished attempt at another patch (the host's pending marker, or provisioning/live pending metadata)
    # left unknown partial changes behind, which only a new VM removes; one at this patch is simply re-applied.
    foreign_pending = next((sha for sha in (receipt.pending_sha256, vm.patch_pending_sha256)
                            if sha and sha not in ("none", patch.patch_sha256)), "")
    action = classify_patch_diff(receipt.manifest, patch, pending_sha256=foreign_pending)
    pending = bool(vm.patch_pending_sha256)
    if action == "noop" and (pending or not receipt.proof):
        # The host names this patch, but only a completion receipt from this boot (and a confirmed bootstrap)
        # proves it: re-apply it live so LIVE_PATCH_APPLIED confirms it before anything is recorded.
        LOG.info("VM %s reports patch %s, but %s; re-applying it live", args.instance_name, patch.patch_sha256[:12],
                 "its bootstrap never confirmed it" if pending else receipt.detail)
    elif action == "noop":
        LOG.info("VM %s is already at patch %s", args.instance_name, patch.patch_sha256[:12])
        # A verified noop still reconciles the GitHub labels (a host may predate label-sync-before-record), and
        # only then records the patch (backfills hosts provisioned before recording).
        sync_runner_labels(args)
        record_patch_metadata(args, zone, patch, boot=confirmed_boot(args, zone, vm))
        return "noop"
    if action == "replace_vm":
        raise RuntimeError(
            f"Patch {patch.patch_sha256[:12]} cannot be live-applied to {args.instance_name}: "
            f"{live_patch_replace_reason(receipt.manifest, patch, foreign_pending)}; "
            "a rolling VM replacement is required instead of --patch-live"
        )
    if action == "recreate_containers":
        raise RuntimeError(
            f"Patch {patch.patch_sha256[:12]} modifies container bind mounts; "
            "containers must be drained and recreated instead of --patch-live"
        )
    # Everything label sync needs is checked BEFORE the host changes, so nothing fails after the patch applied.
    runner_label_sync_plan(args)
    prefix = f"{args.runner_prefix}-"
    live_script = render_live_hot_patch_script(
        patch,
        base_dockerfile=RUNNER_DOCKERFILE,
        runner_version=RUNNER_VERSION,
        runner_prefix=prefix,
    )
    script_b64 = base64.b64encode(live_script.encode("utf-8")).decode("ascii")
    remote_cmd = f"printf '%s' '{script_b64}' | base64 -d | sudo -n bash -euo pipefail"
    # Publish the attempt before the host changes: if it fails part-way, the host stays pending
    # (preview: unknown) instead of still advertising the previous patch as confirmed.
    record_pending_patch_metadata(args, zone, patch)
    output = gcloud(
        ["compute", "ssh", args.instance_name, f"--zone={zone}", "--quiet", f"--command={remote_cmd}"],
        quiet=True,
    )
    if f"LIVE_PATCH_APPLIED:{patch.patch_sha256}" not in output:
        raise RuntimeError(f"Live patch execution did not confirm completion on {args.instance_name}")
    # The GitHub labels follow before anything is recorded: a failed label sync leaves the host pending (so a retry
    # re-applies and syncs again), never recorded as current with stale labels. Only after the host confirmed this
    # exact patch in this boot AND its labels match does the recorded patch change and the pending record go.
    sync_runner_labels(args)
    record_patch_metadata(args, zone, patch, boot=confirmed_boot(args, zone, vm), clear_pending=True)
    LOG.info("Applied live patch %s to %s", patch.patch_sha256[:12], args.instance_name)
    return "hot_patch"


def reconcile_runner_labels(args: argparse.Namespace, zone: str) -> None:
    """Sync the GitHub labels of a running host that is already verified at its patch (a hot-patch noop).

    A host whose patch was recorded before label sync had to succeed first can carry stale labels; a noop never
    runs the live script, so this is the only step that repairs them. Nothing on the host or its metadata changes.
    """
    vm = observed_deployment_vm(args, zone)
    args.deployment_vm = vm  # Strict pools verify cached runner identities against this VM.
    if not vm.running:
        raise RuntimeError(f"Cannot reconcile the runner labels of non-running VM: {vm_details(vm)}")
    sync_runner_labels(args)


def live_patch_replace_reason(current_manifest: dict | None, patch: RunnerPatch, foreign_pending: str) -> str:
    """Why classify_patch_diff answered replace_vm for this host (for the refusal message)."""
    if foreign_pending:
        return (f"an attempt to apply patch {foreign_pending[:12]} never completed, so its partial changes "
                "are unknown")
    if current_manifest is not None:
        current = validate_patch_dict(current_manifest)
        if current.hardware.arch == patch.hardware.arch and not set(patch.hardware.isa) - set(current.hardware.isa):
            dropped = destructive_patch_changes(current, patch)
            if dropped:
                return "the live script cannot undo applied changes (" + "; ".join(dropped) + ")"
    return "it changes the hardware architecture or CPU ISA requirements"


def wait_for_bootstrap(args: argparse.Namespace, zone: str, deployment_id: str) -> None:
    """Wait for this exact startup-script execution before removing its metadata."""
    started = getattr(args, "deployment_started_at", None) or time.monotonic()
    deadline = started + args.registration_timeout_seconds
    marker = "sudo cat /var/lib/gha-runner-bootstrap.complete 2>/dev/null || true"
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["gcloud", "compute", "ssh", args.instance_name, f"--zone={zone}",
                 f"--project={PROJECT}", "--quiet", f"--command={marker}"],
                text=True, capture_output=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and result.stdout.strip() == deployment_id:
            return
        time.sleep(10)
    raise TimeoutError(f"Startup script did not complete deployment {deployment_id}; startup metadata retained for diagnosis")


def verify_runner_storage(args: argparse.Namespace, zone: str) -> None:
    """Require the data disk to back Docker and containerd without exposing its socket."""
    prefix = f"{args.runner_prefix}-{args.instance_name}-"
    command = """set -euo pipefail
mountpoint -q /mnt/runner-data
test "$(docker info --format=json | jq -r .DockerRootDir)" = /mnt/runner-data/docker
systemctl show -p ExecStart --value containerd | grep -F -- '--root /mnt/runner-data/containerd' >/dev/null
sudo test -d /mnt/runner-data/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots
runner_count=0
for n in $(docker ps --format '{{.Names}}' | grep -F -- "$RUNNER_PREFIX" || true); do
  runner_count=$((runner_count + 1))
  binds=$(docker inspect --format '{{json .HostConfig.Binds}}' "$n")
  total=$(docker exec "$n" df -B1 / | awk 'NR == 2 {print $2}')
  available=$(docker exec "$n" df -B1 / | awk 'NR == 2 {print $4}')
  test "$total" -ge 70000000000
  test "$available" -ge 20000000000
  if printf '%s' "$binds" | grep -F '/var/run/docker.sock:/var/run/docker.sock' >/dev/null; then
    echo "Runner has host Docker socket mounted: $n" >&2
    exit 1
  fi
done
test "$runner_count" -eq $EXPECTED_RUNNER_COUNT
        """.replace("$RUNNER_PREFIX", shlex.quote(prefix)).replace(
            "$EXPECTED_RUNNER_COUNT", str(args.runner_count))
    gcloud(["compute", "ssh", args.instance_name, f"--zone={zone}", "--quiet", f"--command={command}"], quiet=True)


def remove_startup_metadata_if_vm_exists(args: argparse.Namespace, zone: str) -> None:
    exists = subprocess.run(["gcloud", "compute", "instances", "describe", args.instance_name,
                             "--zone", zone, "--project", PROJECT, "--format=value(name)"],
                            text=True, capture_output=True)
    if exists.returncode == 0:
        gcloud(["compute", "instances", "remove-metadata", args.instance_name, f"--zone={zone}",
                "--keys=startup-script", "--quiet"])



def deployment_details(args: argparse.Namespace) -> str:
    if hasattr(args, "deployment_vm"):
        return vm_details(args.deployment_vm)
    configure_storage(args)
    vm = GceVm(args.instance_name, args.zone or f"{args.region}-a", "PROVISIONING",
               args.instance_type, True, vcpu=args.cpu, ram_gib=args.ram,
               requested_cpu_platform=getattr(args, 'min_cpu_platform', '') or '',
               disks=({"boot": True, "diskSizeGb": 40, "diskType": args.boot_disk_type,
                       "interface": args.boot_disk_interface},
                      {"boot": False, "diskSizeGb": args.disk_size_gb, "diskType": args.disk_type,
                       "interface": args.disk_interface}))
    return "planned " + vm_details(vm)


def observed_deployment_vm(args: argparse.Namespace, zone: str) -> GceVm:
    arch, required_isa = _requested_arch_and_isa(args)
    for vm in list_gcloud_vms(PROJECT, name_prefix=args.instance_name):
        if vm.name == args.instance_name and vm.zone == zone:
            requested = getattr(args, 'min_cpu_platform', None)
            if requested and vm.running and not cpu_platform_satisfies(
                    args.instance_type, vm.cpu_platform, requested, arch=arch, required_isa=required_isa):
                raise RuntimeError(f'Observed CPU platform {vm.cpu_platform or "unknown"} does not match requested {requested}: {vm_details(vm)}')
            return vm
    raise RuntimeError(f"Cannot observe deployed VM specifications: {deployment_details(args)}")


def main() -> int:
    global PROJECT, ORG
    configure_logging()
    load_env(ROOT / ".env")
    args = args_parser().parse_args()
    PROJECT, ORG = args.project, args.org
    zone = args.zone or f"{args.region}-a"
    select_sizing(args)
    if getattr(args, "patch_live", False):
        apply_live_patch(args, zone)
        return 0
    if args.existing_instance:
        args.deployment_vm = observed_deployment_vm(args, zone)
    hourly = verify_price_and_capacity(args, zone)
    LOG.info(f"Plan: {deployment_details(args)}; {args.runner_count} runners, {f'{args.runner_cpu} vCPU cap' if args.fixed_runner_sizing else f'equal CPU shares (sized at {args.runner_cpu} vCPU)'} and {args.runner_memory_gb} GiB memory cap each; {args.disk_size_gb} GiB auto-delete data disk.")
    if not args.apply:
        LOG.info("Preview only. Add --apply to provision through GCE startup metadata and register the runners.")
        return 0

    target = resolve_target(getattr(args, "target", None) or getattr(args, "org", ORG))
    group_name = ""
    if target.is_org and getattr(args, "runner_group_id", None):
        group = verify_runner_group(args)
        group_name = group["name"]
    if args.existing_instance:
        adoption_preflight(args, zone)  # Before the retained-host start, the first change to the VM.
        start_retained_strict_host(args, zone)
        verify_strict_identities(args)
    token = generate_registration_token(target)
    deployment_id = uuid.uuid4().hex
    script = render_startup(args, token, group_name, deployment_id)
    token = ""

    failure: BaseException | None = None
    online: list[str] = []
    try:
        args.deployment_started_at = time.monotonic()
        log_project_costs(args.project, args.billing_export_table)
        apply_startup(args, zone, script)
        args.deployment_vm = observed_deployment_vm(args, zone)
        # The boot that runs this deployment's startup script, captured BEFORE the bootstrap marker is read. The
        # marker persists on disk across reboots, so it proves completion only for a boot that is still current:
        # every later observation through publication must show this same boot, or nothing is confirmed (a reboot
        # after the marker read would otherwise lend the previous boot's completion to the new boot).
        bootstrap_vm = args.deployment_vm
        write_deployment_receipt(args, args.deployment_vm)
        wait_for_bootstrap(args, zone, deployment_id)
        args.deployment_vm = observed_deployment_vm(args, zone)
        if not args.deployment_vm.running:
            raise RuntimeError(f'Deployed VM is not running: {deployment_details(args)}')
        boot_problem = boot_change_problem(args, bootstrap_vm, args.deployment_vm)
        if boot_problem:
            raise RuntimeError(boot_problem)
        online = online_runners(args)
        if len(online) != args.runner_count:
            raise RuntimeError(f"Only {len(online)}/{args.runner_count} expected runners are online")
        sync_runner_labels(args)
        verify_runner_storage(args, zone)
        online = online_runners(args)
        if len(online) == args.runner_count:
            # This deployment's bootstrap marker, every runner online, and verified storage: only now does the
            # host advertise the patch its startup script applied (and the pending record go away).
            if not hasattr(args, "startup_patch"):
                raise RuntimeError("apply_startup did not record which patch the startup script applies")
            record_patch_metadata(args, zone, args.startup_patch, clear_pending=True,
                                  boot=confirmed_boot(args, zone, bootstrap_vm))
    except BaseException as exc:
        failure = exc
    finally:
        try:
            remove_startup_metadata_if_vm_exists(args, zone)
        except BaseException as exc:
            if failure is None:
                failure = RuntimeError(f"Could not remove one-use runner token from startup metadata: {exc}")
    if failure:
        raise failure
    LOG.info(f"Org verification: {deployment_details(args)}; {len(online)}/{args.runner_count} online: {', '.join(online) if online else '(none)'}")
    if len(online) != args.runner_count:
        raise RuntimeError("Some runners did not come online; the one-use token was removed. Rerun to bootstrap missing runners with a fresh token.")
    LOG.info(f"Deployment complete: {deployment_details(args)}. Compute ${hourly:.6f}/hour per VM, below ${args.max_cost}; disk cost is separate.")
    return 0
