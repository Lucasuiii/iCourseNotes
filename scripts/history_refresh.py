"""Manual historical preview and separately approved, backed-up publication."""
from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from src.pipeline import history_refresh as policy

WORKFLOW = '.github/workflows/history_refresh.yml'


def isolated_flags():
    if any(os.environ.get(k) != 'false' for k in ('AUTO_COURSE_TERMS', 'PUBLISH_RESULTS', 'SEND_EMAIL')):
        raise ValueError('Historical preview must disable publication, email and automatic terms')
    if os.environ.get('SHARD_MODE') != 'shared':
        raise ValueError('Historical preview requires the shared pool')
    if any(os.environ.get(k, '').strip() for k in ('VALIDATION_COURSE_ID', 'VALIDATION_LECTURE_RANKS',
            'VALIDATION_BEFORE_DATE', 'VALIDATION_ON_DATE', 'VALIDATION_SOURCE_RUN_ID', 'VALIDATION_SELECTION_RUN_ID')):
        raise ValueError('Historical and validation selections cannot be combined')
    if os.environ.get('VALIDATION_LECTURE_RANK', '1') != '1':
        raise ValueError('Historical preview cannot use a recording rank')


def selection(runtime, client, db, revision, selected):
    """Select exact previously successful IDs, never ranks or a latest recording."""
    from src.runtime import config
    from src.runtime.session_rules import lecture_is_selected
    isolated_flags()
    course = selected['course_id']
    if course not in runtime.configured_courses(os.environ['COURSE_IDS']):
        raise ValueError('Historical course is no longer subscribed')
    manifest = policy.baseline_manifest(db.conn, selected, revision)
    history = runtime.snapshot(db, runtime.root()/'history-snapshot.db')
    detail = client.get_course_detail(course)
    tasks = []
    for target in manifest['targets']:
        matches = [r for r in detail.get('lectures', []) if str(r.get('sub_id')) == target['sub_id']]
        if len(matches) != 1 or matches[0].get('date') != target['date']:
            raise ValueError('Exact historical recording unavailable or changed')
        lecture = dict(matches[0], sub_id=target['sub_id'], _history_refresh=target)
        if not lecture_is_selected(course, lecture, {}, exclusions=config.COURSE_SESSION_EXCLUSIONS):
            raise ValueError('Historical recording is excluded')
        if not client.get_video_url(course, target['sub_id']):
            raise ValueError('Exact historical recording is not playable')
        tasks.append((course, detail['title'], lecture))
    scratch = runtime.Database(str(runtime.root()/'history-preview.db'))
    try:
        scratch.upsert_course(course, detail['title'], detail.get('teacher', ''))
        for _, _, lecture in tasks:
            scratch.insert_lecture(lecture['sub_id'], course, lecture.get('sub_title', ''), lecture['date'])
        return {'queue.json': runtime.shards.encoded(tasks), 'database.db': runtime.snapshot(scratch, runtime.root()/'scratch.db'),
                'history.db': history, 'history-refresh.json': policy.encoded(manifest)}
    finally:
        scratch.conn.close()


def validate_queue(files, selected):
    manifest = json.loads(files['history-refresh.json']); tasks = json.loads(files['queue.json'])
    if (manifest.get('schema') != 1 or len(tasks) != len(selected['lecture_ids'])
            or len(manifest.get('targets', [])) != len(tasks)):
        raise ValueError('Frozen historical queue changed')
    for slot, (task, target) in enumerate(zip(tasks, manifest['targets'])):
        if (target['slot'] != slot or target['course_id'] != selected['course_id']
                or target['sub_id'] != selected['lecture_ids'][slot] or str(task[0]) != target['course_id']
                or str(task[2]['sub_id']) != target['sub_id'] or task[2].get('_history_refresh') != target
                or task[2].get('date') != target['date']):
            raise ValueError('Frozen historical selection changed')


def run_info(run):
    policy.numeric(run)
    from scripts.production_pool import api
    return api(f'repos/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{run}')


