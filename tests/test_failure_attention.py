import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.merge_db import merge_delta as merge
from src.data.database import Database


class FailureAttentionTests(unittest.TestCase):
    @staticmethod
    def _database(path: Path) -> Database:
        db = Database(str(path))
        db.upsert_course("course-1", "课程一", "教师")
        db.insert_lecture("lecture-1", "course-1", "第一讲", "2026-09-18")
        return db

    def test_third_failure_pauses_and_notice_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._database(Path(tmp) / "icourse.db")
            for _ in range(3):
                db.update_error("lecture-1", "summarize", "provider timeout")

            self.assertEqual(db.get_unprocessed_lectures("course-1"), [])
            attention = db.get_attention_lectures(["course-1"])
            self.assertEqual([row["sub_id"] for row in attention], ["lecture-1"])

            db.mark_failure_notified_batch(["lecture-1"])
            self.assertEqual(db.get_attention_lectures(["course-1"]), [])
            self.assertEqual(
                len(db.get_attention_lectures(
                    ["course-1"], only_unnotified=False
                )),
                1,
            )
            self.assertIsNotNone(
                db.get_lecture("lecture-1")["failure_notified_at"]
            )
            db.conn.close()

    def test_no_audio_failure_is_not_silently_marked_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._database(Path(tmp) / "icourse.db")
            for _ in range(3):
                db.update_error("lecture-1", "no_audio", "no audio stream")
            row = db.get_lecture("lecture-1")
            self.assertEqual(row["error_count"], 3)
            self.assertIsNone(row["processed_at"])
            self.assertEqual(len(db.get_attention_lectures(["course-1"])), 1)
            db.conn.close()

    def test_manual_retry_reset_survives_database_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote_path = Path(tmp) / "remote.db"
            local_path = Path(tmp) / "local.db"
            remote = self._database(remote_path)
            for _ in range(3):
                remote.update_error("lecture-1", "transcribe", "temporary")
            remote.mark_failure_notified_batch(["lecture-1"])
            remote.conn.close()
            shutil.copyfile(remote_path, local_path)

            local = Database(str(local_path))
            changed = local.retry_attention_lectures(["lecture-1"])
            self.assertEqual(changed, 1)
            local.update_error("lecture-1", "transcribe", "still temporary")
            local.conn.close()

            merge(str(local_path), str(remote_path))
            conn = sqlite3.connect(remote_path)
            row = conn.execute(
                """SELECT error_count, failure_notified_at, retry_generation
                   FROM lectures WHERE sub_id = 'lecture-1'"""
            ).fetchone()
            conn.close()
            self.assertEqual(row, (1, None, 1))

    def test_suppressed_lecture_is_scrubbed_and_never_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._database(Path(tmp) / "icourse.db")
            db.update_transcript("lecture-1", "private transcript")
            db.update_summary("lecture-1", "private summary", "test-model")
            db.conn.execute(
                """INSERT INTO ppt_pages
                   (sub_id, page_num, created_sec, text, ocr_status)
                   VALUES ('lecture-1', 1, 0, 'private OCR', 'done')"""
            )
            db.conn.commit()

            self.assertEqual(
                db.suppress_lectures("course-1", ["lecture-1"]), 1
            )
            row = db.get_lecture("lecture-1")
            self.assertIsNotNone(row["deleted_at"])
            self.assertIsNone(row["summary"])
            self.assertIsNone(row["transcript"])
            self.assertEqual(db.count_total_ppt_pages("lecture-1"), 0)
            self.assertEqual(db.get_unprocessed_lectures("course-1"), [])
            self.assertIn("lecture-1", db.get_processed_sub_ids("course-1"))
            db.conn.close()

    def test_suppression_wins_over_stale_summary_during_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote_path = Path(tmp) / "remote.db"
            local_path = Path(tmp) / "local.db"
            remote = self._database(remote_path)
            remote.update_transcript("lecture-1", "old transcript")
            remote.update_summary("lecture-1", "old summary", "old-model")
            remote.mark_processed("lecture-1")
            remote.conn.close()
            shutil.copyfile(remote_path, local_path)

            local = Database(str(local_path))
            local.suppress_lectures("course-1", ["lecture-1"])
            local.conn.close()
            merge(str(local_path), str(remote_path))

            conn = sqlite3.connect(remote_path)
            row = conn.execute(
                """SELECT summary, transcript, summary_model, emailed_at,
                          deleted_at
                   FROM lectures WHERE sub_id = 'lecture-1'"""
            ).fetchone()
            conn.close()
            self.assertEqual(row[:4], (None, None, None, None))
            self.assertIsNotNone(row[4])

    def test_remote_suppression_wins_over_stale_local_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote_path = Path(tmp) / "remote.db"
            local_path = Path(tmp) / "local.db"
            local = self._database(local_path)
            local.update_transcript("lecture-1", "stale transcript")
            local.update_summary("lecture-1", "stale summary", "old-model")
            local.mark_processed("lecture-1")
            local.conn.close()
            shutil.copyfile(local_path, remote_path)

            remote = Database(str(remote_path))
            remote.suppress_lectures("course-1", ["lecture-1"])
            remote.conn.close()
            merge(str(local_path), str(remote_path))

            conn = sqlite3.connect(remote_path)
            row = conn.execute(
                """SELECT summary, transcript, summary_model, deleted_at
                   FROM lectures WHERE sub_id = 'lecture-1'"""
            ).fetchone()
            conn.close()
            self.assertEqual(row[:3], (None, None, None))
            self.assertIsNotNone(row[3])


if __name__ == "__main__":
    unittest.main()
