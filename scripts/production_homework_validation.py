"""One bounded tail review of completed audio; no fresh Qwen or publication."""
from __future__ import annotations
import base64
import copy
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

from scripts import production_qwen as pipeline
from scripts import sharded_qwen_pilot as shards
from scripts.production_result_export import encrypt, identity
from src.ai.qwen_review_ledger import review_prepared, validate_ledger
from src.pipeline.prepared_lecture import assemble_material

SECONDS = 600


def validation_config():
    """Load the exact historical validation scope from private configuration."""
    from pathlib import Path
    raw = os.environ.get('HOMEWORK_VALIDATION_CONFIG')
    if raw is None:
        path = Path(__file__).resolve().parents[1]/'.local-history-refresh/homework_validation.json'
        if path.is_file():
            raw = path.read_text()
    try:
        value = json.loads(raw or '')
        required = ('source_run_id', 'prior_run_id', 'course_id', 'date', 'audio_hash',
                    'plan_hash', 'quote_hash', 'interval', 'prior_clips', 'prior_seconds')
        if not isinstance(value, dict) or any(key not in value for key in required):
            raise ValueError('Invalid validation scope')
        return value
    except (ValueError, TypeError):
        raise ValueError('Private historical validation scope unavailable') from None


def scoped_material(original):
    end = original['audio_seconds']; start = max(0, end-SECONDS)
    # A boundary block has no word times. Exclude it rather than align its
    # full text against a clipped waveform or import speech before the scope.
    chunks = [dict(c) for c in original['full_chunks'] if c['start'] >= start]
    if not chunks: raise ValueError('No original complete blocks in tail')
    data = dict(original, full_chunks=chunks, transcript='\n'.join(c['text'] for c in chunks),
                segments=[{'start_ms': round(c['start']*1000), 'end_ms': round(c['end']*1000), 'text': c['text']}
                          for c in chunks], weak_windows=[])
    return data, {'start': start, 'end': end, 'boundary_block_excluded': True}


def isolated_ledger(original):
    validate_ledger(original)
    if not original.get('complete') or original.get('failed') or original.get('error_type'):
        raise ValueError('Original classroom review is not safely complete')
    state = copy.deepcopy(original)
    state.pop('complete', None); state.pop('material', None)
    state.pop('homework', None)
    state.update(intervals=[], weak_intervals=[], unresolved=[])
    # All reservations and their original timestamps remain charged; only the
    # new assignment stage is enabled. No generic reselection or weak rescue.
    return state


def acceptance(state, scope, baseline_count, summary, raw_saved):
    homework = state.get('material', {}).get('homework', {})
    fresh = state.get('attempts', [])[baseline_count:]
    visual = homework.get('visual', {})
    checks = {
        'assignment_detected': bool(homework.get('candidates')),
        'focus_located': bool(homework.get('intervals')) and not homework.get('unresolved'),
        'focus_inside_tail': bool(fresh) and all(scope['start']*1000 <= a['interval']['start_ms']
                                               < a['interval']['end_ms'] <= scope['end']*1000 for a in fresh),
        'cloud_completed_nonempty': bool(fresh) and all(a['status'] == 'complete' and a.get('segments') for a in fresh),
        'only_assignment_calls': bool(fresh) and all(a['interval'].get('kind') == 'homework' for a in fresh),
        'review_complete': state.get('complete') is True and not state.get('failed') and not state.get('error_type'),
        'visual_completed': visual.get('capture_status') == 'complete' and visual.get('reference_status') == 'supported',
        'summary_has_assignment_section': '作业与课务提醒' in summary,
        'raw_transcript_preserved': raw_saved,
    }
    validate_ledger(state)
    return checks


