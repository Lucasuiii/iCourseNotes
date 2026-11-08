"""Gather-only Doubao repair of exhausted Qwen gaps, with shared durable quota.

Original Worker/queue results remain immutable. Only validated cloud responses
are incorporated into derived finalization rows, in their original time order.
"""
import copy
import math
from src.pipeline.qwen_plan import fingerprint, validate_result
from src.ai.qwen_review_ledger import validate_ledger
from src.ai.segment_rescue import MAX_CLOUD_SECONDS, MAX_CLOUD_CLIPS
from src.ai.doubao_asr import rescue_intervals_pcm


def _aligned_parts(row):
    """Require a complete sample timeline before splicing partial recognition."""
    parts = row.get('recognized_segments', [])
    if not isinstance(parts, list):
        return None
    if any(not isinstance(p, dict) or not isinstance(p.get('text'), str)
           or any(type(p.get(k)) not in (int, float) or not math.isfinite(p[k])
                  for k in ('start', 'end')) for p in parts):
        return None
    if '\n'.join(p['text'] for p in parts if p['text']) != row['text']:
        return None  # Historical concatenated text has no trustworthy alignment.
    timeline = sorted(parts + row['missing_intervals'], key=lambda p: p['start'])
    position = row['start']
    for p in timeline:
        if abs(p['start']-position) > 1/16000 + 1e-9 or p['end'] <= p['start']:
            return None
        position = p['end']
    if abs(position-row['end']) > 1/16000 + 1e-9:
        return None
    return copy.deepcopy(parts)


def _valid_segments(segments, interval):
    if not isinstance(segments, list) or not segments:
        return False  # No-speech/empty responses cannot repair known ASR gaps.
    previous = interval['start_ms']
    for s in segments:
        if (not isinstance(s, dict) or type(s.get('start_ms')) is not int
                or type(s.get('end_ms')) is not int
                or not previous <= s['start_ms'] < s['end_ms'] <= interval['end_ms']
                or not isinstance(s.get('text'), str) or not s['text'].strip()):
            return False
        previous = s['end_ms']
    return True


def repair_missing(plan, results, audio_path, state, checkpoint, *, api_key):
    """Use <=30s gap clips within the existing 900s/40-call whole-lecture ledger.

    A clip Doubao answers with no utterances is recorded as no_speech: the gap is
    closed with empty text and spending continues. Transport/service errors still
    stop all further fallback for the lecture.

    Reservation is checkpointed before submission. Failed/unknown submissions
    never refund quota or replay. Successful responses survive gather retries.
    Requests round outwards to milliseconds; logical row boundaries stay exact.
    """
    validate_ledger(state)
    if not callable(checkpoint):
        raise ValueError('Missing ASR fallback requires durable checkpoints')
    shards_seen = set()
    for result in results:
        seen = validate_result(plan, result, result['shard_id'])
        shard = result['shard_id']
        if shard in shards_seen or seen != set(plan['shards'][shard]['chunk_ids']):
            raise ValueError('Missing or duplicate original blocks before fallback')
        shards_seen.add(shard)
    if shards_seen != set(range(len(plan['shards']))):
        raise ValueError('Missing original shards before fallback')
    repaired = copy.deepcopy(results)
    if not api_key:
        return repaired
    rows = sorted((r for result in results for r in result['chunks']), key=lambda r: r['chunk_id'])
    binding = {'plan_hash': fingerprint(plan), 'source_hash': fingerprint(rows)}
    saved = state.get('asr_fallback')
    if saved is not None and saved != binding:
        raise ValueError('Missing ASR fallback belongs to different immutable results')
    state['asr_fallback'] = binding
    attempts = state.setdefault('attempts', [])
    for result in repaired:
        for row in result['chunks']:
            if not row.get('missing_intervals'):
                continue
            parts = _aligned_parts(row)
            if parts is None:
                continue
            remaining = []
            for gap in row['missing_intervals']:
                start = gap['start']
                while start < gap['end']:
                    start_ms = math.floor(start*1000+1e-7)
                    end = min(gap['end'], (start_ms+30000)/1000)
                    interval = {'start_ms': start_ms, 'end_ms': math.ceil(end*1000-1e-7),
                                'kind': 'missing_asr'}
                    seconds = (interval['end_ms']-interval['start_ms'])/1000
                    item = next((a for a in attempts if a['interval'] == interval), None)
                    overlaps = any(interval['start_ms'] < a['interval']['end_ms']
                                   and a['interval']['start_ms'] < interval['end_ms'] for a in attempts)
                    if (item is None and not overlaps and not state.get('failed')
                            and len(attempts) < MAX_CLOUD_CLIPS
                            and state.get('seconds', 0)+seconds <= MAX_CLOUD_SECONDS):
                        item = {'interval': interval, 'seconds': seconds, 'status': 'reserved'}
                        attempts.append(item)
                        state['seconds'] = state.get('seconds', 0)+seconds
                        checkpoint()  # Unknown outcomes retain the reservation.
                        print(f'[Doubao fallback] Clip {len(attempts)} reserved; '
                              f'lecture quota={state["seconds"]:.3f}s/{MAX_CLOUD_SECONDS}s.', flush=True)
                        try:
                            rescues, used, failed = rescue_intervals_pcm(audio_path, api_key, [interval],
                                max_seconds=seconds, max_clips=1, hotwords=plan['recognition_terms'])
                            answered = (not failed and abs(used-seconds) < 1e-6
                                        and len(rescues) == 1 and rescues[0][0] == interval)
                            segments = rescues[0][1] if answered else []
                            valid = answered and _valid_segments(segments, interval)
                            # A completed request with no utterances is an explicit "no speech" answer
                            # (breaks, before/after class), not an unavailable service.
                            silent = answered and segments == []
                        except Exception:
                            segments, valid, silent = [], False, False
                        item.update(status='complete' if valid else 'no_speech' if silent else 'failed',
                                    segments=segments if valid else [])
                        if not (valid or silent):
                            state['failed'] = True  # Stop further spending on an unavailable service.
                        checkpoint()
                        print(f'[Doubao fallback] Clip {len(attempts)} {item["status"]}.', flush=True)
                    if item is not None and item['status'] == 'complete':
                        if not _valid_segments(item.get('segments'), interval):
                            raise ValueError('Invalid cached missing ASR response')
                        parts.append({'start': start, 'end': end,
                                      'text': ' '.join(s['text'] for s in item['segments']),
                                      'source': 'doubao_fallback'})
                    elif item is not None and item['status'] == 'no_speech':
                        if item.get('segments') != []:
                            raise ValueError('Invalid cached no-speech response')
                        parts.append({'start': start, 'end': end, 'text': '', 'source': 'doubao_no_speech'})
                    else:
                        remaining.append(dict(gap, start=start, end=end))
                    start = end
            parts.sort(key=lambda p: p['start'])
            row.update(text='\n'.join(p['text'] for p in parts if p['text']),
                       recognized_segments=parts, missing_intervals=remaining,
                       quality_state='missing_audio' if remaining else 'doubao_fallback')
        result['complete'] = not any(r.get('missing_intervals') for r in result['chunks'])
        validate_result(plan, result, result['shard_id'])
    validate_ledger(state)
    gaps = sum(len(r.get('missing_intervals', [])) for s in repaired for r in s['chunks'])
    print(f'[Doubao fallback] Finalization has {gaps} unresolved intervals.', flush=True)
    return repaired
