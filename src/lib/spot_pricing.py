"""Spot and on-demand price lookup helpers."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from .compute_api import CAPACITY_ROOT, LOG, AsyncGoogleApi, debug, log

def _catalog_page(api_key: str, page_token: str | None) -> dict:
    query = {"key": api_key, "pageSize": "5000"}
    if page_token:
        query["pageToken"] = page_token
    url = "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus?" + urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": "gcp-spot-finder/0.2"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        message = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"Billing Catalog API HTTP {exc.code}: {message}") from exc


def _sku_hourly(sku: dict) -> float | None:
    info = (sku.get("pricingInfo") or [{}])[0]
    expression = info.get("pricingExpression", {})
    unit = expression.get("usageUnit", "").lower()
    if unit not in {"h", "hour", "hours"} and not unit.endswith((".h", "-h", "_h")):
        return None
    rates = expression.get("tieredRates", [])
    if not rates:
        return None
    price = rates[0].get("unitPrice", {})
    if price.get("currencyCode") != "USD":
        return None
    return float(price.get("units", 0)) + float(price.get("nanos", 0)) / 1_000_000_000


async def load_catalog_rates(api_key: str | None) -> dict[tuple[str, str, str, str], float]:
    """Load regional Spot and on-demand CPU/RAM rates from GCP Billing."""
    if not api_key:
        return {}
    skus: list[dict] = []
    token: str | None = None
    pages = 0
    while True:
        page = await asyncio.to_thread(_catalog_page, api_key, token)
        pages += 1
        skus.extend(page.get("skus", []))
        token = page.get("nextPageToken")
        if not token:
            break
    result = parse_component_rates(skus)
    log(1, "loaded %d regional Spot/on-demand component rates from %d Billing Catalog pages", len(result), pages)
    return result


def parse_component_rates(skus) -> dict[tuple[str, str, str, str], float]:
    """Index regional Spot and on-demand CPU/RAM rates by machine series."""
    result: dict[tuple[str, str, str, str], float] = {}
    for sku in skus:
        category = sku.get("category", {})
        group = category.get("resourceGroup", "").upper()
        usage = category.get("usageType", "").lower()
        if usage not in {"ondemand", "preemptible", "spot"} or group not in {"CPU", "RAM"}:
            continue
        description = sku.get("description", "")
        # The series is the first word, as in "Spot Preemptible N2D AMD Instance Core"; this
        # excludes Custom, Extended, and sole-tenancy SKUs that runner shapes never use.
        match = re.match(r"(?:Spot Preemptible\s+)?([A-Za-z][0-9A-Za-z]*)\s+(?:(?:AMD|Intel|Arm)\s+)?"
                         r"Instance\s+(Core|RAM)\b", description, re.IGNORECASE)
        if not match:
            continue
        series = match.group(1).lower()
        component = "CPU" if match.group(2).lower() == "core" else "RAM"
        price_kind = "ondemand" if usage == "ondemand" else "spot"
        hourly = _sku_hourly(sku)
        if hourly is None:
            continue
        for region in sku.get("serviceRegions", []):
            key = (price_kind, region, series, component)
            result[key] = min(hourly, result.get(key, hourly))
    return result


def price_from_history(data: dict) -> float | None:
    history = data.get("priceHistory") or []
    if not history:
        return None
    latest = max(history, key=lambda row: row.get("interval", {}).get("endTime", ""))
    price = latest.get("listPrice", {})
    if price.get("currencyCode", "USD") != "USD":
        return None
    return float(price.get("units", 0)) + float(price.get("nanos", 0)) / 1_000_000_000


async def spot_price(api: AsyncGoogleApi, project: str, region: str,
                     machine: str) -> tuple[str, str, float | None]:
    log(1, "querying region %s from GCP", region)
    log(2, "requesting Spot price for %s/%s", region, machine)
    debug("POST Compute Engine capacityHistory for %s/%s", region, machine)
    # Although some generated reference pages show a v1 URL, the endpoint is
    # currently served from the beta API. The v1 request returns HTTP 400
    # "The service is not available for this project"; beta succeeds.
    path = f"{CAPACITY_ROOT}/projects/{quote(project, safe='')}/regions/{quote(region, safe='')}/advice/capacityHistory?fields=priceHistory"
    body = {
        "types": ["PRICE"],
        "instanceProperties": {
            "scheduling": {"provisioningModel": "SPOT"},
            "machineType": machine,
        },
    }
    # resourceNotReady can persist for several seconds; back off up to 16 seconds.
    attempts = 6
    for attempt in range(attempts):
        try:
            data = await api.post_json(path, body)
            break
        except RuntimeError as exc:
            transient = ("resourceNotReady" in str(exc)
                         or any(f"HTTP {code}" in str(exc) for code in (500, 502, 503, 504)))
            if not transient or attempt == attempts - 1:
                raise
            await asyncio.sleep(2 ** attempt)
            debug("retrying transient price error for %s/%s: %s", region, machine, exc)
    return region, machine, price_from_history(data)
