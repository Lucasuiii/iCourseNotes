"""Bounded Actions stage dispatch; one controller plus at most fourteen jobs.

Only opaque task/worker IDs and timing audits are public. Immutable inputs and
the dispatch journal are encrypted on a run-specific CAS ref. A dispatch whose
outcome is unknown is never repeated and continues to occupy its reservation.
"""
from __future__ import annotations
import json
import copy
import math
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from scripts.asr_queue_store import GitHubQueueStore
from scripts.coordination_transport import CoordinationError, RETRYABLE, diagnostic, read_json, read_with_retry
from scripts.pool_progress import PoolProgress, show_progress

from src.pipeline.runner_budget import (MAX_TOTAL, MAX_JOBS, MAX_COURSES, MAX_WORKERS,
    TARGET_SECONDS, DEFAULT_RTF, estimate_rtf, desired_workers)
WORKFLOW = 'qwen_production_stage.yml'
TERMINAL = {'success', 'failure', 'cancelled', 'skipped', 'timed_out', 'action_required', 'startup_failure', 'stale', 'neutral'}
# Only explicit CLI HTTP responses proving rejection release a reservation.
# 408/429, server failures, invalid replies and lost responses remain unknown.
DISPATCH_REJECTIONS = frozenset((400, 401, 403, 404, 405, 410, 422))


class DispatchRejected(CoordinationError):
    def __init__(self, status):
        self.status = status
        super().__init__('github_dispatch', 'authorization' if status in (401, 403) else 'command_failed', 1)


class PoolStore(GitHubQueueStore):
    def __init__(self, run_id, key, **kwargs):
        super().__init__(run_id, 0, key, **kwargs)
        self.ref = f'refs/heads/codex/runner-pool-{run_id}'
        self.aad = f'icourse-runner-pool-v1:{os.environ["GITHUB_REPOSITORY"]}:{run_id}'.encode()


def store_for(run=None):
    from scripts.sharded_qwen_pilot import key
    return PoolStore(run or os.environ['GITHUB_RUN_ID'], key())


class OwnerStore(GitHubQueueStore):
    """Bridge the parent lock when dispatched children outlive their parent."""
    def __init__(self, key, **kwargs):
        super().__init__('0', 0, key, **kwargs)
        self.ref = 'refs/heads/codex/runner-pool-owner'
        self.aad = f'icourse-runner-pool-owner-v1:{os.environ["GITHUB_REPOSITORY"]}'.encode()


def claim_owner(state, owner_store, *, open_pool=store_for, inspect_parent=None, poll=None, transfer=True):
    revision, owner = owner_store.read()
    if owner is not None:
        if (set(owner) != {'schema', 'run_id'} or owner['schema'] != 1
                or not re.fullmatch('[0-9]+', owner['run_id'])):
            raise ValueError('Invalid runner-pool owner; refusing to reset')
        if owner['run_id'] == state['run_id']: return
        inspect_parent = inspect_parent or (lambda run: api(f'repos/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{run}'))
        if inspect_parent(owner['run_id'])['status'] != 'completed':
            raise ValueError('Previous pool parent still active')
        previous = open_pool(owner['run_id'])
        try:
            old_revision, old = read_state(previous)
            before = copy.deepcopy(old)
            (poll or Actions(old).poll)(old)
            save_if_changed(previous, old_revision, old, before)
            if any(t['status'] != 'completed' for t in old['tickets']):
                raise ValueError('Previous pool children active or dispatch outcome unknown')
        finally: previous.close()
    if not transfer: return
    if not owner_store.compare_and_swap(revision, {'schema': 1, 'run_id': state['run_id']}):
        raise ValueError('Another controller claimed the runner pool')


def verify_previous_pool():
    from scripts.sharded_qwen_pilot import key
    owner = OwnerStore(key())
    try: claim_owner({'run_id': os.environ['GITHUB_RUN_ID']}, owner, transfer=False)
    finally: owner.close()


