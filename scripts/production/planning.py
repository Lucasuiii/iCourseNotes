"""Formal planning stage, with explicit runtime services and frozen inputs."""
from __future__ import annotations
import json
import os
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo


def validation_course(runtime):
    course = os.environ.get('VALIDATION_COURSE_ID', '').strip()
    rank = runtime.validation_rank()
    ranks = runtime.validation_ranks()
    source = os.environ.get('VALIDATION_SOURCE_RUN_ID', '').strip()
    selection_source = os.environ.get('VALIDATION_SELECTION_RUN_ID', '').strip()
    on_date = runtime.validation_on_date()
    if on_date and (source or selection_source):
        raise ValueError('Exact validation date cannot change a frozen source selection')
    if selection_source and (not selection_source.isascii() or not selection_source.isdigit()):
        raise ValueError('Invalid validation selection source')
    if source and selection_source:
        raise ValueError('Conflicting validation sources')
    if source and (not source.isascii() or not source.isdigit()):
        raise ValueError('Invalid validation source run')
    if not course and (rank != 1 or os.environ.get('VALIDATION_LECTURE_RANKS', '').strip()
                       or runtime.validation_before_date() or on_date or source or selection_source):
        raise ValueError('Recording rank is only allowed in isolated course validation')
    if course:
        courses = [item.strip() for item in course.split(',')]
        if (len(courses) > 5 or len(set(courses)) != len(courses)
                or any(not item.isascii() or not item.isdigit() for item in courses)):
            raise ValueError('Invalid validation courses')
        if len(ranks) > 1 and len(courses) != 1:
            raise ValueError('Multiple recording ranks require exactly one validation course')
        if source and (len(courses) != 1 or len(ranks) != 1):
            raise ValueError('Source-run validation requires exactly one course')
        course = ','.join(courses)
        if os.environ.get('PUBLISH_RESULTS') != 'false' or os.environ.get('SEND_EMAIL') != 'false':
            raise ValueError('Classroom validation requires publication and email disabled')
        if any(item not in runtime.configured_courses(os.environ['COURSE_IDS']) for item in courses):
            raise ValueError('Validation course is not subscribed')
    return course


def validation_rank(runtime):
    raw = os.environ.get('VALIDATION_LECTURE_RANK', '1').strip()
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 10:
        raise ValueError('Invalid reverse recording rank')
    return int(raw)


def validation_ranks(runtime):
    raw = os.environ.get('VALIDATION_LECTURE_RANKS', '').strip()
    if not raw: return [runtime.validation_rank()]
    if runtime.validation_rank() != 1:
        raise ValueError('Conflicting validation recording ranks')
    values = [part.strip() for part in raw.split(',')]
    if (not 1 <= len(values) <= 5 or any(not v.isascii() or not v.isdigit()
            or not 1 <= int(v) <= 10 for v in values)):
        raise ValueError('Invalid validation recording ranks')
    ranks = [int(v) for v in values]
    if len(set(ranks)) != len(ranks): raise ValueError('Duplicate validation recording ranks')
    return ranks


def validation_before_date(runtime):
    raw = os.environ.get('VALIDATION_BEFORE_DATE', '').strip()
    if raw:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            raise ValueError('Invalid validation cutoff date')
        try: datetime.strptime(raw, '%Y-%m-%d')
        except ValueError: raise ValueError('Invalid validation cutoff date') from None
    return raw


def validation_on_date(runtime):
    raw = os.environ.get('VALIDATION_ON_DATE', '').strip()
    if raw:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            raise ValueError('Invalid exact validation date')
        try: datetime.strptime(raw, '%Y-%m-%d')
        except ValueError: raise ValueError('Invalid exact validation date') from None
    return raw


