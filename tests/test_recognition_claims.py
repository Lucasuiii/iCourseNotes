"""Local recognition claims: expiry, merge precedence and the daily cloud filter."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.merge_db import merge_delta
from scripts.production.planning import daily_filter
from src.data.database import Database
from src.runtime import local_claims

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def task(course, sub):
    return (course, 'Course', {'sub_id': sub, 'date': '2026-10-30'})


class ClaimTests(unittest.TestCase):
    def test_claim_expires(self):
        key, value = local_claims.make('40392', '695697', now=NOW)
        self.assertEqual(key, 'local_claim:40392:695697')
        self.assertTrue(local_claims.is_active(value, now=NOW + timedelta(hours=47)))
        self.assertFalse(local_claims.is_active(value, now=NOW + timedelta(hours=49)))

    def test_malformed_claims_are_inactive_and_never_win(self):
        _, good = local_claims.make('1', '2', now=NOW)
        for bad in (None, '', 'x', '{}', '{"expires_at": "2026-11-30T00:00:00"}'):
            self.assertFalse(local_claims.is_active(bad, now=NOW))
            self.assertFalse(local_claims.newer(bad, good))

    def test_later_claim_wins(self):
        _, early = local_claims.make('1', '2', now=NOW)
        _, late = local_claims.make('1', '2', now=NOW + timedelta(hours=1))
        self.assertTrue(local_claims.newer(late, early))
        self.assertFalse(local_claims.newer(early, late))
        self.assertTrue(local_claims.newer(early, None))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.delta, self.remote = root/'delta.db', root/'remote.db'
        for path in (self.delta, self.remote):
            Database(str(path)).conn.close()

    def meta(self, path, key, value):
        with sqlite3.connect(path) as db:
            db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (key, value))

    def claim(self, path):
        with sqlite3.connect(path) as db:
            row = db.execute("SELECT value FROM meta WHERE key='local_claim:1:2'").fetchone()
        return row[0] if row else None

    def test_meta_only_delta_adds_claim(self):
        key, value = local_claims.make('1', '2', now=NOW)
        self.meta(self.delta, key, value)
        merge_delta(str(self.delta), str(self.remote))
        self.assertEqual(self.claim(self.remote), value)

    def test_older_claim_does_not_replace_newer(self):
        key, early = local_claims.make('1', '2', now=NOW)
        _, late = local_claims.make('1', '2', now=NOW + timedelta(hours=1))
        self.meta(self.remote, key, late); self.meta(self.delta, key, early)
        merge_delta(str(self.delta), str(self.remote))
        self.assertEqual(self.claim(self.remote), late)

    def test_delta_without_claims_keeps_remote_claim(self):
        key, value = local_claims.make('1', '2', now=NOW)
        self.meta(self.remote, key, value)
        merge_delta(str(self.delta), str(self.remote))
        self.assertEqual(self.claim(self.remote), value)


class FakeDB:
    def __init__(self, meta):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)')
        self.conn.executemany('INSERT INTO meta VALUES (?,?)', meta.items())


class FakeClient:
    def __init__(self, urls):
        self.urls = urls

    def get_video_url(self, course, sub):
        value = self.urls[sub]
        if isinstance(value, Exception):
            raise value
        return value


class DailyFilterTests(unittest.TestCase):
    def test_claimed_skipped_unplayable_deferred_errors_kept(self):
        claim_key, claim = local_claims.make('1', 'a', now=NOW)
        stale_key, stale = local_claims.make('1', 'b', now=NOW - timedelta(days=3))
        db = FakeDB({claim_key: claim, stale_key: stale})
        client = FakeClient({'a': 'https://example.invalid/a', 'b': 'https://example.invalid/b',
                             'c': None, 'd': RuntimeError('lookup failed')})
        tasks = [task('1', s) for s in ('a', 'b', 'c', 'd')]
        ready, claimed, deferred = daily_filter(tasks, db, client, now=NOW)
        self.assertEqual([t[2]['sub_id'] for t in ready], ['b', 'd'])
        self.assertEqual((claimed, deferred), (1, 1))


if __name__ == '__main__':
    unittest.main()
