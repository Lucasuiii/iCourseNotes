import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.rerun_date import prepare, verify
from src.data.database import Database
from src.pipeline import summary_review


class RerunDateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "icourse.db"
        self.manifest = Path(self.tmp.name) / "targets.json"
        db = Database(str(self.db_path))
        for course_id, sub_id, run_date in (
            ("course-a", "sub-a", "2026-09-22"),
            ("course-b", "sub-b", "2026-09-22"),
            ("course-a", "other-day", "2026-09-21"),
        ):
            db.upsert_course(course_id, course_id, "teacher")
            db.insert_lecture(sub_id, course_id, sub_id, run_date)
            db.update_transcript(sub_id, "original transcript")
            db.update_summary(sub_id, "original summary", "old/model")
            db.mark_processed(sub_id)
            db.mark_emailed(sub_id)
        db.conn.close()

    def test_prepare_and_verify_only_exact_date(self):
        env_path = Path(self.tmp.name) / "github-env"
        prepare(self.db_path, self.manifest, "2026-09-22", 2,
                "course-a,course-b", env_path)
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(set(manifest["ids"]), {"sub-a", "sub-b"})
        self.assertEqual(env_path.read_text().strip(),
                         "RERUN_TARGET_IDS=sub-a,sub-b")
        self.assertTrue(self.db_path.with_suffix(".pre-rerun.db").exists())

        with sqlite3.connect(self.db_path) as conn:
            untouched = conn.execute(
                "SELECT summary FROM lectures WHERE sub_id = 'other-day'"
            ).fetchone()[0]
            self.assertEqual(untouched, "original summary")
            for sub_id in manifest["ids"]:
                row = conn.execute(
                    "SELECT transcript, summary, processed_at, emailed_at "
                    "FROM lectures WHERE sub_id = ?", (sub_id,)
                ).fetchone()
                self.assertEqual(row, (None, None, None, None))

        with self.assertRaises(ValueError):
            verify(self.db_path, self.manifest)

        with sqlite3.connect(self.db_path) as conn:
            for sub_id in manifest["ids"]:
                conn.execute(
                    """UPDATE lectures SET transcript = 'new transcript',
                       summary = 'new summary', summary_model = 'new/model',
                       processed_at = '2026-09-23T02:00:00',
                       emailed_at = '2026-09-23T02:01:00'
                       WHERE sub_id = ?""", (sub_id,),
                )
        self.assertEqual(verify(self.db_path, self.manifest), 2)

    def test_wrong_count_does_not_reset_database(self):
        with self.assertRaises(ValueError):
            prepare(self.db_path, self.manifest, "2026-09-22", 3,
                    "course-a,course-b")
        self.assertFalse(self.manifest.exists())
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT summary FROM lectures WHERE sub_id = 'sub-a'"
            ).fetchone()[0], "original summary")

    def test_explicit_rerun_regenerates_same_and_changed_material_and_keeps_backup(self):
        for material in ("original material", "changed material"):
            with self.subTest(material=material):
                db = Database(str(self.db_path))
                self.addCleanup(db.conn.close)
                db.update_summary("sub-a", "original summary", "old/model")
                db.update_transcript("sub-a", "original transcript")
                db.mark_processed("sub-a")
                db.mark_emailed("sub-a")
                db.write_meta("summary_review:sub-a", "")
                old = Mock(return_value=("old reviewed summary", "old/model", []))
                summary_review.draft(db, "course-a", "sub-a", "original material", old)
                for sub in ("sub-a", "other-day", "sub-b"):
                    db.write_meta("summary_figures:" + sub, "old figure state")
                db.write_meta("summary_review:other-day", "unselected review state")
                prepare(self.db_path, self.manifest, "2026-09-22", 1, "course-a")
                self.assertIsNone(db.read_meta("summary_review:sub-a"))
                self.assertIsNone(db.read_meta("summary_figures:sub-a"))
                self.assertEqual(db.read_meta("summary_review:other-day"), "unselected review state")
                self.assertEqual(db.read_meta("summary_figures:other-day"), "old figure state")
                self.assertEqual(db.read_meta("summary_figures:sub-b"), "old figure state")
                with sqlite3.connect(self.db_path.with_suffix(".pre-rerun.db")) as snapshot:
                    state = json.loads(snapshot.execute(
                        "SELECT value FROM meta WHERE key='summary_review:sub-a'"
                    ).fetchone()[0])
                    self.assertEqual(state["draft_summary"], "old reviewed summary")
                    self.assertEqual(snapshot.execute(
                        "SELECT value FROM meta WHERE key='summary_figures:sub-a'"
                    ).fetchone()[0], "old figure state")
                generate = Mock(return_value=("new summary", "new/model", []))
                text, _, _, state = summary_review.draft(db, "course-a", "sub-a", material, generate)
                self.assertEqual(text, "new summary")
                self.assertEqual(state["material_sha256"], summary_review.digest(material))
                generate.assert_called_once()
                # Ordinary resume still reuses this new draft without another request.
                summary_review.draft(db, "course-a", "sub-a", material, generate)
                generate.assert_called_once()

    def test_incomplete_target_is_rejected_before_reset(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE lectures SET emailed_at = NULL WHERE sub_id = 'sub-b'")
        with self.assertRaises(ValueError):
            prepare(self.db_path, self.manifest, "2026-09-22", 2,
                    "course-a,course-b")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT summary FROM lectures WHERE sub_id = 'sub-a'"
            ).fetchone()[0], "original summary")


if __name__ == "__main__":
    unittest.main()
