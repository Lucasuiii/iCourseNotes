"""Exercise actual deletion workflow shell steps against offline encrypted data."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import yaml

from src.data.database import Database
from src.data.sharder import shard_database, load_index, reassemble_database
from src.pipeline.summary_review import digest
from scripts.validate_db import validate_database

ROOT = Path(__file__).resolve().parents[1]
KEY = 'offline-delete-test-key-' + '0' * 32
REQUEST = '12345678-1234-4234-8234-123456789abc'


class DeleteCourseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'remote.git'
        self.writer = self.root / 'writer'
        self.workspace = self.root / 'workspace'
        self.env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
                        DB_ENCRYPTION_KEY=KEY, STUID='', UISPSW='', SUBSCRIBED_COURSE_IDS='',
                        GITHUB_WORKSPACE=str(self.workspace), GITHUB_ENV=str(self.root / 'github-env'),
                        GITHUB_TOKEN='offline-token', REQUEST_ID=REQUEST,
                        PYTHONPATH='',
                        PRIVATE_ACTION_REQUEST=json.dumps({'request_id': REQUEST, 'course_ids': '10'}),
                        GIT_CONFIG_COUNT='1',
                        GIT_CONFIG_KEY_0=f'url.{self.remote}.insteadOf',
                        GIT_CONFIG_VALUE_0='https://x-access-token:offline-token@github.com/offline/fixture.git',
                        GIT_TERMINAL_PROMPT='0')
        self.git(self.root, 'init', '--bare', str(self.remote))
        self.writer.mkdir()
        self.git(self.writer, 'init', '-b', 'data')
        self.git(self.writer, 'remote', 'add', 'origin', str(self.remote))
        self.db_path = self.root / 'source.db'
        db = Database(str(self.db_path))
        self.addCleanup(db.conn.close)
        self.db = db
        for course, sub in [('10', '1'), ('10', '2'), ('20', '3')]:
            db.upsert_course(course, 'offline course', '')
            db.insert_lecture(sub, course, sub, '2026-10-01')
            db.update_summary(sub, 'old notes', 'test')
            db.update_transcript(sub, 'old transcript')
            db.mark_processed(sub)
            db.insert_ppt_pages_pending(sub, [{'page_num': 1, 'created_sec': 0, 'pptimgurl': 'offline'}])
            state = {'schema': 2, 'review_scope': 'theory', 'course_id': course, 'sub_id': sub,
                     'date': '2026-10-01', 'summary_sha256': digest('old notes'),
                     'reviewed_summary': 'old notes', 'status': 'passed',
                     'result': {'verdict': 'pass', 'issues': [], 'checks': []}}
            db.write_meta('summary_review:' + sub, json.dumps(state))
            db.write_meta('summary_figures:' + sub, json.dumps({
                'schema': 1, 'course_id': course, 'sub_id': sub, 'status': 'complete', 'figures': []}))
        db.write_meta('unrelated-setting', 'keep')
        validate_database(str(self.db_path))
        self.publish_writer('base')
        self.base = self.git(self.writer, 'rev-parse', 'HEAD').stdout.strip()
        self.git(self.root, 'clone', '--branch', 'data', str(self.remote), str(self.workspace))
        for name in ['scripts', 'src']:
            (self.workspace / name).symlink_to(ROOT / name, target_is_directory=True)
        workflow = yaml.load((ROOT / '.github/workflows/delete_course.yml').read_text(), Loader=yaml.BaseLoader)
        self.steps = {s['name']: s['run'] for s in workflow['jobs']['delete']['steps'] if 'run' in s}

    def git(self, cwd, *args):
        result = subprocess.run(['git', '-c', 'user.name=Offline Test', '-c', 'user.email=test@example.invalid', *args],
                                cwd=cwd, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def step(self, name, success=True):
        script = self.steps[name].replace('${{ github.repository }}', 'offline/fixture')
        script = script.replace('/tmp/db_deploy', str(self.root / 'deploy'))
        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=self.workspace,
                                env=self.env, capture_output=True, text=True, timeout=60)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, 'stale deletion unexpectedly pushed: ' + result.stdout)
        return result

    def fetch_and_delete(self, sub_ids=''):
        self.env['PRIVATE_ACTION_REQUEST'] = json.dumps({
            'request_id': REQUEST, 'course_ids': '10', 'sub_ids': sub_ids})
        self.step('Fetch and decrypt database')
        env_path = Path(self.env['GITHUB_ENV'])
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                name, value = line.split('=', 1)
                self.env[name] = value
        self.step('Delete lecture(s) or course(s)')

    def publish_writer(self, message):
        shard_database(str(self.db_path), str(self.writer / 'data'), KEY)
        self.git(self.writer, 'add', 'data')
        self.git(self.writer, 'commit', '-m', message)
        self.git(self.writer, 'push', 'origin', 'data')

    def read_remote(self):
        checkout = self.root / 'readback'
        self.git(self.root, 'clone', '--branch', 'data', str(self.remote), str(checkout))
        path = self.root / 'readback.db'
        reassemble_database(load_index(str(checkout / 'data/icourse-index.enc'), KEY),
                            str(checkout / 'data/shards'), str(path), KEY)
        validate_database(str(path))
        return sqlite3.connect(path)

    def test_course_deletion_with_reviews_and_figures_publishes_and_preserves_other_course(self):
        self.fetch_and_delete()
        self.step('Re-shard and push')
        with self.read_remote() as conn:
            self.assertEqual(conn.execute('SELECT course_id FROM courses').fetchall(), [('20',)])
            self.assertEqual(conn.execute('SELECT sub_id,summary FROM lectures').fetchall(), [('3', 'old notes')])
            self.assertEqual(conn.execute('SELECT sub_id FROM ppt_pages').fetchall(), [('3',)])
            keys = {r[0] for r in conn.execute('SELECT key FROM meta')}
            self.assertTrue({'summary_review:3', 'summary_figures:3', 'unrelated-setting'} <= keys)
            self.assertFalse({'summary_review:1', 'summary_review:2', 'summary_figures:1', 'summary_figures:2'} & keys)
        tip = self.git(self.remote, 'rev-parse', 'refs/heads/data').stdout.strip()
        self.assertEqual(self.git(self.remote, 'rev-parse', tip + '^').stdout.strip(), self.base)

    def assert_concurrent_update_survives(self, sub_ids):
        self.fetch_and_delete(sub_ids)
        self.db.update_summary('3', 'new notes', 'test')
        raw = json.loads(self.db.read_meta('summary_review:3'))
        raw.update(summary_sha256=digest('new notes'), reviewed_summary='new notes')
        self.db.write_meta('summary_review:3', json.dumps(raw))
        self.publish_writer('concurrent new notes')
        latest = self.git(self.remote, 'rev-parse', 'refs/heads/data').stdout.strip()
        result = self.step('Re-shard and push', success=False)
        self.assertIn('[rejected]', result.stderr)
        self.assertEqual(self.git(self.root / 'deploy', 'rev-parse', 'HEAD^').stdout.strip(), self.base)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'refs/heads/data').stdout.strip(), latest)
        with self.read_remote() as conn:
            self.assertEqual(conn.execute("SELECT summary FROM lectures WHERE sub_id='3'").fetchone()[0], 'new notes')
            self.assertEqual(conn.execute('SELECT count(*) FROM lectures').fetchone()[0], 3)

    def test_concurrent_publish_rejects_stale_single_lecture_suppression(self):
        self.assert_concurrent_update_survives('1')

    def test_pending_lecture_deletion_survives_encrypted_reload_and_catalog_sync(self):
        from unittest.mock import MagicMock, patch
        import main
        from scripts.merge_db import merge
        with self.db.conn:
            self.db.conn.execute("UPDATE lectures SET processed_at=NULL, summary=NULL, transcript=NULL WHERE sub_id='1'")
            self.db.conn.execute("DELETE FROM meta WHERE key IN ('summary_review:1','summary_figures:1')")
        self.publish_writer('pending lesson')
        self.fetch_and_delete('1')
        self.step('Re-shard and push')
        with self.read_remote() as conn:
            row = conn.execute("SELECT deleted_at,summary,transcript FROM lectures WHERE sub_id='1'").fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1:], (None, None))
        path = self.root / 'readback.db'
        # A stale worker cannot resurrect deleted content during publication.
        merge(str(self.db_path), str(path))
        db = Database(str(path))
        self.addCleanup(db.conn.close)
        client = MagicMock()
        client.get_course_detail.return_value = {
            'title': 'offline course', 'teacher': '', 'lectures': [
                {'sub_id': '1', 'sub_title': 'removed lesson', 'date': '2026-10-01', 'has_playback': True},
                {'sub_id': '4', 'sub_title': 'new lesson', 'date': '2026-10-02', 'has_playback': True},
            ]}
        with patch.object(main.config, 'COURSE_IDS', ['10']), patch.object(main, '_in_run_scope', return_value=True):
            selected = main._enumerate_lectures(client, db, MagicMock())
        self.assertEqual([str(t[2]['sub_id']) for t in selected.lectures], ['4'])
        self.assertIsNotNone(db.get_lecture('1')['deleted_at'])
        self.assertIsNone(db.get_lecture('1')['summary'])

    def test_concurrent_publish_rejects_stale_whole_course_deletion(self):
        self.assert_concurrent_update_survives('')

    def test_course_deletion_is_atomic_if_a_later_statement_fails(self):
        with self.db.conn:
            self.db.conn.execute("""CREATE TRIGGER reject_course_delete BEFORE DELETE ON courses
                WHEN OLD.course_id='10' BEGIN SELECT RAISE(ABORT, 'offline failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.delete_course('10')
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM lectures').fetchone()[0], 3)
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM ppt_pages').fetchone()[0], 3)
        self.assertIsNotNone(self.db.read_meta('summary_review:1'))
        self.assertIsNotNone(self.db.read_meta('summary_figures:1'))
        validate_database(str(self.db_path))
