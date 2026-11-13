"""Pure shared-runner limits and workload estimates; no Actions transport."""
import math
import statistics

MAX_TOTAL = 15
MAX_JOBS = MAX_TOTAL - 1  # The controller itself consumes a Runner.
MAX_COURSES = 5
MAX_WORKERS = 6
# Every preparation logs in with the same UIS account and streams media over WebVPN.
# Concurrent preparations keep invalidating each other's media session (302 to login
# every ~30 s), so the pool fetches one lecture at a time; ASR still overlaps.
MAX_PREPARES = 1
TARGET_SECONDS = 75 * 60
DEFAULT_RTF = 2.0


def estimate_rtf(rows, fallback=DEFAULT_RTF):
    values = [r['decode_seconds']/(r['end']-r['start']) for r in rows
              if isinstance(r.get('decode_seconds'), (int, float))
              and math.isfinite(r['decode_seconds']) and r['decode_seconds'] > 0
              and r['end'] > r['start']]
    return min(10.0, max(.25, statistics.median(values))) if len(values) >= 5 else fallback


def desired_workers(seconds, blocks, rtf=DEFAULT_RTF):
    if (not math.isfinite(seconds) or seconds < 0 or not math.isfinite(rtf)
            or not .25 <= rtf <= 10 or type(blocks) is not int or blocks < 0):
        raise ValueError('Invalid workload estimate')
    return min(MAX_WORKERS, blocks, max(1, math.ceil(seconds*rtf/TARGET_SECONDS))) if blocks else 0
