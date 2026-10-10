"""Gather acceptance of small, explicit ASR gaps; audio gates stay strict."""
from __future__ import annotations
import math
from src.pipeline.qwen_plan import validate_block_row

SHORT_GAP_LIMIT_SECONDS = 15
SHORT_GAP_POLICY = 'short_gaps_under_15s_v1'


def recognition_coverage(rows, *, allow_short_missing=False):
    gaps = []
    for row in rows:
        validate_block_row(row, row)
        if row.get('missing_intervals'):
            from src.ai.qwen_missing_fallback import _aligned_parts
            if _aligned_parts(row) is None:
                raise ValueError('Missing recognition has no aligned partial timeline')
            gaps.extend(dict(gap, chunk_id=row['chunk_id']) for gap in row['missing_intervals'])
    return _coverage(gaps, allow_short_missing=allow_short_missing)


def _coverage(gaps, *, allow_short_missing):
    # Round outwards to source samples, then count overlapping intervals once.
    # This is a whole-lecture limit, never a per-block allowance.
    spans = sorted((math.floor(g['start']*16000), math.ceil(g['end']*16000)) for g in gaps)
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    samples = sum(end-start for start, end in merged)
    return dict(complete=not gaps, accepted=not gaps or (
        allow_short_missing and samples < SHORT_GAP_LIMIT_SECONDS*16000),
        policy=SHORT_GAP_POLICY if allow_short_missing else 'strict',
        limit_seconds=SHORT_GAP_LIMIT_SECONDS if allow_short_missing else 0,
        missing_seconds=samples/16000, missing_intervals=gaps)


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
    if coverage.get('policy') not in ('strict', SHORT_GAP_POLICY):
        raise ValueError('Unknown historical recognition policy')
    expected = _coverage(gaps, allow_short_missing=coverage['policy'] == SHORT_GAP_POLICY)
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
    return (f"转录提示：有 {coverage['missing_seconds']:g} 秒语音未识别（{periods}）。"
            '摘要仅依据已识别内容，这些时段请对照原录播。')
