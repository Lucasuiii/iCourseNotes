"""SQLite storage for tracking courses and lectures."""

import os
import sqlite3
import threading
from datetime import datetime, timezone

from src.runtime import config
from src.data.schema import (
    LECTURES_MIGRATION_COLUMNS,
    PPT_PAGES_MIGRATION_COLUMNS,
    SCHEMA_SQL,
)


class Database:
    """SQLite database for course and lecture tracking.

    Thread safety: ``check_same_thread=False`` lets OCR worker threads share
    one connection.  The plain sqlite3 wrapper is *not* internally
    thread-safe at the cursor level, and since the PPT prefetch path runs
    OCR (and now stage-1/2 registration) in background threads *while* the
    main thread keeps writing lecture rows, every method that touches
    ``self.conn`` — reads included — takes ``self._lock`` (an RLock).  The
    per-call cost is negligible next to the SQL itself.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            self.conn.close()
            raise RuntimeError("SQLite foreign-key enforcement unavailable")
        # WAL gets readers (the frontend's read-only export step, or the
        # status badge in long runs) off the writer's lock chain, so the
        # nightly run's many small writes don't block ad-hoc reads.  Pair
        # with NORMAL sync for ~3-4x write throughput vs FULL — safe enough
        # given the workflow re-encrypts + uploads after the run completes.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_tables()

    def _init_tables(self):
        with self._lock, self.conn:
            self.conn.executescript(SCHEMA_SQL)

            existing_lectures = {
                row[1]
                for row in self.conn.execute("PRAGMA table_info(lectures)").fetchall()
            }
            for col, typedef in LECTURES_MIGRATION_COLUMNS:
                if col not in existing_lectures:
                    self.conn.execute(
                        f"ALTER TABLE lectures ADD COLUMN {col} {typedef}"
                    )

            existing_ppt = {
                row[1]
                for row in self.conn.execute(
                    "PRAGMA table_info(ppt_pages)"
                ).fetchall()
            }
            for col, typedef in PPT_PAGES_MIGRATION_COLUMNS:
                if col not in existing_ppt:
                    self.conn.execute(
                        f"ALTER TABLE ppt_pages ADD COLUMN {col} {typedef}"
                    )

    def write_meta(self, key: str, value: str):
        """Persist a key-value pair (e.g. COURSE_IDS from CI secret)."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def read_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,),
            ).fetchone()
        return row["value"] if row else None

    def read_meta_prefix(self, prefix: str) -> list[str]:
        with self._lock:
            rows=self.conn.execute('SELECT value FROM meta WHERE substr(key,1,?)=? ORDER BY key',
                                   (len(prefix),prefix)).fetchall()
        return [row['value'] for row in rows]

    def upsert_course(self, course_id: str, title: str, teacher: str):
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO courses (course_id, title, teacher)
                   VALUES (?, ?, ?)
                   ON CONFLICT(course_id) DO UPDATE SET
                       title=excluded.title, teacher=excluded.teacher""",
                (course_id, title, teacher),
            )

    def upsert_all_courses_for_term(self, term: str,
                                    rows: list[dict]) -> tuple[int, int]:
        """Replace the catalog of courses for ``term``.

        Each row in ``rows`` must have at minimum ``course_id``; ``title``,
        ``teacher``, ``dept`` are optional.  Rows with course_ids missing
        from the new list are DELETE-d for this term (so dropped courses
        disappear from the frontend picker).

        Returns ``(deleted, upserted)``.

        We delete-then-upsert under one transaction so the term's catalog
        is never half-empty during a concurrent frontend export.
        """
        now = datetime.now(timezone.utc).isoformat()
        keep_ids = {str(r["course_id"]) for r in rows if r.get("course_id")}
        with self._lock, self.conn:
            if keep_ids:
                placeholders = ",".join("?" * len(keep_ids))
                cur = self.conn.execute(
                    f"""DELETE FROM all_courses
                        WHERE term = ?
                          AND course_id NOT IN ({placeholders})""",
                    [term, *keep_ids],
                )
            else:
                cur = self.conn.execute(
                    "DELETE FROM all_courses WHERE term = ?", (term,),
                )
            deleted = cur.rowcount or 0
            upserted = 0
            for r in rows:
                cid = r.get("course_id")
                if not cid:
                    continue
                self.conn.execute(
                    """INSERT INTO all_courses
                          (course_id, term, title, teacher, dept, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(course_id, term) DO UPDATE SET
                          title=excluded.title,
                          teacher=excluded.teacher,
                          dept=excluded.dept,
                          last_seen_at=excluded.last_seen_at""",
                    (str(cid), term,
                     r.get("title"), r.get("teacher"), r.get("dept"), now),
                )
                upserted += 1
        return deleted, upserted

    def list_all_courses(self, term: str | None = None) -> list[dict]:
        """Return the full course catalog, optionally scoped to a term.

        Ordered by term DESC, then title — newest semester first so the
        frontend picker shows current-term courses on top.
        """
        if term is None:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT * FROM all_courses ORDER BY term DESC, title"
                ).fetchall()
        else:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT * FROM all_courses WHERE term = ? ORDER BY title",
                    (term,),
                ).fetchall()
        return [dict(r) for r in rows]

    def list_catalog_terms(self) -> set[str]:
        """Return stored semester names without loading the course catalog."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT term FROM all_courses"
            ).fetchall()
        return {row["term"] for row in rows}

    def insert_lecture(
        self, sub_id: str, course_id: str, sub_title: str, date: str
    ) -> bool:
        """Insert a new lecture. Returns True if inserted, False if already exists."""
        with self._lock, self.conn:
            cursor = self.conn.execute(
                """INSERT INTO lectures (sub_id, course_id, sub_title, date)
                   VALUES (?, ?, ?, ?) ON CONFLICT(sub_id) DO NOTHING""",
                (sub_id, course_id, sub_title, date),
            )
        return cursor.rowcount == 1

    def get_processed_sub_ids(self, course_id: str) -> set[str]:
        """Return sub_ids that have been fully processed."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT sub_id FROM lectures WHERE course_id = ? "
                "AND (processed_at IS NOT NULL OR deleted_at IS NOT NULL)",
                (course_id,),
            ).fetchall()
        return {row["sub_id"] for row in rows}

    def get_unprocessed_lectures(self, course_id: str | None = None,
                                  max_errors: int = 3) -> list[dict]:
        """Return lectures that need (re-)processing.

        Only returns lectures whose ``error_count`` is below *max_errors* —
        a repeatedly-failing lecture is paused after that many attempts and
        surfaced through the attention notification path rather than
        clogging every workflow run or disappearing silently.
        """
        query = (
            "SELECT * FROM lectures"
            " WHERE processed_at IS NULL"
            "   AND deleted_at IS NULL"
            "   AND (error_count IS NULL OR error_count < ?)"
        )
        params: tuple = (max_errors,)
        if course_id:
            query += " AND course_id = ?"
            params = (max_errors, course_id)
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_transcript(self, sub_id: str, transcript: str):
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE lectures SET transcript = ? WHERE sub_id = ?",
                (transcript, sub_id),
            )

    def clear_transcript(self, sub_id: str):
        """Discard an unusable ASR result so a later run transcribes again."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE lectures SET transcript = NULL WHERE sub_id = ?",
                (sub_id,),
            )

    def mark_processed(self, sub_id: str):
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE lectures SET processed_at = ? WHERE sub_id = ?",
                (datetime.now(timezone.utc).isoformat(), sub_id),
            )

    def mark_emailed(self, sub_id: str):
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE lectures SET emailed_at = ? WHERE sub_id = ?",
                (datetime.now(timezone.utc).isoformat(), sub_id),
            )

    def mark_emailed_batch(self, sub_ids: list[str]):
        """Mark multiple lectures as emailed in a single transaction."""
        if not sub_ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.conn:
            self.conn.executemany(
                "UPDATE lectures SET emailed_at = ? WHERE sub_id = ?",
                [(now, sid) for sid in sub_ids],
            )

    def update_error(self, sub_id: str, stage: str, error_msg: str):
        """Record a processing error for a lecture."""
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE lectures
                   SET error_stage = ?, error_msg = ?,
                       error_count = COALESCE(error_count, 0) + 1
                   WHERE sub_id = ?""",
                (stage, error_msg, sub_id),
            )

    def clear_error(self, sub_id: str):
        """Clear error state after successful processing."""
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE lectures
                   SET error_stage = NULL, error_msg = NULL, error_count = 0,
                       failure_notified_at = NULL
                   WHERE sub_id = ?""",
                (sub_id,),
            )

    def get_attention_lectures(
        self, course_ids: list[str] | None = None, max_errors: int = 3,
        only_unnotified: bool = True,
    ) -> list[dict]:
        """Return paused failures, optionally limited to unsent notices."""
        query = (
            "SELECT l.*, c.title AS course_title, c.teacher "
            "FROM lectures l JOIN courses c ON l.course_id = c.course_id "
            "WHERE l.processed_at IS NULL "
            "AND l.deleted_at IS NULL "
            "AND COALESCE(l.error_count, 0) >= ?"
        )
        params: list[object] = [max_errors]
        if only_unnotified:
            query += " AND l.failure_notified_at IS NULL"
        if course_ids:
            placeholders = ",".join("?" for _ in course_ids)
            query += f" AND l.course_id IN ({placeholders})"
            params.extend(str(cid) for cid in course_ids)
        query += " ORDER BY l.date, l.sub_id"
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def mark_failure_notified_batch(self, sub_ids: list[str]) -> None:
        """Record a successfully delivered failure notice."""
        if not sub_ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.conn:
            self.conn.executemany(
                """UPDATE lectures
                   SET failure_notified_at = ?
                   WHERE sub_id = ? AND processed_at IS NULL
                     AND COALESCE(error_count, 0) >= 3""",
                [(now, sub_id) for sub_id in sub_ids],
            )

    def retry_attention_lectures(self, sub_ids: list[str]) -> int:
        """Re-arm selected paused failures for an immediate manual retry.

        ``retry_generation`` makes the reset survive the additive database
        merge used by the deployment workflow.
        """
        if not sub_ids:
            return 0
        placeholders = ",".join("?" for _ in sub_ids)
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"""UPDATE lectures
                    SET error_stage = NULL, error_msg = NULL, error_count = 0,
                        failure_notified_at = NULL,
                        retry_generation = COALESCE(retry_generation, 0) + 1
                    WHERE processed_at IS NULL
                      AND COALESCE(error_count, 0) >= 3
                      AND sub_id IN ({placeholders})""",
                [str(sub_id) for sub_id in sub_ids],
            )
        return cur.rowcount or 0

    def delete_course(self, course_id: str) -> int:
        """Delete course content and its lecture review/figure state atomically."""
        course_id = str(course_id)
        with self._lock, self.conn:
            for prefix in ("summary_review:", "summary_figures:"):
                self.conn.execute(
                    """DELETE FROM meta WHERE key IN (
                        SELECT ? || sub_id FROM lectures WHERE course_id = ?
                    )""", (prefix, course_id),
                )
            self.conn.execute(
                """DELETE FROM ppt_pages WHERE sub_id IN (
                    SELECT sub_id FROM lectures WHERE course_id = ?
                )""", (course_id,),
            )
            deleted = self.conn.execute(
                "DELETE FROM lectures WHERE course_id = ?", (course_id,),
            ).rowcount
            self.conn.execute("DELETE FROM courses WHERE course_id = ?", (course_id,))
        return deleted

    def suppress_lectures(self, course_id: str, sub_ids: list[str]) -> int:
        """Erase selected lecture content and keep persistent tombstones.

        Keeping the row (with ``deleted_at`` set) prevents the next catalog
        sync from treating the same iCourse sub_id as a brand-new lecture.
        """
        ids = list(dict.fromkeys(str(sub_id) for sub_id in sub_ids if sub_id))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.conn:
            found = self.conn.execute(
                f"""SELECT COUNT(*) FROM lectures
                    WHERE course_id = ? AND sub_id IN ({placeholders})""",
                [str(course_id), *ids],
            ).fetchone()[0]
            if found != len(ids):
                raise ValueError("one or more selected lectures do not exist")
            self.conn.execute(
                f"DELETE FROM ppt_pages WHERE sub_id IN ({placeholders})",
                ids,
            )
            self.conn.execute(f"DELETE FROM meta WHERE key IN (SELECT 'summary_figures:' || sub_id FROM lectures WHERE course_id=? AND sub_id IN ({placeholders}))", [str(course_id), *ids])
            self.conn.execute(f"DELETE FROM meta WHERE key IN (SELECT 'summary_review:' || sub_id FROM lectures WHERE course_id=? AND sub_id IN ({placeholders}))", [str(course_id), *ids])
            cur = self.conn.execute(
                f"""UPDATE lectures SET
                        transcript = NULL, summary = NULL,
                        summary_model = NULL, emailed_at = NULL,
                        error_msg = NULL, error_count = 0,
                        error_stage = NULL, failure_notified_at = NULL,
                        processed_at = ?, deleted_at = ?
                    WHERE course_id = ? AND sub_id IN ({placeholders})""",
                [now, now, str(course_id), *ids],
            )
        return cur.rowcount or 0

    def update_ppt_page(self, sub_id: str, page_num: int,
                        text: str | None, status: str):
        """Mark a page's OCR result.

        ``status`` is free-form (no CHECK constraint); the pipeline uses
        'done' | 'failed' | 'dedup_dropped' | 'invalid'. ``get_done_ppt_pages``
        only surfaces 'done', so dropped/invalid pages naturally vanish from
        the prompt.

        Thread-safe: the OCR pool workers all hit this method, so the write
        is serialised on ``self._lock``.
        """
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE ppt_pages
                   SET text = ?, ocr_status = ?, ocr_at = ?
                   WHERE sub_id = ? AND page_num = ?""",
                (text, status, datetime.now(timezone.utc).isoformat(), sub_id, page_num),
            )

    def update_ppt_page_dhash(self, sub_id: str, page_num: int,
                              dhash: str | None):
        """Record the perceptual hash of a page; status is left untouched.

        Called between download and OCR so the dedup pass has dhashes for
        every successfully-downloaded page in one place. ``dhash`` may be
        None when image decode fails (treated as 'no dedup signal').

        Locked even though only the main thread normally writes here, so
        a future caller from another thread doesn't silently race.
        """
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE ppt_pages SET dhash = ? WHERE sub_id = ? AND page_num = ?",
                (dhash, sub_id, page_num),
            )

    def insert_ppt_pages_pending(self, sub_id: str, items: list[dict]) -> int:
        """Bulk-insert PPT page rows with status='pending'.

        items: list of {page_num, created_sec, pptimgurl}.
        Existing rows are left untouched (INSERT OR IGNORE), so this is safe
        to call repeatedly across reruns and across concurrent workers.
        Returns number of newly inserted rows.
        """
        if not items:
            return 0
        with self._lock, self.conn:
            cur = self.conn.executemany(
                """INSERT OR IGNORE INTO ppt_pages
                       (sub_id, page_num, created_sec, pptimgurl, ocr_status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                [
                    (sub_id, int(it["page_num"]), int(it["created_sec"]),
                     it.get("pptimgurl"))
                    for it in items
                ],
            )
            return cur.rowcount or 0

    def get_pending_ppt_pages(self, sub_id: str) -> list[dict]:
        """Pages still awaiting OCR. Workers claim via update_ppt_page."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT page_num, created_sec, pptimgurl, dhash
                   FROM ppt_pages
                   WHERE sub_id = ? AND ocr_status = 'pending'
                   ORDER BY created_sec""",
                (sub_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_done_ppt_pages(self, sub_id: str) -> list[dict]:
        """Successfully-OCR'd pages, sorted by time. Used by the bucketer."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT page_num, created_sec, text, pptimgurl
                   FROM ppt_pages
                   WHERE sub_id = ? AND ocr_status = 'done'
                     AND text IS NOT NULL AND text != ''
                   ORDER BY created_sec""",
                (sub_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_pending_ppt_pages(self, sub_id: str) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM ppt_pages "
                "WHERE sub_id = ? AND ocr_status = 'pending'",
                (sub_id,),
            ).fetchone()[0]

    def count_total_ppt_pages(self, sub_id: str) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM ppt_pages WHERE sub_id = ?",
                (sub_id,),
            ).fetchone()[0]

    def get_max_ppt_created_sec(self, sub_id: str) -> int:
        """Timestamp (sec offset) of the lecture's last PPT screenshot.

        Screenshots are taken every 20-30 s throughout the recording, so
        this is a cheap lower bound on the lecture duration — used to
        detect tail-truncated official transcripts.  Returns 0 when the
        lecture has no registered pages.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(created_sec) FROM ppt_pages WHERE sub_id = ?",
                (sub_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_ppt_status_counts(self, sub_id: str) -> dict[str, int]:
        """Return persisted PPT status counts for content-quality checks."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT ocr_status, COUNT(*) AS count
                   FROM ppt_pages WHERE sub_id = ? GROUP BY ocr_status""",
                (sub_id,),
            ).fetchall()
        return {str(row["ocr_status"]): int(row["count"]) for row in rows}

    def update_summary(self, sub_id: str, summary: str, model: str):
        """Save summary and model name."""
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE lectures
                   SET summary = ?, summary_model = ?
                   WHERE sub_id = ? AND deleted_at IS NULL""",
                (summary, model, sub_id),
            )

    def get_lecture(self, sub_id: str) -> dict | None:
        """Get a single lecture row by sub_id."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM lectures WHERE sub_id = ?", (sub_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_unsent_lectures(self) -> list[dict]:
        """Find lectures that are processed but not yet emailed."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.*, c.title AS course_title, c.teacher
                   FROM lectures l
                   JOIN courses c ON l.course_id = c.course_id
                   WHERE l.processed_at IS NOT NULL
                     AND l.deleted_at IS NULL
                     AND l.emailed_at IS NULL
                     AND l.summary IS NOT NULL""",
            ).fetchall()
        return [dict(row) for row in rows]

    def sync_dates_from_sub(self) -> int:
        """Fix date rows where the stored value is wrong or badly formatted.

        sub_title format is "2026-03-05第6-8节"; the embedded date is the
        real class date.  Also corrects zero-padding so that SQLite ORDER BY
        date works correctly (e.g. "2026-3-2" → "2026-03-02").
        Returns number of rows corrected.
        """
        with self._lock, self.conn:
            cur = self.conn.execute(r"""
                UPDATE lectures
                SET date = substr(sub_title, 1, 10)
                WHERE sub_title GLOB '????-??-??*'
                  AND date != substr(sub_title, 1, 10)
            """)
            return cur.rowcount or 0
