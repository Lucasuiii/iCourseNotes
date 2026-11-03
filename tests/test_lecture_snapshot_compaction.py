"""A small scoped delta must not retain another course's deleted SQLite pages."""
import sqlite3
import tempfile
from pathlib import Path
import unittest

from src.data.database import Database
from scripts.production_db import lecture_snapshot


class SnapshotCompactionTests(unittest.TestCase):
    def test_other_course_pages_are_reclaimed_without_losing_target_or_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=Database(str(root/'source.db'))
            try:
                db.upsert_course('1','目标','');db.upsert_course('2','其他','')
                db.insert_lecture('11','1','目标','2026-10-21')
                db.insert_lecture('22','2','其他','2026-10-22')
                db.update_transcript('11','保留的完整转录')
                db.update_transcript('22','x'*(2*1024*1024))
                raw=lecture_snapshot(db,root/'delta.db','1','11')
                self.assertLess(len(raw),256*1024)
                with sqlite3.connect(root/'delta.db') as delta:
                    self.assertEqual(delta.execute('select sub_id,transcript from lectures').fetchall(),[('11','保留的完整转录')])
                    self.assertEqual(delta.execute('pragma freelist_count').fetchone()[0],0)
                self.assertEqual(len(db.get_lecture('22')['transcript']),2*1024*1024)
            finally:db.conn.close()
