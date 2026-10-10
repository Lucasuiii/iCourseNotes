"""Exact historical selections and atomic, explicitly approved replacements.

No network, model, SMTP or encryption services are used by this policy layer.
"""
from contextlib import closing
import hashlib
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo
import re
import sqlite3

MAX_REFRESH = 5


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def numeric(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,20}', value):
        raise ValueError('Invalid history identity')
    return value


def request(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'course_id', 'lecture_ids'}:
        raise ValueError('Invalid history request')
    course = numeric(value['course_id'])
    ids = value['lecture_ids']
    if not isinstance(ids, str):
        raise ValueError('Invalid history selection')
    ids = [numeric(v.strip()) for v in ids.split(',')]
    if not 1 <= len(ids) <= MAX_REFRESH or len(set(ids)) != len(ids):
        raise ValueError('History selection exceeds limit or contains duplicates')
    return {'course_id': course, 'lecture_ids': ids}


def scope_keys(course, sub_id):
    return ('qwen_pipeline:'+sub_id, 'auto_glossary:'+course+':'+sub_id)


def lesson_state(conn, course, sub_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM lectures WHERE course_id=? AND sub_id=?',
                       (course, sub_id)).fetchone()
    if row is None:
        raise ValueError('Historical lecture unavailable')
    return {'lecture': dict(row), 'ppt': [dict(r) for r in conn.execute(
        'SELECT * FROM ppt_pages WHERE sub_id=? ORDER BY page_num', (sub_id,))],
        'meta': [dict(r) for r in conn.execute(
            'SELECT * FROM meta WHERE key IN (?,?) ORDER BY key', scope_keys(course, sub_id))]}


def completed(row, *, require_model=False):
    if (row.get('deleted_at') or not all(isinstance(row.get(k), str) and row[k].strip()
            for k in ('transcript', 'summary', 'processed_at'))
            or require_model and not row.get('summary_model')
            or row.get('error_count') or row.get('error_stage') or row.get('error_msg')):
        raise ValueError('Historical result is not complete')


def baseline_manifest(conn, selected, revision):
    if not re.fullmatch('[0-9a-f]{40}', revision or ''):
        raise ValueError('History refresh requires an existing database revision')
    targets = []
    for slot, sub_id in enumerate(selected['lecture_ids']):
        old = lesson_state(conn, selected['course_id'], sub_id)
        completed(old['lecture'])
        day = old['lecture']['date']
        if (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
                or day > datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()):
            raise ValueError('Historical recording has an invalid or future date')
        targets.append({'slot': slot, 'course_id': selected['course_id'], 'sub_id': sub_id,
                        'date': old['lecture']['date'], 'before_hash': digest(old)})
    return {'schema': 1, 'baseline_revision': revision, 'targets': targets}


def candidate(path, target):
    with closing(sqlite3.connect(path)) as conn:
        state = lesson_state(conn, target['course_id'], target['sub_id'])
        row = state['lecture']; completed(row, require_model=True)
        if (row['date'] != target['date'] or row['emailed_at'] or row['failure_notified_at']
                or conn.execute('SELECT COUNT(*) FROM lectures').fetchone()[0] != 1
                or conn.execute('SELECT COUNT(*) FROM courses').fetchone()[0] != 1
                or conn.execute('SELECT course_id FROM courses').fetchone()[0] != target['course_id']
                or conn.execute('SELECT COUNT(*) FROM all_courses').fetchone()[0]
                or any(r[0] != target['sub_id'] for r in conn.execute('SELECT sub_id FROM ppt_pages'))
                or any(r[0] not in scope_keys(target['course_id'], target['sub_id'])
                       for r in conn.execute('SELECT key FROM meta'))):
            raise ValueError('Candidate crosses historical lecture scope')
    # The caller supplies the independently authenticated finalization state.
    return state


