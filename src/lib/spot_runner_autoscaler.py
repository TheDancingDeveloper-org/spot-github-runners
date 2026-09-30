"""Maintain an online floor of cost-capped GitHub Actions runners."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

from .autoscaling_policy import affordable_offers, vm_count_for_floor
from .compute_api import get_auth
from .environment import load_env
from .runner_labels import counts_toward_capacity
from .runner_fleet import GitHubRunner, list_github_runners
from .spot_finder import find

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger("spot_runner_autoscaler")
DOCKER_LABEL = "docker"


def count_online_runners(org: str, label: str) -> int:
    return sum(runner.online and runner_is_managed(runner)
               and counts_toward_capacity(runner.labels, label)
               for runner in list_github_runners(org))


def runner_is_managed(runner: GitHubRunner) -> bool:
    """Return whether a runner belongs to this GCP x64 fleet."""
    current_shape = "n2-standard-32" in runner.labels
    legacy_toronto = {
        "oxidex-spot-7x27", "northamerica-northeast2"
    }.issubset(runner.labels)
    return (current_shape or legacy_toronto) and "X64" in runner.labels


def docker_pool_hosts(runners: list[GitHubRunner] | tuple[GitHubRunner, ...]) -> set[str]:
    """Return host keys for online Docker runners without the Spot label."""
    hosts = set()
    for runner in runners:
        if (not runner.online or not runner_is_managed(runner)
                or DOCKER_LABEL not in runner.labels or "spot" in runner.labels):
            continue
        # Runner names end in the per-host numeric slot, such as -1 or -2.
        host, separator, slot = runner.name.rpartition("-")
        if separator and slot.isdecimal():
            hosts.add(host)
        else:
            hosts.add(runner.name)
    return hosts


def docker_pool_state(org: str) -> tuple[int, int]:
    """Return the online Docker runner count and distinct host count."""
    runners = list_github_runners(org)
    docker_runners = [runner for runner in runners
                      if runner.online and runner_is_managed(runner)
                      and DOCKER_LABEL in runner.labels and "spot" not in runner.labels]
    return len(docker_runners), len(docker_pool_hosts(docker_runners))


def docker_pool_satisfied(runner_count: int, host_count: int,
                          minimum_runners: int = 2, minimum_hosts: int = 2) -> bool:
    """Check the Docker runner count and host-diversity floors."""
    return runner_count >= minimum_runners and host_count >= minimum_hosts


def maximum_runner_name_length(prefix: str, instance_name: str, runner_count: int) -> int:
    """Estimate the longest runner name when GCE's hostname matches its VM name."""
    return max(len(f"{prefix}-{instance_name}-{index}") for index in range(1, runner_count + 1))


def discover_offers(project: str, max_daily_usd: Decimal, top: int = 500):
    auth = get_auth(project)
    offers, _, _, errors = asyncio.run(
        find(auth, min_vcpu=32, min_ram=128, top=top, concurrency=200,
             billing_api_key=os.environ.get("GOOGLE_CLOUD_API_KEY"))
    )
    for error in errors[:5]:
        LOG.warning("Spot scan warning: %s", error)
    return eligible_offers(offers, max_daily_usd)


def eligible_offers(offers, max_daily_usd: Decimal):
    """Keep affordable offers with exactly 32 vCPU and 128 GiB of RAM."""
    exact_shapes = [offer for offer in offers if offer.vcpu == 32 and offer.ram_gib == 128]
    return affordable_offers(exact_shapes, max_daily_usd)


def runner_capacity(vcpu: int) -> int:
    if vcpu < 4:
        return 0
    return vcpu // 4


def launch_offer(offer, args: argparse.Namespace) -> None:
    """Try each zone until the manager confirms a complete runner deployment."""
    hourly_limit = args.max_daily_usd / Decimal(24)
    result = subprocess.run(
        ["gcloud", "compute", "zones", "list", f"--project={args.project}",
         f"--filter=name~'^{offer.region}-' AND status=UP", "--format=value(name)"],
        check=True, text=True, capture_output=True,
    )
    zones = result.stdout.split()
    if not zones:
        raise RuntimeError(f"No active Compute Engine zones found in {offer.region}")
    for zone in zones:
        letter = zone.rsplit("-", 1)[-1]
        suffix = f"{offer.region.replace('-', '')}-{letter}-{int(time.time())}"
        instance_name = f"oxidex-runners-{suffix}"
        runner_prefix = "gha"
        longest_name = maximum_runner_name_length(runner_prefix, instance_name, 8)
        if longest_name > 64:
            raise RuntimeError(f"Generated GitHub runner name would be {longest_name} characters (maximum 64)")
        command = [
            sys.executable, str(ROOT / "src" / "spot_runner_manager.py"),
            "--max-cost", str(hourly_limit), "--instance-type", offer.machine_type,
            "--region", offer.region, "--zone", zone,
            "--cpu", str(offer.vcpu), "--ram", str(int(offer.ram_gib)),
            "--instance-name", instance_name,
            "--disk-device-name", f"runner-data-{suffix}",
            "--disk-size-gb", str(args.disk_size_gb),
            "--runner-prefix", runner_prefix,
            "--runner-label", args.label, "--apply",
        ]
        LOG.info("Deploying %s in %s at $%.4f/day compute", offer.machine_type, zone, offer.hourly_usd * 24)
        result = subprocess.run(command, check=False)
        if result.returncode == 0:
            return
        LOG.warning("Deployment in %s failed; trying another zone", zone)
    raise RuntimeError(f"No zone accepted {offer.machine_type} in {offer.region}")