def acquire_owner(state, key):
    owner = OwnerStore(key)
    try: claim_owner(state, owner)
    finally: owner.close()


def initial_state(run, sha, task_count, flags):
    if (not str(run).isascii() or not str(run).isdigit() or not re.fullmatch('[0-9a-f]{40}', sha)
            or type(task_count) is not int or not 0 <= task_count <= 256):
        raise ValueError('Invalid runner-pool identity')
    return {'schema': 1, 'run_id': str(run), 'sha': sha, 'task_count': task_count,
            'flags': flags, 'created_at': datetime.now(timezone.utc).date().isoformat(),
            'tickets': [], 'courses': {str(i): {'phase': 'new'} for i in range(task_count)}}


def validate(state):
    initial_state(state['run_id'], state['sha'], state['task_count'], state['flags'])
    if state.get('schema') != 1 or set(state['courses']) != {str(i) for i in range(state['task_count'])}:
        raise ValueError('Invalid runner-pool journal')
    if (set(state['flags']) != {'AUTO_COURSE_TERMS', 'PUBLISH_RESULTS', 'SEND_EMAIL'}
            or any(v not in ('true', 'false') for v in state['flags'].values())
            or any(c['phase'] not in ('new', 'prepare', 'asr', 'gather', 'publish', 'done', 'failed') for c in state['courses'].values())):
        raise ValueError('Invalid runner-pool configuration')
    identities = set()
    active = []
    for t in state['tickets']:
        if (type(t['slot']) is not int or t['slot'] not in range(state['task_count']) or t['stage'] not in ('prepare', 'asr', 'gather', 'publish')
                or type(t['attempt']) is not int or not 1 <= t['attempt'] <= 20
                or type(t['worker']) is not int or not 0 <= t['worker'] < MAX_WORKERS
                or not re.fullmatch('[0-9a-f]{32}', t['nonce']) or t['nonce'] in identities
                or t['status'] not in ('reserved', 'queued', 'in_progress', 'waiting', 'pending', 'requested', 'completed')
                or (t.get('run') and not str(t['run']).isascii())
                or (t.get('run') and not str(t['run']).isdigit())):
            raise ValueError('Invalid stage reservation')
        identities.add(t['nonce'])
        if 'dispatch_rejected_http' in t and (t['dispatch_rejected_http'] not in DISPATCH_REJECTIONS
                or t['status'] != 'completed' or t.get('run') is not None or t.get('conclusion') != 'failure'):
            raise ValueError('Invalid rejected dispatch reservation')
        if t['status'] != 'completed': active.append(t)
    if len(active) > MAX_JOBS:
        raise ValueError('Runner budget exceeded')
    slots = {t['slot'] for t in active}
    slots |= {int(k) for k, c in state['courses'].items() if c['phase'] in ('prepare', 'asr', 'gather', 'publish')}
    if len(slots) > MAX_COURSES:
        raise ValueError('Active course budget exceeded')
    for slot in slots:
        local = [t for t in active if t['slot'] == slot]
        if len(local) > MAX_WORKERS or (len(local) > 1 and any(t['stage'] != 'asr' for t in local)):
            raise ValueError('Overlapping course stages')


def read_state(store):
    revision, state = store.read()
    if state is None: raise ValueError('Runner journal missing; refusing to reset reservations')
    validate(state)
    return revision, state


def save(store, revision, state):
    validate(state)
    if not store.compare_and_swap(revision, state):
        raise ValueError('Another controller changed the runner journal')


def save_if_changed(store, revision, state, before):
    validate(state)
    if state != before:
        save(store, revision, state)


