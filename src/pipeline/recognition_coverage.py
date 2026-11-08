"""Gather acceptance of small, explicit ASR gaps; audio gates stay strict."""
from __future__ import annotations
import math
from src.pipeline.qwen_plan import validate_block_row

SHORT_GAP_LIMIT_SECONDS = 15
SHORT_GAP_POLICY = 'short_gaps_under_15s_v1'
# A few failed blocks (e.g. breaks or after-class audio a model loops on) may reach the
# summary, which then carries an explicit notice with the missing periods.
LIMITED_GAP_POLICY = 'limited_block_gaps_v2'
LIMITED_GAP_MAX_SECONDS = 300
LIMITED_GAP_MAX_FRACTION = 0.03
LIMITED_GAP_MAX_BLOCKS = 3
TOLERANT_POLICIES = (SHORT_GAP_POLICY, LIMITED_GAP_POLICY)


def recognition_coverage(rows, *, allow_short_missing=False, policy=None):
    """allow_short_missing selects the current tolerant policy; policy= rechecks a stored one."""
    gaps = []
    for row in rows:
        validate_block_row(row, row)
        if row.get('missing_intervals'):
            from src.ai.qwen_missing_fallback import _aligned_parts
            if _aligned_parts(row) is None:
                raise ValueError('Missing recognition has no aligned partial timeline')
            gaps.extend(dict(gap, chunk_id=row['chunk_id']) for gap in row['missing_intervals'])
    if policy is None:
        policy = LIMITED_GAP_POLICY if allow_short_missing else 'strict'
    span = max((r['end'] for r in rows), default=0)-min((r['start'] for r in rows), default=0)
    return _coverage(gaps, policy=policy, lecture_seconds=span)


def _coverage(gaps, *, policy, lecture_seconds=0):
    # Round outwards to source samples, then count overlapping intervals once.
    # This is a whole-lecture limit, never a per-block allowance.
    if policy not in ('strict',) + TOLERANT_POLICIES:
        raise ValueError('Unknown recognition coverage policy')
    spans = sorted((math.floor(g['start']*16000), math.ceil(g['end']*16000)) for g in gaps)
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    samples = sum(end-start for start, end in merged)
    short = samples < SHORT_GAP_LIMIT_SECONDS*16000
    report = dict(complete=not gaps, accepted=not gaps or (policy != 'strict' and short),
                  policy=policy, limit_seconds=0 if policy == 'strict' else SHORT_GAP_LIMIT_SECONDS,
                  missing_seconds=samples/16000, missing_intervals=gaps)
    if policy == LIMITED_GAP_POLICY:
        lecture = round(max(0, lecture_seconds), 3)
        limit = round(max(SHORT_GAP_LIMIT_SECONDS,
                          min(LIMITED_GAP_MAX_SECONDS, LIMITED_GAP_MAX_FRACTION*lecture)), 3)
        blocks = len({g.get('chunk_id') for g in gaps})
        report.update(limit_seconds=limit, lecture_seconds=lecture, gap_blocks=blocks,
                      max_gap_blocks=LIMITED_GAP_MAX_BLOCKS,
                      accepted=not gaps or short or (limit > SHORT_GAP_LIMIT_SECONDS and samples <= limit*16000
                                                     and blocks <= LIMITED_GAP_MAX_BLOCKS))
    return report


def validate_coverage_report(plan, coverage):
    """Recheck the compact authenticated report after full rows are discarded."""
    if not isinstance(coverage, dict) or not isinstance(coverage.get('missing_intervals'), list):
        raise ValueError('Invalid historical recognition coverage')
    gaps = coverage['missing_intervals']
    for gap in gaps:
        if (not isinstance(gap, dict) or type(gap.get('chunk_id')) is not int
                or not 0 <= gap['chunk_id'] < len(plan['blocks'])
                or any(type(gap.get(k)) not in (int, float) or not math.isfinite(gap[k])
                       for k in ('start', 'end'))):
            raise ValueError('Invalid historical recognition gap')
        block = plan['blocks'][gap['chunk_id']]
        if not block['start'] <= gap['start'] < gap['end'] <= block['end']:
            raise ValueError('Historical recognition gap changed the source timeline')
    if coverage.get('policy') not in ('strict',) + TOLERANT_POLICIES:
        raise ValueError('Unknown historical recognition policy')
    blocks = plan['blocks']
    span = max((b['end'] for b in blocks), default=0)-min((b['start'] for b in blocks), default=0)
    expected = _coverage(gaps, policy=coverage['policy'], lecture_seconds=span)
    if coverage != expected or not expected['accepted']:
        raise ValueError('Historical recognition coverage is not accepted')
    return expected


def missing_recognition_notice(coverage):
    if not coverage or not coverage.get('missing_intervals'):
        return ''
    def stamp(seconds):
        millis = round(seconds*1000)
        hours, rest = divmod(millis, 3600000)
        minutes, rest = divmod(rest, 60000)
        return f'{hours:02}:{minutes:02}:{rest/1000:06.3f}'
    periods = '、'.join(f"{stamp(g['start'])}–{stamp(g['end'])}" for g in coverage['missing_intervals'])
    return (f"转录提示：有 {round(coverage['missing_seconds'], 1):g} 秒语音未识别（{periods}）。"
            '摘要仅依据已识别内容，这些时段请对照原录播。')
