"""Find and rank low-cost GCP Spot VM offers."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time

from .compute_api import (
    DEFAULT_CONCURRENCY,
    LOG,
    QUOTA_COOLDOWN,
    Auth,
    AsyncGoogleApi,
    Offer,
    debug,
    get_auth,
    is_quota_error,
    list_machines,
    list_regions,
    log,
    record_quota,
    wait_for_quota_cooldown,
)
from .log_format import configure_logging, error_detail
from .spot_pricing import load_catalog_rates, spot_price

async def find(auth: Auth, min_vcpu: int, min_ram: float, top: int | None,
               concurrency: int, list_price_baseline: float | None = None
               , billing_api_key: str | None = None
               , runners: int | None = None
               ) -> tuple[list[Offer], int, int, list[str]]:
    await wait_for_quota_cooldown(auth.project)
    api = AsyncGoogleApi(auth.token, concurrency)
    inventory_started = time.perf_counter()
    regions = await list_regions(api, auth.project)
    if billing_api_key and list_price_baseline is None:
        (machine_types, unreachable_zones), catalog_rates = await asyncio.gather(
            list_machines(api, auth.project, min_vcpu, int(min_ram * 1024)),
            load_catalog_rates(billing_api_key))
    else:
        machine_types, unreachable_zones = await list_machines(
            api, auth.project, min_vcpu, int(min_ram * 1024))
        catalog_rates = {}
    if unreachable_zones:
        LOG.warning("aggregated machine inventory could not reach %d zone scope(s): %s",
                    len(unreachable_zones), ", ".join(unreachable_zones[:10]))
    region_names = {item["name"] for item in regions}
    shapes: dict[tuple[str, str], tuple[int, float]] = {}
    for item in machine_types:
        name = item.get("name", "")
        zone = item.get("zone", "").rsplit("/", 1)[-1]
        region = re.sub(r"-[a-z]$", "", zone)
        vcpu = int(item.get("guestCpus", 0))
        ram = float(item.get("memoryMb", 0)) / 1024
        # TPU families and bare-metal shapes are rejected by the capacity
        # history endpoint; skip the known unsupported classes before fan-out.
        lower_name = name.lower()
        unsupported_series = lower_name.startswith(("ct", "tpu", "a4x"))
        supported = not unsupported_series and not lower_name.endswith("-metal")
        if region in region_names and name and supported and vcpu >= min_vcpu and ram >= min_ram:
            shapes[(region, name)] = (vcpu, ram)
    log(1, "GCP inventory: %d active regions; %d shapes qualify (%.2fs)",
        len(region_names), len(shapes), time.perf_counter() - inventory_started)
    log(2, "aggregated machine inventory contains %d rows", len(machine_types))
    if getattr(LOG, "verbosity", 0) >= 3:
        for (region, machine), (vcpu, ram) in sorted(shapes.items()):
            debug("qualifying %s/%s: %d vCPU, %.1f GiB", region, machine, vcpu, ram)

    if not shapes:
        return [], len(region_names), 0, []

    offers: list[Offer] = []
    errors: list[str] = []
    price_started = time.perf_counter()
    total = len(shapes)
    missing_spot_prices: list[tuple[str, str]] = []
    # A full GCP Billing SKU scan is the fast path: prices are already
    # regional, all pages are fetched once, and VM totals are reconstructed
    # from each series' CPU and RAM rates. Keep capacityHistory as a fallback
    # for machine series without component SKUs.
    for (region, machine), (vcpu, ram) in sorted(shapes.items()):
        family = re.match(r"([a-z][a-z0-9]*)-", machine.lower())
        if not family or not catalog_rates:
            missing_spot_prices.append((region, machine))
            continue
        series = family.group(1)
        cpu_spot = catalog_rates.get(("spot", region, series, "CPU"))
        ram_spot = catalog_rates.get(("spot", region, series, "RAM"))
        if cpu_spot is None or ram_spot is None:
            missing_spot_prices.append((region, machine))
            continue
        hourly = cpu_spot * vcpu + ram_spot * ram
        offer = Offer(region, machine, vcpu, ram, hourly)
        offers.append(offer)
        cpu_regular = catalog_rates.get(("ondemand", region, series, "CPU"))
        ram_regular = catalog_rates.get(("ondemand", region, series, "RAM"))
        baseline = list_price_baseline
        if baseline is None and cpu_regular is not None and ram_regular is not None:
            baseline = cpu_regular * vcpu + ram_regular * ram
        if baseline and baseline > 0:
            discount = 100.0 * (1.0 - hourly / baseline)
            if discount >= 95.0:
                print_offer(offer, prefix=f"{discount:.1f}% off list: ", runners=runners)
    completed = total - len(missing_spot_prices)
    semaphore = asyncio.Semaphore(concurrency)
    quota_regions: set[str] = set()
    last_quota_at: float | None = None

    async def lookup(region: str, machine: str) -> tuple[str, str, float | None, str | None, bool]:
        nonlocal last_quota_at
        async with semaphore:
            if region in quota_regions:
                return region, machine, None, None, True
            try:
                result_region, result_machine, hourly = await spot_price(api, auth.project, region, machine)
                return result_region, result_machine, hourly, None, False
            except Exception as exc:
                if is_quota_error(exc):
                    quota_regions.add(region)
                    last_quota_at = time.monotonic()
                    record_quota(auth.project)
                    log(1, "quota limit in %s; deferring its remaining Spot price lookups", region)
                    return region, machine, None, None, True
                return region, machine, None, str(exc), False

    pending = missing_spot_prices
    for sweep in range(2):
        deferred: list[tuple[str, str]] = []
        tasks = [asyncio.create_task(lookup(region, machine)) for region, machine in pending]
        for task in asyncio.as_completed(tasks):
            if sweep == 0:
                completed += 1
            region, machine, hourly, error, was_deferred = await task
            if was_deferred:
                deferred.append((region, machine))
                continue
            if error:
                errors.append(error)
                debug("price lookup failed: %s", error)
                continue
            vcpu, ram = shapes[(region, machine)]
            if hourly is None:
                log(2, "no Spot price: %s/%s", region, machine)
                continue
            offer = Offer(region, machine, vcpu, ram, hourly)
            offers.append(offer)
            if list_price_baseline and list_price_baseline > 0:
                baseline = list_price_baseline
            else:
                family = re.match(r"([a-z][a-z0-9]*)-", machine.lower())
                baseline = None
                if family:
                    cpu_rate = catalog_rates.get(("ondemand", region, family.group(1), "CPU"))
                    ram_rate = catalog_rates.get(("ondemand", region, family.group(1), "RAM"))
                    if cpu_rate is not None and ram_rate is not None:
                        baseline = cpu_rate * vcpu + ram_rate * ram
            if baseline is not None and baseline > 0:
                discount = 100.0 * (1.0 - hourly / baseline)
                if discount >= 95.0:
                    print_offer(offer, prefix=f"{discount:.1f}% off list: ", runners=runners)
            log(1, "completed price request for region %s from GCP (%d/%d)",
                region, completed, total)
            log(2, "Spot price %s/%s = $%.9f/hour", region, machine, hourly)
            debug("price result %s/%s, %d vCPU, %.1f GiB", region, machine, vcpu, ram)

        if not deferred or sweep == 1:
            if deferred:
                errors.append(f"quota still limits {len(deferred)} price request(s) after the deferred retry")
            break
        elapsed_since_quota = time.monotonic() - (last_quota_at or time.monotonic())
        delay = max(0.0, QUOTA_RETRY_DELAY - elapsed_since_quota)
        log(1, "deferring %d price requests across %d quota-limited region(s); retrying in %.1fs",
            len(deferred), len(quota_regions), delay)
        if delay:
            await asyncio.sleep(delay)
        quota_regions.clear()
        pending = deferred

    elapsed = time.perf_counter() - price_started
    debug("completed %d price requests in %.2fs (%.1f requests/s)", completed, elapsed,
          completed / elapsed if elapsed else 0.0)
    offers.sort(key=lambda offer: (offer.hourly_usd, offer.region, offer.machine_type))
    return offers[:top], len(region_names), total, errors


def runner_count(offer: Offer, runners: int | None) -> float:
    return float(runners) if runners is not None else offer.vcpu / 4


def vcpu_per_runner(offer: Offer, runners: int | None) -> float:
    return offer.vcpu / runner_count(offer, runners)


def print_offer(offer: Offer, prefix: str = "", runners: int | None = None) -> None:
    vcpu_share = vcpu_per_runner(offer, runners)
    count = runner_count(offer, runners)
    print(f"{prefix}{offer.region} {offer.machine_type}: {offer.vcpu} vCPU, "
          f"{offer.ram_gib:.1f} GiB, ${offer.hourly_usd:.6f}/hour "
          f"({count:g} runners, {vcpu_share:g} vCPU/runner; "
          f"${offer.hourly_usd * 24:.4f}/day, "
          f"${offer.hourly_usd * 730:.4f}/month at 730h, "
          f"${offer.hourly_usd * 24 / vcpu_share:.4f}/runner/day, "
          f"${offer.hourly_usd * 730 / vcpu_share:.4f}/runner/month)", flush=True)


def parse_ram(value: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:gib|gb)?\s*", value, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError("RAM must be a number, optionally followed by GB or GiB")
    return float(match.group(1))


def render_table(offers: list[Offer], runners: int) -> None:
    headers = ("Region", "Instance type", "vCPU #", "RAM (GiB)", "Price/hour (USD)",
               "Price/day (USD)", "Price/month (USD, 730h)",
               "Runners", "vCPU/runner", "Price/runner/day (USD)",
               "Price/runner/month (USD)")
    rows = [(o.region, o.machine_type, str(o.vcpu), f"{o.ram_gib:.1f}",
             f"${o.hourly_usd:.6f}", f"${o.hourly_usd * 24:.4f}",
             f"${o.hourly_usd * 730:.4f}",
             f"{runner_count(o, runners):g}", f"{vcpu_per_runner(o, runners):g}",
             f"${o.hourly_usd * 24 / vcpu_per_runner(o, runners):.4f}",
             f"${o.hourly_usd * 730 / vcpu_per_runner(o, runners):.4f}") for o in offers]
    widths = [max([len(headers[i]), *(len(row[i]) for row in rows)]) for i in range(len(headers))]
    print(" | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def main() -> int:
    parser = argparse.ArgumentParser(description="Find cheap GCP Spot VMs across all active regions.")
    parser.add_argument("--provider", default="GCP", type=str.upper, choices=("GCP",), help="Provider (currently GCP)")
    parser.add_argument("--project", help="GCP project ID (defaults to active gcloud project)")
    parser.add_argument("--min-vcpu", type=int, default=32, help="Minimum vCPU count (default: 32)")
    parser.add_argument("--min-ram", type=parse_ram, default=128, metavar="GB", help="Minimum RAM (e.g. 128 or 128GB; default: 128)")
    parser.add_argument("--runners", type=int,
                        help="Runner count per instance (default: vCPU / 4; one runner per 4 vCPU)")
    parser.add_argument("--top", type=int, default=10, help="Number of cheapest results (default: 10)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Concurrent API requests (default: 600)")
    parser.add_argument("--list-price", type=float, help="Optional on-demand hourly price baseline (USD) for 95%%-off alerts")
    parser.add_argument("--billing-api-key", default=None,
                        help="GCP Billing Catalog API key (or set GOOGLE_CLOUD_API_KEY) for automatic 95%%-off checks")
    parser.add_argument("-v", action="count", default=0, dest="verbosity",
                        help="Increase diagnostic detail; repeat (-v, -vv, -vvv)")
    args = parser.parse_args()
    LOG.verbosity = args.verbosity
    configure_logging(logging.DEBUG if args.verbosity >= 3 else logging.INFO)
    if min(args.min_vcpu, args.min_ram, args.top, args.concurrency) <= 0 or (args.runners is not None and args.runners <= 0):
        parser.error("--min-vcpu, --min-ram, --top, --concurrency, and --runners must be positive")
    if args.list_price is not None and args.list_price <= 0:
        parser.error("--list-price must be positive")
    try:
        auth = get_auth(args.project)
        log(1, "using GCP project %s", auth.project)
        log(1, "starting asynchronous GCP Compute API scan")
        started = time.perf_counter()
        catalog_key = args.billing_api_key or os.environ.get("GOOGLE_CLOUD_API_KEY")
        offers, region_count, candidates, errors = asyncio.run(
            find(auth, args.min_vcpu, args.min_ram, args.top, args.concurrency,
                 args.list_price, catalog_key, args.runners))
        elapsed = time.perf_counter() - started
    except (OSError, RuntimeError) as exc:
        print(f"error: {error_detail(exc)}", file=sys.stderr)
        return 1

    print(f"GCP Spot VM prices for project {auth.project}; {region_count} active regions, "
          f"{candidates} regional shapes scanned in {elapsed:.2f}s.")
    if args.list_price is None and not catalog_key:
        print("95% discount alerts are disabled: set GOOGLE_CLOUD_API_KEY or pass --billing-api-key "
              "to compare against public on-demand rates.")
    if errors:
        print(f"Warning: {len(errors)} price request(s) failed; results may be incomplete.", file=sys.stderr)
        for error in errors[:5]:
            print(f"  {error_detail(error)}", file=sys.stderr)
    if not offers:
        print("No matching Spot price data found.", file=sys.stderr)
        return 2
        print("Prices are compute-only; disk, network, OS licensing, and capacity are excluded.")
    render_table(offers, args.runners)
    return 0
