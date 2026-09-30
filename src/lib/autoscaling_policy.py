"""Pure capacity and price rules for the runner autoscaler."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Iterable

from .compute_api import Offer


def affordable_offers(offers: Iterable[Offer], max_daily_usd: Decimal) -> list[Offer]:
    """Return offers below the strict per-VM daily compute cap, cheapest first."""
    if max_daily_usd <= 0:
        raise ValueError("max_daily_usd must be positive")
    selected = [offer for offer in offers if Decimal(str(offer.hourly_usd)) * 24 < max_daily_usd]
    return sorted(selected, key=lambda offer: (offer.hourly_usd, offer.region, offer.machine_type))


def vm_count_for_floor(current_runners: int, minimum_runners: int, runners_per_vm: int) -> int:
    """Calculate the number of whole hosts needed to restore the runner floor."""
    if current_runners < 0 or minimum_runners < 1 or runners_per_vm < 1:
        raise ValueError("runner counts must be nonnegative and capacities must be positive")
    deficit = max(0, minimum_runners - current_runners)
    return math.ceil(deficit / runners_per_vm)
