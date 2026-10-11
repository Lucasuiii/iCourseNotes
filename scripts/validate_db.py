#!/usr/bin/env python3
"""Fail closed unless an iCourse SQLite database is complete and readable."""

from __future__ import annotations

import os
import sqlite3
import sys

# The CLI runs from scripts/ while validation now imports pipeline modules.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


REQUIRED_TABLES = {"courses", "lectures", "ppt_pages", "all_courses", "meta"}


def validate_database(path: str) -> None:
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ValueError("database file is missing or empty")

    # Read-only mode prevents validation from silently creating or repairing a
    # broken file.  query_only is an additional guard against accidental writes.
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise ValueError("SQLite integrity_check failed")
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError("database is missing required tables")
        from src.pipeline.summary_figures import FIGURE_REF, validate_assets
        import json
        from src.pipeline.summary_review import validate_published
        for key, raw in conn.execute("SELECT key,value FROM meta WHERE key GLOB 'summary_review:*'"):
            sid = key.partition(':')[2]
            state = json.loads(raw)
            lesson = conn.execute('SELECT course_id,summary,date FROM lectures WHERE sub_id=?', (sid,)).fetchone()
            if not lesson or state.get('course_id') != str(lesson[0]) or state.get('sub_id') != sid:
                raise ValueError('Summary review crosses course scope')
            if lesson[1]:
                validate_published(state, lesson[0], sid, lesson[1], lesson[2])
        for key, raw in conn.execute("SELECT key,value FROM meta WHERE key GLOB 'summary_figures:*'"):
            sid = key.partition(':')[2]; state = json.loads(raw)
            validate_assets(state, sid)
            lesson = conn.execute('SELECT course_id,summary,date FROM lectures WHERE sub_id=?', (sid,)).fetchone()
            if lesson and str(lesson[0]) != state.get('course_id'):
                raise ValueError('Figure assets cross course scope')
            if lesson and lesson[1]:
                refs = [m[2] for m in FIGURE_REF.finditer(lesson[1])]
                if sorted(refs) != sorted(f['id'] for f in state['figures']):
                    raise ValueError('Figure references do not match assets')
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} DATABASE", file=sys.stderr)
        return 2
    try:
        validate_database(sys.argv[1])
    except (OSError, sqlite3.Error, ValueError) as exc:
        # Do not echo paths or database contents into a public Actions log.
        print(f"database validation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("Database integrity check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