def install_launch_agent(args: argparse.Namespace) -> Path:
    """Install a per-user LaunchAgent that restarts the autoscaler after login."""
    import plistlib

    home = Path.home()
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / "com.swacktools.spot-runner-autoscaler.plist"
    plist = {
        "Label": "com.swacktools.spot-runner-autoscaler",
        "ProgramArguments": [sys.executable, str(ROOT / "src" / "spot_runner_autoscaler.py")],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": str(home / "Library" / "Logs" / "spot-runner-autoscaler.log"),
        "StandardErrorPath": str(home / "Library" / "Logs" / "spot-runner-autoscaler.log"),
    }
    plist_path.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], check=False)
    return plist_path


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="swack-tools")
    parser.add_argument("--project", default="homelab-424523")
    parser.add_argument("--minimum-runners", type=int, default=25)
    parser.add_argument("--minimum-docker-runners", type=int, default=2)
    parser.add_argument("--minimum-docker-hosts", type=int, default=2)
    parser.add_argument("--label", default="spot")
    parser.add_argument("--max-daily-usd", type=Decimal, default=Decimal("5"))
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--disk-size-gb", type=int, default=100)
    parser.add_argument("--install-launch-agent", action="store_true")
    parser.add_argument("--once", action="store_true", help="Check and scale once, then exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = args_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if (args.minimum_runners < 1 or args.minimum_docker_runners < 1
            or args.minimum_docker_hosts < 1 or args.interval_seconds < 1
            or args.disk_size_gb < 1):
        raise ValueError("runner floors, interval, and disk size must be positive")
    if args.max_daily_usd <= 0:
        raise ValueError("max daily VM cost must be positive")
    load_env(ROOT / ".env")
    if args.install_launch_agent:
        print(f"Installed LaunchAgent at {install_launch_agent(args)}")
        return 0

    while True:
        current = count_online_runners(args.org, args.label)
        LOG.info("Managed runners across Spot and Docker pools: %d online; target %d",
                 current, args.minimum_runners)
        docker_count, docker_hosts = docker_pool_state(args.org)
        LOG.info("Online Docker runners: %d across %d hosts; targets %d runners across %d hosts",
                 docker_count, docker_hosts, args.minimum_docker_runners, args.minimum_docker_hosts)
        if (current < args.minimum_runners or not docker_pool_satisfied(
                docker_count, docker_hosts, args.minimum_docker_runners, args.minimum_docker_hosts)):
            offers = discover_offers(args.project, args.max_daily_usd)
            if not offers:
                LOG.error("No Spot VM offers are below $%s/day", args.max_daily_usd)
            else:
                for offer in offers:
                    current = count_online_runners(args.org, args.label)
                    docker_count, docker_hosts = docker_pool_state(args.org)
                    spot_deficit = max(0, args.minimum_runners - current)
                    docker_runner_deficit = max(0, args.minimum_docker_runners - docker_count)
                    docker_host_deficit = max(0, args.minimum_docker_hosts - docker_hosts)
                    if not (spot_deficit or not docker_pool_satisfied(
                            docker_count, docker_hosts,
                            args.minimum_docker_runners, args.minimum_docker_hosts)):
                        break
                    capacity = runner_capacity(offer.vcpu)
                    if capacity < 1:
                        continue
                    spot_hosts = vm_count_for_floor(current, args.minimum_runners, capacity)
                    docker_hosts_needed = max(
                        1 if docker_host_deficit else 0,
                        (docker_runner_deficit + 1) // 2,
                    )
                    needed = max(spot_hosts, docker_hosts_needed)
                    for _ in range(needed):
                        current = count_online_runners(args.org, args.label)
                        docker_count, docker_hosts = docker_pool_state(args.org)
                        if (current >= args.minimum_runners and docker_pool_satisfied(
                                docker_count, docker_hosts,
                                args.minimum_docker_runners, args.minimum_docker_hosts)):
                            break
                        try:
                            launch_offer(offer, args)
                        except (RuntimeError, OSError) as exc:
                            LOG.warning("Could not deploy %s in %s: %s", offer.machine_type, offer.region, exc)
                            break
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