def validate_candidate(path, files, target, run):
    from src.pipeline.qwen_plan import fingerprint, validate_plan
    from src.ai.qwen_review_ledger import validate_ledger
    from src.pipeline.prepared_lecture import validate_audio_duration
    spec = json.loads(files['specification.json']); review = json.loads(files['review.json'])
    plan = spec.get('plan', {}); validate_plan(plan); validate_ledger(review)
    marker = spec.get('lecture', {}).get('_history_refresh')
    if (spec.get('mode') != 'sharded' or marker != target
            or str(spec.get('course_id')) != target['course_id']
            or str(spec['lecture'].get('sub_id')) != target['sub_id']
            or spec['lecture'].get('date') != target['date']
            or plan.get('run_id') != run or plan.get('course_slot') != target['slot']
            or plan.get('selection', {}).get('course_id') != target['course_id']
            or plan.get('selection', {}).get('sub_id') != target['sub_id']
            or review.get('complete') is not True):
        raise ValueError('Historical candidate lacks complete authenticated recognition')
    validate_audio_duration(plan['audio_seconds'], spec.get('media_seconds'))
    state = candidate(path, target)
    metadata = json.loads(next((r['value'] for r in state['meta']
                               if r['key'] == 'qwen_pipeline:'+target['sub_id']), '{}'))
    if (metadata.get('complete') is not True or metadata.get('plan_hash') != fingerprint(plan)
            or metadata.get('audio_sha256') != plan['audio_sha256']
            or metadata.get('audio_seconds') != plan['audio_seconds']
            or metadata.get('transcript_sha256') != hashlib.sha256(state['lecture']['transcript'].encode()).hexdigest()):
        raise ValueError('Historical completion checkpoint mismatch')
    from src.pipeline.recognition_coverage import validate_coverage_report, missing_recognition_notice
    coverage = metadata.get('recognition_coverage')
    if coverage is not None:
        coverage = validate_coverage_report(plan, coverage)
    failures = [a for a in review.get('attempts', []) if a['status'] != 'complete']
    if coverage and not coverage['complete']:
        # These inputs come from the authenticated finalization checkpoint.
        # Keep failed rescue calls and quota charged; never accept unknown calls.
        if (metadata.get('review') != review or review.get('error_type')
                or missing_recognition_notice(coverage) not in state['lecture']['summary']):
            raise ValueError('Historical short-gap checkpoint mismatch')
    if failures or review.get('failed'):
        if (not coverage or coverage['complete'] or not failures or not review.get('failed')
                or any(a['status'] != 'failed' or a['interval'].get('kind') != 'missing_asr'
                       for a in failures)):
            raise ValueError('Historical candidate has unresolved recognition review')
        import math
        spans = sorted((math.floor(g['start']*1000+1e-7), math.ceil(g['end']*1000-1e-7))
                       for g in coverage['missing_intervals'])
        merged = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(end, merged[-1][1])
            else:
                merged.append([start, end])
        if any(not any(start <= a['interval']['start_ms'] < a['interval']['end_ms'] <= end
                       for start, end in merged) for a in failures):
            raise ValueError('Historical failed rescue is outside the retained gaps')
    return state


def validate_approval(approval):
    if (not isinstance(approval, dict) or approval.get('schema') != 1
            or not re.fullmatch('[0-9a-f]{40}', approval.get('source_sha', ''))
            or not re.fullmatch('[0-9a-f]{40}', approval.get('baseline_revision', ''))):
        raise ValueError('Invalid history approval')
    numeric(approval.get('source_run'))
    targets = approval.get('targets', [])
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_REFRESH:
        raise ValueError('Invalid approval size')
    identities = set()
    for slot, item in enumerate(targets):
        course, sub_id = numeric(item['course_id']), numeric(item['sub_id'])
        if ((course, sub_id) in identities or item.get('slot') != slot
                or not isinstance(item.get('date'), str)
                or any(not re.fullmatch('[0-9a-f]{64}', item.get(k, ''))
                       for k in ('before_hash', 'candidate_hash'))):
            raise ValueError('Invalid approval target')
        identities.add((course, sub_id))
    return digest(approval)


def replace_batch(path, approval, candidates):
    """All targets or none; stale previews cannot overwrite any selected state."""
    approval_hash = validate_approval(approval)
    marker_key = 'history_refresh:'+approval_hash
    if set(candidates) != {t['slot'] for t in approval['targets']}:
        raise ValueError('Approval candidate set changed')
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        marker = conn.execute('SELECT value FROM meta WHERE key=?', (marker_key,)).fetchone()
        if marker:
            if marker[0] != encoded(approval).decode():
                raise ValueError('History approval marker changed')
            return False  # An ambiguous push/repeated apply is never a second overwrite.
        states = []
        for target in approval['targets']:
            old = lesson_state(conn, target['course_id'], target['sub_id'])
            completed(old['lecture'])
            if digest(old) != target['before_hash']:
                raise ValueError('Historical result changed since preview')
            fresh = candidate(candidates[target['slot']], target)
            if digest(fresh) != target['candidate_hash']:
                raise ValueError('Approved historical candidate changed')
            states.append((target, old, fresh))
        for target, old, fresh in states:
            row = fresh['lecture']; sub_id = target['sub_id']
            conn.execute('''UPDATE lectures SET transcript=?, summary=?, summary_model=?,
                processed_at=?, error_msg=NULL, error_stage=NULL, error_count=0,
                retry_generation=? WHERE sub_id=? AND course_id=?''',
                (row['transcript'], row['summary'], row['summary_model'], row['processed_at'],
                 (old['lecture']['retry_generation'] or 0)+1, sub_id, target['course_id']))
            # Course/lesson identity, tombstone and both email receipts stay intact.
            conn.execute('DELETE FROM ppt_pages WHERE sub_id=?', (sub_id,))
            for page in fresh['ppt']:
                columns = list(page)
                conn.execute('INSERT INTO ppt_pages ('+','.join('"'+c+'"' for c in columns)+') VALUES ('
                             +','.join('?' for _ in columns)+')', [page[c] for c in columns])
            conn.execute('DELETE FROM meta WHERE key IN (?,?)', scope_keys(target['course_id'], sub_id))
            for item in fresh['meta']:
                conn.execute('INSERT INTO meta(key,value) VALUES (?,?)', (item['key'], item['value']))
        conn.execute('INSERT INTO meta(key,value) VALUES (?,?)', (marker_key, encoded(approval).decode()))
    return True
