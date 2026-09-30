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
from .runner_fleet import list_github_runners
from .spot_finder import find

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger("spot_runner_autoscaler")


def count_online_runners(org: str, label: str) -> int:
    return sum(runner.online and counts_toward_capacity(runner.labels, label)
               for runner in list_github_runners(org))


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
    return max(0, vcpu // 4 - 2)


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
        command = [
            sys.executable, str(ROOT / "src" / "spot_runner_manager.py"),
            "--max-cost", str(hourly_limit), "--instance-type", offer.machine_type,
            "--region", offer.region, "--zone", zone,
            "--cpu", str(offer.vcpu), "--ram", str(int(offer.ram_gib)),
            "--instance-name", f"oxidex-runners-{suffix}",
            "--disk-device-name", f"runner-data-{suffix}",
            "--disk-size-gb", str(args.disk_size_gb),
            "--runner-prefix", f"oxidex-{suffix}",
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
    if args.minimum_runners < 1 or args.interval_seconds < 1 or args.disk_size_gb < 1:
        raise ValueError("runner floor, interval, and disk size must be positive")
    if args.max_daily_usd <= 0:
        raise ValueError("max daily VM cost must be positive")
    load_env(ROOT / ".env")
    if args.install_launch_agent:
        print(f"Installed LaunchAgent at {install_launch_agent(args)}")
        return 0

    while True:
        current = count_online_runners(args.org, args.label)
        LOG.info("GitHub runners with %r label: %d online; target %d", args.label, current, args.minimum_runners)
        if current < args.minimum_runners:
            offers = discover_offers(args.project, args.max_daily_usd)
            if not offers:
                LOG.error("No Spot VM offers are below $%s/day", args.max_daily_usd)
            else:
                for offer in offers:
                    current = count_online_runners(args.org, args.label)
                    if current >= args.minimum_runners:
                        break
                    capacity = runner_capacity(offer.vcpu)
                    if capacity < 1:
                        continue
                    needed = vm_count_for_floor(current, args.minimum_runners, capacity)
                    for _ in range(needed):
                        if count_online_runners(args.org, args.label) >= args.minimum_runners:
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
