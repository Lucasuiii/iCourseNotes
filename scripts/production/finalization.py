"""Formal finalization stage, with explicit runtime services and frozen inputs."""
from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone
from src.pipeline.recognition_coverage import recognition_coverage, SHORT_GAP_LIMIT_SECONDS


def gather(runtime):
    slot = int(os.environ['COURSE_SLOT'])
    files = runtime.read_preparation(); spec = runtime.read_json(files['specification.json'])
    prior = runtime.root()/'previous'
    restored_current = runtime.artifact(f'qwen-production-state-{slot}', prior)
    if restored_current:
        saved = runtime.decode(prior/'state.enc', 'state')
        if saved['specification.json'] != files['specification.json']:
            raise ValueError('Finalization checkpoint belongs to a different prepared lecture')
        runtime.validate_checkpoint_age(saved, os.environ['GITHUB_RUN_ID'], slot, prior_only=True)
        files['database.db'] = saved['database.db']
        review = runtime.read_json(saved['review.json'])
    else:
        if runtime.last_finalization_attempt(os.environ['GITHUB_RUN_ID'], slot, prior_only=True):
            raise ValueError('Prior finalization quota is unknown; fresh review forbidden')
        review = spec.get('review', {})
    from src.ai.qwen_review_ledger import validate_ledger
    from src.runtime import config
    validate_ledger(review)
    (runtime.root()/'course.db').write_bytes(files['database.db'])
    db = runtime.CheckpointDatabase(str(runtime.root()/'course.db'))
    course, lecture = spec['course_id'], spec['lecture']; sub_id = str(lecture['sub_id'])
    material = spec.get('material')
    initial_errors = db.get_lecture(sub_id).get('error_count') or 0
    coverage = material.get('recognition_coverage') if material else None
    missing_intervals = coverage.get('missing_intervals', []) if coverage else []

    def checkpoint():
        row = db.get_lecture(sub_id)
        if spec['mode'] == 'sharded' or (material is not None and db.get_lecture(sub_id).get('transcript')):
            metadata = {'review': review, 'updated_at': datetime.now(timezone.utc).isoformat()}
            if spec['mode'] == 'sharded':
                metadata['recovery'] = {'run_id': os.environ['GITHUB_RUN_ID'], 'task_slot': slot,
                                        'plan_hash': runtime.fingerprint(spec['plan'])}
            if material is not None and db.get_lecture(sub_id).get('transcript'):
                durable = {k:v for k,v in material.items() if k != 'audio_path'}
                transcript = db.get_lecture(sub_id)['transcript']
                metadata.update(material=durable, transcript_sha256=hashlib.sha256(transcript.encode()).hexdigest())
            if row.get('processed_at'):
                # Completed histories need only audit identity and quota totals,
                # not another copy of the full classroom in the metadata shard.
                metadata.pop('material', None); metadata.pop('recovery', None)
                metadata['complete'] = True
                if material:
                    metadata['recognition_coverage'] = material.get('recognition_coverage')
                    metadata.update(audio_sha256=material['audio_sha256'], plan_hash=material['plan_hash'],
                                    audio_seconds=material['audio_seconds'])
                    cost = runtime.asr_cost(material)
                    if cost: metadata['asr_cost'] = cost
            db.write_meta('qwen_pipeline:'+sub_id, json.dumps(metadata, ensure_ascii=False))
        payload = {'specification.json': files['specification.json'], 'review.json': runtime.shards.encoded(review),
                   'attempt.json': runtime.shards.encoded(int(os.environ.get('GITHUB_RUN_ATTEMPT', '1'))),
                   'database.db': runtime.lecture_snapshot(db, runtime.root()/'snapshot.db', course, sub_id)}
        runtime.encode(payload, 'state', runtime.out('state.enc'))
        if lecture.get('_validation'):
            plan = spec.get('plan', {})
            frozen_terms = plan.get('recognition_terms', spec.get('glossary_snapshot', {}).get('terms', []))
            from src.ai.automatic_glossary import AutomaticGlossary
            stages = AutomaticGlossary(db, course).stages()
            audit = dict(lecture['_validation'], mode=spec['mode'],
                asr_execution=plan.get('execution', 'not_started'),
                automatic_terms=os.environ.get('AUTO_COURSE_TERMS') == 'true',
                frozen_terms_count=len(frozen_terms),
                frozen_terms_sha256=runtime.fingerprint(frozen_terms),
                glossary_saved=bool(db.read_meta('auto_glossary:'+str(course)+':'+sub_id)),
                glossary_candidates=sum(g['stage'] == 'candidate' for g in stages),
                glossary_confirmed=sum(g['stage'] == 'confirmed' for g in stages),
                planned_shards=len(plan.get('shards', [])), planned_blocks=len(plan.get('blocks', [])),
                audio_seconds=plan.get('audio_seconds', spec.get('audio_seconds')), media_seconds=spec.get('media_seconds'),
                audio_diagnostics=spec.get('audio_diagnostics', {}), preparation_error_code=spec.get('error_code'),
                transcript_chars=len(row.get('transcript') or ''), summary_chars=len(row.get('summary') or ''),
                processed=bool(row.get('processed_at')), emailed=bool(row.get('emailed_at')),
                error_stage=row.get('error_stage'), error_count=row.get('error_count') or 0,
                review_seconds=review.get('seconds', 0), review_clips=len(review.get('attempts', [])),
                review_complete=bool(review.get('complete')), review_failed=bool(review.get('failed')),
                review_error_type=review.get('error_type'),
                fallback_configured=bool(config.DOUBAO_ASR_API_KEY),
                fallback_clips=sum(a['interval'].get('kind') == 'missing_asr' for a in review.get('attempts', [])),
                fallback_seconds=sum(a['seconds'] for a in review.get('attempts', []) if a['interval'].get('kind') == 'missing_asr'),
                fallback_completed_clips=sum(a['interval'].get('kind') == 'missing_asr' and a['status'] == 'complete' for a in review.get('attempts', [])),
                fallback_uncertain_clips=sum(a['interval'].get('kind') == 'missing_asr' and a['status'] == 'reserved' for a in review.get('attempts', [])),
                fallback_failed_clips=sum(a['interval'].get('kind') == 'missing_asr' and a['status'] == 'failed' for a in review.get('attempts', [])),
                homework_candidates=len(review.get('homework', {}).get('candidates', [])),
                homework_deferred=review.get('homework', {}).get('deferred_count', 0),
                homework_clips=sum(a['interval'].get('kind') == 'homework' for a in review.get('attempts', [])),
                homework_visual_status=review.get('homework', {}).get('visual', {}).get('status'),
                homework_visual_capture_status=review.get('homework', {}).get('visual', {}).get('capture_status'),
                homework_visual_reference_status=review.get('homework', {}).get('visual', {}).get('reference_status'),
                homework_vision_calls=len(review.get('homework', {}).get('vision_calls', [])),
                homework_vision_call_statuses=[c['status'] for c in review.get('homework', {}).get('vision_calls', [])],
                homework_vision_frame_count=sum(len(c.get('images', [])) for c in review.get('homework', {}).get('vision_calls', [])),
                homework_vision_image_count=sum(c.get('image_count', 0) for c in review.get('homework', {}).get('vision_calls', [])),
                missing_intervals=missing_intervals,
                asr_missing_seconds=coverage['missing_seconds'] if coverage else 0,
                asr_missing_limit_seconds=SHORT_GAP_LIMIT_SECONDS,
                asr_integrity_passed=bool(material and (material.get('recognition_coverage') or {}).get('accepted', material.get('complete'))),
                asr_gap_tolerated=bool(material and not material.get('complete') and (material.get('recognition_coverage') or {}).get('accepted')),
                asr_complete=bool(material and material.get('complete') and not missing_intervals))
            runtime.out('validation-result.json').write_bytes(runtime.shards.encoded(audit))
    db.checkpoint = checkpoint
    try:
        checkpoint()
        if spec['mode'] == 'failed': raise ValueError('Preparation failed')
        if spec['mode'] == 'sharded':
            from src.pipeline.prepared_lecture import assemble_material, validate_audio_duration
            plan = spec['plan']
            if plan.get('execution') == 'shared_queue':
                results = runtime.shared_results(plan, require_complete=False)
            else:
                results = []
                for shard in plan['shards']:
                    path = runtime.root()/'results'/f'qwen-production-asr-{slot}-{shard["shard_id"]}'/'worker-result.enc'
                    result = runtime.read_json(runtime.decode(path, f'result-{shard["shard_id"]}')['result.json'])
                    runtime.validate_result(plan, result, shard['shard_id'])
                    results.append(result)
            missing_intervals = [dict(gap,chunk_id=row['chunk_id'])
                for result in results for row in result['chunks']
                for gap in row.get('missing_intervals',[])]
            coverage = recognition_coverage([row for result in results for row in result['chunks']], allow_short_missing=True)
            if missing_intervals and not config.DOUBAO_ASR_API_KEY:
                print('[Doubao fallback] API unavailable; Qwen missing intervals retained.', flush=True)
                from src.ai.qwen_transcriber import IncompleteQwenRecognitionError
                if not coverage['accepted']:
                    raise IncompleteQwenRecognitionError(missing_intervals)
            if hashlib.sha256(files['lecture.flac']).hexdigest() != plan['audio_sha256']:
                raise ValueError('Prepared audio hash changed')
            flac = runtime.root()/'lecture.flac'; flac.write_bytes(files['lecture.flac'])
            raw = runtime.root()/'audio.raw'
            runtime.shards.command(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(flac), '-f', 'f32le',
                            '-ac', '1', '-ar', '16000', '-y', str(raw)], timeout=300)
            if abs(raw.stat().st_size/64000-plan['audio_seconds']) > .1:
                raise ValueError('Decoded finalization audio differs from the immutable plan')
            validate_audio_duration(plan['audio_seconds'], spec.get('media_seconds'))
            if missing_intervals and config.DOUBAO_ASR_API_KEY:
                from src.ai.qwen_missing_fallback import repair_missing
                results = repair_missing(plan, results, str(raw), review, checkpoint,
                                         api_key=config.DOUBAO_ASR_API_KEY)
                missing_intervals = [dict(gap,chunk_id=row['chunk_id'])
                    for result in results for row in result['chunks']
                    for gap in row.get('missing_intervals',[])]
                coverage = recognition_coverage([row for result in results for row in result['chunks']], allow_short_missing=True)
                if not coverage['accepted']:
                    from src.ai.qwen_transcriber import IncompleteQwenRecognitionError
                    raise IncompleteQwenRecognitionError(missing_intervals)
            material = assemble_material(plan, results, audio_path=str(raw), media_seconds=spec['media_seconds'], allow_short_missing=True)
            material['official_support'] = spec.get('official_support', [])
        row = db.get_lecture(sub_id)
        if spec['mode'] != 'finished' or (row.get('summary') and not row.get('deleted_at')):
            from src.pipeline.lecture_runner import LectureRunner
            from src.ai.qwen_transcriber import QwenTranscriber
            from src.ai.summarizer import Summarizer
            from src.runtime.reporter import Reporter
            from types import SimpleNamespace
            scheduler = SimpleNamespace(audio_downloader=SimpleNamespace(release=lambda *_: None))
            summarizer = None if row.get('summary') else Summarizer()
            runner = LectureRunner(None, db, scheduler, QwenTranscriber(), summarizer, Reporter())
            runner.run(course, spec['course_title'], lecture, prepared_asr=material,
                       prepared_ppt=True, review_state=review if material else None, checkpoint=checkpoint)
            row = db.get_lecture(sub_id)
            if not row.get('processed_at'):
                raise RuntimeError('LectureRunner retained a retryable processing failure')
        checkpoint()
        # A legitimate no-content recording may be terminal in production,
        # but it cannot validate ASR and summary integration. Retain its audit
        # and encrypted checkpoint while making the isolated trial fail.
        if lecture.get('_validation') and (
                not (row.get('transcript') or '').strip()
                or not (row.get('summary') or '').strip()):
            raise ValueError('Isolated validation produced no transcript or summary')
    except Exception as error:
        row = db.get_lecture(sub_id)
        # Phase-specific errors already committed by LectureRunner/prepare are
        # counted once. A missing shard or hard failure gets an explicit stage.
        already_counted = (restored_current and row.get('error_stage')) or spec['mode'] == 'failed'
        if not already_counted and (row.get('error_count') or 0) == initial_errors:
            db.update_error(sub_id, 'sharded_finalize', runtime.failure_code(error))
        checkpoint()
        raise
    finally:
        db.conn.close()