def validation_source_queue(runtime, source):
    """Explicit new-input trial, locked to a pre-ASR failure's exact lesson.

    This is not ASR recovery: only a completed preparation with no retained
    audio/plan is eligible. Completed ASR or any cloud review cannot be reset.
    The original empty database/history and already frozen glossary are kept.
    """
    info = json.loads(subprocess.check_output(['gh', 'api',
        f'repos/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{source}'],
        stderr=subprocess.PIPE, timeout=60))
    if (info['status'] != 'completed' or info.get('conclusion') != 'failure'
            or info.get('path', '').split('@')[0] != '.github/workflows/parallel_pilot.yml'):
        raise ValueError('Validation source must be a completed failed formal pilot')
    target = runtime.root()/'validation-source'
    runtime.artifact('qwen-production-queue', target/'queue', run=source, required=True)
    runtime.artifact('qwen-production-prepare-0', target/'prepared', run=source, required=True)
    with runtime.shards.environment({'GITHUB_RUN_ID': source, 'COURSE_SLOT': '0'}):
        files = runtime.decode(target/'queue'/'queue.enc', 'queue')
        prepared = runtime.decode(target/'prepared'/'prepared.enc', 'prepared')
    tasks = runtime.read_json(files['queue.json'])
    spec = runtime.read_json(prepared['specification.json'])
    if (len(tasks) != 1 or spec.get('mode') != 'failed' or spec.get('plan')
            or 'lecture.flac' in prepared or spec.get('material') or spec.get('review')
            or str(tasks[0][0]) != str(spec.get('course_id')) or tasks[0][2] != spec.get('lecture')):
        raise ValueError('Validation source is not an unrecoverable pre-ASR preparation')
    frozen = spec.get('glossary_snapshot')
    if (os.environ.get('AUTO_COURSE_TERMS') == 'true') != bool(frozen):
        raise ValueError('Validation source glossary mode differs')
    if frozen:
        runtime.validate_frozen_terms(frozen, str(tasks[0][0]), tasks[0][2])
        tasks[0][2]['_frozen_glossary'] = frozen
    tasks[0][2]['_validation']['source_run_id'] = source
    files['queue.json'] = runtime.shards.encoded(tasks)
    return files


def validation_selection_queue(runtime, source):
    """Explicitly authorized fresh trial; freeze an ended pre-ASR batch selection.

    This does not recover/repeat recognized blocks or reset cloud review quota.
    Any existing ASR/gather/publication stage makes this entry ineligible.
    """
    from scripts import production_pool as pool
    info=pool.api(f'repos/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{source}')
    if info['status']!='completed' or info['path'].split('@')[0]!='.github/workflows/parallel_pilot.yml':
        raise ValueError('Selection source must be an ended formal pilot')
    store=pool.store_for(source)
    try: state=pool.read_state(store)[1]
    finally: store.close()
    if state['run_id']!=source or state['sha']!=info['head_sha']:
        raise ValueError('Selection source journal differs')
    flags={key:os.environ.get(key,'false') for key in ('AUTO_COURSE_TERMS','PUBLISH_RESULTS','SEND_EMAIL')}
    if state['flags']!=flags or flags['PUBLISH_RESULTS']!='false' or flags['SEND_EMAIL']!='false':
        raise ValueError('Selection source flags differ')
    for ticket in state['tickets']:
        if ticket['stage']!='prepare' or not ticket.get('run'):
            raise ValueError('Selection source has recognition or unresolved stages; reuse required')
        child=pool.api(f'repos/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{ticket["run"]}')
        if (child['status']!='completed' or child['head_sha']!=state['sha']
                or child['path'].split('@')[0]!='.github/workflows/'+pool.WORKFLOW
                or child['display_title']!=f'icourse-stage-{source}-{ticket["nonce"]}' or child['run_attempt']!=1):
            raise ValueError('Selection source child still active or mismatched')
    target=runtime.root()/'selection-source'
    runtime.artifact('qwen-production-queue',target,run=source,required=True)
    with runtime.shards.environment({'GITHUB_RUN_ID':source}):
        files=runtime.decode(target/'queue.enc','queue')
    tasks=runtime.read_json(files['queue.json'])
    if len(tasks)!=state['task_count'] or any(not task[2].get('_validation') for task in tasks):
        raise ValueError('Selection source is not an isolated validation queue')
    for task in tasks:
        task[2]['_validation'].pop('source_run_id',None)
        task[2]['_validation']['selection_source_run_id']=source
    files['queue.json']=runtime.shards.encoded(tasks)
    return files


