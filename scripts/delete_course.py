#!/usr/bin/env python3
"""Delete one course, including its lecture review and figure metadata."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.database import Database


def main() -> int:
    if len(sys.argv) != 3 or not sys.argv[2]:
        print("usage: delete_course.py DATABASE COURSE_ID", file=sys.stderr)
        return 2
    db = Database(sys.argv[1])
    try:
        count = db.delete_course(sys.argv[2])
    finally:
        db.conn.close()
    print(f"Removed {count} lecture(s) from selected course.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
