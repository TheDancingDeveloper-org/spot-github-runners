#!/usr/bin/env python3
"""Provision GCE Spot hosts and register persistent swack-tools/oxidex runners."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .environment import load_env
from .machine_profiles import configure_storage
from .fleet_logging import vm_details
from .runner_fleet import GceVm, list_gcloud_vms
from .log_format import configure_logging, error_detail, single_line
from .runner_labels import DEFAULT_RUNNER_LABELS, custom_labels_for_runner
from .runner_assets import RUNNER_DOCKERFILE, RUNNER_ENTRYPOINT, STARTUP_SCRIPT

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
    token = run(["gcloud", "auth", "print-access-token"], quiet=True)
    endpoint = "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus"
    query = urllib.parse.urlencode({"currencyCode": "USD", "pageSize": 5000})
    page: str | None = None
    rates: dict[str, Decimal] = {}
    while True:
        url = endpoint + "?" + query + ("&pageToken=" + urllib.parse.quote(page) if page else "")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise RuntimeError(f"Unable to verify live GCP Spot prices: {exc}") from exc
        for sku in result.get("skus", []):
            desc = sku.get("description", "")
            if region not in sku.get("serviceRegions", []) or not desc.lower().startswith(f"spot preemptible {family.lower()} "):
                continue
            key = "cpu" if "Instance Core running" in desc else "ram" if "Instance Ram running" in desc else None
            if not key:
                continue
            tiers = sku.get("pricingInfo", [{}])[0].get("pricingExpression", {}).get("tieredRates", [])
            if tiers:
                price = tiers[0].get("unitPrice", {})
                amount = Decimal(price.get("units", "0")) + Decimal(price.get("nanos", 0)) / Decimal(1_000_000_000)
                if amount > 0:
                    rates[key] = amount
        page = result.get("nextPageToken")
        if not page:
            break
    if set(rates) != {"cpu", "ram"}:
        raise RuntimeError(f"Exact Spot {family.upper()} core/RAM SKUs not found for {region}; refusing deployment")
    return rates["cpu"], rates["ram"]


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--org", default=ORG)
    parser.add_argument("--max-cost", type=Decimal, required=True, help="Maximum compute USD/hour for this VM")
    parser.add_argument("--instance-type", default="n2-standard-32")
    parser.add_argument("--region", default="asia-south2")
    parser.add_argument("--zone", default="")
    parser.add_argument("--ram", type=int, default=128, help="Expected VM RAM GiB")
    parser.add_argument("--cpu", type=int, default=32, help="Expected VM vCPU")
    parser.add_argument("--instance-name", default="oxidex-spot-runners-asia")
    parser.add_argument("--disk-size-gb", type=int, default=100)
    parser.add_argument("--disk-type", default=None,
                        help="Data disk type; defaults to the machine family's compatible storage")
    parser.add_argument("--disk-device-name", default="runner-data")
    parser.add_argument("--runner-count", type=int, default=4)
    parser.add_argument("--runner-cpu", type=int, default=4)
    parser.add_argument("--runner-memory-gb", type=int, default=15)
    parser.add_argument("--docker-runner-count", type=int, default=2,
                        help="Number of runners that receive only the docker label and host socket")
    parser.add_argument("--fixed-runner-sizing", action="store_true", help="Use the explicit runner sizing values")
    parser.add_argument("--runner-label", default="oxidex-spot-7x27")
    parser.add_argument("--runner-group-id", type=int, default=10)
    parser.add_argument("--runner-prefix", default="oxidex-asia")
    parser.add_argument("--existing-instance", action="store_true", help="Attach/bootstrap an existing Spot VM")
    parser.add_argument("--apply", action="store_true", help="Run provisioning and startup-script deployment")
    return parser


def select_sizing(args: argparse.Namespace) -> None:
    configure_storage(args)
    if not args.fixed_runner_sizing:
        if args.cpu < 4:
            raise RuntimeError("At least 4 host vCPUs are required for one runner")
        args.runner_count = args.cpu // 4
        args.runner_cpu = 4
        args.runner_memory_gb = (args.ram - 8) // args.runner_count
    if args.runner_count < 1 or args.runner_cpu * args.runner_count > args.cpu:
        raise RuntimeError("Runner count or CPU limits exceed the requested host capacity")
    if args.docker_runner_count < 0 or args.docker_runner_count > args.runner_count:
        raise RuntimeError("Docker runner count must be between zero and the total runner count")
    if args.runner_memory_gb * args.runner_count > args.ram - 8:
        raise RuntimeError("Runner RAM exceeds (host RAM - 8 GiB); lower runner count/memory")
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
    group = json.loads(run(["gh", "api", f"orgs/{ORG}/actions/runner-groups/{args.runner_group_id}"], quiet=True))
    if group.get("visibility") != "selected":
        raise RuntimeError("Runner group must be repository-scoped")
    repos = json.loads(run(["gh", "api", f"orgs/{ORG}/actions/runner-groups/{args.runner_group_id}/repositories"], quiet=True))
    if not any(repo.get("full_name") == f"{ORG}/oxidex" for repo in repos.get("repositories", [])):
        raise RuntimeError(f"Runner group {args.runner_group_id} is not enabled for {ORG}/oxidex")
    return group


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


def render_startup(args: argparse.Namespace, token: str, group_name: str, deployment_id: str) -> str:
    source = STARTUP_SCRIPT
    replacements = {
        "DISK_DEVICE": f"/dev/disk/by-id/google-{args.disk_device_name}",
        "DOCKERFILE_B64": base64.b64encode(RUNNER_DOCKERFILE.encode()).decode("ascii"),
        "ENTRYPOINT_B64": base64.b64encode(RUNNER_ENTRYPOINT.encode()).decode("ascii"),
        "RUNNER_VERSION": "2.337.0",
        "ORG": ORG,
        "RUNNER_LABELS": f"{args.runner_label},{args.instance_type},{args.region}",
        "DOCKER_RUNNER_LABELS": f"docker,{args.instance_type},{args.region}",
        "RUNNER_GROUP": group_name,
        "RUNNER_PREFIX": args.runner_prefix,
        "REGISTRATION_TOKEN": shlex.quote(token),
        "RUNNER_COUNT": str(args.runner_count),
        "RUNNER_CPU": str(args.runner_cpu),
        "RUNNER_MEMORY_GB": str(args.runner_memory_gb),
        "DOCKER_RUNNER_COUNT": str(args.docker_runner_count),
        "INSTANCE_NAME": args.instance_name,
        "DEPLOYMENT_ID": deployment_id,
    }
    for key, value in replacements.items():
        source = source.replace("{{" + key + "}}", value)
    if "{{" in source:
        raise RuntimeError("Unexpanded value in GCE startup script")
    return source


def apply_startup(args: argparse.Namespace, zone: str, script: str) -> None:
    configure_storage(args)
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

    disk = subprocess.run(["gcloud", "compute", "disks", "describe", args.disk_device_name,
                           "--zone", zone, "--project", PROJECT, "--format=json"], text=True, capture_output=True)
    created_disk = False
    if not disk.stdout:
        performance = (["--provisioned-iops=3000", "--provisioned-throughput=140"]
                       if args.disk_type == 'hyperdisk-balanced' else [])
        gcloud(["compute", "disks", "create", args.disk_device_name, f"--zone={zone}",
                f"--size={args.disk_size_gb}GB", f"--type={args.disk_type}",
                "--labels=managed-by=spot-runner-manager", *performance, "--quiet"])
        created_disk = True
    else:
        disk_info = json.loads(disk.stdout)
        actual_disk_type = disk_info.get('type', '').rsplit('/', 1)[-1]
        if actual_disk_type != args.disk_type:
            raise RuntimeError(f'Existing disk type {actual_disk_type or "unknown"} differs from {args.disk_type}')
        if disk_info.get("users") and not vm:
            raise RuntimeError(f"Disk {args.disk_device_name} is already attached; refusing to create another VM")

    with tempfile.NamedTemporaryFile("w", prefix="gha-startup-", delete=False) as temp:
        temp.write(script)
        script_path = temp.name
    try:
        if vm:
            attached = {disk.get("deviceName") for disk in vm.get("disks", [])}
            if args.disk_device_name not in attached:
                gcloud(["compute", "instances", "attach-disk", args.instance_name, f"--zone={zone}",
                        f"--disk={args.disk_device_name}", f"--device-name={args.disk_device_name}", "--mode=rw",
                        f"--interface={args.disk_interface}", "--auto-delete", "--quiet"])
            else:
                gcloud(["compute", "instances", "set-disk-auto-delete", args.instance_name,
                        f"--zone={zone}", f"--disk={args.disk_device_name}", "--auto-delete", "--quiet"])
            gcloud(["compute", "instances", "add-metadata", args.instance_name, f"--zone={zone}",
                    f"--metadata-from-file=startup-script={script_path}"])
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
                gcloud(["compute", "instances", "create", args.instance_name, f"--zone={zone}",
                        f"--machine-type={args.instance_type}", "--provisioning-model=SPOT",
                        "--instance-termination-action=STOP", "--maintenance-policy=TERMINATE",
                        f"--boot-disk-type={args.boot_disk_type}", "--boot-disk-size=40GB",
                        f"--boot-disk-interface={args.disk_interface}", *boot_performance,
                        "--image-family=debian-13", "--image-project=debian-cloud",
                        f"--disk=name={args.disk_device_name},device-name={args.disk_device_name},mode=rw,auto-delete=yes,interface={args.disk_interface}",
                        network, f"--metadata-from-file=startup-script={script_path}", "--quiet"])
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


def online_runners(args: argparse.Namespace) -> list[str]:
    expected_names = {f"{args.runner_prefix}-{args.instance_name}-{n}" for n in range(1, args.runner_count + 1)}
    online: list[str] = []
    for _ in range(120):
        output = run(["gh", "api", "--paginate", f"orgs/{ORG}/actions/runners", "--jq", ".runners[] | @json"], quiet=True)
        runners = [json.loads(row) for row in output.splitlines() if row.strip()]
        matches = [runner for runner in runners if runner.get("name") in expected_names]
        online = [runner["name"] for runner in matches if runner.get("status") == "online"]
        if online and hasattr(args, "deployment_started_at") and not getattr(args, "registration_logged", False):
            LOG.info("Instance first seen online in %s runners, registered to take jobs: %s; %.1f seconds "
                     "from starting VM deployment; %d/%d runners online", ORG, deployment_details(args),
                     time.monotonic() - args.deployment_started_at, len(online), args.runner_count)
            args.registration_logged = True
        if len(online) == args.runner_count:
            return online
        time.sleep(10)
    return online


def sync_runner_labels(args: argparse.Namespace) -> None:
    """Give builder runners only the docker custom label; keep the standard pool separate."""
    expected_names = {f"{args.runner_prefix}-{args.instance_name}-{n}": n
                      for n in range(1, args.runner_count + 1)}
    output = run(["gh", "api", "--paginate", f"orgs/{ORG}/actions/runners",
                  "--jq", ".runners[] | @json"], quiet=True)
    seen: set[str] = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        runner = json.loads(line)
        name = runner.get("name")
        if name not in expected_names:
            continue
        seen.add(name)
        index = expected_names[name]
        desired = custom_labels_for_runner(index, args.docker_runner_count,
                                           [args.runner_label, args.instance_type, args.region])
        current = {label.get("name") for label in runner.get("labels", [])} - DEFAULT_RUNNER_LABELS
        if current != set(desired):
            response = run(["gh", "api", "--method", "PUT",
                            f"orgs/{ORG}/actions/runners/{runner['id']}/labels", "--input", "-"],
                           quiet=True, input_data=json.dumps({"labels": desired}))
            actual = {label.get("name") for label in json.loads(response).get("labels", [])} - DEFAULT_RUNNER_LABELS
            if actual != set(desired):
                raise RuntimeError(f"GitHub labels for {name} are {sorted(actual)}, expected {sorted(desired)}")
    if seen != set(expected_names):
        missing = sorted(set(expected_names) - seen)
        raise RuntimeError(f"GitHub did not list all expected runners for label sync: {missing}")


def wait_for_bootstrap(args: argparse.Namespace, zone: str, deployment_id: str) -> None:
    """Wait for this exact startup-script execution before removing its metadata."""
    deadline = time.monotonic() + 1200
    marker = "sudo cat /var/lib/gha-runner-bootstrap.complete 2>/dev/null || true"
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["gcloud", "compute", "ssh", args.instance_name, f"--zone={zone}",
             f"--project={PROJECT}", "--quiet", f"--command={marker}"],
            text=True, capture_output=True,
        )
        if result.returncode == 0 and result.stdout.strip() == deployment_id:
            return
        time.sleep(10)
    raise TimeoutError(f"Startup script did not complete deployment {deployment_id}; startup metadata retained for diagnosis")


def verify_runner_storage_and_socket(args: argparse.Namespace, zone: str) -> None:
    """Require the data disk to back Docker, containerd overlays, and runner Docker access."""
    prefix = f"{args.runner_prefix}-{args.instance_name}-"
    command = """set -euo pipefail
