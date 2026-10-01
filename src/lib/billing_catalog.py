"""Read a current authenticated Compute Engine Billing Catalog snapshot."""
import json
import subprocess
import urllib.request
from functools import lru_cache
from urllib.parse import urlencode
from decimal import Decimal
from datetime import datetime, timezone


@lru_cache(maxsize=1)
def catalog_skus() -> tuple[dict, ...]:
    token = subprocess.run(['gcloud', 'auth', 'print-access-token'], check=True,
                           text=True, capture_output=True, timeout=60).stdout.strip()
    if not token:
        raise RuntimeError('No credentials for Billing Catalog price lookup')
    rows = []
    page_token = None
    while True:
        query = {'currencyCode': 'USD', 'pageSize': '5000'}
        if page_token:
            query['pageToken'] = page_token
        url = 'https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus?' + urlencode(query)
        request = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(request, timeout=60) as response:
            page = json.load(response)
        rows.extend(page.get('skus', []))
        page_token = page.get('nextPageToken')
        if not page_token:
            return tuple(rows)


def clear_catalog_cache() -> None:
    catalog_skus.cache_clear()


def monthly_to_daily(monthly: Decimal) -> Decimal:
    """Normalize monthly disk SKU prices using Google's 730-hour month."""
    return monthly * 24 / 730


def unit_price(sku: dict, usage_unit: str) -> Decimal | None:
    now = datetime.now(timezone.utc)
    infos = [info for info in sku.get('pricingInfo', [])
             if not info.get('effectiveTime') or datetime.fromisoformat(info['effectiveTime'].replace('Z', '+00:00')) <= now]
    if not infos:
        return None
    expression = max(infos, key=lambda info: info.get('effectiveTime', '')).get('pricingExpression', {})
    actual_unit = expression.get('usageUnit', '')
    if actual_unit != usage_unit and not (usage_unit == 'h' and actual_unit.endswith(('.h', '-h', '_h'))):
        return None
    tiers = expression.get('tieredRates') or []
    if len(tiers) != 1 or Decimal(str(tiers[0].get('startUsageAmount', 0))) != 0:
        return None
    price = tiers[0].get('unitPrice', {})
    if price.get('currencyCode') != 'USD':
        return None
    amount = Decimal(str(price.get('units', 0))) + Decimal(str(price.get('nanos', 0))) / 1_000_000_000
    return amount if amount.is_finite() and amount >= 0 else None
