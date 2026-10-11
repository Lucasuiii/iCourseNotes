"""Offline publication checks against a frozen pre-run SQLite snapshot."""
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from scripts.merge_db import merge, MergeConflict, snapshot_baseline
from src.data.database import Database


class BaselineMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.before, self.local, self.remote = [root/name for name in ('before.db', 'local.db', 'remote.db')]
        db = Database(str(self.before))
        with db.conn:
            db.conn.execute("INSERT INTO courses VALUES ('10','Course','Teacher')")
            for sid in ('1', '2'):
                db.conn.execute("INSERT INTO lectures(sub_id,course_id,sub_title,date,summary,transcript,processed_at) VALUES (?,'10','Lesson','2026-10-01','old summary','old transcript','old time')", (sid,))
                db.conn.execute("INSERT INTO meta VALUES (?, 'old review')", ('summary_review:'+sid,))
        db.conn.close()
        for path in (self.local, self.remote): shutil.copyfile(self.before, path)

    def update(self, path, sid, summary):
        with sqlite3.connect(path) as db:
            db.execute('UPDATE lectures SET summary=?,transcript=?,summary_model=?,retry_generation=retry_generation+1 WHERE sub_id=?', (summary, summary+' transcript', summary+' model', sid))
            db.execute('UPDATE meta SET value=? WHERE key=?', (summary+' review', 'summary_review:'+sid))

    def test_unmodified_local_history_preserves_remote_replacement_and_new_lesson(self):
        self.update(self.remote, '1', 'approved remote')
        self.update(self.local, '2', 'local result')
        with sqlite3.connect(self.remote) as db:
            db.execute("INSERT INTO lectures(sub_id,course_id,sub_title) VALUES ('3','10','New')")
        merge(str(self.local), str(self.remote), str(self.before))
        with sqlite3.connect(self.remote) as db:
            self.assertEqual(db.execute('SELECT sub_id,summary FROM lectures ORDER BY sub_id').fetchall(), [('1','approved remote'), ('2','local result'), ('3',None)])
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='summary_review:1'").fetchone()[0], 'approved remote review')
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='summary_review:2'").fetchone()[0], 'local result review')

    def test_dual_changes_conflict_before_any_rows_are_written(self):
        self.update(self.local, '1', 'local'); self.update(self.remote, '1', 'remote')
        self.update(self.local, '2', 'must not write')
        old = self.remote.read_bytes()
        with self.assertRaises(MergeConflict): merge(str(self.local), str(self.remote), str(self.before))
        self.assertEqual(self.remote.read_bytes(), old)

    def test_scope_metadata_change_cannot_attach_to_concurrently_changed_summary(self):
        self.update(self.remote, '1', 'remote')
        with sqlite3.connect(self.local) as db:
            db.execute("UPDATE meta SET value='local cache' WHERE key='summary_review:1'")
        with self.assertRaises(MergeConflict): merge(str(self.local), str(self.remote), str(self.before))

    def test_remote_hard_delete_is_not_resurrected_by_unchanged_local(self):
        with sqlite3.connect(self.remote) as db:
            db.execute("DELETE FROM lectures WHERE sub_id='1'")
            db.execute("DELETE FROM meta WHERE key='summary_review:1'")
        self.update(self.local, '2', 'new')
        merge(str(self.local), str(self.remote), str(self.before))
        with sqlite3.connect(self.remote) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM lectures WHERE sub_id='1'").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='summary_review:1'").fetchone())

    def test_local_cache_deletion_and_identical_updates_are_applied(self):
        for path in (self.remote, self.local): self.update(path, '1', 'same')
        with sqlite3.connect(self.local) as db:
            db.execute("DELETE FROM meta WHERE key='summary_review:2'")
        merge(str(self.local), str(self.remote), str(self.before))
        with sqlite3.connect(self.remote) as db:
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='summary_review:2'").fetchone())
        merge(str(self.local), str(self.remote), str(self.before))

    def test_wal_snapshot_and_missing_baseline(self):
        target = self.before.with_name('snapshot.db')
        with sqlite3.connect(self.local) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute("UPDATE lectures SET summary='committed WAL' WHERE sub_id='1'"); db.commit()
            snapshot_baseline(str(self.local), str(target))
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute("SELECT summary FROM lectures WHERE sub_id='1'").fetchone()[0], 'committed WAL')
        with self.assertRaises(ValueError): merge(str(self.local), str(self.remote), str(target)+'missing')

    def test_workflow_uses_pre_run_baseline_and_same_publish_parent(self):
        workflow = (Path(__file__).resolve().parents[1]/'.github/workflows/single_run.yml').read_text()
        self.assertLess(workflow.index('snapshot_baseline'), workflow.index('- name: Prepare exact-date rerun'))
        self.assertIn('--baseline data/baseline.db', workflow)
        self.assertIn('git fetch -q --depth=1 origin "$remote_revision"', workflow)
        self.assertNotIn('git fetch -q --depth=1 origin data', workflow)

    def test_missing_snapshot_is_not_silently_created_as_an_empty_database(self):
        with self.assertRaisesRegex(ValueError, 'baseline'):
            merge(str(self.local), str(self.remote))
        missing = self.remote.with_name('missing.db')
        with self.assertRaises(ValueError): merge(str(missing), str(self.remote), str(self.before))
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError): snapshot_baseline(str(missing), str(self.before))
