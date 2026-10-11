"""Guarded, runner-local reset and verification for an exact-date rerun.

The old database stays in the data branch until every selected lecture has
been regenerated and emailed.  The local snapshot is never uploaded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import date
from pathlib import Path


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def _targets(conn: sqlite3.Connection, run_date: str, course_ids: set[str]) -> list[dict]:
    placeholders = ",".join("?" for _ in course_ids)
    rows = conn.execute(
        f"""SELECT sub_id, course_id, date, summary, transcript,
                   processed_at, emailed_at, deleted_at
            FROM lectures
            WHERE date = ? AND course_id IN ({placeholders})
            ORDER BY course_id, sub_id""",
        [run_date, *sorted(course_ids)],
    ).fetchall()
    return [dict(row) for row in rows]


def prepare(db_path: Path, manifest_path: Path, run_date: str,
            expected_count: int, course_ids_raw: str,
            github_env: Path | None = None) -> int:
    if date.fromisoformat(run_date).isoformat() != run_date:
        raise ValueError("rerun date must be YYYY-MM-DD")
    if not 1 <= expected_count <= 20:
        raise ValueError("expected count must be between 1 and 20")
    course_ids = {part.strip() for part in course_ids_raw.split(",") if part.strip()}
    if not course_ids or any(not _ID_RE.fullmatch(part) for part in course_ids):
        raise ValueError("configured course IDs are missing or invalid")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = _targets(conn, run_date, course_ids)
        if len(rows) != expected_count:
            raise ValueError("date rerun target count differs from expected count")
        if any(
            row["deleted_at"] or not row["summary"] or not row["transcript"]
            or not row["processed_at"] or not row["emailed_at"]
            for row in rows
        ):
            raise ValueError("date rerun includes a deleted or incomplete lecture")
        if any(not _ID_RE.fullmatch(row["sub_id"]) for row in rows):
            raise ValueError("date rerun includes an invalid lecture ID")

        # Preserve a local, plaintext snapshot only inside the ephemeral CI
        # runner.  It is neither committed nor uploaded as an artifact.
        snapshot_path = db_path.with_suffix(".pre-rerun.db")
        with sqlite3.connect(snapshot_path) as snapshot:
            conn.backup(snapshot)

        ids = [row["sub_id"] for row in rows]
        with conn:
            # This is an explicitly authorized new run, not failure recovery.
            # Its previous review/figure requests remain in the local backup.
            conn.executemany(
                "DELETE FROM meta WHERE key = ?",
                [(prefix + sub_id,) for sub_id in ids
                 for prefix in ("summary_review:", "summary_figures:")],
            )
            conn.executemany(
                """UPDATE lectures SET
                       transcript = NULL, summary = NULL, summary_model = NULL,
                       processed_at = NULL, emailed_at = NULL,
                       error_msg = NULL, error_stage = NULL, error_count = 0,
                       failure_notified_at = NULL
                   WHERE sub_id = ? AND deleted_at IS NULL""",
                [(sub_id,) for sub_id in ids],
            )

        manifest_path.write_text(json.dumps({
            "date": run_date,
            "ids": ids,
            "old_emailed_at": {row["sub_id"]: row["emailed_at"] for row in rows},
        }), encoding="utf-8")
        if github_env is not None:
            with github_env.open("a", encoding="utf-8") as out:
                out.write(f"RERUN_TARGET_IDS={','.join(ids)}\n")
        print(f"Prepared {len(ids)} exact-date lecture(s) for rerun.")
        return len(ids)
    finally:
        conn.close()


def verify(db_path: Path, manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ids = manifest["ids"]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("rerun manifest is empty or duplicated")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for sub_id in ids:
            row = conn.execute(
                """SELECT date, transcript, summary, summary_model,
                          processed_at, emailed_at, deleted_at, error_count
                   FROM lectures WHERE sub_id = ?""",
                (sub_id,),
            ).fetchone()
            if not row or row["date"] != manifest["date"]:
                raise ValueError("rerun target missing or date changed")
            if (row["deleted_at"] or not row["transcript"] or not row["summary"]
                or not row["summary_model"] or not row["processed_at"]
                or not row["emailed_at"] or row["emailed_at"] ==
                    manifest["old_emailed_at"][sub_id]
                or row["error_count"]):
                raise ValueError("rerun did not complete and deliver every lecture")
        print(f"Verified {len(ids)} regenerated and emailed lecture(s).")
        return len(ids)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("--db", type=Path, default=Path("data/icourse.db"))
    parser.add_argument("--manifest", type=Path,
                        default=Path("data/rerun-targets.json"))
    parser.add_argument("--date")
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args()
    try:
        if args.mode == "prepare":
            if not args.date or args.expected_count is None:
                raise ValueError("rerun date and expected count are required")
            prepare(
                args.db, args.manifest, args.date, args.expected_count,
                os.environ.get("COURSE_IDS", ""),
                Path(os.environ["GITHUB_ENV"]) if os.environ.get("GITHUB_ENV") else None,
            )
        else:
            verify(args.db, args.manifest)
        return 0
    except (KeyError, ValueError, sqlite3.Error, OSError) as exc:
        # The public workflow log must not contain course labels or raw data.
        print(f"[Rerun] Guard failed: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
