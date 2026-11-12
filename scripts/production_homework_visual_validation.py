"""Replay one authorized board window; no audio fetch, decoding or cloud calls."""
import base64
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
import subprocess
import sys

from scripts import production_qwen as pipeline
from scripts import sharded_qwen_pilot as shards
from scripts.production_homework_validation import scoped_material, validation_config
from scripts.production_result_export import encrypt
from src.ai.homework_review import assignment_candidates, prioritize_candidates
from src.pipeline.prepared_lecture import assemble_material



def replay_selection(original, profile=None):
    profile = validation_config() if profile is None else profile
    if original['audio_sha256'] != profile['audio_hash'] or original['plan_hash'] != profile['plan_hash']:
        raise ValueError('Authorized source identity changed')
    data, scope = scoped_material(original)
    candidates = prioritize_candidates(assignment_candidates(data['full_chunks']))
    if (len(candidates) != 1 or candidates[0]['id'] != profile['interval']['chunk_id'] or
            hashlib.sha256(candidates[0]['quote'].encode()).hexdigest() != profile['quote_hash']):
        raise ValueError('Recorded alignment no longer matches the original quote')
    # Exact original forced alignment from the successful first tail run.
    interval = dict(profile['interval'], text=candidates[0]['quote'])
    return data, scope, candidates, [interval]


def require_source(run, workflow):
    repo = os.environ['GITHUB_REPOSITORY']
    info = json.loads(subprocess.check_output(['gh', 'api', f'repos/{repo}/actions/runs/{run}'],
                                            stderr=subprocess.PIPE, timeout=60))
    if info['status'] != 'completed' or info['conclusion'] != 'success' or info['path'].split('@')[0] != workflow:
        raise ValueError('Required successful source unavailable')


def run():
    profile = validation_config()
    if os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1':
        raise ValueError('Only one authorized replay attempt is allowed')
    recipient = base64.b64decode(os.environ['RECIPIENT_PUBLIC_KEY'], validate=True)
    if len(recipient) != 32: raise ValueError('Invalid recipient')
    require_source(profile['source_run_id'], '.github/workflows/parallel_pilot.yml')
    require_source(profile['prior_run_id'], '.github/workflows/qwen_homework_validation.yml')
    target = pipeline.root()/'visual-source'
    pipeline.artifact('qwen-homework-tail-audit', target/'prior-audit', run=profile['prior_run_id'], required=True)
    prior = json.loads((target/'prior-audit'/'homework-audit.json').read_text())
    if (prior.get('passed') is not True or prior['total_review_clips'] != profile['prior_clips']
            or abs(prior['total_review_seconds']-profile['prior_seconds']) > 1e-6
            or prior.get('publish_results') or prior.get('send_email')):
        raise ValueError('Prior completed review usage is unavailable')
    pipeline.artifact('qwen-production-state-0', target/'state', run=profile['source_run_id'], required=True)
    with shards.environment({'GITHUB_RUN_ID': profile['source_run_id'], 'COURSE_SLOT': '0'}):
        source = pipeline.decode(target/'state'/'state.enc', 'state')
    spec = json.loads(source['specification.json'])
    if spec['course_id'] != profile['course_id'] or spec['lecture']['date'] != profile['date']:
        raise ValueError('Wrong classroom')
    results = []
    for shard in spec['plan']['shards']:
        n = shard['shard_id']; path = target/f'asr-{n}'
        pipeline.artifact(f'qwen-production-asr-0-{n}', path, run=profile['source_run_id'], required=True)
        with shards.environment({'GITHUB_RUN_ID': profile['source_run_id'], 'COURSE_SLOT': '0'}):
            results.append(json.loads(pipeline.decode(path/'worker-result.enc', f'result-{n}')['result.json']))
    original = assemble_material(spec['plan'], results, media_seconds=spec['media_seconds'])
    data, scope, candidates, intervals = replay_selection(original, profile)
    payload = {'source_run_id': profile['source_run_id'], 'previous_tail_run_id': profile['prior_run_id'], 'scope': scope,
               'audio_sha256': profile['audio_hash'], 'plan_hash': profile['plan_hash'],
               'previous_review_seconds': prior['total_review_seconds'], 'previous_review_clips': prior['total_review_clips'],
               'asr_decoded_blocks': 0, 'new_cloud_seconds': 0, 'new_cloud_clips': 0,
               'publish_results': False, 'send_email': False, 'previews': []}
    def checkpoint():
        pipeline.out('homework-visual-result.enc').write_bytes(encrypt(
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(), recipient, profile['source_run_id'], 0))
    checkpoint()
    def observe(image, row):
        if image and row['source'] == 'video_frame':
            from src.pipeline.homework_previews import retain_preview
            retain_preview(payload['previews'], image, row)
            checkpoint()
    from main import login_with_retry
    from src.api.icourse import ICourseClient
    from src.pipeline.homework_visual import collect_visual_evidence
    visual = collect_visual_evidence(ICourseClient(login_with_retry()), profile['course_id'], str(spec['lecture']['sub_id']),
        candidates, intervals, audio_seconds=data['audio_seconds'], frame_observer=observe)
    payload.update(candidates=candidates, intervals=intervals, visual=visual)
    checkpoint()
    audit = {k: payload[k] for k in ['scope', 'asr_decoded_blocks', 'new_cloud_seconds', 'new_cloud_clips',
             'publish_results', 'send_email', 'previous_review_seconds', 'previous_review_clips']}
    audit.update(mode='visual_only_replay', date=profile['date'], capture_status=visual['capture_status'],
                 reference_status=visual['reference_status'], frames=len(visual['frames']),
                 frame_status_counts={s: sum(f['status'] == s for f in visual['frames'])
                                      for s in sorted({f['status'] for f in visual['frames']})},
                 execution_complete=True, reference_verified=visual['reference_status'] == 'supported')
    pipeline.out('homework-visual-audit.json').write_text(json.dumps(audit, ensure_ascii=False))
    # Diagnosis distinguishes successful execution from reference quality;
    # unknown or missing references remain unverified, never relabelled passed.


if __name__ == '__main__':
    os.environ['QWEN_PRODUCTION_TASK'] = 'true'; os.environ['COURSE_SLOT'] = '0'
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()): run()
        print('Visual-only replay completed; no ASR, cloud review, publication or email')
    except Exception as error:
        print(f'Visual-only replay failed ({type(error).__name__}); private details withheld')
        sys.exit(1)