def validate_frozen_terms(runtime, frozen, course, lecture):
    terms = frozen.get('terms')
    if (frozen.get('schema') != 1 or str(frozen.get('course_id')) != course
            or str(frozen.get('sub_id')) != str(lecture['sub_id'])
            or frozen.get('lecture_date') != lecture.get('date')
            or not isinstance(terms, list) or len(terms) > 30
            or any(not isinstance(t, str) or not 1 <= len(t) <= 80 for t in terms)
            or frozen.get('terms_sha256') != runtime.fingerprint(terms)):
        raise ValueError('Frozen glossary belongs to another lecture or input')


def latest_validation_task(runtime, client, db, course, *, today=None, rank=1, before_date='', on_date=''):
    """Probe actual playback, including entries with stale playback_status.

    No benchmark acquisition limit or cached summary is used. Deleted lectures
    remain excluded; an empty holiday schedule is a normal unavailable entry.
    """
    if type(rank) is not int or not 1 <= rank <= 10:
        raise ValueError('Invalid reverse recording rank')
    if before_date:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', before_date):
            raise ValueError('Invalid validation cutoff date')
        try: datetime.strptime(before_date, '%Y-%m-%d')
        except ValueError: raise ValueError('Invalid validation cutoff date') from None
    today = today or datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    if on_date:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', on_date):
            raise ValueError('Invalid exact validation date')
        try: datetime.strptime(on_date, '%Y-%m-%d')
        except ValueError: raise ValueError('Invalid exact validation date') from None
    detail = client.get_course_detail(course)
    from src.runtime import config
    from src.runtime.session_rules import lecture_is_selected
    candidates = []; excluded_sub_ids = set()
    for lecture in detail.get('lectures', []):
        date = str(lecture.get('date', ''))
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date): continue
        try: datetime.strptime(date, '%Y-%m-%d')
        except ValueError: continue
        sub_id = str(lecture.get('sub_id', ''))
        if date > today or not sub_id.isascii() or not sub_id.isdigit(): continue
        if before_date and date >= before_date: continue
        if on_date and date != on_date: continue
        if (db.get_lecture(sub_id) or {}).get('deleted_at'): continue
        if not lecture_is_selected(course, lecture, {}, exclusions=config.COURSE_SESSION_EXCLUSIONS):
            excluded_sub_ids.add(sub_id)
            continue
        period = re.search(r'第\s*(\d+)', str(lecture.get('sub_title', '')))
        candidates.append((date, int(period.group(1)) if period else -1, int(sub_id), lecture))
    seen, skipped, playable = set(), 0, 0
    for _, _, _, lecture in sorted(candidates, key=lambda item: item[:3], reverse=True):
        sub_id = str(lecture['sub_id'])
        if sub_id in seen: continue
        seen.add(sub_id)
        # Use the same API resolution/fallbacks as AudioDownloader. Do not
        # freeze an expiring private URL in the queue or print it in Actions.
        if client.get_video_url(course, sub_id):
            playable += 1
            if playable != rank:
                continue
            selected = dict(lecture, sub_id=sub_id,
                            _validation={'date': lecture['date'], 'skipped_unavailable': skipped,
                                         'playable_rank': rank})
            if excluded_sub_ids:
                selected['_validation']['skipped_excluded'] = len(excluded_sub_ids)
            if before_date:
                selected['_validation']['before_date'] = before_date
            if on_date:
                selected['_validation']['on_date'] = on_date
            return (course, detail['title'], selected), detail.get('teacher', '')
        skipped += 1
    raise ValueError('Requested playable non-future lecture rank unavailable')


