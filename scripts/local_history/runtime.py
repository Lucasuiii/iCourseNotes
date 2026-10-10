"""Sequential local acquisition/ASR/finalization with durable block checkpoints."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from scripts.local_history.storage import file_hash, private_dir
from src.pipeline import history_refresh as policy
from src.pipeline.qwen_plan import (build_audio_plan, fingerprint, validate_plan,
                                    validate_block_row, incomplete_row)


class Paused(RuntimeError):
    pass


class PreflightBlocked(RuntimeError):
    code = 'active_campus_workflows'

    def __init__(self, runs):
        self.runs = sorted(set(runs))
        super().__init__('Campus workflows are active; wait until they finish before local acquisition')


def budget(deadline):
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise Paused('Local run reached its time budget; resume with run')
    return remaining


def choose_access_mode(requested):
    """Use a credential-free campus probe; never fall back after login starts."""
    if requested in ('direct','webvpn'):return requested
    if requested != 'auto':raise ValueError('Invalid local campus access mode')
    from src.api.webvpn import WebVPNSession
    probe=WebVPNSession(access_mode='direct')
    try:
        probe.probe_login_service()
        return 'direct'
    except Exception:
        return 'webvpn'
    finally:
        probe.session.close()


def check_actions(repository, *, allow_active=False):
    """Read active runs; an explicit per-run override permits concurrent login."""
    from scripts.production_db import command
    active = []
    for status in ('in_progress', 'queued', 'waiting', 'pending', 'requested'):
        pages = json.loads(command(['gh', 'api', '--paginate', '--slurp',
            f'repos/{repository}/actions/runs?status={status}&per_page=100']))
        for run in (run for payload in pages for run in payload['workflow_runs']):
            if run.get('path', '').split('@')[0] in {
                '.github/workflows/check.yml', '.github/workflows/single_run.yml',
                '.github/workflows/history_refresh.yml', '.github/workflows/parallel_pilot.yml',
                '.github/workflows/qwen_production_lecture.yml', '.github/workflows/qwen_production_resources.yml',
                '.github/workflows/qwen_production_stage.yml', '.github/workflows/qwen_production_validation.yml',
                '.github/workflows/qwen_sharded_course.yml', '.github/workflows/qwen_sharded_pilot.yml',
                '.github/workflows/qwen_homework_validation.yml', '.github/workflows/qwen_homework_visual_validation.yml',
                '.github/workflows/qwen_asr_benchmark.yml', '.github/workflows/doubao_asr_smoke.yml'}:
                active.append(str(run['id']))
    if active and not allow_active:
        raise PreflightBlocked(active)
    return sorted(set(active))


def transcribe_pending(transcriber, plan, audio, rows, save, deadline, *, allow_cloud_repair=False):
    """A saved complete block never invokes inference again; gaps are retried."""
    import numpy as np
    validate_plan(plan)
    known = {}
    for row in rows:
        index = row['chunk_id']
        if type(index) is not int or not 0 <= index < len(plan['blocks']) or index in known:
            raise ValueError('Invalid saved block identities')
        validate_block_row(plan['blocks'][index], row)
        if allow_cloud_repair or not incomplete_row(row):
            known[index] = row
    transcriber.set_terms(plan['recognition_terms'])
    transcriber.last_vad_windows = plan['vad_windows']
    try:
        with open(audio, 'rb') as stream:
            def load(block):
                stream.seek(round(block['start']*16000)*4)
                return np.frombuffer(stream.read(block['samples']*4), dtype='<f4').copy()
            for block in plan['blocks']:
                index = block['chunk_id']
                if index in known:
                    continue
                remaining = budget(deadline)
                result = transcriber.recognize_blocks([block], load, timeout=remaining, keep_model=True)[0]
                validate_block_row(block, result)
                known[index] = result
                save([known[i] for i in sorted(known)])
                if incomplete_row(result) and not allow_cloud_repair:
                    raise RuntimeError('Recognition has unresolved audio gaps; old data is preserved')
        return [known[i] for i in sorted(known)]
    finally:
        transcriber.release_model()


def candidate_files(store, target):
    tag = target['course_id']+'-'+target['sub_id']
    state = store.read(tag+'.enc')
    from src.runtime.audio_preparation import validate_prepared_audio
    from src.pipeline.prepared_lecture import assemble_material
    spec, review = state['spec'], state['review']
    validate_prepared_audio(spec)
    rows = state.get('final_rows', state.get('rows', []))
    results = [{ 'shard_id': s['shard_id'], 'plan_hash': fingerprint(spec['plan']),
                'complete': not any(incomplete_row(r) for r in rows if r['chunk_id'] in s['chunk_ids']),
                'chunks': [r for r in rows if r['chunk_id'] in s['chunk_ids']]}
               for s in spec['plan']['shards']]
    material = assemble_material(spec['plan'], results, media_seconds=spec.get('media_seconds'), allow_short_missing=True)
    if state.get('recognition_coverage') is not None and state['recognition_coverage'] != material['recognition_coverage']:
        raise ValueError('Candidate recognition coverage changed')
    if review.get('error_type') or review.get('material', {}).get('uncertain_calls'):
        raise ValueError('Candidate review has an unresolved failure')
    manifest = store.read('manifest.enc')
    if manifest.get('cloud_review'):
        if (review.get('complete') is not True or review.get('local_policy')
                or spec.get('review_runtime') != manifest.get('review_runtime')):
            raise ValueError('Candidate lacks the frozen full review pipeline')
    if spec.get('requested_audio_acquisition') != manifest.get('audio_acquisition'):
        raise ValueError('Candidate acquisition mode differs from the frozen plan')
    if (manifest['audio_acquisition'] == 'aac_auto' and
            spec['audio_diagnostics'].get('source_transport', {}).get('mode') not in ('aac_ranges', 'aac_mp4_fallback')):
        raise ValueError('Candidate lacks verified AAC acquisition or explicit format fallback evidence')
    if spec.get('plan', {}).get('local_backend') != manifest['backend']:
        raise ValueError('Candidate backend differs from the frozen local plan')
    if any(v.get('status') != 'complete' for v in review.get('homework', {}).get('vision_calls', [])):
        raise ValueError('Candidate vision review has an unresolved call')
    path = store.root/tag/'candidate.db'
    files = {'specification.json': policy.encoded(spec), 'review.json': policy.encoded(review)}
    fresh = policy.validate_candidate(path, files, target, spec['plan']['run_id'])
    import json
    metadata = json.loads(next((r['value'] for r in fresh['meta']
                               if r['key']=='qwen_pipeline:'+target['sub_id']), '{}'))
    recorded = metadata.get('recognition_coverage')
    if (recorded is not None and recorded != material['recognition_coverage']) or (
            not material['complete'] and recorded is None):
        raise ValueError('Candidate coverage differs from the retained ASR rows')
    if manifest.get('automatic_terms'):
        frozen = spec.get('glossary_snapshot', {})
        if (frozen.get('course_id') != target['course_id'] or frozen.get('sub_id') != target['sub_id']
                or frozen.get('lecture_date') != target['date']
                or frozen.get('history_revision') != manifest['baseline_revision']
                or frozen.get('terms') != spec['plan']['recognition_terms']
                or frozen.get('terms_sha256') != fingerprint(spec['plan']['recognition_terms'])):
            raise ValueError('Candidate glossary differs from its frozen historical evidence')
    return path, fresh


def process(store, manifest, target, deadline, *, allow_active_actions=False):
    from src.api.auth_recovery import initial_authenticated_session, fresh_media_session
    from src.api.icourse import ICourseClient
    from src.ai.course_glossary import course_terms
    from src.ai.summarizer import Summarizer
    from src.data.database import Database
    from src.pipeline.ppt_pipeline import PPTPipeline
    from src.pipeline.lecture_runner import LectureRunner
    from src.pipeline.prepared_lecture import assemble_material
    from src.runtime.audio_preparation import collect_decode_diagnostics, validate_prepared_audio
    from src.runtime.reporter import Reporter
    from src.runtime.scheduler import Scheduler
    from scripts.local_history.backends import MLXTranscriber
    course, sub = target['course_id'], target['sub_id']
    tag = course+'-'+sub
    root = private_dir(store.root/tag)
    state = store.read(tag+'.enc') if store.exists(tag+'.enc') else {'rows': [], 'review': {}}
    def save():
        store.save(tag+'.enc', state)
    def stage(value):
        state['stage'] = value; save()
    audio = root/'audio.raw'
    reporter = Reporter()
    scheduler = Scheduler(reporter)
    scheduler.audio_downloader.audio_mode = manifest['audio_acquisition']
    scheduler.audio_downloader.allow_fresh_session_escalation = True
    transcriber = MLXTranscriber(manifest['model_path'])
    db = Database(str(root/'candidate.db'))
    vpn = client = None
    try:
        # Campus check is intentionally deferred until actual work, never doctor/plan.
        budget(deadline)
        active = check_actions(manifest['repository'], allow_active=allow_active_actions)
        state['campus_preflight'] = {'allow_active_actions': allow_active_actions,
                                     'active_actions': active}
        save()
        if active and allow_active_actions:
            print('按本次授权继续本地登录；同时运行的 Actions：'+', '.join(active), flush=True)
        stage('authentication')
        mode=state.get('access_mode') or choose_access_mode(manifest.get('campus_access','auto'))
        state['access_mode']=mode;save()
        from src.api.webvpn import WebVPNSession
        from functools import partial
        vpn = initial_authenticated_session(factory=partial(WebVPNSession,access_mode=mode))
        client = ICourseClient(vpn, media_reauth_factory=partial(fresh_media_session, read_timeout=60,access_mode=mode))
        client._media_reauth_timeout = 120
        stage('source_selection')
        detail = client.get_course_detail(course)
        matches = [r for r in detail['lectures'] if str(r['sub_id']) == sub]
        if len(matches) != 1 or matches[0]['date'] != target['date']:
            raise ValueError('Frozen historical recording changed or is unavailable')
        lecture = dict(matches[0], sub_id=sub, _history_refresh=target)
        from src.runtime import config
        from src.runtime.session_rules import lecture_is_selected
        if not lecture_is_selected(course, lecture, config.COURSE_SESSION_RULES,
                config.COURSE_SESSION_OVERRIDE_DATES, exclusions=config.COURSE_SESSION_EXCLUSIONS):
            raise ValueError('Frozen recording is excluded by current session rules')
        db.upsert_course(course, detail['title'], detail.get('teacher', ''))
        db.insert_lecture(sub, course, lecture.get('sub_title', ''), target['date'])
        os.chmod(root/'candidate.db', 0o600)
        if manifest.get('automatic_terms') and 'glossary_snapshot' not in state:
            from src.ai.automatic_glossary import AutomaticGlossary
            from scripts.local_history.storage import atomic
            history_path = root/'glossary-history.db'
            atomic(history_path, store.read_bytes('baseline.db.enc'))
            history = Database(str(history_path))
            try:
                state['glossary_snapshot'] = AutomaticGlossary(history, course).freeze(
                    detail['title'], sub, lecture_date=target['date'])
                state['glossary_snapshot']['history_revision'] = manifest['baseline_revision']
                save()
            finally:
                history.conn.close()
                history_path.unlink(missing_ok=True)
        if 'spec' not in state:
            if shutil.disk_usage(root).free < 4*1024**3:
                raise RuntimeError('Less than 4 GiB free; acquisition paused before download')
            stage('acquisition')
            scheduler.audio_downloader.schedule(client, course, sub, preserve_timestamps=True)
            handle = scheduler.audio_downloader.get(sub, timeout=min(180, budget(deadline)))
            if handle is None:
                state['acquisition_failure'] = scheduler.audio_downloader.startup_failure(sub)
                save()
                raise RuntimeError('Audio acquisition did not start')
            last_size, changed = -1, time.monotonic()
            while handle.process.poll() is None:
                budget(deadline)
                size = Path(handle.path).stat().st_size if Path(handle.path).exists() else 0
                if size != last_size:
                    last_size, changed = size, time.monotonic()
                if time.monotonic()-changed > 300:
                    raise TimeoutError('Audio acquisition stopped making progress')
                time.sleep(.5)
            diagnostics = collect_decode_diagnostics(handle, retained=True)
            state['audio_diagnostics'] = diagnostics; save()
            spec = {'mode': 'sharded', 'course_id': course, 'course_title': detail['title'],
                    'campus_access':mode,
                    'requested_audio_acquisition': manifest['audio_acquisition'],
                    'lecture': lecture, 'audio_seconds': diagnostics['audio_seconds'],
                    'media_seconds': diagnostics['media_seconds'], 'audio_diagnostics': diagnostics,
                    'preparation_timing': {'stream_eof': True}}
            validate_prepared_audio(spec)
            shutil.copyfile(handle.path, audio); audio.chmod(0o600)
            scheduler.audio_downloader.release(sub)
            stage('vad')
            with open(audio, 'rb') as stream:
                chunks = transcriber.prepare_pcm_stream(stream.read, lambda: True, lambda: b'', lambda: 0,
                    audio_path=str(audio), timeout=budget(deadline))
            plan = build_audio_plan({'selection': {'course_id': course, 'sub_id': sub},
                    'audio_seconds': diagnostics['audio_seconds'], 'full_chunks': [dict(start=a, end=b) for a,b in chunks],
                    'recognition_terms': (state['glossary_snapshot']['terms'] if manifest.get('automatic_terms')
                                          else course_terms(detail['title'])[:30]), 'vad_windows': transcriber.last_vad_windows},
                    reference={'kind': 'local'}, course_slot=0, run_id=manifest['run_id'],
                    audio_sha256=file_hash(audio), production=True)
            # This is reference segmentation provenance, not a claim that converted
            # MLX weights equal the production Hugging Face revision.
            plan['local_backend'] = manifest['backend']
            spec['plan'] = plan
            if manifest.get('automatic_terms'):
                spec['glossary_snapshot'] = state['glossary_snapshot']
            state['spec'] = spec; save()
        spec = state['spec']; plan = spec['plan']
        spec['review_runtime'] = manifest.get('review_runtime')
        if spec.get('requested_audio_acquisition') != manifest['audio_acquisition']:
            raise ValueError('Saved audio acquisition mode differs from the frozen plan')
        if file_hash(audio) != plan['audio_sha256']:
            raise ValueError('Saved audio changed; resume refused')
        budget(deadline)
        prepared_figures = []
        if not state.get('ppt_complete'):
            stage('ppt')
            with db.conn:
                db.conn.execute("UPDATE ppt_pages SET ocr_status='pending' WHERE sub_id=? AND ocr_status='failed'", (sub,))
            ppt = PPTPipeline(db, scheduler, reporter)
            stats = ppt.submit(client, course, sub, defer_ocr=True).drain()
            prepared_figures = ppt.figure_frames(sub)
            if stats.failed or db.conn.execute("SELECT COUNT(*) FROM ppt_pages WHERE ocr_status IN ('failed','pending')").fetchone()[0]:
                raise RuntimeError('PPT material is incomplete; old notes preserved')
            state['ppt_complete'] = True; save()
        def save_rows(rows):
            state['rows'] = rows; save()
        stage('asr')
        if 'final_rows' not in state:
            state['rows'] = transcribe_pending(transcriber, plan, audio, state['rows'], save_rows, deadline,
                                              allow_cloud_repair=manifest.get('cloud_review',False))
        rows = state.get('final_rows',state['rows'])
        results = [{'shard_id': s['shard_id'], 'plan_hash': fingerprint(plan),
                    'complete': not any(incomplete_row(r) for r in rows if r['chunk_id'] in s['chunk_ids']),
                    'chunks': [r for r in rows if r['chunk_id'] in s['chunk_ids']]}
                   for s in plan['shards']]
        if any(incomplete_row(row) for row in rows) and 'final_rows' not in state:
            budget(deadline);stage('asr_fallback')
            from src.ai.qwen_missing_fallback import repair_missing
            results = repair_missing(plan,results,str(audio),state['review'],save,
                                     api_key=config.DOUBAO_ASR_API_KEY)
        state['final_rows'] = sorted([row for result in results for row in result['chunks']],
                                     key=lambda row:row['chunk_id']);save()
        material = assemble_material(plan, results, audio_path=str(audio), media_seconds=spec.get('media_seconds'),
                                     allow_short_missing=True)
        state['recognition_coverage'] = material['recognition_coverage'];save()
        if manifest.get('review_runtime'):
            material['alignment_model_path'] = manifest['review_runtime']['alignment_model_path']
        if 'official_support' not in spec:
            try:
                support = (client.get_transcript_segments(sub) or []) if config.USE_OFFICIAL_TRANSCRIPT else []
                spec['official_support'] = support if isinstance(support,list) and len(json.dumps(support,ensure_ascii=False))<=20000 else []
            except Exception:
                spec['official_support'] = []
            save()
        material['official_support'] = spec['official_support']
        review = state['review']
        failures = [a for a in review.get('attempts', []) if a['status'] != 'complete']
        retained_missing = (not material['complete'] and material['recognition_coverage']['accepted']
                            and failures and review.get('failed')
                            and all(a['status'] == 'failed' and a['interval'].get('kind') == 'missing_asr'
                                    for a in failures))
        if review.get('error_type') or ((review.get('failed') or failures) and not retained_missing):
            raise RuntimeError('Cloud review contains an uncertain/failed call; manual inspection required')
        budget(deadline)
        stage('summary')
        summarizer = Summarizer()
        runner = LectureRunner(client, db, scheduler, transcriber, summarizer, reporter)
        # Reuse the normal material quality, assignment evidence and summary prompt.
        if not runner.run(course, spec['course_title'], spec['lecture'], prepared_asr=material,
                review_state=review, checkpoint=save, prepared_ppt=True, prepared_figures=prepared_figures):
            raise RuntimeError('Lecture finalization did not produce a complete summary')
        if not manifest.get('cloud_review') and not config.DOUBAO_ASR_API_KEY and not review.get('error_type') and review.get('homework', {}).get('cloud_unavailable'):
            review.update(complete=True, local_policy='local_asr_without_cloud_rescue')
        row = db.get_lecture(sub)
        db.write_meta('qwen_pipeline:'+sub, json.dumps({'complete': True,
            'plan_hash': fingerprint(plan), 'audio_sha256': plan['audio_sha256'],
            'audio_seconds': plan['audio_seconds'], 'backend': manifest['backend'],
            'recognition_coverage': material['recognition_coverage'],
            'review': review, 'transcript_sha256': hashlib.sha256(row['transcript'].encode()).hexdigest()}, ensure_ascii=False))
        save()
        stage('candidate_validation')
        candidate_files(store, target)
        stage('complete')
    except Exception as error:
        state['last_error_type'] = type(error).__name__
        # Preserve code locations, never exception messages, URLs or locals.
        frames=[];trace=error.__traceback__;repo=Path(__file__).resolve().parents[2]
        while trace is not None:
            try:
                relative=Path(trace.tb_frame.f_code.co_filename).resolve().relative_to(repo)
                frames.append({'file':str(relative),'line':trace.tb_lineno,
                               'function':trace.tb_frame.f_code.co_name})
            except ValueError:pass
            trace=trace.tb_next
        state['last_error_frames']=frames[-6:]
        if isinstance(error,AttributeError) and isinstance(error.name,str) and error.name.isidentifier():
            state['missing_attribute']=error.name
        save()
        raise
    finally:
        transcriber.release_model()
        scheduler.shutdown()
        db.conn.close()
        if client is not None:
            client.close_media_session()
        if vpn is not None:
            vpn.session.close()