def source_pool(info, state, run, *, active_review=False, inspect=None):
    from scripts import production_pool as pool
    pool.validate(state)
    inspect = inspect or run_info
    own_review = active_review and run == os.environ.get('GITHUB_RUN_ID')
    if (info['path'].split('@')[0] != WORKFLOW or info.get('event') != 'workflow_dispatch' or state['run_id'] != run
            or state['sha'] != info['head_sha'] or not 1 <= state['task_count'] <= policy.MAX_REFRESH
            or state['flags'] != {k: 'false' for k in ('AUTO_COURSE_TERMS', 'PUBLISH_RESULTS', 'SEND_EMAIL')}
            or not (info['status'] == 'completed' and info['conclusion'] == 'success'
                    or own_review and info['status'] == 'in_progress')
            or any(t['status'] != 'completed' for t in state['tickets'])
            or any(c['phase'] != 'done' for c in state['courses'].values())):
        raise ValueError('Historical source is not a completed isolated preview')
    for slot in range(state['task_count']):
        for stage in ('prepare', 'gather', 'publish'):
            tickets = [t for t in state['tickets'] if t['slot'] == slot and t['stage'] == stage]
            if not tickets or tickets[-1].get('conclusion') != 'success' or not tickets[-1].get('run'):
                raise ValueError('Historical source stage is incomplete')
            ticket = tickets[-1]; child = inspect(ticket['run'])
            if (child['status'] != 'completed' or child['conclusion'] != 'success'
                    or child['head_sha'] != state['sha'] or child['run_attempt'] != 1
                    or child['path'].split('@')[0] != '.github/workflows/'+pool.WORKFLOW
                    or child['display_title'] != f'icourse-stage-{run}-{ticket["nonce"]}'):
                raise ValueError('Historical child source mismatch')


def read_source(run, *, active_review=False):
    from scripts import production_pool as pool
    store = pool.store_for(run)
    try: state = pool.read_state(store)[1]
    finally: store.close()
    source_pool(run_info(run), state, run, active_review=active_review)
    return state


def review():
    from scripts import production_qwen as runtime
    from scripts.sharded_qwen_pilot import environment
    from scripts.production_result_export import encrypt
    run = os.environ['GITHUB_RUN_ID']; state = read_source(run, active_review=True)
    recipient = base64.b64decode(os.environ['RECIPIENT_PUBLIC_KEY'], validate=True)
    if len(recipient) != 32: raise ValueError('Invalid review recipient')
    runtime.artifact('qwen-production-queue', runtime.root()/'queue-source', run=run, required=True)
    files = runtime.decode(runtime.root()/'queue-source'/'queue.enc', 'queue')
    manifest = json.loads(files['history-refresh.json'])
    if len(manifest['targets']) != state['task_count']:
        raise ValueError('Historical pool and queue differ')
    history = runtime.root()/'baseline.db'; history.write_bytes(files['history.db'])
    approval = dict(manifest, source_run=run, source_sha=state['sha'], targets=[])
    payload = {}; comparisons = []
    for target in manifest['targets']:
        slot = target['slot']; destination = runtime.root()/'review-source'/str(slot)
        runtime.artifact(f'qwen-production-state-{slot}', destination, run=run, required=True)
        with environment({'COURSE_SLOT': str(slot)}):
            final = runtime.decode(destination/'state.enc', 'state')
        path = runtime.root()/f'candidate-{slot}.db'; path.write_bytes(final['database.db'])
        fresh = policy.validate_candidate(path, final, target, run)
        with closing(sqlite3.connect(history)) as conn:
            old = policy.lesson_state(conn, target['course_id'], target['sub_id'])
        if policy.digest(old) != target['before_hash']:
            raise ValueError('Historical baseline changed inside preview')
        approval['targets'].append(dict(target, candidate_hash=policy.digest(fresh)))
        payload[f'candidate-{slot}.db'] = final['database.db']
        metadata = json.loads(next(r['value'] for r in fresh['meta']
                                   if r['key'] == 'qwen_pipeline:'+target['sub_id']))
        coverage = metadata.get('recognition_coverage')
        comparisons.append({'slot': slot, 'course_id': target['course_id'], 'sub_id': target['sub_id'],
                            'date': target['date'], 'old': old['lecture'], 'new': fresh['lecture'],
                            'recognition_complete': coverage['complete'] if coverage else True,
                            'recognition_coverage': coverage, 'review': json.loads(final['review.json'])})
    approval_hash = policy.validate_approval(approval)
    payload['approval.json'] = policy.encoded(approval)
    runtime.encode(payload, 'history-approval', runtime.out('approval.enc'))
    runtime.out('comparison.enc').write_bytes(encrypt(policy.encoded({
        'source_run': run, 'approval_sha256': approval_hash, 'lectures': comparisons}), recipient, run, 0))
    audit = {'status': 'review_ready', 'lecture_count': len(comparisons), 'approval_sha256': approval_hash,
             'publication': False, 'email': False}
    runtime.out('history-audit.json').write_bytes(policy.encoded(audit))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as out:
            incomplete = sum(not c['recognition_complete'] for c in comparisons)
            out.write(f'历史预览完成：{len(comparisons)} 堂结果，其中 {incomplete} 堂保留识别缺口，未覆盖、未发信。\n\n'
                      f'先解密 comparison.enc 对比，再通过 apply 输入本次 run ID 和确认指纹：`{approval_hash}`。\n')
    print('Encrypted historical comparison ready; no publication or email')


