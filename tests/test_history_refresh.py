"""Historical replacement uses synthetic SQLite, encryption and local Git only."""
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import yaml

from src.data.database import Database
from src.pipeline import history_refresh as policy
from scripts import history_refresh as entry
from scripts import production_qwen as runtime
from scripts.production_db import snapshot, lecture_snapshot
from scripts import production_pool as pool
from test_qwen_production_pipeline import fixture

ROOT = Path(__file__).resolve().parents[1]
ISOLATED = {'AUTO_COURSE_TERMS': 'false', 'PUBLISH_RESULTS': 'false', 'SEND_EMAIL': 'false',
            'SHARD_MODE': 'shared', 'COURSE_IDS': '10', 'VALIDATION_LECTURE_RANK': '1',
            'VALIDATION_COURSE_ID': '', 'VALIDATION_LECTURE_RANKS': '', 'VALIDATION_BEFORE_DATE': '',
            'VALIDATION_SOURCE_RUN_ID': '', 'VALIDATION_SELECTION_RUN_ID': ''}


def historical(path, count=2):
    db = Database(str(path)); db.upsert_course('10', '课程', '教师')
    for sid in map(str, range(1, count+1)):
        db.insert_lecture(sid, '10', '历史课'+sid, '2026-10-04')
        db.update_transcript(sid, '旧转录'+sid); db.update_summary(sid, '旧摘要'+sid, 'old')
        db.mark_processed(sid); db.mark_emailed_batch([sid])
        db.conn.execute('UPDATE lectures SET failure_notified_at=?,retry_generation=2 WHERE sub_id=?', ('old-receipt', sid))
        db.write_meta('qwen_pipeline:'+sid, json.dumps({'old': True}))
    db.upsert_course('20', '其他课程', '教师'); db.insert_lecture('9', '20', '其他课', '2026-10-03')
    db.update_summary('9', '不应改变', 'old'); db.mark_processed('9')
    db.write_meta('unrelated', 'preserve'); db.conn.commit()
    return db


def final_candidate(path, target):
    plan, _ = fixture(slot=target['slot'], run='99')
    plan['selection'] = {'course_id': target['course_id'], 'sub_id': target['sub_id']}
    db = Database(str(path)); db.upsert_course(target['course_id'], '课程', '教师')
    db.insert_lecture(target['sub_id'], target['course_id'], '2026-10-04 第3节', target['date'])
    db.update_transcript(target['sub_id'], '新的完整转录'); db.update_summary(target['sub_id'], '新完整摘要', 'new')
    db.mark_processed(target['sub_id'])
    review = {'complete': True, 'failed': False, 'attempts': [], 'seconds': 0}
    db.write_meta('qwen_pipeline:'+target['sub_id'], json.dumps({
        'complete': True, 'plan_hash': policy.digest(plan), 'audio_sha256': plan['audio_sha256'],
        'audio_seconds': plan['audio_seconds'], 'transcript_sha256': hashlib.sha256('新的完整转录'.encode()).hexdigest()}))
    payload = lecture_snapshot(db, path.with_suffix('.snapshot.db'), target['course_id'], target['sub_id'])
    db.conn.close(); path.write_bytes(payload)
    return {'database.db': payload, 'specification.json': policy.encoded({
        'mode': 'sharded', 'course_id': target['course_id'], 'course_title': '课程',
        'lecture': {'sub_id': target['sub_id'], 'date': target['date'], '_history_refresh': target},
        'plan': plan, 'media_seconds': 600}), 'review.json': policy.encoded(review)}


class HistoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.remote = self.root/'formal.db'
        db = historical(self.remote)
        try:
            self.manifest = policy.baseline_manifest(db.conn, policy.request(
                '{"course_id":"10","lecture_ids":"1,2"}'), 'a'*40)
        finally: db.conn.close()
        self.approval = dict(self.manifest, source_run='99', source_sha='b'*40, targets=[])
        self.candidates = {}; self.files = {}
        for target in self.manifest['targets']:
            path = self.root/f'candidate-{target["slot"]}.db'
            files = final_candidate(path, target)
            state = policy.validate_candidate(path, files, target, '99')
            self.approval['targets'].append(dict(target, candidate_hash=policy.digest(state)))
            self.candidates[target['slot']] = path; self.files[target['slot']] = files

    def state(self, sid):
        with closing(sqlite3.connect(self.remote)) as conn:
            return policy.lesson_state(conn, '10', sid)

    def short_gap_candidate(self, spans=None, *, attempts=True):
        from test_short_recognition_gaps import with_gaps
        from src.pipeline.recognition_coverage import recognition_coverage, missing_recognition_notice
        plan, results = with_gaps(spans or {0: [(21.195, 28.695)]})
        coverage = recognition_coverage([r for result in results for r in result['chunks']], allow_short_missing=True)
        review = {'complete': True, 'failed': attempts, 'seconds': 7.5 if attempts else 0,
                  'attempts': [{'seconds': 7.5, 'status': 'failed', 'segments': [],
                                'interval': {'start_ms': 21195, 'end_ms': 28695,
                                             'kind': 'missing_asr'}}] if attempts else []}
        path = self.candidates[0]
        with closing(sqlite3.connect(path)) as conn, conn:
            metadata = json.loads(conn.execute('SELECT value FROM meta WHERE key="qwen_pipeline:1"').fetchone()[0])
            metadata.update(recognition_coverage=coverage, review=review)
            conn.execute('UPDATE meta SET value=? WHERE key="qwen_pipeline:1"', (json.dumps(metadata),))
            conn.execute('UPDATE lectures SET summary=?', ('新完整摘要\n\n'+missing_recognition_notice(coverage),))
        self.files[0]['database.db'] = path.read_bytes()
        self.files[0]['review.json'] = policy.encoded(review)
        return review, coverage

    def test_short_gap_failed_rescue_is_accepted_without_refunding_or_claiming_complete(self):
        review, coverage = self.short_gap_candidate()
        state = policy.validate_candidate(self.candidates[0], self.files[0], self.manifest['targets'][0], '99')
        metadata = json.loads(next(r['value'] for r in state['meta'] if r['key'] == 'qwen_pipeline:1'))
        self.assertFalse(metadata['recognition_coverage']['complete'])
        self.assertEqual(metadata['recognition_coverage'], coverage)
        self.assertEqual(metadata['review'], review)
        self.assertEqual(review['seconds'], 7.5)
        self.assertEqual(review['attempts'][0]['status'], 'failed')

    def test_short_gap_without_cloud_call_can_still_be_reviewed(self):
        self.short_gap_candidate(attempts=False)
        policy.validate_candidate(self.candidates[0], self.files[0], self.manifest['targets'][0], '99')

    def test_short_gap_rejects_unknown_other_failed_or_inconsistent_review(self):
        path = self.candidates[0]; target = self.manifest['targets'][0]
        for change in ('reserved', 'weak', 'outside', 'unfinished', 'error', 'missing-failure',
                       'no-attempt', 'mismatched-ledger', 'no-notice', 'no-coverage',
                       'forged-seconds', 'forged-complete', 'bad-policy', 'wrong-block', 'nan'):
            with self.subTest(change=change):
                review, coverage = self.short_gap_candidate()
                if change == 'reserved': review['attempts'][0]['status'] = 'reserved'
                if change == 'weak': review['attempts'][0]['interval']['kind'] = 'weak'
                if change == 'outside': review['attempts'][0]['interval'].update(start_ms=30000,end_ms=37500)
                if change == 'unfinished': review['complete'] = False
                if change == 'error': review['error_type'] = 'RuntimeError'
                if change == 'missing-failure': review['failed'] = False
                if change == 'no-attempt': review.update(attempts=[], seconds=0)
                if change == 'forged-seconds': coverage['missing_seconds'] = 0
                if change == 'forged-complete': coverage['complete'] = True
                if change == 'bad-policy': coverage['policy'] = 'anything'
                if change == 'wrong-block': coverage['missing_intervals'][0]['chunk_id'] = 1
                if change == 'nan': coverage['missing_intervals'][0]['start'] = float('nan')
                with closing(sqlite3.connect(path)) as conn, conn:
                    metadata = json.loads(conn.execute('SELECT value FROM meta WHERE key="qwen_pipeline:1"').fetchone()[0])
                    metadata.update(review=review, recognition_coverage=coverage)
                    if change == 'mismatched-ledger': metadata['review'] = {}
                    if change == 'no-coverage': metadata.pop('recognition_coverage')
                    if change == 'no-notice': conn.execute('UPDATE lectures SET summary="无缺口提示"')
                    conn.execute('UPDATE meta SET value=? WHERE key="qwen_pipeline:1"', (json.dumps(metadata),))
                self.files[0]['review.json'] = policy.encoded(review)
                with self.assertRaises(ValueError): policy.validate_candidate(path, self.files[0], target, '99')

    def test_historical_gap_limit_is_whole_lecture_and_strictly_under_fifteen(self):
        for spans in ({0: [(10,25)]}, {0: [(10,25.001)]}, {0: [(10,18)],1: [(130,138)]}):
            with self.subTest(spans=spans):
                self.short_gap_candidate(spans, attempts=False)
                with self.assertRaises(ValueError):
                    policy.validate_candidate(self.candidates[0], self.files[0], self.manifest['targets'][0], '99')

    def test_exact_request_rejects_empty_duplicate_over_limit_and_non_numeric_ids(self):
        for raw in ('{}', '{"course_id":"10","lecture_ids":""}',
                    '{"course_id":"10","lecture_ids":"1,1"}',
                    '{"course_id":"10","lecture_ids":"1,2,3,4,5,6"}',
                    '{"course_id":"10","lecture_ids":"1;2"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError): policy.request(raw)

    def test_batch_replaces_content_but_preserves_identity_receipts_and_other_courses(self):
        old = self.state('1')['lecture']
        self.assertTrue(policy.replace_batch(self.remote, self.approval, self.candidates))
        new = self.state('1')['lecture']
        for field in ('course_id', 'sub_id', 'sub_title', 'date', 'emailed_at', 'failure_notified_at', 'deleted_at'):
            self.assertEqual(new[field], old[field], field)
        self.assertEqual(new['summary'], '新完整摘要'); self.assertEqual(new['retry_generation'], 3)
        with closing(sqlite3.connect(self.remote)) as conn:
            self.assertEqual(conn.execute('SELECT summary FROM lectures WHERE sub_id="9"').fetchone()[0], '不应改变')
            self.assertEqual(conn.execute('SELECT value FROM meta WHERE key="unrelated"').fetchone()[0], 'preserve')

    def test_second_target_changed_aborts_every_target(self):
        first = self.state('1')
        with closing(sqlite3.connect(self.remote)) as conn, conn:
            conn.execute('UPDATE lectures SET summary="后来更新" WHERE sub_id="2"')
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            policy.replace_batch(self.remote, self.approval, self.candidates)
        self.assertEqual(self.state('1'), first)

    def test_tombstone_mail_receipt_and_ppt_changes_invalidate_preview(self):
        for sql in ('UPDATE lectures SET deleted_at="deleted" WHERE sub_id="2"',
                    'UPDATE lectures SET emailed_at="later" WHERE sub_id="2"',
                    'INSERT INTO ppt_pages(sub_id,page_num,created_sec,text) VALUES("2",1,0,"new")'):
            baseline = self.remote.read_bytes()
            with closing(sqlite3.connect(self.remote)) as conn, conn: conn.execute(sql)
            with self.assertRaises(ValueError): policy.replace_batch(self.remote, self.approval, self.candidates)
            self.assertEqual(self.state('1')['lecture']['summary'], '旧摘要1')
            self.remote.write_bytes(baseline)

    def test_candidate_corruption_and_cross_course_scope_are_rejected(self):
        with closing(sqlite3.connect(self.candidates[1])) as conn, conn:
            conn.execute('UPDATE lectures SET summary="different"')
        with self.assertRaisesRegex(ValueError, 'candidate changed'):
            policy.replace_batch(self.remote, self.approval, self.candidates)
        self.assertEqual(self.state('1')['lecture']['summary'], '旧摘要1')
        with closing(sqlite3.connect(self.candidates[1])) as conn, conn:
            conn.execute('INSERT INTO courses(course_id,title) VALUES("90","other")')
        with self.assertRaisesRegex(ValueError, 'crosses'):
            policy.replace_batch(self.remote, self.approval, self.candidates)

    def test_candidate_requires_complete_original_plan_review_and_matching_checkpoint(self):
        files = self.files[0]; target = self.manifest['targets'][0]
        for change in ('mode', 'missing-review', 'unfinished-attempt', 'wrong-run'):
            edited = copy.deepcopy(files); spec = json.loads(edited['specification.json'])
            if change == 'mode': spec['mode'] = 'finished'
            if change == 'wrong-run': spec['plan']['run_id'] = '100'
            edited['specification.json'] = policy.encoded(spec)
            if change == 'missing-review': edited['review.json'] = policy.encoded({'complete': False})
            if change == 'unfinished-attempt': edited['review.json'] = policy.encoded({'complete': True,
                'seconds': 1, 'attempts': [{'seconds': 1, 'status': 'reserved', 'interval': {'start_ms': 0, 'end_ms': 1000}}]})
            with self.subTest(change=change), self.assertRaises(ValueError):
                policy.validate_candidate(self.candidates[0], edited, target, '99')
        with closing(sqlite3.connect(self.candidates[0])) as conn, conn:
            conn.execute('UPDATE meta SET value=?', (json.dumps({'complete': True}),))
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            policy.validate_candidate(self.candidates[0], files, target, '99')

    def test_repeated_approval_is_idempotent_and_never_replaces_later_changes(self):
        self.assertTrue(policy.replace_batch(self.remote, self.approval, self.candidates))
        with closing(sqlite3.connect(self.remote)) as conn, conn:
            conn.execute('UPDATE lectures SET summary="later update" WHERE sub_id="1"')
        self.assertFalse(policy.replace_batch(self.remote, self.approval, self.candidates))
        self.assertEqual(self.state('1')['lecture']['summary'], 'later update')

    def test_baseline_rejects_incomplete_deleted_and_absent_formal_history(self):
        with closing(sqlite3.connect(self.remote)) as conn, conn:
            for field in ('summary', 'transcript', 'processed_at'):
                before = conn.execute('SELECT '+field+' FROM lectures WHERE sub_id="1"').fetchone()[0]
                conn.execute('UPDATE lectures SET '+field+'=NULL WHERE sub_id="1"')
                with self.assertRaises(ValueError): policy.baseline_manifest(conn, {'course_id':'10','lecture_ids':['1']}, 'a'*40)
                conn.execute('UPDATE lectures SET '+field+'=? WHERE sub_id="1"', (before,))
            with self.assertRaises(ValueError): policy.baseline_manifest(conn, {'course_id':'10','lecture_ids':['1']}, None)

    def test_prepare_apply_encrypts_full_backup_before_publication(self):
        from scripts.sharded_qwen_pilot import environment
        outdir = self.root/'runtime'; outdir.mkdir()
        def load(path): shutil.copyfile(self.remote, path); return 'a'*40
        with environment({'RUNNER_TEMP': str(outdir), 'DB_ENCRYPTION_KEY':'x'*40,
                          'QWEN_PRODUCTION_TASK':'true','GITHUB_RUN_ID':'100','COURSE_SLOT':'0'}), \
             patch.object(entry, 'approval_source', return_value=(self.approval, self.candidates)), \
             patch('scripts.production_pool.verify_previous_pool'), \
             patch.object(runtime, 'load_remote', side_effect=load), \
             patch.object(runtime, 'write_outputs') as outputs, patch.object(runtime, 'publish') as publish:
            entry.prepare_apply(); outputs.assert_called_once_with(changed='true'); publish.assert_not_called()
            backup = runtime.decode(runtime.out('backup.enc'), 'history-backup')
            restored = self.root/'restored.db'; restored.write_bytes(backup['database.db'])
            with closing(sqlite3.connect(restored)) as conn:
                self.assertEqual(conn.execute('SELECT summary FROM lectures WHERE sub_id="1"').fetchone()[0], '旧摘要1')
            with self.assertRaises(Exception):
                with environment({'GITHUB_RUN_ID': '101'}): runtime.decode(runtime.out('backup.enc'), 'history-backup')
            self.assertEqual(self.state('1')['lecture']['summary'], '旧摘要1')

    def test_review_encrypts_comparison_and_apply_uses_exact_authenticated_candidates(self):
        self.review_and_apply()

    def test_short_gap_preview_comparison_and_approved_apply_preserve_missing_status(self):
        self.short_gap_candidate()
        self.review_and_apply(short_gap=True)

    def review_and_apply(self, *, short_gap=False):
        from scripts.sharded_qwen_pilot import environment
        from scripts.production_result_export import decrypt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        import base64
        private=X25519PrivateKey.generate()
        public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        key=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        db=Database(str(self.remote))
        try: baseline=snapshot(db,self.root/'baseline-snapshot.db')
        finally: db.conn.close()
        queue={'history-refresh.json':policy.encoded(self.manifest),'history.db':baseline}
        original_decode=runtime.decode
        def decode(path,role):
            if role=='queue': return queue
            if role=='state': return self.files[int(os.environ['COURSE_SLOT'])]
            return original_decode(path,role)
        env={'RUNNER_TEMP':str(self.root),'DB_ENCRYPTION_KEY':'x'*40,'QWEN_PRODUCTION_TASK':'true',
             'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','RECIPIENT_PUBLIC_KEY':base64.b64encode(public).decode()}
        state={'task_count':2,'sha':'b'*40}
        with environment(env), patch.object(entry,'read_source',return_value=state), \
             patch.object(runtime,'artifact'), patch.object(runtime,'decode',side_effect=decode):
            entry.review()
            report=json.loads(decrypt(runtime.out('comparison.enc').read_bytes(),key,'99',0))
            self.assertEqual(report['lectures'][0]['old']['summary'],'旧摘要1')
            lesson = report['lectures'][0]
            self.assertEqual(lesson['recognition_complete'], not short_gap)
            if short_gap:
                self.assertEqual(lesson['recognition_coverage']['missing_seconds'], 7.5)
                self.assertIn('7.5 秒语音未识别', lesson['new']['summary'])
                self.assertTrue(lesson['review']['failed'])
                from scripts.history_review_client import comparison
                private_path = self.root/'private.key'; private_path.write_bytes(key)
                readable = self.root/'comparison.md'
                comparison(runtime.out('comparison.enc'), private_path, '99', readable)
                self.assertIn('不完整转录', readable.read_text())
                self.assertIn('00:00:21.195–00:00:28.695', readable.read_text())
                self.assertNotIn('已通过完整识别', readable.read_text().split('## 课次 2')[0])
            else:
                self.assertEqual(lesson['new']['summary'],'新完整摘要')
            audit=json.loads(runtime.out('history-audit.json').read_bytes())
            self.assertNotIn('course_id',audit);self.assertNotIn('date',audit)
            approved=runtime.out('approval.enc').read_bytes()
            self.assertNotIn('旧摘要'.encode(),approved)
            def artifact(_name,target,**_kw):
                target.mkdir(parents=True,exist_ok=True);(target/'approval.enc').write_bytes(approved)
            with environment({'GITHUB_RUN_ID':'100','SOURCE_RUN_ID':'99','COURSE_IDS':'10',
                              'APPROVAL_SHA256':report['approval_sha256']}), \
                 patch.object(runtime,'artifact',side_effect=artifact), \
                 patch('src.runtime.config.COURSE_SESSION_EXCLUSIONS',{'10':[(6,1,2)]}):
                approval,paths=entry.approval_source()
                self.assertEqual(policy.digest(approval),report['approval_sha256'])
                self.assertEqual(set(paths),{0,1})
                if short_gap:
                    old = self.state('1')['lecture']
                    self.assertTrue(policy.replace_batch(self.remote, approval, paths))
                    fresh = self.state('1')
                    self.assertEqual(fresh['lecture']['emailed_at'], old['emailed_at'])
                    self.assertEqual(fresh['lecture']['failure_notified_at'], old['failure_notified_at'])
                    self.assertIn('7.5 秒语音未识别', fresh['lecture']['summary'])
                    metadata = json.loads(next(r['value'] for r in fresh['meta'] if r['key']=='qwen_pipeline:1'))
                    self.assertFalse(metadata['recognition_coverage']['complete'])
                    self.assertEqual(metadata['review']['seconds'], 7.5)
                    self.assertEqual(metadata['review']['attempts'][0]['status'], 'failed')
                with environment({'APPROVAL_SHA256':'f'*64}),self.assertRaises(ValueError): entry.approval_source()
                with patch('src.runtime.config.COURSE_SESSION_EXCLUSIONS',{'10':[(6,3,3)]}), \
                     self.assertRaisesRegex(ValueError,'now excluded'): entry.approval_source()


class HistoricalEntryTests(unittest.TestCase):
    def test_original_plan_entry_generates_exact_frozen_historical_queue(self):
        from src.api import icourse  # Load native crypto before the temporary module map.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); db=historical(root/'before.db')
            try: before=snapshot(db,root/'history-original.db')
            finally: db.conn.close()
            client=MagicMock(); client.get_course_detail.return_value={'title':'课程','teacher':'教师',
                'lectures':[{'sub_id':'1','date':'2026-10-04','sub_title':'课次'}]}
            client.get_video_url.return_value='mock-private-url'
            def load(path): path.write_bytes(before); return 'a'*40
            store=MagicMock();store.read.return_value=(None,None)
            modules={'main':SimpleNamespace(login_with_retry=lambda:object(),
                                           _enumerate_lectures=MagicMock(), _crawl_semester_catalog=MagicMock())}
            env=dict(ISOLATED,HISTORY_REFRESH_TARGETS='{"course_id":"10","lecture_ids":"1"}',
                     GITHUB_ACTIONS='false',GITHUB_RUN_ID='99',GITHUB_RUN_ATTEMPT='1',GITHUB_SHA='b'*40,
                     QWEN_PRODUCTION_TASK='true',DB_ENCRYPTION_KEY='x'*40,COURSE_SLOT='0')
            with patch.dict(os.environ,env), patch.dict(sys.modules,modules), \
                 patch.object(icourse,'ICourseClient',return_value=client), \
                 patch.object(runtime,'root',return_value=root), patch.object(runtime,'artifact',return_value=False), \
                 patch.object(runtime,'load_remote',side_effect=load), patch.object(runtime,'write_outputs'), \
                 patch.object(pool,'store_for',return_value=store), patch.object(pool,'save') as save:
                runtime.plan()
                files=runtime.decode(root/'out'/'queue.enc','queue')
                self.assertEqual(json.loads(files['queue.json'])[0][2]['sub_id'],'1')
                entry.validate_queue(files,{'course_id':'10','lecture_ids':['1']})
                self.assertEqual(files['history.db'],before)
                self.assertEqual(save.call_args.args[2]['task_count'],1)
                audit=json.loads((root/'out'/'plan-audit.json').read_bytes())
                self.assertEqual(set(audit),{'mode','lecture_count','publication','email'})

    def test_apply_input_cannot_change_selection_or_skip_explicit_fingerprint(self):
        env={'HISTORY_ACTION':'apply','SOURCE_RUN_ID':'99','APPROVAL_SHA256':'a'*64,
             'HISTORY_REFRESH_TARGETS':'','RECIPIENT_PUBLIC_KEY':''}
        with patch.dict(os.environ,env): entry.inputs()
        for field,value in [('SOURCE_RUN_ID',''),('APPROVAL_SHA256',''),
                            ('HISTORY_REFRESH_TARGETS','new selection'),('RECIPIENT_PUBLIC_KEY','new key')]:
            with patch.dict(os.environ,dict(env,**{field:value})),self.assertRaises(ValueError): entry.inputs()

    def test_preview_rejects_publication_email_automatic_terms_and_conflicting_selection(self):
        for key in ('PUBLISH_RESULTS', 'SEND_EMAIL', 'AUTO_COURSE_TERMS', 'VALIDATION_SOURCE_RUN_ID'):
            with patch.dict(os.environ, dict(ISOLATED, **{key:'true'})):
                with self.assertRaises(ValueError): entry.isolated_flags()

    def test_exact_selection_keeps_baseline_and_uses_empty_scratch_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); db = historical(root/'old.db'); before = db.get_lecture('1')
            client = MagicMock(); client.get_course_detail.return_value = {'title':'课程','teacher':'教师',
                'lectures':[{'sub_id':'1','date':'2026-10-04','sub_title':'课次'}]}
            client.get_video_url.return_value = 'mock-private-url'
            with patch.dict(os.environ, ISOLATED), patch.object(runtime,'root',return_value=root):
                files = entry.selection(runtime,client,db,'a'*40,{'course_id':'10','lecture_ids':['1']})
                entry.validate_queue(files, {'course_id':'10','lecture_ids':['1']})
                with self.assertRaises(ValueError): entry.validate_queue(files,{'course_id':'10','lecture_ids':['2']})
            self.assertEqual(db.get_lecture('1'),before)
            path=root/'scratch-check.db';path.write_bytes(files['database.db'])
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT summary,transcript,emailed_at FROM lectures').fetchone(),(None,None,None))
            client.get_video_url.assert_called_once_with('10','1');db.conn.close()

    def test_source_requires_completed_isolated_matching_parent_and_stage_identities(self):
        state = pool.initial_state('99','b'*40,1,{k:'false' for k in ('AUTO_COURSE_TERMS','PUBLISH_RESULTS','SEND_EMAIL')})
        state['courses']['0']['phase']='done'; infos={}
        for index, name in enumerate(('prepare','gather','publish')):
            ticket={'nonce':str(index+1)*32,'slot':0,'stage':name,'worker':0,'attempt':1,
                    'status':'completed','run':str(100+index),'conclusion':'success'}
            state['tickets'].append(ticket)
            infos[ticket['run']]={'path':'.github/workflows/'+pool.WORKFLOW,'status':'completed','conclusion':'success',
                'head_sha':state['sha'],'run_attempt':1,'display_title':f'icourse-stage-99-{ticket["nonce"]}'}
        info={'path':entry.WORKFLOW,'event':'workflow_dispatch','status':'completed','conclusion':'success','head_sha':state['sha']}
        entry.source_pool(info,state,'99',inspect=infos.__getitem__)
        for changes in ({'status':'in_progress'}, {'conclusion':'cancelled'}, {'head_sha':'c'*40}, {'path':'.github/workflows/parallel_pilot.yml'}):
            with self.assertRaises(ValueError): entry.source_pool(dict(info,**changes),state,'99',inspect=infos.__getitem__)
        state['flags']['SEND_EMAIL']='true'
        with self.assertRaises(ValueError): entry.source_pool(info,state,'99',inspect=infos.__getitem__)

    def test_stage_authorization_allows_history_only_with_bounded_isolated_flags(self):
        from scripts.production_pool_stage import authorize
        from test_production_pool import ChildAuthorizationTests
        helper=ChildAuthorizationTests(); state,ticket,infos=helper.setup_context()
        infos['99']['path']=entry.WORKFLOW
        authorize('99',ticket['nonce'],'100',read=lambda:state,inspect=infos.__getitem__)
        state['flags']['PUBLISH_RESULTS']='true'
        with self.assertRaises(ValueError): authorize('99',ticket['nonce'],'100',read=lambda:state,inspect=infos.__getitem__)

    def test_workflow_is_manual_preview_has_no_publication_and_apply_requires_uploaded_backup(self):
        workflow=yaml.load((ROOT/entry.WORKFLOW).read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']),{'workflow_dispatch'})
        self.assertEqual(workflow['concurrency']['group'],'icourse-data-${{ github.repository }}')
        preview=workflow['jobs']['preview']['with']
        self.assertEqual([preview[k] for k in ('automatic_terms','publish_results','send_email')],['false']*3)
        steps=workflow['jobs']['apply']['steps']
        upload=next(i for i,s in enumerate(steps) if s.get('uses')=='actions/upload-artifact@v4')
        commit=next(i for i,s in enumerate(steps) if s.get('run')=='python -m scripts.history_refresh commit-apply')
        self.assertLess(upload,commit); self.assertIn('success()',steps[commit]['if'])
        self.assertEqual(steps[upload]['with']['if-no-files-found'],'error')
        self.assertNotIn('SMTP',str(workflow['jobs']['apply']))
        self.assertNotIn('STUID',str(workflow['jobs']['apply']))
        validate_steps=workflow['jobs']['validate']['steps']
        self.assertNotIn('env',validate_steps[-1])  # Raw selections must not be echoed by Actions.
        parent=yaml.load((ROOT/'.github/workflows/parallel_pilot.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertNotIn('HISTORY_REFRESH_TARGETS',parent['jobs']['plan']['env'])
        steps=parent['jobs']['plan']['steps']
        mask=next(i for i,s in enumerate(steps) if s.get('run')=='python -m scripts.history_refresh mask-selection')
        setup=next(i for i,s in enumerate(steps) if s.get('uses')=='./.github/actions/qwen-pilot-runtime')
        self.assertLess(mask,setup)

    def test_multiline_selection_is_masked_before_single_line_environment_output(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);event=path/'event.json';env=path/'env'
            raw='{\n"course_id":"38404",\n"lecture_ids":"101,102"\n}'
            event.write_bytes(policy.encoded({'inputs':{'targets':raw}}));output=io.StringIO()
            with patch.dict(os.environ,{'GITHUB_EVENT_PATH':str(event),'GITHUB_ENV':str(env)}),redirect_stdout(output):
                entry.mask_selection()
            self.assertIn('%0A',output.getvalue())
            self.assertEqual(len(env.read_text().splitlines()),1)
            frozen=policy.request(env.read_text().strip().split('=',1)[1])
            self.assertEqual(frozen['lecture_ids'],['101','102'])

    def test_commit_checks_uploaded_backup_before_any_git_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'staging.json').write_bytes(policy.encoded({'backup_sha256':'a'*64}))
            with patch.object(runtime,'root',return_value=root), \
                 patch.object(runtime,'artifact',side_effect=ValueError('Required recovery artifact is absent')), \
                 patch('scripts.production_db.command') as command:
                with self.assertRaises(ValueError): entry.commit_apply()
                command.assert_not_called()

    def test_commit_rejects_changed_remote_revision_before_sharding_or_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); target=root/'apply.db';db=historical(target);db.conn.close()
            backup=b'encrypted-test-backup'; (root/'uploaded-backup').mkdir()
            (root/'uploaded-backup'/'backup.enc').write_bytes(backup)
            (root/'staging.json').write_bytes(policy.encoded({'backup_sha256':hashlib.sha256(backup).hexdigest(),
                'merged_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'revision':'a'*40}))
            with patch.dict(os.environ,{'GITHUB_REPOSITORY':'example/repo'}), \
                 patch.object(runtime,'root',return_value=root), patch.object(runtime,'artifact'), \
                 patch('scripts.production_db.command',return_value=('b'*40+'\trefs/heads/data').encode()) as command, \
                 patch('src.data.sharder.shard_database') as sharder:
                with self.assertRaisesRegex(ValueError,'changed after backup'): entry.commit_apply()
                sharder.assert_not_called()
                self.assertEqual(command.call_count,1)

    def test_local_git_commit_confirms_lost_push_response_without_replaying_publication(self):
        from scripts import production_db as storage
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); bare=root/'remote.git';checkout=root/'seed';checkout.mkdir()
            def git(*args,cwd=None):
                return subprocess.check_output(['git',*args],cwd=cwd,stderr=subprocess.PIPE)
            git('init','--bare','-q',str(bare));git('init','-q',str(checkout))
            git('checkout','-q','-b','data',cwd=checkout)
            (checkout/'data').mkdir();(checkout/'data'/'placeholder').write_text('seed')
            git('add','data',cwd=checkout)
            git('-c','user.name=test','-c','user.email=test@example.invalid','commit','-qm','seed',cwd=checkout)
            git('push',str(bare),'HEAD:refs/heads/data',cwd=checkout)
            revision=git('rev-parse','HEAD',cwd=checkout).decode().strip()
            target=root/'apply.db';db=historical(target);db.conn.close()
            backup=b'opaque encrypted backup';(root/'uploaded-backup').mkdir()
            (root/'uploaded-backup'/'backup.enc').write_bytes(backup)
            (root/'staging.json').write_bytes(policy.encoded({'backup_sha256':hashlib.sha256(backup).hexdigest(),
                'merged_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'revision':revision,
                'approval_sha256':'a'*64,'source_run':'99'}))
            original=storage.command;url='https://github.com/example/repo.git'; pushes=[]
            def local(args,**kwargs):
                result=original([str(bare) if a==url else a for a in args],**kwargs)
                if args[:2]==['git','push']:
                    pushes.append(args)
                    raise subprocess.CalledProcessError(1,args)  # Remote accepted, response lost.
                return result
            with patch.dict(os.environ,{'GITHUB_REPOSITORY':'example/repo','DB_ENCRYPTION_KEY':'x'*40}), \
                 patch.object(runtime,'root',return_value=root), patch.object(runtime,'artifact'), \
                 patch.object(storage,'command',side_effect=local):
                entry.commit_apply()
            head=git('--git-dir',str(bare),'rev-parse','refs/heads/data').decode().strip()
            self.assertNotEqual(head,revision)
            self.assertEqual(git('--git-dir',str(bare),'rev-parse',head+'^').decode().strip(),revision)
            names=git('--git-dir',str(bare),'ls-tree','-r','--name-only',head).decode().splitlines()
            self.assertIn('data/icourse-index.enc',names)
            self.assertTrue(all(n.endswith('.enc') for n in names))
            self.assertEqual(len(pushes),1)


class PrivateComparisonClientTests(unittest.TestCase):
    def test_key_is_private_and_existing_key_is_never_overwritten(self):
        from scripts.history_review_client import keygen
        import base64
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'private.key';public=keygen(path)
            self.assertEqual(len(base64.b64decode(public)),32)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            before=path.read_bytes()
            with self.assertRaises(FileExistsError): keygen(path)
            self.assertEqual(path.read_bytes(),before)

    def test_authenticated_comparison_creates_private_readable_old_new_report(self):
        from scripts.history_review_client import keygen, comparison
        from scripts.production_result_export import encrypt
        import base64
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);private=root/'review.key';public=keygen(private)
            payload={'source_run':'99','approval_sha256':'a'*64,'lectures':[{'sub_id':'1','date':'2026-10-04',
                'old':{'summary':'旧摘要','transcript':'旧转录'},'new':{'summary':'新摘要','transcript':'新转录'},'review':{}}]}
            encrypted=root/'comparison.enc';encrypted.write_bytes(encrypt(policy.encoded(payload),base64.b64decode(public),'99',0))
            target=root/'report.md'
            self.assertEqual(comparison(encrypted,private,'99',target),'a'*64)
            for text in ('旧摘要','新摘要','旧转录','新转录'): self.assertIn(text,target.read_text())
            self.assertEqual(target.stat().st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError): comparison(encrypted,private,'99',target)

    def test_wrong_run_or_corrupt_ciphertext_creates_no_plaintext_output(self):
        from scripts.history_review_client import keygen, comparison
        from scripts.production_result_export import encrypt
        import base64
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);private=root/'review.key';public=keygen(private)
            encrypted=root/'comparison.enc';encrypted.write_bytes(encrypt(b'{}',base64.b64decode(public),'99',0))
            target=root/'report.md'
            with self.assertRaises(Exception): comparison(encrypted,private,'100',target)
            self.assertFalse(target.exists())
            encrypted.write_bytes(encrypted.read_bytes()[:-1]+b'!')
            with self.assertRaises(Exception): comparison(encrypted,private,'99',target)
            self.assertFalse(target.exists())


if __name__=='__main__': unittest.main()