def api(path, *, payload=None, allow_404=False):
    args = ['gh', 'api', path]
    if payload is not None: args += ['--method', 'POST', '--input', '-']
    def request():
        result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, timeout=120)
        if result.returncode:
            if allow_404 and '(HTTP 404)' in result.stderr: return None
            raise subprocess.CalledProcessError(result.returncode, args, stderr=result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None
    # Dispatch/creation can succeed with a lost response. Never repeat a POST.
    if payload is None:
        return read_with_retry('github_read', request)
    try:
        return request()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        from scripts.coordination_transport import transport_code
        if path.endswith('/dispatches') and isinstance(error, subprocess.CalledProcessError):
            raw = error.stderr or ''
            if isinstance(raw, bytes): raw = raw.decode(errors='replace')
            statuses = re.findall(r'\(HTTP ([0-9]{3})\)', raw)
            if len(statuses) == 1 and int(statuses[0]) in DISPATCH_REJECTIONS:
                raise DispatchRejected(int(statuses[0])) from error
        raise CoordinationError('github_dispatch', transport_code(error), 1) from error


def artifact_sources(state, name):
    match = re.fullmatch(r'qwen-production-(prepare(?:-audit)?|worker-plan|worker-input|asr|shared|state|published)-(\d+)(?:-(\d+))?', name)
    if not match:
        match = re.fullmatch(r'qwen-validation-result-(\d+)', name)
        if not match: return []
        kind, slot, worker = 'state', int(match[1]), None
    else:
        kind, slot, worker = match[1], int(match[2]), match[3]
    stage = {'prepare': 'prepare', 'prepare-audit': 'prepare', 'worker-plan': 'prepare', 'worker-input': 'prepare', 'asr': 'asr', 'shared': 'asr',
             'state': 'gather', 'published': 'publish'}[kind]
    return [str(t['run']) for t in reversed(state['tickets']) if t.get('run') and t['slot'] == slot
            and t['stage'] == stage and (worker is None or t['worker'] == int(worker))]


def reserve(state, slot, stage, attempt, worker=0):
    active = [t for t in state['tickets'] if t['status'] != 'completed']
    if len(active) >= MAX_JOBS: return None
    if any(t['slot'] == slot and t['stage'] == stage and t['worker'] == worker for t in active):
        return None
    ticket = {'nonce': uuid.uuid4().hex, 'slot': slot, 'stage': stage, 'worker': worker,
              'attempt': attempt, 'status': 'reserved', 'run': None}
    state['tickets'].append(ticket)
    if stage != 'asr': state['courses'][str(slot)]['phase'] = stage
    state['courses'][str(slot)]['generation'] = attempt
    validate(state)
    return ticket


def choose_asr(state, workloads):
    """Give each ready course one worker, then prioritize remaining compute.

    Claims include running blocks in the estimate, but a new worker requires
    at least one pending block. Never expand a completed or failed course.
    """
    active = [t for t in state['tickets'] if t['status'] != 'completed']
    options = []
    for slot, work in workloads.items():
        if state['courses'][str(slot)]['phase'] != 'asr' or not work['pending_blocks']: continue
        occupied = {t['worker'] for t in active if t['slot'] == slot and t['stage'] == 'asr'}
        cap = min(work['worker_cap'], desired_workers(work['remaining_seconds'], work['remaining_blocks'], work['rtf']))
        if len(occupied) >= cap: continue
        # Avoid repeatedly restarting a finished worker in the same attempt.
        used = {t['worker'] for t in state['tickets'] if t['slot'] == slot and t['stage'] == 'asr'
                and t['attempt'] == work['attempt']}
        free = [i for i in range(work['worker_cap']) if i not in used]
        if not free: continue
        options.append(((0 if not occupied else 1, -work['remaining_seconds']*work['rtf']/(len(occupied)+1), slot), slot, free[0]))
    return min(options)[1:] if options else None


def recover(state, attempt):
    """Only ended child runs can be replaced; successful stages stay reused."""
    validate(state)
    if any(t['status'] != 'completed' for t in state['tickets']):
        raise ValueError('Previous stage runs still active or dispatch outcome unknown')
    for slot, course in state['courses'].items():
        if course['phase'] in ('new', 'done'): continue
        history = [t for t in state['tickets'] if t['slot'] == int(slot)]
        last = history[-1]
        if last['attempt'] >= attempt: continue
        stage = last['stage'] if course['phase'] == 'failed' else course['phase']
        relevant = [t for t in history if t['stage'] == stage and t['attempt'] == last['attempt']]
        succeeded = relevant and all(t.get('conclusion') == 'success' for t in relevant)
        course['phase'] = ({'prepare': 'asr', 'asr': 'asr', 'gather': 'publish', 'publish': 'done'}[stage]
                           if succeeded else ('new' if stage == 'prepare' else stage))
        course['generation'] = attempt
        course.pop('error_type', None)


class Actions:
    def __init__(self, state):
        self.state = state
        self.repo = os.environ['GITHUB_REPOSITORY']

    def source_ref(self):
        workflow = api(f'repos/{self.repo}/actions/workflows/{WORKFLOW}')
        if workflow.get('path') != '.github/workflows/'+WORKFLOW or workflow.get('state') != 'active':
            raise ValueError('Stage workflow not registered or inactive')
        ref = 'codex/runner-source-'+self.state['run_id']
        existing = api(f'repos/{self.repo}/git/ref/heads/{ref}', allow_404=True)
        if existing is None:
            # A transport error here is not retried or interpreted as absence.
            api(f'repos/{self.repo}/git/refs', payload={'ref': 'refs/heads/'+ref, 'sha': self.state['sha']})
        elif existing['object']['sha'] != self.state['sha']:
            raise ValueError('Frozen workflow source changed')
        return ref

    def dispatch(self, ticket, ref):
        api(f'repos/{self.repo}/actions/workflows/{WORKFLOW}/dispatches', payload={
            'ref': ref, 'inputs': {'parent_run_id': self.state['run_id'], 'ticket': ticket['nonce']}})

    def poll(self, state):
        pages = read_json(lambda: subprocess.check_output(['gh', 'api', '--paginate', '--slurp',
            f'repos/{self.repo}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&created=%3E%3D{state["created_at"]}&per_page=100'],
            stderr=subprocess.PIPE, timeout=120))
        matches = {}
        prefix = 'icourse-stage-'+state['run_id']+'-'
        for page in pages:
            for run in page['workflow_runs']:
                if not run.get('display_title', '').startswith(prefix): continue
                nonce = run['display_title'][len(prefix):]
                matches.setdefault(nonce, []).append(run)
        for t in state['tickets']:
            runs = matches.get(t['nonce'], [])
            if len(runs) > 1: raise ValueError('Duplicate stage dispatch; manual resolution required')
            if not runs:
                if t.get('run'): raise ValueError('Registered child run disappeared')
                continue  # Reservation stays charged, never re-dispatched.
            if 'dispatch_rejected_http' in t:
                raise ValueError('Rejected dispatch unexpectedly has a child run')
            r = runs[0]
            if r['head_sha'] != state['sha'] or r['path'].split('@')[0] != '.github/workflows/'+WORKFLOW:
                raise ValueError('Stage workflow source mismatch')
            if t.get('run') and str(t['run']) != str(r['id']): raise ValueError('Stage run identity changed')
            t.update(run=str(r['id']), status=r['status'])
            if r['status'] == 'completed':
                if r.get('conclusion') not in TERMINAL: raise ValueError('Unknown stage conclusion')
                t['conclusion'] = r['conclusion']
                if t['stage'] == 'gather' and 'entered' not in t:
                    jobs = api(f'repos/{self.repo}/actions/runs/{r["id"]}/jobs?per_page=100')['jobs']
                    t['entered'] = any(j.get('steps') is None or any(s['name'] == 'Finalize through LectureRunner with saved quota'
                        and s.get('started_at') and s.get('conclusion') != 'skipped' for s in j.get('steps', [])) for j in jobs)


def workload(slot, course, attempt):
    from scripts import production_qwen as pipeline
    from scripts.shared_asr_worker import store_for as queue_store
    from src.pipeline.asr_queue import validate_queue
    if 'plan' not in course:
        target = pipeline.root()/'pool-inputs'/str(slot)
        if not pipeline.artifact(f'qwen-production-worker-plan-{slot}', target, required=True):
            raise ValueError('Prepared stage disappeared')
        with pipeline.shards.environment({'COURSE_SLOT': str(slot)}):
            files = pipeline.decode(target/'worker-plan.enc', 'worker-plan')
        spec = json.loads(files['specification.json'])
        course['mode'] = spec['mode']; course['plan'] = spec.get('plan', {})
        # Keep only the immutable small plan in the private journal, not audio.
    plan = course['plan']
    if course['mode'] != 'sharded':
        return {'complete': True, 'pending_blocks': 0, 'remaining_blocks': 0, 'remaining_seconds': 0,
                'worker_cap': 0, 'rtf': DEFAULT_RTF, 'attempt': attempt}
    store = queue_store(plan)
    try: _, queue = store.read()
    finally: store.close()
    if queue is None: raise ValueError('Shared queue disappeared')
    validate_queue(plan, queue)
    pending, remaining, rows, failed = [], [], [], []
    for b in plan['blocks']:
        row = queue['blocks'][str(b['chunk_id'])]
        if row['status'] == 'complete': rows.append(row['result']); continue
        if row['status'] == 'failed': failed.append(row['result']); continue
        remaining.append(b)
        if row['status'] == 'pending' or row.get('attempt', attempt) < attempt: pending.append(b)
    return {'complete': not remaining and not failed, 'settled': not remaining,
            'total_blocks': len(plan['blocks']), 'completed_blocks': len(rows),
            'claimed_blocks': len(remaining)-len(pending),
            'failed_blocks': len(failed), 'pending_blocks': len(pending), 'remaining_blocks': len(remaining),
            'remaining_seconds': sum(b['end']-b['start'] for b in remaining),
            'worker_cap': len(plan['shards']), 'rtf': estimate_rtf(rows, plan.get('runner_policy', {}).get('cost_rtf', DEFAULT_RTF)), 'attempt': attempt}


def refresh_phases(state, works):
    for raw, course in state['courses'].items():
        slot = int(raw); tickets = [t for t in state['tickets'] if t['slot'] == slot]
        active = [t for t in tickets if t['status'] != 'completed']
        if active: continue  # Even on failure wait for all running workers.
        if not tickets: continue
        phase = course['phase']; last = tickets[-1]
        if phase in ('done', 'failed'): continue
        relevant = [t for t in tickets if t['stage'] == phase and t['attempt'] == course.get('generation', last['attempt'])]
        if not relevant and phase != 'asr': continue
        if any(t['conclusion'] != 'success' for t in relevant):
            course['phase'] = 'failed'; course.pop('plan', None); continue
        if phase == 'prepare': course['phase'] = 'asr'
        elif phase == 'asr' and (works.get(slot, {}).get('complete') or works.get(slot, {}).get('settled')): course['phase'] = 'gather'
        elif phase == 'gather': course['phase'] = 'publish'
        elif phase == 'publish': course['phase'] = 'done'; course.pop('plan', None)


def reconcile_dispatch(store, actions, nonce, *, sleep=time.sleep):
    """Discover an accepted POST without ever replaying it or freeing its slot.

    Register every visible child, including earlier successful dispatches that
    are waiting for controller confirmation. Identity and CAS checks still fail
    closed; temporary read outages consume the same six-query budget.
    """
    for query in range(6):
        revision, state = read_state(store)
        before = copy.deepcopy(state)
        try:
            actions.poll(state)
        except CoordinationError as error:
            if error.operation != 'github_read' or error.code not in RETRYABLE:
                raise
        else:
            save_if_changed(store, revision, state, before)
            ticket = next(t for t in state['tickets'] if t['nonce'] == nonce)
            if ticket.get('run'):
                return True
        if query < 5:
            sleep(5)
    return False


def controller(store, actions, *, attempt, clock=time.monotonic, sleep=time.sleep, timeout=5.5*3600,
               get_work=workload, acquire=acquire_owner, progress=None):
    _, identity = read_state(store)
    acquire(identity, store.key)
    began = clock(); actions.source_ref()  # Freeze before any model/prepare job dispatch.
    revision, state = read_state(store)
    before = copy.deepcopy(state)
    # Poll prior runs before accepting a recovery generation.
    actions.poll(state)
    if attempt > 1: recover(state, attempt)
    save_if_changed(store, revision, state, before)
    while clock()-began < timeout:
        revision, state = read_state(store)
        before = copy.deepcopy(state)
        actions.poll(state)
        works = {}
        for raw, course in state['courses'].items():
            if course['phase'] == 'asr': works[int(raw)] = get_work(int(raw), course, attempt)
        refresh_phases(state, works)
        # A just-completed preparation becomes available without a batch barrier.
        for raw, course in state['courses'].items():
            if course['phase'] == 'asr' and int(raw) not in works:
                works[int(raw)] = get_work(int(raw), course, attempt)
        refresh_phases(state, works)
        save_if_changed(store, revision, state, before)
        show_progress(progress, state, works)
        if all(c['phase'] in ('done', 'failed') for c in state['courses'].values()):
            return state
        while True:
            revision, state = read_state(store)
            active = [t for t in state['tickets'] if t['status'] != 'completed']
            if len(active) >= MAX_JOBS: break
            active_courses = {int(k) for k, c in state['courses'].items() if c['phase'] not in ('new', 'done', 'failed')}
            choice = None
            # Finished lectures finalize first; no dependence on another course.
            for raw, c in state['courses'].items():
                if c['phase'] in ('gather', 'publish') and not any(t['slot'] == int(raw) for t in active):
                    choice = (int(raw), c['phase'], 0); break
            # Give ready ASR courses basic service before admitting more prepares.
            selected = choose_asr(state, works)
            if choice is None and selected is not None: choice = (selected[0], 'asr', selected[1])
            if choice is None and len(active_courses) < MAX_COURSES:
                raw = next((k for k, c in state['courses'].items() if c['phase'] == 'new'), None)
                if raw is not None: choice = (int(raw), 'prepare', 0)
            if choice is None: break
            ticket = reserve(state, *choice[:2], attempt, worker=choice[2])
            if ticket is None: break
            save(store, revision, state)  # Durable reservation before dispatch.
            try:
                actions.dispatch(ticket, 'codex/runner-source-'+state['run_id'])
            except DispatchRejected as error:
                # Stop this parent, but persist proof that no child was accepted.
                # Recovery may create a new nonce; uncertain POSTs never get here.
                revision, state = read_state(store)
                rejected = next(t for t in state['tickets'] if t['nonce'] == ticket['nonce'])
                if rejected['status'] != 'reserved' or rejected.get('run') is not None:
                    raise ValueError('Dispatch reservation changed during rejection') from error
                rejected.update(status='completed', conclusion='failure', dispatch_rejected_http=error.status)
                save(store, revision, state)
                raise
            except CoordinationError as error:
                if error.operation != 'github_dispatch' or error.code not in RETRYABLE:
                    raise
                print('派发响应暂不可确认；查询已预约 Worker，保留票据且不重复派发。', flush=True)
                if not reconcile_dispatch(store, actions, ticket['nonce'], sleep=sleep):
                    raise
                print('已确认 Worker 运行身份，控制器继续调度。', flush=True)
            # Successful response still leaves a reservation until discoverable.
        show_progress(progress, state, works)
        sleep(30)
    raise TimeoutError('Controller budget exhausted; child runs remain preserved')


def artifact_runs(run, name, *, allow_legacy=False):
    store = store_for(run)
    try:
        _, state = store.read()
        if state is None and allow_legacy: return []
        if state is None: raise ValueError('Runner journal missing')
        validate(state)
    finally: store.close()
    return artifact_sources(state, name)


def finalization_attempt(run, slot, prior_only=False, *, allow_legacy=False):
    store = store_for(run)
    try:
        _, state = store.read()
        if state is None and allow_legacy: return None
        if state is None: raise ValueError('Runner journal missing')
        validate(state)
    finally: store.close()
    current = int(os.environ.get('GITHUB_RUN_ATTEMPT', '1'))
    return max([t['attempt'] for t in state['tickets'] if t['slot'] == slot and t['stage'] == 'gather'
                and t.get('entered', False) and (not prior_only or t['attempt'] < current)] or [0])


def public_audit(state, ended, error=None):
    reasons = {
        'Stage workflow not registered or inactive': 'stage_workflow_inactive',
        'Previous pool parent still active': 'previous_parent_active',
        'Previous pool children active or dispatch outcome unknown': 'previous_children_unresolved',
        'Previous stage runs still active or dispatch outcome unknown': 'recovery_children_unresolved',
        'Another controller changed the runner journal': 'journal_conflict',
        'Runner journal missing': 'journal_missing',
        'Runner journal missing; refusing to reset reservations': 'journal_missing',
        'Shared queue disappeared': 'block_queue_missing',
        'Registered child run disappeared': 'child_run_missing',
        'Duplicate stage dispatch; manual resolution required': 'duplicate_dispatch',
        'Rejected dispatch unexpectedly has a child run': 'dispatch_rejection_conflict',
        'Stage workflow source mismatch': 'child_source_mismatch',
        'Controller budget exhausted; child runs remain preserved': 'controller_deadline',
        'GitHub stage API failed; response withheld': 'github_api_failure',
        'One or more course stages failed; checkpoints retained': 'course_stage_failure',
    }
    result = {'protocol': 1, 'parent_run_id': state['run_id'], 'all_ended': ended,
            'error_code': reasons.get(str(error), 'controller_failure') if error else None,
            'children': [{k: t.get(k) for k in ('slot', 'stage', 'worker', 'attempt', 'run', 'status', 'conclusion', 'dispatch_rejected_http')}
                         for t in state['tickets']]}
    if diagnostic(error):
        result.update(error_code='coordination_failure', coordination=diagnostic(error))
    return result


def main():
    from scripts import production_qwen as pipeline
    store = store_for()
    progress = None
    try:
        progress = PoolProgress(pipeline.out('pool-progress.json'), repository=os.environ['GITHUB_REPOSITORY'],
                                summary=os.environ.get('GITHUB_STEP_SUMMARY'))
    except Exception:
        print('进度显示暂不可用；调度与检查点继续按原流程处理。', flush=True)
    ended = False; error = None; state = {'run_id': os.environ['GITHUB_RUN_ID'], 'tickets': []}
    try:
        _, state = read_state(store)
        result = controller(store, Actions(state), attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')), progress=progress)
        ended = all(t['status'] == 'completed' for t in result['tickets'])
        if any(c['phase'] == 'failed' for c in result['courses'].values()):
            raise ValueError('One or more course stages failed; checkpoints retained')
    except Exception as failure:
        error = failure
        raise
    finally:
        try:
            _, state = read_state(store)
            ended = all(t['status'] == 'completed' for t in state['tickets'])
        except Exception: pass  # Still retain a safe failure marker on transport errors.
        show_progress(progress, state, {}, final=True, error=error is not None)
        pipeline.out('pool-audit.json').write_text(json.dumps(public_audit(state, ended, error)))
        pipeline.write_outputs(all_ended=ended)
        store.close()


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(f'Runner pool failed ({type(error).__name__}); reservations retained', flush=True)
        raise SystemExit(1)
