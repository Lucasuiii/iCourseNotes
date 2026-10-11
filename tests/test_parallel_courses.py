from pathlib import Path
import sqlite3
import tempfile
import unittest
from scripts.parallel_courses import MAX_PARALLEL, configured_courses, selected_course, course_delta, seal, unseal
from scripts.merge_db import merge_delta as merge
from src.data.database import Database


class ParallelCourseTests(unittest.TestCase):
    def test_slots_and_concurrency(self):
        self.assertEqual(MAX_PARALLEL,5)
        self.assertEqual(configured_courses('10,20,10'),['10','20'])
        self.assertEqual(selected_course('10,20',1),'20')
        for raw in ('','10,evil','1; echo bad'):
            with self.assertRaises(ValueError): configured_courses(raw)
        for index in (-1,2,True):
            with self.assertRaises(ValueError): selected_course('10,20',index)

    def test_ciphertext_tamper_and_cross_run_rejected(self):
        key='k'*32
        blob=seal(b'sensitive course data',key,'99','1')
        self.assertNotIn(b'sensitive',blob)
        self.assertEqual(unseal(blob,key,'99','1'),b'sensitive course data')
        for bad,run,slot in ((blob,'98','1'),(blob,'99','2'),(blob[:-1]+bytes([blob[-1]^1]),'99','1')):
            with self.assertRaises(Exception): unseal(bad,key,run,slot)

    def test_only_selected_course_merged_without_stale_other_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.db';target=root/'target.db';delta=root/'delta.db'
            db=Database(str(source))
            for cid,sid in [('10','1'),('20','2')]:
                db.upsert_course(cid,'课程','教师')
                db.insert_lecture(sid,cid,'课次','2026-10-04')
                db.update_summary(sid,'旧摘要','model')
            db.write_meta('auto_glossary:10:1','{}')
            db.write_meta('auto_glossary:20:2','{}')
            db.conn.close()
            remote=Database(str(target))
            remote.upsert_course('20','课程','教师')
            remote.insert_lecture('2','20','课次','2026-10-04')
            remote.update_summary('2','新的另一个课程摘要','model')
            remote.conn.close()
            course_delta(source,delta,'10')
            conn=sqlite3.connect(delta)
            self.assertEqual(conn.execute('SELECT course_id FROM courses').fetchall(),[('10',)])
            self.assertEqual(conn.execute('SELECT key FROM meta').fetchall(),[('auto_glossary:10:1',)])
            conn.close()
            merge(str(delta),str(target))
            remote=Database(str(target))
            self.assertEqual(remote.get_lecture('2')['summary'],'新的另一个课程摘要')
            self.assertEqual(remote.get_lecture('1')['summary'],'旧摘要')
            remote.conn.close()

    def test_workflow_workers_cannot_mail_or_publish(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/parallel_pilot.yml').read_text()
        self.assertIn('max-parallel: 5',text)
        self.assertIn('fail-fast: false',text)
        self.assertNotIn('schedule:',text)
        import yaml
        child=yaml.load((Path(__file__).resolve().parents[1]/'.github/workflows/qwen_production_lecture.yml').read_text(),Loader=yaml.BaseLoader)
        worker=child['jobs']['asr']
        self.assertEqual(worker['permissions']['contents'],'write')  # encrypted coordination ref only
        self.assertNotIn('production_qwen publish', str(worker['steps']))
        self.assertNotIn('SMTP_EMAIL',worker['env'])
        self.assertNotIn('PUBLISH_RESULTS',worker['env'])
        self.assertEqual(worker['strategy']['max-parallel'],'3')


if __name__=='__main__': unittest.main()
