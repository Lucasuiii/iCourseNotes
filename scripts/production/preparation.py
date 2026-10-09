"""Formal preparation stage, with explicit runtime services and frozen inputs."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone


def encode_audio_chunks(runtime, source, plan, files, destination):
    """Seek exact samples in retained FLAC, including VAD overlaps and gaps."""
    import soundfile as sf
    runtime.validate_plan(plan)
    with sf.SoundFile(str(source)) as audio:
        if audio.samplerate != 16000 or audio.channels != 1 or audio.subtype != 'PCM_24':
            raise ValueError('Retained FLAC format changed')
        for block in plan['blocks']:
            start = round(block['start']*16000)
            audio.seek(start)
            samples = audio.read(block['samples'], dtype='int32', always_2d=True)
            if len(samples) != block['samples']:
                raise ValueError('Retained audio has missing planned samples')
            chunk = Path(destination)/f"chunk-{block['chunk_id']}.flac"
            sf.write(str(chunk), samples, 16000, format='FLAC', subtype='PCM_24')
            files[chunk.name] = chunk.read_bytes()
            block['flac_sha256'] = hashlib.sha256(files[chunk.name]).hexdigest()
    runtime.validate_plan(plan)


def task_files(runtime):
    files = runtime.decode(runtime.root()/'inbox'/'queue.enc', 'queue')
    tasks = runtime.read_json(files['queue.json'])
    slot = int(os.environ['COURSE_SLOT'])
    if not 0 <= slot < len(tasks): raise ValueError('Invalid queue task slot')
    course, title, lecture = tasks[slot]
    (runtime.root()/'course.db').write_bytes(files['database.db'])
    return runtime.Database(str(runtime.root()/'course.db')), str(course), title, lecture


def read_preparation(runtime):
    return runtime.decode(runtime.root()/'inbox'/'prepared.enc', 'prepared')


def freeze_course_terms(runtime, db, course, title, sub_id):
    """See earlier published lessons even when the task queue was planned before them."""
    from src.ai.automatic_glossary import AutomaticGlossary
    if os.environ.get('PUBLISH_RESULTS') != 'true':
        return AutomaticGlossary(db, course).freeze(title, sub_id)
    history_path = runtime.root()/'glossary-history.db'
    revision = runtime.load_remote(history_path)
    if revision is None:
        return AutomaticGlossary(db, course).freeze(title, sub_id)
    history = runtime.Database(str(history_path))
    try:
        # The selected lesson may be new and absent from published history.
        # Its immutable queue date controls which older records are eligible.
        date = (db.get_lecture(sub_id) or {}).get('date')
        frozen = AutomaticGlossary(history, course).freeze(title, sub_id, lecture_date=date)
        frozen['history_revision'] = revision
        return frozen
    finally:
        history.conn.close()


def recover_preparation(runtime, db, course, sub_id):
    """Carry immutable audio and completed blocks into the next normal run."""
    raw = db.read_meta('qwen_pipeline:'+sub_id)
    metadata = json.loads(raw) if raw else {}
    recovery = metadata.get('recovery')
    if not recovery:
        return None
    old_run, old_slot = str(recovery['run_id']), int(recovery['task_slot'])
    target = runtime.root()/'recovery'
    runtime.artifact(f'qwen-production-prepare-{old_slot}', target, run=old_run, required=True)
    with runtime.shards.environment({'GITHUB_RUN_ID': old_run, 'COURSE_SLOT': str(old_slot)}):
        files = runtime.decode(target/'prepared.enc', 'prepared')
    spec = runtime.read_json(files['specification.json']); original = spec['plan']
    runtime.validate_plan(original)
    if (runtime.fingerprint(original) != recovery['plan_hash']
            or original['selection'] != {'course_id': course, 'sub_id': sub_id}):
        raise ValueError('Recovery belongs to another lecture or audio plan')
    plan = dict(original, run_id=os.environ['GITHUB_RUN_ID'], course_slot=int(os.environ['COURSE_SLOT']))
    spec.update(plan=plan, review=metadata.get('review', {}))
    saved_dir = target/'finalization'
    if runtime.artifact(f'qwen-production-state-{old_slot}', saved_dir, run=old_run):
        with runtime.shards.environment({'GITHUB_RUN_ID': old_run, 'COURSE_SLOT': str(old_slot)}):
            saved = runtime.decode(saved_dir/'state.enc', 'state')
        if saved['specification.json'] != files['specification.json']:
            raise ValueError('Recovered review state belongs to a different input')
        runtime.validate_checkpoint_age(saved, old_run, old_slot)
        spec['review'] = runtime.read_json(saved['review.json'])
    elif runtime.last_finalization_attempt(old_run, old_slot):
        raise ValueError('Prior finalization has no quota checkpoint; fresh review forbidden')
    for shard in original['shards']:
        shard_id = shard['shard_id']; destination = target/str(shard_id)
        result = None
        if runtime.artifact(f'qwen-production-asr-{old_slot}-{shard_id}', destination, run=old_run):
            with runtime.shards.environment({'GITHUB_RUN_ID': old_run, 'COURSE_SLOT': str(old_slot)}):
                result = runtime.read_json(runtime.decode(destination/'worker-result.enc', f'result-{shard_id}')['result.json'])
        elif f'completed-{shard_id}.json' in files:
            result = runtime.read_json(files[f'completed-{shard_id}.json'])
        if result is not None:
            runtime.validate_result(original, result, shard_id)
            result['plan_hash'] = runtime.fingerprint(plan)
            runtime.validate_result(plan, result, shard_id)
            files[f'completed-{shard_id}.json'] = runtime.shards.encoded(result)
    if original.get('execution') == 'shared_queue':
        from scripts.shared_asr_worker import store_for
        from src.pipeline.asr_queue import SharedQueue
        info = json.loads(subprocess.check_output(['gh', 'api',
            f'repos/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{old_run}'],
            stderr=subprocess.PIPE, timeout=60))
        if info['status'] != 'completed':
            raise ValueError('Cannot recover a shared queue while its source run is active')
        store = store_for(original)
        try:
            queue = SharedQueue(original, store)
            previous = runtime.shared_local_checkpoints(original)
            queue.restore_rows([row for rows in previous.values() for row in rows], int(info['run_attempt'])+1)
            results = queue.results(require_complete=False)
        finally:
            store.close()
        for result in results:
            result['plan_hash'] = runtime.fingerprint(plan)
            files[f'completed-{result["shard_id"]}.json'] = runtime.shards.encoded(result)
    files['database.db'] = runtime.lecture_snapshot(db, runtime.root()/'snapshot.db', course, sub_id)
    files['specification.json'] = runtime.shards.encoded(spec)
    return files


def retain_prepared_audio(runtime, handle, specification, files):
    """Keep actual samples and safe numeric evidence before any quality gate.

    A failed/timed-out decode remains a failure. Retention never pads to the
    video duration, retries the URL, or makes this audio eligible for ASR.
    """
    if handle.process.poll() is None:
        handle.process.terminate()
        try: handle.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            handle.process.kill(); handle.process.wait(timeout=5)
        specification['decode_interrupted'] = True
    pcm = Path(handle.path)
    diagnostics = runtime.collect_decode_diagnostics(handle,
        media_seconds=specification.get('media_seconds'),
        interrupted=bool(specification.get('decode_interrupted')), retained='lecture.flac' in files)
    size = diagnostics['pcm_bytes']
    specification.update(audio_seconds=diagnostics['audio_seconds'], media_seconds=diagnostics['media_seconds'])
    specification['audio_diagnostics'] = diagnostics
    if not size or size % 4:
        return
    flac = runtime.root()/'lecture.flac'
    if 'lecture.flac' not in files:
        runtime.shards.command(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'f32le', '-ar', '16000', '-ac', '1',
                        '-i', str(pcm), '-c:a', 'flac', '-y', str(flac)], timeout=300)
        files['lecture.flac'] = flac.read_bytes()
    diagnostics.update(audio_retained=True, audio_sha256=hashlib.sha256(files['lecture.flac']).hexdigest())


def prepare_audio_stream(runtime, transcriber, handle, specification):
    """Allow progressing downloads to finish, within a bounded prepare job."""
    try:
        with open(handle.path, 'rb') as audio:
            return transcriber.prepare_pcm_stream(audio.read, lambda: handle.process.poll() is not None,
                lambda: b''.join(handle.stderr_chunks), lambda: handle.process.returncode,
                audio_path=handle.path, timeout=runtime.PREPARE_STREAM_TIMEOUT, idle_timeout=runtime.PREPARE_IDLE_TIMEOUT)
    finally:
        specification['preparation_timing'] = {'timeout_seconds': runtime.PREPARE_STREAM_TIMEOUT,
            'idle_timeout_seconds': runtime.PREPARE_IDLE_TIMEOUT}
        stats = getattr(transcriber, 'last_prepare_stats', None)
        if isinstance(stats, dict):
            specification['preparation_timing'].update(stats)


def preparation_failure_audit(runtime, specification, files, error, *, saved=False, secondary=(), fallback=False,
                              full_counts=None):
    """Public diagnostics contain fixed codes/counts, never exception text or names."""
    from scripts import production_prepared_bundle as bundle
    phase = specification.get('prepare_phase', 'restore_or_task_load')
    phases = {'restore_or_task_load', 'imports', 'recovery', 'login', 'ppt', 'glossary',
              'audio_download', 'vad', 'audio_validation', 'chunk_encoding', 'ppt_drain',
              'snapshot', 'initial_publish', 'queue_initialize', 'checkpoint_write', 'outputs'}
    diagnostics = specification.get('audio_diagnostics', {})
    from src.runtime.audio_preparation import safe_transport_diagnostics, ERROR_PATTERNS
    audit = {'phase': phase if phase in phases else 'unknown',
        'error_type': type(error).__name__, 'error_code': runtime.failure_code(error),
        'secondary_error_types': [type(e).__name__ for e in secondary],
        'checkpoint_saved': saved, 'fallback_audio_only': fallback,
        'audio_retained': saved and 'lecture.flac' in files,
        'file_count': len(files), 'content_bytes': sum(len(v) for v in files.values()),
        'total_size_limit_bytes': bundle.MAX_BYTES, 'part_size_limit_bytes': bundle.PART_BYTES,
        'planned_blocks': len(specification.get('plan', {}).get('blocks', [])),
        'planned_workers': len(specification.get('plan', {}).get('shards', []))}
    if specification.get('audio_startup_diagnostics'):
        startup = dict(specification['audio_startup_diagnostics'])
        if 'source_transport' in startup:
            startup['source_transport'] = safe_transport_diagnostics(startup['source_transport'])
        audit['audio_startup_diagnostics'] = startup
    if phase == 'login':
        from src.api.webvpn import authentication_failure
        audit['authentication'] = authentication_failure(error)
        audit['error_code'] = audit['authentication']['failure']
    if full_counts is not None:
        audit.update(full_checkpoint_file_count=full_counts[0], full_checkpoint_content_bytes=full_counts[1])
    timing = specification.get('preparation_timing', {})
    for name in ('elapsed_seconds', 'idle_seconds', 'timeout_seconds', 'idle_timeout_seconds'):
        value = timing.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            import math
            if math.isfinite(value) and value >= 0: audit['preparation_' + name] = value
    if isinstance(timing.get('stream_eof'), bool):
        audit['preparation_stream_eof'] = timing['stream_eof']
    for name in ('audio_seconds', 'media_seconds', 'duration_gap_seconds', 'decode_return_code',
                 'decode_interrupted', 'stderr_complete', 'timeline_preserved'):
        value = diagnostics.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            import math
            if math.isfinite(value): audit[name] = value
        elif isinstance(value, bool): audit[name] = value
    transport = safe_transport_diagnostics(diagnostics.get('source_transport'))
    if transport: audit['source_transport'] = transport
    counts = diagnostics.get('decode_error_counts')
    if type(counts) is dict:
        audit['decode_error_counts'] = {code:count for code, count in counts.items()
            if code in {*ERROR_PATTERNS, 'stderr_read_error'} and type(count) is int and 0 < count <= 1_000_000}
    bundle.atomic_write(runtime.out('prepare-failure.json'), runtime.shards.encoded(audit))


def preserve_preparation_failure(runtime, db, sub_id, specification, files, handle, error):
    """A second retention failure must not mask the original preparation error."""
    secondary = []
    try:
        runtime.preparation_failure_audit(specification, files, error)
    except Exception as failure:
        secondary.append(failure)
    try:
        db.update_error(sub_id, 'prepare', type(error).__name__)
    except Exception as failure:
        secondary.append(failure)
    if handle is not None:
        try:
            runtime.retain_prepared_audio(handle, specification, files)
        except Exception as failure:
            secondary.append(failure)
            specification['audio_retention_error_type'] = type(failure).__name__
    specification.update(prepare_error_type=type(error).__name__, prepare_error_code=runtime.failure_code(error))
    if specification.get('mode') != 'sharded' or 'lecture.flac' not in files:
        specification.update(mode='failed', error_type=type(error).__name__, error_code=runtime.failure_code(error))
    try:
        files['database.db'] = runtime.lecture_snapshot(db, runtime.root()/'snapshot.db', specification['course_id'], sub_id)
    except Exception as failure:
        secondary.append(failure)
    files['specification.json'] = runtime.shards.encoded(specification)
    full_counts = (len(files), sum(len(v) for v in files.values()))
    runtime.preparation_failure_audit(specification, files, error, secondary=secondary, full_counts=full_counts)
    fallback = False
    try:
        runtime.encode(files, 'prepared', runtime.out('prepared.enc'))
    except Exception as failure:
        secondary.append(failure)
        # Blocks duplicate the retained source. Keep the source and its original
        # plan for diagnosis, but never mark an audio-only fallback ASR-ready.
        fallback = True
        files = {name: value for name, value in files.items() if not name.startswith('chunk-')}
        from scripts.production_prepared_bundle import MAX_BYTES
        if sum(len(v) for v in files.values()) > MAX_BYTES:
            files.pop('lecture.flac', None)
        specification.update(mode='failed', error_type=type(error).__name__,
            error_code=runtime.failure_code(error), checkpoint_fallback='audio_only',
            recovery_blocked=True)
        specification.setdefault('audio_diagnostics', {})['audio_retained'] = 'lecture.flac' in files
        files['specification.json'] = runtime.shards.encoded(specification)
        try:
            runtime.encode(files, 'prepared', runtime.out('prepared.enc'))
        except Exception as final_failure:
            secondary.append(final_failure)
            runtime.preparation_failure_audit(specification, files, error, secondary=secondary, fallback=True,
                                      full_counts=full_counts)
            return
    runtime.preparation_failure_audit(specification, files, error, saved=True, secondary=secondary, fallback=fallback,
                              full_counts=full_counts)


def prepare(runtime):
    slot = int(os.environ['COURSE_SLOT'])
    if runtime.artifact(f'qwen-production-prepare-{slot}', runtime.root()/'previous'):
        files = runtime.decode(runtime.root()/'previous'/'prepared.enc', 'prepared')
        specification = runtime.read_json(files['specification.json'])
        if specification['mode'] == 'failed' and 'lecture.flac' in files:
            # Never replace retained failed input with a silently fresh fetch.
            runtime.encode(files, 'prepared', runtime.out('prepared.enc'))
            if specification.get('error_code') == 'incomplete_audio':
                raise ValueError('Production audio is incomplete')
            raise ValueError('Retained preparation failed; explicit repair required')
        if specification['mode'] != 'failed':
            if specification.get('plan', {}).get('execution') == 'shared_queue':
                from scripts.shared_asr_worker import initialize
                initialize(specification['plan'], files)
            runtime.encode(files, 'prepared', runtime.out('prepared.enc'))
            runtime.write_outputs(workers={'shard_id': list(range(len(specification.get('plan', {}).get('shards', [])))) or [-1]})
            return
    db, course, title, lecture = runtime.task_files()
    sub_id = str(lecture['sub_id'])
    scheduler = None
    handle = None
    specification = {'course_id': course, 'course_title': title, 'lecture': lecture, 'prepare_phase': 'imports'}
    files = {}
    try:
        from src.pipeline.prepared_lecture import cached_material
        from src.ai.qwen_transcriber import QwenTranscriber
        from src.ai.course_glossary import course_terms
        from main import login_with_retry
        from src.api.icourse import ICourseClient
        from src.runtime.scheduler import Scheduler
        from src.runtime.reporter import Reporter
        from src.pipeline.ppt_pipeline import PPTPipeline
        existing = db.get_lecture(sub_id)
        if existing.get('deleted_at') or existing.get('summary'):
            specification['mode'] = 'finished'
        else:
            if not existing.get('transcript'):
                specification['prepare_phase'] = 'recovery'
                recovered = runtime.recover_preparation(db, course, sub_id)
                if recovered:
                    recovered_plan = runtime.read_json(recovered['specification.json'])['plan']
                    if recovered_plan.get('execution') == 'shared_queue':
                        from scripts.shared_asr_worker import initialize
                        initialize(recovered_plan, recovered)
                    runtime.encode(recovered, 'prepared', runtime.out('prepared.enc'))
                    plan = runtime.read_json(recovered['specification.json'])['plan']
                    runtime.write_outputs(workers={'shard_id': list(range(len(plan['shards']))) or [-1]})
                    return
            # Audio retrieval uses the production downloader and its unchanged
            # authenticated playback fallback chain; no benchmark acquisition cap.
            specification['prepare_phase'] = 'login'
            from src.api.auth_recovery import fresh_media_session
            reporter = Reporter(); client = ICourseClient(login_with_retry(), media_reauth_factory=fresh_media_session)
            scheduler = Scheduler(reporter)
            specification['prepare_phase'] = 'ppt'
            ppt = PPTPipeline(db, scheduler, reporter).submit(client, course, sub_id, defer_ocr=True)
            from src.runtime import config
            if config.USE_OFFICIAL_TRANSCRIPT:
                try:
                    support = client.get_transcript_segments(sub_id) or []
                    # Auxiliary evidence is bounded and never replaces Qwen text.
                    specification['official_support'] = support if len(json.dumps(support, ensure_ascii=False)) <= 20000 else []
                except Exception:
                    specification['official_support'] = []
            cached = cached_material(db, existing)
            if existing.get('transcript'):
                specification['mode'] = 'cached'
                if cached:
                    material, review = cached
                    # Audio has already been released after the saved transcript.
                    # Keep used quota, completed variants and unresolved calls.
                    review.setdefault('material', {})
                    if not review.get('complete'):
                        review['material']['remaining_review_unavailable_without_audio'] = True
                        review['complete'] = True
                    specification.update(material=material, review=review)
            else:
                terms = course_terms(title)
                specification['prepare_phase'] = 'glossary'
                if os.environ.get('AUTO_COURSE_TERMS') == 'true':
                    glossary_snapshot = lecture.get('_frozen_glossary') or runtime.freeze_course_terms(db, course, title, sub_id)
                    runtime.validate_frozen_terms(glossary_snapshot, course, lecture)
                    terms = glossary_snapshot['terms']
                    specification['glossary_snapshot'] = glossary_snapshot
                specification['prepare_phase'] = 'audio_download'
                scheduler.audio_downloader.schedule(client, course, sub_id, preserve_timestamps=True)
                handle = scheduler.audio_downloader.get(sub_id, timeout=180)
                if handle is None:
                    specification['audio_startup_diagnostics'] = scheduler.audio_downloader.startup_failure(sub_id)
                    raise ValueError('No playable production audio')
                began = time.monotonic()
                while not Path(handle.path).exists():
                    if handle.process.poll() is not None or time.monotonic()-began > 60:
                        raise ValueError('Decoded audio did not become available')
                    time.sleep(.1)
                transcriber = QwenTranscriber()
                specification['prepare_phase'] = 'vad'
                began_vad = time.monotonic()
                windows = runtime.prepare_audio_stream(transcriber, handle, specification)
                specification.setdefault('prepare_timings', {})['audio_and_vad_seconds'] = time.monotonic()-began_vad
                duration = transcriber.last_audio_duration
                media = transcriber.last_media_duration
                specification.update(audio_seconds=duration, media_seconds=media,
                    vad_windows=transcriber.last_vad_windows, full_chunks=[{'start':a,'end':b} for a,b in windows])
                specification['prepare_phase'] = 'audio_validation'
                runtime.retain_prepared_audio(handle, specification, files)
                if abs(specification['audio_seconds']-duration) > 1/16000:
                    raise ValueError('VAD input differs from retained PCM samples')
                runtime.validate_prepared_audio(specification)
                media = specification.get('media_seconds')
                flac = runtime.root()/'lecture.flac'
                digest = specification['audio_diagnostics']['audio_sha256']
                plan = runtime.build_audio_plan({'selection': {'course_id': course, 'sub_id': sub_id},
                    'audio_seconds': duration, 'full_chunks': [{'start': a, 'end': b} for a,b in windows],
                    'vad_windows': transcriber.last_vad_windows, 'recognition_terms': terms},
                    reference={'pipeline': 'production'}, course_slot=slot, run_id=os.environ['GITHUB_RUN_ID'],
                    audio_sha256=digest, mode=os.environ.get('SHARD_MODE', '2'), production=True,
                    worker_cap=6 if os.environ.get('POOL_ARTIFACTS') == 'true' else 3,
                    cost_rtf=runtime.historical_asr_cost(db, course, lecture.get('date', '')))
                specification['prepare_phase'] = 'chunk_encoding'
                began_chunks = time.monotonic()
                runtime.encode_audio_chunks(flac, plan, files, runtime.root())
                specification.setdefault('prepare_timings', {})['chunk_encoding_seconds'] = time.monotonic()-began_chunks
                specification.update(mode='sharded', plan=plan, media_seconds=media)
            specification['prepare_phase'] = 'ppt_drain'
            ppt.drain()
        specification['prepare_phase'] = 'snapshot'
        files.update({'specification.json': runtime.shards.encoded(specification),
                      'database.db': runtime.lecture_snapshot(db, runtime.root()/'snapshot.db', course, sub_id)})
        if specification['mode'] == 'sharded' and os.environ.get('PUBLISH_RESULTS') == 'true':
            specification['prepare_phase'] = 'initial_publish'
            db.write_meta('qwen_pipeline:'+sub_id, json.dumps({'review': {}, 'recovery': {
                'run_id': os.environ['GITHUB_RUN_ID'], 'task_slot': slot,
                'plan_hash': runtime.fingerprint(specification['plan'])},
                'updated_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
            delta = runtime.root()/'initial-state.db'
            files['database.db'] = runtime.lecture_snapshot(db, delta, course, sub_id)
            # Persist the recovery identity before any worker or cloud review
            # starts. A lost job cannot look like a brand-new lecture next run.
            runtime.publish(delta, course, sub_id)
        if specification.get('plan', {}).get('execution') == 'shared_queue':
            specification['prepare_phase'] = 'queue_initialize'
            from scripts.shared_asr_worker import initialize
            initialize(specification['plan'], files)
        specification['prepare_phase'] = 'checkpoint_write'
        files['specification.json'] = runtime.shards.encoded(specification)
        began_bundle = time.monotonic()
        runtime.encode(files, 'prepared', runtime.out('prepared.enc'))
        runtime.out('prepare-timing.json').write_bytes(runtime.shards.encoded({**specification.get('prepare_timings', {}),
            'checkpoint_seconds': time.monotonic()-began_bundle}))
        specification['prepare_phase'] = 'outputs'
        runtime.write_outputs(workers={'shard_id': list(range(len(specification.get('plan', {}).get('shards', [])))) or [-1]})
    except Exception as error:
        try:
            runtime.preserve_preparation_failure(db, sub_id, specification, files, handle, error)
        except Exception:
            # Disk exhaustion can prevent even tiny diagnostic writes. Still
            # propagate the primary error, never a checkpoint-write substitute.
            pass
        raise
    finally:
        if scheduler: scheduler.shutdown()
        db.conn.close()