def plan(runtime):
    historical = os.environ.get('HISTORY_REFRESH_TARGETS', '').strip()
    if historical:
        from scripts import history_refresh
        selected = history_refresh.policy.request(historical)
        history_refresh.isolated_flags()
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        from scripts.production_pool import verify_previous_pool
        verify_previous_pool()  # Also gate legacy mode after an orphaned pool.
    requested = runtime.validation_course()
    courses = requested.split(',') if requested else []
    requested_recordings = [(course, rank) for course in courses for rank in runtime.validation_ranks()]
    # On a workflow rerun keep opaque slot identities and exact selections fixed.
    if runtime.artifact('qwen-production-queue', runtime.root()/'previous'):
        files = runtime.decode(runtime.root()/'previous'/'queue.enc', 'queue')
    elif os.environ.get('VALIDATION_SELECTION_RUN_ID', '').strip():
        if int(os.environ.get('GITHUB_RUN_ATTEMPT','1'))>1:
            raise ValueError('Rerun has lost its queue checkpoint; refusing a new selection')
        files=runtime.validation_selection_queue(os.environ['VALIDATION_SELECTION_RUN_ID'].strip())
    elif os.environ.get('VALIDATION_SOURCE_RUN_ID', '').strip():
        if int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')) > 1:
            raise ValueError('Rerun has lost its queue checkpoint; refusing a new selection')
        files = runtime.validation_source_queue(os.environ['VALIDATION_SOURCE_RUN_ID'].strip())
    else:
        if int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')) > 1:
            raise ValueError('Rerun has lost its queue checkpoint; refusing a new selection')
        from main import login_with_retry, _enumerate_lectures, _crawl_semester_catalog
        from src.api.icourse import ICourseClient
        from src.runtime.reporter import Reporter
        db = runtime.Database(str(runtime.root()/'queue.db'))
        try:
            db.conn.close(); revision = runtime.load_remote(runtime.root()/'queue.db'); db = runtime.Database(str(runtime.root()/'queue.db'))
            reporter = Reporter()
            client = ICourseClient(login_with_retry())
            if historical:
                files = history_refresh.selection(runtime, client, db, revision, selected)
                tasks = runtime.read_json(files['queue.json'])
            elif courses:
                exact = {'on_date': runtime.validation_on_date()} if runtime.validation_on_date() else {}
                selections = [runtime.latest_validation_task(client, db, course, rank=rank,
                    before_date=runtime.validation_before_date(), **exact) for course, rank in requested_recordings]
                history = runtime.snapshot(db, runtime.root()/'history.db')
                # A separate scratch database forces this authorized lecture
                # through ASR even when production already has a summary.
                # The original encrypted history remains in the queue bundle.
                db.conn.close(); db = runtime.Database(str(runtime.root()/'validation.db'))
                tasks = []
                for task, teacher in selections:
                    course = task[0]
                    db.upsert_course(course, task[1], teacher)
                    lecture = task[2]
                    db.insert_lecture(lecture['sub_id'], course, lecture.get('sub_title', ''), lecture['date'])
                    tasks.append(task)
            else:
                _crawl_semester_catalog(client, db, reporter)
                enumeration = _enumerate_lectures(client, db, reporter)
                tasks = enumeration.lectures
                tasks = [t for t in tasks if (db.get_lecture(str(t[2]['sub_id'])).get('error_count') or 0) < 3]
            if len(tasks) > runtime.MAX_TASKS:
                raise ValueError('Queue exceeds 256 tasks; narrow the subscribed course scope')
            if not historical:
                files = {'queue.json': runtime.shards.encoded(tasks), 'database.db': runtime.snapshot(db, runtime.root()/'snapshot.db')}
                if courses: files['history.db'] = history
                else: files['enumeration.json'] = runtime.shards.encoded(enumeration.public_audit())
        finally:
            db.conn.close()
    tasks = runtime.read_json(files['queue.json'])
    identities = [(str(t[0]), str(t[2]['sub_id'])) for t in tasks]
    if len(tasks) > runtime.MAX_TASKS or len(set(identities)) != len(identities):
        raise ValueError('Invalid or duplicate lecture queue')
    if historical:
        history_refresh.validate_queue(files, selected)
        runtime.out('plan-audit.json').write_bytes(runtime.shards.encoded({
            'mode': 'historical_preview', 'lecture_count': len(tasks), 'publication': False, 'email': False}))
    elif 'history-refresh.json' in files:
        raise ValueError('Historical queue requires explicit original selection')
    if courses:
        if len(tasks) != len(requested_recordings) or [str(t[0]) for t in tasks] != [c for c, _ in requested_recordings]:
            raise ValueError('Validation queue does not match the requested courses')
        from src.runtime.session_rules import lecture_is_selected
        from src.runtime import config
        for (course, rank), task in zip(requested_recordings, tasks):
            lecture = task[2]; audit = lecture.get('_validation')
            if (not audit or audit.get('playable_rank', 1) != rank
                    or audit.get('before_date', '') != runtime.validation_before_date()
                    or audit.get('on_date', '') != runtime.validation_on_date()
                    or (runtime.validation_on_date() and lecture.get('date') != runtime.validation_on_date())
                    or (runtime.validation_before_date() and str(lecture.get('date', '')) >= runtime.validation_before_date())):
                raise ValueError('Validation queue does not match the requested recording')
            if audit.get('source_run_id', '') != os.environ.get('VALIDATION_SOURCE_RUN_ID', '').strip():
                raise ValueError('Validation queue does not match its source run')
            if audit.get('selection_source_run_id','')!=os.environ.get('VALIDATION_SELECTION_RUN_ID','').strip():
                raise ValueError('Validation queue does not match its selection source')
            if not lecture_is_selected(course, lecture, {}, exclusions=config.COURSE_SESSION_EXCLUSIONS):
                raise ValueError('Frozen validation lesson is now excluded')
        selection = tasks[0][2]['_validation'] if len(tasks) == 1 else {'courses': [
            dict(task_slot=i, course_id=str(task[0]), **task[2]['_validation']) for i, task in enumerate(tasks)]}
        runtime.out('validation-selection.json').write_bytes(runtime.shards.encoded(selection))
    runtime.encode(files, 'queue', runtime.out('queue.enc'))
    if os.environ.get('SHARD_MODE') == 'shared' and tasks:
        from scripts.production_pool import initial_state, read_state, save, store_for
        flags = {key: os.environ.get(key, 'false') for key in ('AUTO_COURSE_TERMS', 'PUBLISH_RESULTS', 'SEND_EMAIL')}
        expected = initial_state(os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_SHA'], len(tasks), flags)
        store = store_for()
        try:
            revision, previous = store.read()
            if previous is None:
                if int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')) > 1:
                    raise ValueError('Runner journal missing; refusing new reservations')
                save(store, revision, expected)
            else:
                read_state(store)
                if any(previous[k] != expected[k] for k in ('run_id', 'sha', 'task_count', 'flags')):
                    raise ValueError('Frozen runner-pool configuration changed')
        finally: store.close()
        runtime.out('pool-audit.json').write_bytes(runtime.shards.encoded({'protocol': 1, 'parent_run_id': os.environ['GITHUB_RUN_ID']}))
    if 'enumeration.json' in files:
        runtime.out('plan-audit.json').write_bytes(files['enumeration.json'])
    runtime.write_outputs(tasks={'include': [{'task_slot': i} for i in range(len(tasks))]}, count=len(tasks))
    print(f'Planned {len(tasks)} lectures; at most 5 active pipelines and 15 runners', flush=True)
