"""Schedule tier observations without adding post-observation work to their wait."""
import math


def tier_poll_delay(state, interval, started_at, finished_at):
    rows = state.get('latest_resources')
    if not isinstance(rows, list) or not rows:
        return interval
    stamps = [row.get('observed_at') if isinstance(row, dict) else None for row in rows]
    if any(isinstance(stamp, bool) or not isinstance(stamp, (int, float))
           or not math.isfinite(stamp) for stamp in stamps):
        return interval
    observed_at = stamps[0]
    if any(stamp != observed_at for stamp in stamps) or not started_at <= observed_at <= finished_at:
        return interval  # Missing/newly stale evidence and clock reversals keep the full wait.
    # The next iteration polls jobs before sampling, so genuine samples remain
    # at least interval apart. Sleeping after health/price work otherwise makes
    # six five-minute observations age out of the unchanged 30-minute history.
    return max(0, interval - (finished_at - observed_at))
