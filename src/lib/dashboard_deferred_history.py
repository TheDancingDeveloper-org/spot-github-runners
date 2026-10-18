"""Provider-independent size and pricing boundaries for deferred disk history.

Contexts are opaque JSON-compatible dictionaries owned by the caller. The first
observed size and context also apply before their first timestamp, allowing
creation-time backfill without applying later tax or catalog changes retroactively.
"""

from collections.abc import Callable
import copy
import math
from typing import Any


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def append_deferred_context(disk: dict, at: float, context: dict) -> bool:
    """Append a changed context, copying nested state and returning whether it changed.

    Call in observation order. An empty timeline starts at the first finite value
    among billing_deferred_since, created_epoch, and at (zero is a valid value).
    Later changes start at at; repeated identical snapshots add no boundary.
    Multiple changes at one timestamp are allowed; recovery uses the last one.
    """
    if not _finite(at):
        raise ValueError("Deferred context timestamp must be finite")
    if not isinstance(context, dict):
        raise TypeError("Deferred pricing context must be a dictionary")
    history = disk.get("billing_deferred_pricing") or []
    if history and history[-1][1] == context:
        return False
    if not history:
        at = next(value for value in (disk.get("billing_deferred_since"), disk.get("created_epoch"), at)
                  if _finite(value))
    # Detach the existing history too: another carrier may still reference it.
    disk["billing_deferred_pricing"] = copy.deepcopy(history) + [[float(at), copy.deepcopy(context)]]
    return True


def _entries(raw: Any, *, contexts: bool) -> list[tuple[float, Any]]:
    """Read valid persisted entries without changing their order or nested values."""
    if not isinstance(raw, (list, tuple)):
        return []
    entries = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2 or not _finite(item[0]):
            continue
        at, value = item
        valid = isinstance(value, dict) if contexts else _finite(value) and value >= 0
        if valid:
            entries.append((float(at), value))
    # Stable sorting preserves last-write precedence at equal timestamps.
    return sorted(entries, key=lambda item: item[0])


def _value_at(entries: list[tuple[float, Any]], at: float, fallback: Any) -> Any:
    if not entries:
        return fallback
    selected = entries[0][1]
    for boundary, value in entries:
        if boundary > at:
            break
        selected = value
    return selected


def deferred_segments(
    disk: dict,
    start: float,
    end: float,
    fallback_context: dict,
    price_disk: Callable[[dict, dict], float],
) -> list[tuple[float, float, float]]:
    """Price half-open intervals split at every saved size or context boundary.

    Prefer recovered_sizes over billing_deferred_sizes, then the disk's current
    gb. With no saved contexts use fallback_context. Preserve zero prices, sizes,
    and taxes. Ignore malformed stored entries; reject nonfinite window bounds.
    Each callback receives independent deep copies, so even a callback that
    mutates its arguments cannot change inputs or another interval's price.
    """
    if not _finite(start) or not _finite(end):
        raise ValueError("Deferred history window bounds must be finite")
    if end <= start:
        return []
    sizes = (_entries(disk.get("recovered_sizes"), contexts=False)
             or _entries(disk.get("billing_deferred_sizes"), contexts=False))
    contexts = _entries(disk.get("billing_deferred_pricing"), contexts=True)
    boundaries = sorted({float(start), float(end)}
                        | {at for at, _ in sizes + contexts if start < at < end})
    segments = []
    for begin, finish in zip(boundaries, boundaries[1:]):
        candidate = copy.deepcopy(disk)
        candidate["gb"] = _value_at(sizes, begin, disk.get("gb", 0))
        context = copy.deepcopy(_value_at(contexts, begin, fallback_context))
        segments.append((begin, finish, float(price_disk(candidate, context))))
    return segments
