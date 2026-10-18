"""Pure helpers for disjoint intervals of unpriced dashboard history.

Inputs map reasons to legacy ``[start, end]`` pairs or lists of such pairs.
Outputs always use nested lists of float bounds, with ``None`` for an open end.
Negative timestamps and zero-length evidence are preserved; malformed, reversed,
boolean, or nonfinite bounds are ignored. Windows use half-open intervals.
"""

from collections.abc import Mapping
import math
from typing import Any


Interval = list[float | None]
Intervals = dict[str, list[Interval]]
_STORAGE_REASONS = frozenset({"suspended_memory", "fallback_storage"})
_COMPUTE_REASONS = frozenset({"prior_compute", "fallback_compute"})
_TAX_REASONS = frozenset({"fallback_compute_tax", "fallback_storage_tax", "fallback_tax"})


def _finite(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _bounds(raw: Any) -> Interval | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    start, end = raw
    if not _finite(start) or (end is not None and (not _finite(end) or end < start)):
        return None
    return [float(start), None if end is None else float(end)]


def _pairs(raw: Any) -> list[Interval]:
    """Decode valid pairs without merging away explicit zero-length evidence."""
    pair = _bounds(raw)
    candidates = [pair] if pair is not None else (
        [_bounds(item) for item in raw] if isinstance(raw, (list, tuple)) else [])
    return [item for item in candidates if item is not None]


def normalize_unpriced_intervals(raw: Any) -> Intervals:
    """Return fresh sorted intervals per reason, merging only overlap or touching bounds.

    Accept legacy pairs and nested lists/tuples. Invalid entries do not discard
    valid siblings. Empty reasons are omitted; no input objects are mutated.
    """
    normalized: Intervals = {}
    if not isinstance(raw, Mapping):
        return normalized
    for reason, value in raw.items():
        if not isinstance(reason, str):
            continue
        ordered = sorted(_pairs(value), key=lambda pair: pair[0])
        merged: list[Interval] = []
        for start, end in ordered:
            if not merged or (merged[-1][1] is not None and start > merged[-1][1]):
                merged.append([start, end])
            elif merged[-1][1] is None or end is None:
                merged[-1][1] = None
            else:
                merged[-1][1] = max(merged[-1][1], end)
        if merged:
            normalized[reason] = merged
    return normalized


def add_unpriced_interval(intervals: dict, reason: str, start: float, end: float | None) -> None:
    """Add evidence and normalize only ``reason`` in place; repeated adds are idempotent.

    An invalid addition leaves existing valid evidence intact. An open interval
    stays open until the caller explicitly closes its bound before normalization.
    """
    current = normalize_unpriced_intervals({reason: intervals.get(reason)}).get(reason, [])
    updated = normalize_unpriced_intervals({reason: current + [[start, end]]})
    if reason in updated:
        intervals[reason] = updated[reason]
    else:
        intervals.pop(reason, None)


def history_components(raw: Any) -> tuple[bool, bool]:
    """Return principal (compute, storage) flags, excluding all three tax reasons.

    Unrecognized reasons conservatively affect both principal components.
    """
    reasons = normalize_unpriced_intervals(raw).keys() - _TAX_REASONS
    return (any(reason not in _STORAGE_REASONS for reason in reasons),
            any(reason not in _COMPUTE_REASONS for reason in reasons))


def tax_history_components(raw: Any) -> tuple[bool, bool]:
    """Return (compute tax, storage tax) flags; unsplit legacy tax affects both."""
    reasons = normalize_unpriced_intervals(raw)
    legacy = "fallback_tax" in reasons
    return (legacy or "fallback_compute_tax" in reasons,
            legacy or "fallback_storage_tax" in reasons)


def split_legacy_tax_intervals(raw: Any, compute_intervals: Any = None,
                               storage_intervals: Any = None) -> Intervals:
    """Replace legacy tax evidence with tax components clipped to actual charges.

    Component ranges accept the same pair/nested formats as stored history.
    None means unknown charge context and retains every legacy interval; []
    proves no charge and contributes no legacy evidence for that component.
    Preserve and normalize all nonlegacy reasons without mutating inputs.
    Charge ranges are half-open: touching endpoints do not intersect, and empty
    charge ranges contribute nothing. Zero-length legacy evidence is preserved
    when its timestamp is inside a nonempty charge range, or context is unknown.
    """
    normalized = normalize_unpriced_intervals(raw)
    normalized.pop("fallback_tax", None)
    # Clip before merging: [0, 3] plus explicit [3, 3] must retain the latter
    # when the only known charge interval starts at 3.
    legacy = _pairs(raw.get("fallback_tax")) if isinstance(raw, Mapping) else []
    if not legacy:
        return normalized
    for reason, actual in (("fallback_compute_tax", compute_intervals),
                           ("fallback_storage_tax", storage_intervals)):
        if actual is None:
            clipped = [pair[:] for pair in legacy]
        else:
            charges = normalize_unpriced_intervals({"charge": actual}).get("charge", [])
            clipped = []
            for left, right in legacy:
                for charge_start, charge_end in charges:
                    if charge_end is not None and charge_end <= charge_start:
                        continue
                    if right == left:
                        if charge_start <= left and (charge_end is None or left < charge_end):
                            clipped.append([left, right])
                        continue
                    start = max(left, charge_start)
                    end = (charge_end if right is None else right if charge_end is None
                           else min(right, charge_end))
                    if end is None or start < end:
                        clipped.append([start, end])
        if clipped:
            normalized.setdefault(reason, []).extend(clipped)
    return normalize_unpriced_intervals(normalized)


def history_in_window(raw: Any, start: float, end: float, include_storage: bool = True,
                      *, include_tax: bool | None = None) -> bool:
    """Check actual disjoint overlap with ``[start, end)`` in the selected mode.

    Compute principal always counts; storage principal requires include_storage.
    Compute tax requires include_tax, storage tax requires both, and unsplit
    legacy tax conservatively requires only include_tax. None defaults tax to
    include_storage for legacy API modes. Empty/reversed windows and zero-length
    intervals do not overlap. Nonfinite window bounds raise ValueError.
    """
    if not _finite(start) or not _finite(end):
        raise ValueError("window bounds must be finite numbers")
    if end <= start:
        return False
    tax = include_storage if include_tax is None else include_tax

    def selected(reason: str) -> bool:
        if reason in _TAX_REASONS:
            return bool(tax and (reason != "fallback_storage_tax" or include_storage))
        return bool(include_storage or reason not in _STORAGE_REASONS)

    return any(left < end and (right is None or (right > start and right > left))
               for reason, ranges in normalize_unpriced_intervals(raw).items()
               if selected(reason)
               for left, right in ranges)


def compact_unpriced_intervals(raw: Any, now: float, retention: float) -> tuple[Intervals, bool, bool]:
    """Return (recent intervals, compute principal ever, storage principal ever).

    Drop closed intervals ending at or before ``now - retention``. Keep open and
    straddling intervals intact. ``now`` must be finite; retention must also be
    finite and nonnegative. Principal flags are collected before pruning; callers
    collect tax_history_components separately and persist all lifetime flags.
    """
    if not _finite(now) or not _finite(retention) or retention < 0:
        raise ValueError("now must be finite and retention must be finite and nonnegative")
    normalized = normalize_unpriced_intervals(raw)
    compute_ever, storage_ever = history_components(normalized)
    cutoff = now - retention
    recent = {}
    for reason, ranges in normalized.items():
        kept = [pair for pair in ranges if pair[1] is None or pair[1] > cutoff]
        if kept:
            recent[reason] = kept
    return recent, compute_ever, storage_ever