mountpoint -q /mnt/runner-data
test "$(docker info --format=json | jq -r .DockerRootDir)" = /mnt/runner-data/docker
systemctl show -p ExecStart --value containerd | grep -F -- '--root /mnt/runner-data/containerd' >/dev/null
sudo test -d /mnt/runner-data/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots
docker_count=0
runner_count=0
for n in $(docker ps --format '{{.Names}}' | grep -F -- "$RUNNER_PREFIX" || true); do
  runner_count=$((runner_count + 1))
  binds=$(docker inspect --format '{{json .HostConfig.Binds}}' "$n")
  total=$(docker exec "$n" df -B1 / | awk 'NR == 2 {print $2}')
  available=$(docker exec "$n" df -B1 / | awk 'NR == 2 {print $4}')
  test "$total" -ge 70000000000
  test "$available" -ge 20000000000
  index=${n##*-}
  if (( index <= $DOCKER_RUNNER_COUNT )); then
    docker_count=$((docker_count + 1))
    printf '%s' "$binds" | grep -F '/var/run/docker.sock:/var/run/docker.sock' >/dev/null
    docker exec "$n" docker version --format '{{.Server.Version}}' >/dev/null
    docker exec "$n" test -w /var/run/docker.sock
  elif printf '%s' "$binds" | grep -F '/var/run/docker.sock:/var/run/docker.sock' >/dev/null; then
    echo "Non-builder runner has host Docker socket: $n" >&2
    exit 1
  fi
done
test "$docker_count" -eq $DOCKER_RUNNER_COUNT
test "$runner_count" -eq $EXPECTED_RUNNER_COUNT
        """.replace("$RUNNER_PREFIX", shlex.quote(prefix)).replace(
            "$DOCKER_RUNNER_COUNT", str(args.docker_runner_count)
        ).replace("$EXPECTED_RUNNER_COUNT", str(args.runner_count))
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
               disks=({"boot": True, "diskSizeGb": 40, "diskType": args.boot_disk_type,
                       "interface": args.disk_interface},
                      {"boot": False, "diskSizeGb": args.disk_size_gb, "diskType": args.disk_type,
                       "interface": args.disk_interface}))
    return "planned " + vm_details(vm)


def observed_deployment_vm(args: argparse.Namespace, zone: str) -> GceVm:
    for vm in list_gcloud_vms(PROJECT, name_prefix=args.instance_name):
        if vm.name == args.instance_name and vm.zone == zone:
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
    hourly = verify_price_and_capacity(args, zone)
    if args.existing_instance:
        args.deployment_vm = observed_deployment_vm(args, zone)
    LOG.info(f"Plan: {deployment_details(args)}; {args.runner_count} runners, {args.runner_cpu} vCPU/{args.runner_memory_gb} GiB each; {args.disk_size_gb} GiB retained disk.")
    if not args.apply:
        LOG.info("Preview only. Add --apply to provision through GCE startup metadata and register the runners.")
        return 0

    group = verify_runner_group(args)
    token = run(["gh", "api", "--method", "POST", f"orgs/{ORG}/actions/runners/registration-token", "--jq", ".token"], quiet=True)
    if not token:
        raise RuntimeError("GitHub returned an empty org runner registration token")
    deployment_id = uuid.uuid4().hex
    script = render_startup(args, token, group["name"], deployment_id)
    token = ""

    failure: BaseException | None = None
    online: list[str] = []
    try:
        args.deployment_started_at = time.monotonic()
        apply_startup(args, zone, script)
        args.deployment_vm = observed_deployment_vm(args, zone)
        wait_for_bootstrap(args, zone, deployment_id)
        online = online_runners(args)
        if len(online) != args.runner_count:
            raise RuntimeError(f"Only {len(online)}/{args.runner_count} expected runners are online")
        sync_runner_labels(args)
        verify_runner_storage_and_socket(args, zone)
        online = online_runners(args)
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
