"""Enumeration outcomes, enforced relationships, timestamps and CI coverage."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml
import main
from scripts import production_qwen as pipeline
from scripts.production_db import lecture_snapshot
from src.data.database import Database
from src.data.schema import SCHEMA_SQL
from src.runtime.enumeration import CourseEnumerationError


class EnumerationSafetyTests(unittest.TestCase):
    def test_catalog_cannot_requeue_paused_failures_until_explicit_reset(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(main.config, 'COURSE_IDS', ['10']))
            stack.enter_context(patch.object(main.config, 'RERUN_TARGET_IDS', set()))
            stack.enter_context(patch.object(main, '_in_run_scope', return_value=True))
            db = Database(str(Path(tmp) / 'db'))
            self.addCleanup(db.conn.close)
            db.upsert_course('10', 'course', '')
            for sub in ['paused', 'retryable', 'processed', 'suppressed', 'absent']:
                db.insert_lecture(sub, '10', sub, '2026-09-18')
            for _ in range(3):
                db.update_error('paused', 'summary', 'timeout')
            for _ in range(2):
                db.update_error('retryable', 'summary', 'timeout')
            db.mark_processed('processed')
            db.suppress_lectures('10', ['suppressed'])
            client = self.fixture({'10': 'ok'})
            client.get_course_detail.side_effect = None
            client.get_course_detail.return_value = {
                'title': 'course', 'teacher': '', 'lectures': [
                    {'sub_id': sub, 'sub_title': sub, 'date': '2026-09-18', 'has_playback': True}
                    for sub in ['paused', 'retryable', 'processed', 'suppressed', 'new']]}
            def tasks():
                result = main._enumerate_lectures(client, db, MagicMock())
                result.require_success()
                return [str(t[2]['sub_id']) for t in result.lectures]
            self.assertEqual(set(tasks()), {'retryable', 'new', 'absent'})
            self.assertEqual(db.get_lecture('paused')['error_count'], 3)
            self.assertEqual(db.retry_attention_lectures(['paused']), 1)
            ids = tasks()
            self.assertEqual(set(ids), {'paused', 'retryable', 'new', 'absent'})
            self.assertEqual(len(ids), len(set(ids)))

    def fixture(self, outcomes, new=False):
        client = MagicMock(); client.check_alive.return_value = True
        def detail(course):
            if outcomes[course] == 'failed': raise ConnectionError('private signed URL')
            return {'title':'private course', 'teacher':'private teacher', 'lectures':
                [{'sub_id':'lesson', 'sub_title':'第1讲', 'date':'2026-09-18', 'has_playback':True}]
                if new and course == '10' else []}
        client.get_course_detail.side_effect = detail
        return client

    def enumerate(self, outcomes, new=False):
        with tempfile.TemporaryDirectory() as tmp, patch.object(main.config, 'COURSE_IDS', list(outcomes)):
            db = Database(str(Path(tmp)/'db'))
            try: return main._enumerate_lectures(self.fixture(outcomes, new), db, MagicMock())
            finally: db.conn.close()

    def test_clean_empty_requires_both_courses_successful(self):
        r = self.enumerate({'10':'ok', '20':'ok'})
        self.assertEqual(r.lectures, []); self.assertEqual(r.failed_courses, [])
        self.assertEqual(r.successful_courses, ['10','20']); r.require_success()

    def test_partial_failure_keeps_successful_course_tasks_and_never_looks_clean(self):
        r = self.enumerate({'10':'ok', '20':'failed'}, new=True)
        self.assertEqual([t[0] for t in r.lectures], ['10'])
        self.assertEqual(r.failed_courses, ['20'])
        with self.assertRaises(CourseEnumerationError): r.require_success()
        self.assertEqual(r.public_audit()['status'], 'degraded')

    def test_all_failures_have_no_tasks_but_explicit_failure(self):
        r = self.enumerate({'10':'failed', '20':'failed'})
        self.assertEqual(r.lectures, []); self.assertEqual(r.successful_courses, [])
        with self.assertRaises(CourseEnumerationError): r.require_success()
        self.assertEqual(r.public_audit()['failed_course_count'], 2)

    def test_legacy_run_processes_successful_course_before_exposing_partial_failure(self):
        result = self.enumerate({'10': 'ok', '20': 'failed'}, new=True)
        with ExitStack() as stack:
            stack.enter_context(patch.object(main.config, 'COURSE_IDS', ['10', '20']))
            stack.enter_context(patch.object(main.config, 'RERUN_TARGET_IDS', set()))
            stack.enter_context(patch.object(main.config, 'RETRY_ALL_FAILED', False))
            stack.enter_context(patch.object(main.config, 'SMTP_EMAIL', ''))
            for name in ['Database', 'Transcriber', 'Summarizer', 'Scheduler',
                         'Reporter', 'ICourseClient', 'login_with_retry',
                         '_crawl_semester_catalog', '_send_email', '_send_failure_notices']:
                stack.enter_context(patch.object(main, name))
            main.Database.return_value.sync_dates_from_sub.return_value = 0
            stack.enter_context(patch.object(main, '_enumerate_lectures', return_value=result))
            drive = stack.enter_context(patch.object(main, '_drive_lectures'))
            with self.assertRaises(CourseEnumerationError):
                main.run()
            self.assertEqual(drive.call_args.args[-2], result.lectures)
            self.assertEqual(len(result.lectures), 1)

    def test_formal_empty_and_partial_scans_preserve_audit_until_final_failure(self):
        for outcomes, new in [({'10':'ok','20':'ok'}, False), ({'10':'ok','20':'failed'}, False),
                              ({'10':'failed','20':'failed'}, False), ({'10':'ok','20':'failed'}, True)]:
            with self.subTest(outcomes=outcomes), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99',
                    'COURSE_SLOT':'0','QWEN_PRODUCTION_TASK':'true','DB_ENCRYPTION_KEY':'k'*32,
                    'VALIDATION_COURSE_ID':'','VALIDATION_LECTURE_RANK':'1','VALIDATION_BEFORE_DATE':'',
                    'VALIDATION_SOURCE_RUN_ID':'','GITHUB_ACTIONS':'false','PUBLISH_RESULTS':'false',
                    'SEND_EMAIL':'false','GITHUB_OUTPUT':str(Path(tmp)/'outputs')}))
                stack.enter_context(patch.object(main.config,'COURSE_IDS',list(outcomes)))
                stack.enter_context(patch.object(main,'login_with_retry',return_value=None))
                stack.enter_context(patch.object(main,'_crawl_semester_catalog'))
                stack.enter_context(patch('src.api.icourse.ICourseClient',return_value=self.fixture(outcomes, new)))
                stack.enter_context(patch.object(pipeline,'artifact',return_value=False))
                stack.enter_context(patch.object(pipeline,'load_remote',return_value=None))
                pipeline.plan()
                audit=json.loads(pipeline.out('plan-audit.json').read_text())
                self.assertNotIn('10',json.dumps(audit)); self.assertNotIn('private',json.dumps(audit))
                queue=pipeline.decode(pipeline.out('queue.enc'),'queue')
                tasks = json.loads(queue['queue.json'])
                self.assertEqual(len(tasks), int(new))
                if new:
                    self.assertEqual(tasks[0][0], '10')
                inbox=pipeline.root()/'inbox';inbox.mkdir()
                shutil.copyfile(pipeline.out('queue.enc'),inbox/'queue.enc')
                if audit['failed_course_count']:
                    with self.assertRaises(CourseEnumerationError) as caught: pipeline.finalize()
                    self.assertEqual(pipeline.failure_code(caught.exception),'course_enumeration_failed')
                else: pipeline.finalize()
                self.assertTrue(pipeline.out('catalog.enc').exists())


class DatabaseAuditTests(unittest.TestCase):
    def test_foreign_keys_reject_orphans_and_duplicate_insert_is_still_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(str(Path(tmp)/'db'))
            try:
                self.assertEqual(db.conn.execute('PRAGMA foreign_keys').fetchone()[0],1)
                with self.assertRaises(sqlite3.IntegrityError): db.insert_lecture('orphan','missing','课','2026-09-18')
                with self.assertRaises(sqlite3.IntegrityError), db.conn:
                    db.conn.execute("INSERT INTO ppt_pages(sub_id,page_num,created_sec) VALUES('missing',1,0)")
                db.upsert_course('c','课','');self.assertTrue(db.insert_lecture('s','c','课','2026-09-18'))
                self.assertFalse(db.insert_lecture('s','c','课','2026-09-18'))
                db.insert_ppt_pages_pending('s',[{'page_num':1,'created_sec':0,'pptimgurl':'url'}])
                db.upsert_course('c','new title','')
                self.assertEqual(db.conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            finally: db.conn.close()

    def test_new_timestamps_are_aware_and_legacy_values_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db';db=Database(str(path));db.upsert_course('c','课','')
            db.insert_lecture('s','c','课','2026-09-18');db.insert_lecture('old','c','旧','2026-09-18')
            old='2026-09-18T12:34:56'
            with db.conn: db.conn.execute('UPDATE lectures SET processed_at=? WHERE sub_id=?',(old,'old'))
            db.mark_processed('s');db.mark_emailed('s')
            db.insert_ppt_pages_pending('s',[{'page_num':1,'created_sec':0,'pptimgurl':'url'}])
            db.update_ppt_page('s',1,'text','done')
            row=db.get_lecture('s')
            for field in ['processed_at','emailed_at']:
                self.assertEqual(datetime.fromisoformat(row[field]).utcoffset(),timezone.utc.utcoffset(None))
            ocr=db.conn.execute('SELECT ocr_at FROM ppt_pages WHERE sub_id=?',('s',)).fetchone()[0]
            self.assertIsNotNone(datetime.fromisoformat(ocr).tzinfo)
            db.conn.close();db=Database(str(path))
            self.assertEqual(db.get_lecture('old')['processed_at'],old);db.conn.close()

    def test_snapshot_removes_children_before_parents_and_retains_the_selected_lesson(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(str(Path(tmp)/'db'))
            for c,s in [('a','s'),('b','other')]:
                db.upsert_course(c,'课','');db.insert_lecture(s,c,'课','2026-09-18')
                db.insert_ppt_pages_pending(s,[{'page_num':1,'created_sec':0,'pptimgurl':'url'}])
            target=Path(tmp)/'snapshot.db';lecture_snapshot(db,target,'a','s');db.conn.close()
            saved=Database(str(target))
            self.assertEqual(saved.conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(saved.conn.execute('SELECT sub_id FROM lectures').fetchone()[0],'s');saved.conn.close()

    def test_legacy_orphan_is_readable_but_new_orphans_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'old.db'
            with sqlite3.connect(path) as conn:
                conn.executescript(SCHEMA_SQL)
                conn.execute("INSERT INTO lectures(sub_id,course_id,summary) VALUES('old','missing','history')")
            conn.close()
            db=Database(str(path))
            self.assertEqual(db.get_lecture('old')['summary'],'history')
            with self.assertRaises(sqlite3.IntegrityError):db.insert_lecture('new','missing','课','2026-09-18')
            db.conn.close()

    def test_python_and_js_schema_columns_match(self):
        root=Path(__file__).resolve().parents[1]
        js=(root/'frontend/js/schema.js').read_text(); sql=re.search(r'SCHEMA_SQL:\s*`(.*?)`',js,re.S)[1]
        with closing(sqlite3.connect(':memory:')) as python, closing(sqlite3.connect(':memory:')) as browser:
            python.executescript(SCHEMA_SQL);browser.executescript(sql)
            for table in ['courses','lectures','ppt_pages','all_courses','meta']:
                self.assertEqual(python.execute('PRAGMA table_info('+table+')').fetchall(),
                                 browser.execute('PRAGMA table_info('+table+')').fetchall())


class ContinuousIntegrationTests(unittest.TestCase):
    def test_all_prs_and_main_pushes_have_full_tests_without_secrets(self):
        root=Path(__file__).resolve().parents[1]
        workflow=yaml.load((root/'.github/workflows/ci.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertIn('pull_request',workflow['on']);self.assertEqual(workflow['on']['push']['branches'],['main'])
        self.assertNotIn('paths',workflow['on']['push']);self.assertNotIn('paths',workflow['on']['pull_request'] or {})
        text=(root/'.github/workflows/ci.yml').read_text()
        self.assertIn('python -m unittest discover -s tests',text);self.assertIn('npm ci --ignore-scripts',text)
        self.assertNotIn('secrets.',text)