def approval_source():
    from scripts import production_qwen as runtime
    from scripts.sharded_qwen_pilot import environment
    run = os.environ['SOURCE_RUN_ID']; expected = os.environ['APPROVAL_SHA256']
    policy.numeric(run)
    if not re.fullmatch('[0-9a-f]{64}', expected): raise ValueError('Explicit approval fingerprint required')
    state = read_source(run)
    runtime.artifact('icourse-history-review', runtime.root()/'approved-source', run=run, required=True)
    with environment({'GITHUB_RUN_ID': run, 'COURSE_SLOT': '0'}):
        files = runtime.decode(runtime.root()/'approved-source'/'approval.enc', 'history-approval')
    approval = json.loads(files['approval.json'])
    if (policy.validate_approval(approval) != expected or approval['source_run'] != run
            or approval['source_sha'] != state['sha'] or len(approval['targets']) != state['task_count']
            or set(files) != {'approval.json'} | {f'candidate-{t["slot"]}.db' for t in approval['targets']}):
        raise ValueError('Explicit historical approval does not match preview')
    candidates = {}
    for target in approval['targets']:
        if target['course_id'] not in runtime.configured_courses(os.environ['COURSE_IDS']):
            raise ValueError('Historical course is no longer subscribed')
        path = runtime.root()/f'approved-{target["slot"]}.db'
        path.write_bytes(files[f'candidate-{target["slot"]}.db'])
        fresh = policy.candidate(path, target)
        if policy.digest(fresh) != target['candidate_hash']:
            raise ValueError('Approved historical candidate changed')
        from src.runtime.session_rules import lecture_is_selected
        from src.runtime import config
        if not lecture_is_selected(target['course_id'], fresh['lecture'],
                                   {}, exclusions=config.COURSE_SESSION_EXCLUSIONS):
            raise ValueError('Historical recording is now excluded')
        candidates[target['slot']] = path
    return approval, candidates


