"""Validated cross-runner material at the normal LectureRunner boundary."""
from __future__ import annotations
import hashlib
import math
from src.pipeline.qwen_plan import validate_plan, validate_result, fingerprint
from src.ai.qwen_segmentation import deduplicated_chunk_rows
from src.pipeline.recognition_coverage import recognition_coverage, SHORT_GAP_POLICY


def assemble_material(plan, results, *, audio_path=None, media_seconds=None, allow_short_missing=False):
    validate_plan(plan)
    if len(results) != len(plan['shards']):
        raise ValueError('All planned ASR shards must finish before finalization')
    seen, rows = set(), []
    for result in results:
        shard = result['shard_id']
        if shard in seen:
            raise ValueError('Duplicate ASR shard')
        blocks = validate_result(plan, result, shard, require_complete=not allow_short_missing)
        if blocks != set(plan['shards'][shard]['chunk_ids']):
            raise ValueError('All planned ASR blocks must finish before finalization')
        if result.get('complete') is not True and not any(r.get('missing_intervals') for r in result['chunks']):
            raise ValueError('Shard incomplete; summary forbidden')
        seen.add(shard)
        rows.extend(result['chunks'])
    rows.sort(key=lambda row: row['chunk_id'])
    coverage = recognition_coverage(rows, allow_short_missing=allow_short_missing)
    if not coverage['accepted']:
        raise ValueError('Shard incomplete; summary forbidden')
    passages = []
    for row in rows:
        if row.get('missing_intervals'):
            passages.extend(dict(part, chunk_id=row['chunk_id']) for part in row['recognized_segments'])
        else:
            passages.append(row)
    clean = deduplicated_chunk_rows(passages)
    material = dict(selection=plan['selection'], plan_hash=fingerprint(plan), complete=coverage['complete'],
                    recognition_coverage=coverage,
                    audio_seconds=plan['audio_seconds'], media_seconds=media_seconds,
                    audio_sha256=plan['audio_sha256'], recognition_terms=plan['recognition_terms'],
                    full_chunks=rows, vad_windows=plan['vad_windows'], audio_path=audio_path,
                    weak_windows=[{'start_ms': round(max(a, r['start'])*1000),
                                   'end_ms': round(min(b, r['end'])*1000), 'text': r['text']}
                                  for r in rows for a,b in plan['vad_windows']
                                  if min(b,r['end']) > max(a,r['start'])],
                    transcript='\n'.join(row['text'] for row in clean),
                    segments=[dict(start_ms=round(row['start']*1000), end_ms=round(row['end']*1000),
                                   text=row['text']) for row in clean])
    validate_material(material, plan['selection']['course_id'], plan['selection']['sub_id'])
    return material


def validate_material(material, course_id, sub_id):
    if (any(str(material.get('selection', {}).get(k)) != str(v)
                   for k, v in [('course_id', course_id), ('sub_id', sub_id)])
            or not isinstance(material.get('transcript'), str)
            or not isinstance(material.get('segments'), list)
            or not isinstance(material.get('full_chunks'), list)
            or not isinstance(material.get('vad_windows'), list)
            or not isinstance(material.get('recognition_terms'), list)):
        raise ValueError('Invalid prepared lecture identity or material')
    coverage = material.get('recognition_coverage')
    actual = recognition_coverage(material['full_chunks'],
        allow_short_missing=bool(coverage and coverage.get('policy') == SHORT_GAP_POLICY))
    if (not actual['accepted'] or material.get('complete') is not actual['complete']
            or (coverage is not None and coverage != actual)):
        raise ValueError('Invalid prepared recognition coverage')
    duration = material.get('audio_seconds')
    validate_audio_duration(duration, material.get('media_seconds'))
    for segment in material['segments']:
        if (not isinstance(segment.get('text'), str)
                or not 0 <= segment['start_ms'] < segment['end_ms'] <= round(duration*1000)):
            raise ValueError('Prepared segment changed the source timeline')


def validate_audio_duration(duration, media_seconds):
    """The same completeness gate runs before cloud repair and final assembly."""
    if not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid prepared audio duration')
    expected = media_seconds or 0
    # The production downloader has no three-hour sampling cap. Reject a
    # substantial known shortfall rather than publish a partial classroom.
    if expected > 0 and duration < expected - max(120, expected * .05):
        raise ValueError('Prepared audio is incomplete relative to media duration')


def cached_material(db, lecture):
    """Only a transcript tied to saved complete timeline metadata is reusable."""
    import json
    raw = db.read_meta('qwen_pipeline:' + str(lecture['sub_id']))
    if not raw or not lecture.get('transcript'):
        return None
    metadata = json.loads(raw)
    if 'material' not in metadata:
        return None
    material = metadata['material']
    digest = hashlib.sha256(lecture['transcript'].encode()).hexdigest()
    if digest != metadata['transcript_sha256']:
        raise ValueError('Cached transcript differs from its verified checkpoint')
    material['transcript'] = lecture['transcript']
    validate_material(material, lecture['course_id'], lecture['sub_id'])
    return material, metadata['review']