def run():
    profile = validation_config()
    if (os.environ.get('SOURCE_RUN_ID') != profile['source_run_id'] or os.environ.get('SOURCE_SLOT') != '0'
            or os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1'):
        raise ValueError('Only the authorized classroom and first attempt are allowed')
    run = os.environ['SOURCE_RUN_ID']; slot = 0
    identity(run, slot)
    recipient = base64.b64decode(os.environ['RECIPIENT_PUBLIC_KEY'], validate=True)
    if len(recipient) != 32: raise ValueError('Invalid recipient')
    info = json.loads(subprocess.check_output(['gh', 'api',
        f'repos/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{run}'], stderr=subprocess.PIPE, timeout=60))
    if (info['status'] != 'completed' or info['conclusion'] != 'success'
            or info['path'].split('@')[0] != '.github/workflows/parallel_pilot.yml'):
        raise ValueError('Source classroom is not complete')
    target = pipeline.root()/'homework-source'
    pipeline.artifact('qwen-production-prepare-0', target/'prepare', run=run, required=True)
    pipeline.artifact('qwen-production-state-0', target/'state', run=run, required=True)
    with shards.environment({'GITHUB_RUN_ID': run, 'COURSE_SLOT': '0'}):
        prepared = pipeline.decode(target/'prepare'/'prepared.enc', 'prepared')
        source = pipeline.decode(target/'state'/'state.enc', 'state')
    spec = json.loads(prepared['specification.json'])
    if (spec['course_id'] != profile['course_id'] or spec['lecture'].get('date') != profile['date']
            or spec != json.loads(source['specification.json'])):
        raise ValueError('Prepared classroom identity changed')
    results = []
    for shard in spec['plan']['shards']:
        n = shard['shard_id']; path = target/f'asr-{n}'
        pipeline.artifact(f'qwen-production-asr-0-{n}', path, run=run, required=True)
        with shards.environment({'GITHUB_RUN_ID': run, 'COURSE_SLOT': '0'}):
            results.append(json.loads(pipeline.decode(path/'worker-result.enc', f'result-{n}')['result.json']))
    original = assemble_material(spec['plan'], results, media_seconds=spec['media_seconds'])
    flac = prepared['lecture.flac']
    if hashlib.sha256(flac).hexdigest() != original['audio_sha256']:
        raise ValueError('Original audio checksum changed')
    audio = pipeline.root()/'source.flac'; audio.write_bytes(flac)
    raw = pipeline.root()/'source.raw'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(audio), '-f', 'f32le',
                    '-ar', '16000', '-ac', '1', '-y', str(raw)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=300)
    if abs(raw.stat().st_size/64000-original['audio_seconds']) > .1:
        raise ValueError('Decoded source timeline changed')
    original['audio_path'] = str(raw)
    data, scope = scoped_material(original)
    prior = json.loads(source['review.json']); baseline_count = len(prior.get('attempts', []))
    state = isolated_ledger(prior)
    payload = {'source_run_id': run, 'scope': scope, 'audio_sha256': original['audio_sha256'],
               'plan_hash': original['plan_hash'], 'baseline_review_seconds': prior.get('seconds', 0),
               'baseline_review_clips': baseline_count, 'asr_decoded_blocks': 0,
               'source_block_ids': [c['chunk_id'] for c in data['full_chunks']], 'review': state,
               'publish_results': False, 'send_email': False}

    def checkpoint():
        blob = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        pipeline.out('homework-result.enc').write_bytes(encrypt(blob, recipient, run, slot))

    checkpoint()
    from src.pipeline.lecture_runner import LectureRunner
    from src.ai.summarizer import Summarizer
    from src.runtime.reporter import Reporter
    from src.data.database import Database
    db = Database(str(pipeline.root()/'isolated-homework.db'))
    lecture = spec['lecture']; sub_id = str(lecture['sub_id'])
    db.upsert_course(profile['course_id'], spec['course_title'], '')
    db.insert_lecture(sub_id, profile['course_id'], lecture.get('sub_title', ''), lecture['date'])
    db.update_transcript(sub_id, data['transcript'])
    runner = LectureRunner(None, db, SimpleNamespace(),
                           SimpleNamespace(last_chunks=data['full_chunks']), Summarizer(), Reporter())
    runner._homework_course_id = profile['course_id']; runner._homework_sub_id = sub_id
    runner._prepared_asr = data
    runner._historical_terms = data['recognition_terms']
    try:
        reviewed = review_prepared(data, [], runner._summarizer, state, checkpoint,
                                   homework_ocr=runner._homework_visual)
        # Historical reservations still count, but their classroom text must
        # not be sent to a summary advertised as the last ten minutes.
        runner._qwen_review_material = {'homework': reviewed.get('homework', {})}
        title = spec['course_title']+'（录播最后10分钟验证，仅总结所提供片段）'
        summary = runner._summarize(sub_id, title, data['transcript'], data['segments'])
        payload.update(summary=summary, summary_model=db.get_lecture(sub_id)['summary_model'])
        checks = acceptance(state, scope, baseline_count, summary,
                            db.get_lecture(sub_id)['transcript'] == data['transcript'])
        payload['checks'] = checks
        checkpoint()
        # Return only the focused clips for local listening, never the full
        # recording or transcript. All classroom content remains encrypted.
        from src.ai.doubao_asr import _encode_chunk
        payload['clips'] = [{'start_ms': a['interval']['start_ms'], 'end_ms': a['interval']['end_ms'],
                             'mp3_base64': base64.b64encode(_encode_chunk(str(raw), a['interval']['start_ms']/1000,
                                 (a['interval']['end_ms']-a['interval']['start_ms'])/1000)).decode()}
                            for a in state['attempts'][baseline_count:]]
        checkpoint()
        audit = dict(scope, mode='tail_homework_validation', date=lecture['date'],
                     raw_blocks=len(data['full_chunks']), candidates=len(state.get('homework', {}).get('candidates', [])),
                     new_review_clips=len(state['attempts'])-baseline_count,
                     new_review_seconds=state.get('seconds', 0)-prior.get('seconds', 0),
                     total_review_clips=len(state['attempts']), total_review_seconds=state.get('seconds', 0),
                     visual_status=state.get('homework', {}).get('visual', {}).get('status'),
                     visual_capture_status=state.get('homework', {}).get('visual', {}).get('capture_status'),
                     visual_reference_status=state.get('homework', {}).get('visual', {}).get('reference_status'),
                     ocr_frames=len(state.get('homework', {}).get('visual', {}).get('frames', [])),
                     summary_chars=len(summary), checks=checks, passed=all(checks.values()),
                     asr_decoded_blocks=0, publish_results=False, send_email=False)
        pipeline.out('homework-audit.json').write_text(json.dumps(audit, ensure_ascii=False))
        if not all(checks.values()): raise ValueError('Tail homework acceptance failed')
    finally:
        checkpoint(); db.conn.close()


if __name__ == '__main__':
    os.environ['QWEN_PRODUCTION_TASK'] = 'true'
    os.environ['COURSE_SLOT'] = '0'
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            run()
        print('Tail homework review completed; reused original ASR/audio, no publication or email')
    except Exception as error:
        print(f'Tail homework validation failed ({type(error).__name__}); private details withheld')
        sys.exit(1)