def prepare_apply():
    from scripts import production_qwen as runtime
    from scripts.production_pool import verify_previous_pool
    verify_previous_pool()
    approval, candidates = approval_source()
    target = runtime.root()/'apply.db'; revision = runtime.load_remote(target)
    if revision is None: raise ValueError('Formal historical database disappeared')
    before = runtime.Database(str(target))
    try: backup = runtime.snapshot(before, runtime.root()/'backup.db')
    finally: before.conn.close()
    changed = policy.replace_batch(target, approval, candidates)
    runtime.write_outputs(changed=str(changed).lower())
    if not changed:
        print('Historical approval already applied; no second overwrite')
        return
    staging = {'revision': revision, 'approval_sha256': policy.digest(approval),
               'merged_sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'source_run': approval['source_run']}
    runtime.encode({'database.db': backup, 'approval.json': policy.encoded(approval),
                    'staging.json': policy.encoded(staging)}, 'history-backup', runtime.out('backup.enc'))
    staging['backup_sha256'] = hashlib.sha256(runtime.out('backup.enc').read_bytes()).hexdigest()
    (runtime.root()/'staging.json').write_bytes(policy.encoded(staging))
    print('Historical replacement staged; encrypted backup must upload before commit')


def commit_apply():
    from scripts import production_qwen as runtime
    from scripts import production_db as storage
    from src.data.sharder import shard_database
    from scripts.validate_db import validate_database
    staged = json.loads((runtime.root()/'staging.json').read_bytes())
    uploaded = runtime.root()/'uploaded-backup'
    runtime.artifact('icourse-history-backup-'+os.environ.get('GITHUB_RUN_ATTEMPT', '1'),
                     uploaded, required=True)
    if hashlib.sha256((uploaded/'backup.enc').read_bytes()).hexdigest() != staged['backup_sha256']:
        raise ValueError('Uploaded historical backup differs from the staged backup')
    target = runtime.root()/'apply.db'
    if hashlib.sha256(target.read_bytes()).hexdigest() != staged['merged_sha256']:
        raise ValueError('Staged historical database changed')
    validate_database(str(target))
    url = 'https://github.com/'+os.environ['GITHUB_REPOSITORY']+'.git'
    def remote():
        value = storage.command(['git', 'ls-remote', '--heads', url, 'refs/heads/data'], env=storage.auth_env()).decode().split()
        return value[0] if value else None
    if remote() != staged['revision']:
        raise ValueError('Formal database changed after backup; reapply with a fresh backup')
    with tempfile.TemporaryDirectory(prefix='icourse-history-commit-') as tmp:
        checkout = Path(tmp)
        storage.command(['git', 'init', '-q', tmp])
        storage.command(['git', 'fetch', '-q', '--depth=1', url, staged['revision']], cwd=checkout, env=storage.auth_env())
        storage.command(['git', 'checkout', '-q', '-b', 'data', 'FETCH_HEAD'], cwd=checkout)
        shutil.rmtree(checkout/'data', ignore_errors=True)
        shard_database(str(target), str(checkout/'data'), os.environ['DB_ENCRYPTION_KEY'])
        storage.command(['git', 'add', '-A', 'data'], cwd=checkout)
        storage.command(['git', '-c', 'user.name=github-actions[bot]', '-c',
            'user.email=41898282+github-actions[bot]@users.noreply.github.com', 'commit', '-q', '-m',
            'chore: apply explicitly approved historical lecture refresh'], cwd=checkout)
        head = storage.command(['git', 'rev-parse', 'HEAD'], cwd=checkout).decode().strip()
        try: storage.command(['git', 'push', url, 'HEAD:refs/heads/data'], cwd=checkout, env=storage.auth_env())
        except subprocess.CalledProcessError:
            if remote() != head: raise  # Never replay an unknown publication.
    runtime.out('history-audit.json').write_bytes(policy.encoded({
        'status': 'applied', 'approval_sha256': staged['approval_sha256'], 'email': False}))
    print('Approved historical results published; original email receipts preserved')


def inputs():
    if os.environ.get('GITHUB_EVENT_PATH') and not os.environ.get('HISTORY_ACTION'):
        supplied = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes()).get('inputs', {})
        for key, name in {'action':'HISTORY_ACTION','targets':'HISTORY_REFRESH_TARGETS',
                'recipient_public_key':'RECIPIENT_PUBLIC_KEY','source_run_id':'SOURCE_RUN_ID',
                'approval_sha256':'APPROVAL_SHA256'}.items():
            os.environ[name] = supplied.get(key, '')
    action = os.environ['HISTORY_ACTION']
    if action == 'preview':
        policy.request(os.environ['HISTORY_REFRESH_TARGETS'])
        if os.environ.get('SOURCE_RUN_ID') or os.environ.get('APPROVAL_SHA256'):
            raise ValueError('Preview cannot include an apply approval')
        recipient = base64.b64decode(os.environ['RECIPIENT_PUBLIC_KEY'], validate=True)
        if len(recipient) != 32: raise ValueError('Preview requires a review recipient public key')
    elif action == 'apply':
        policy.numeric(os.environ.get('SOURCE_RUN_ID'))
        if not re.fullmatch('[0-9a-f]{64}', os.environ.get('APPROVAL_SHA256', '')):
            raise ValueError('Apply requires the reviewed approval fingerprint')
        if os.environ.get('HISTORY_REFRESH_TARGETS') or os.environ.get('RECIPIENT_PUBLIC_KEY'):
            raise ValueError('Apply cannot change the preview selection')
    else: raise ValueError('Invalid historical operation')


def mask_selection():
    """Read caller event locally: never echo raw classroom IDs in step env."""
    supplied = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes()).get('inputs', {})
    raw = supplied.get('targets', '')
    selected = policy.request(raw)
    normalized = policy.encoded({'course_id':selected['course_id'],
                                'lecture_ids':','.join(selected['lecture_ids'])}).decode()
    for value in (raw, normalized, selected['course_id'], *selected['lecture_ids']):
        safe = value.replace('%','%25').replace('\r','%0D').replace('\n','%0A')
        print('::add-mask::'+safe)
    with open(os.environ['GITHUB_ENV'],'a') as out:
        out.write('HISTORY_REFRESH_TARGETS='+normalized+'\n')


if __name__ == '__main__':
    os.environ['QWEN_PRODUCTION_TASK'] = 'true'
    try: {'inputs': inputs, 'mask-selection': mask_selection, 'review': review,
          'prepare-apply': prepare_apply, 'commit-apply': commit_apply}[sys.argv[1]]()
    except Exception as error:
        print(f'Historical operation refused ({type(error).__name__}); private details withheld')
        raise SystemExit(1)
