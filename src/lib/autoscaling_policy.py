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


def resource_decision(samples, state: dict, timestamp: float) -> str:
    """Two distinct five-minute checks: either >75% grows, all <75% shrinks.

    Unknown data breaks the streak; equality holds. Never mask a hot node
    with a fleet average. Caller enforces lifecycle, price and host bounds.
    """
    if not samples or any(not math.isfinite(v) or not 0 <= v <= 1
                          for pair in samples for v in pair):
        state.clear()
        return 'wait'
    previous = state.get('timestamp')
    if previous is not None and timestamp - previous < 300:
        return 'wait'
    direction = ('up' if any(cpu > .75 or memory > .75 for cpu, memory in samples)
                 else 'down' if all(cpu < .75 and memory < .75 for cpu, memory in samples)
                 else 'wait')
    streak = state.get('streak', 0) + 1 if (state.get('direction') == direction
             and previous is not None and timestamp - previous <= 450) else 1
    state.update(timestamp=timestamp, direction=direction, streak=streak)
    if direction != 'wait' and streak >= 2:
        state['streak'] = 0
        return direction
    return 'wait'
