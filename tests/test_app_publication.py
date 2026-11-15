"""Application approval and publication regressions, using temporary data only."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bridge = module('publication_bridge', ROOT/'macos-app/bridge/app_bridge.py')
support = module('publication_support_test', ROOT/'macos-app/bridge/templates/publication_support.py')
queue = module('push_queue_test', ROOT/'macos-app/bridge/templates/app_push_queue.py')


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root/'lesson'
        self.control.mkdir()
        (self.control/'app_publish.py').write_text('')
        self.note = {'publishable': True, 'approved': True, 'summary_sha256': 'a'*64,
                     'sub_id': '12', 'course_title': '示例课程', 'date': '2026-01-01'}
        bridge.write_json(self.control/'app-approval.json', {'summary_sha256': 'a'*64})

    def test_single_confirmation_binds_the_current_text(self):
        args = argparse.Namespace(control=str(self.control), summary_sha256='a'*64, confirm=True, checks=None, notes='')
        with patch.object(bridge, 'lecture_control', return_value=self.control), patch.object(bridge, 'local_note', return_value=self.note), patch.object(bridge, 'out'):
            bridge.cmd_approve(args)
        saved = bridge.read_json(self.control/'app-approval.json')
        self.assertEqual(saved['approval_mode'], 'single_confirmation')
        self.assertEqual(len(saved['checks']), 1)
        self.assertEqual(saved['summary_sha256'], 'a'*64)

    def test_changed_text_still_requires_review(self):
        args = argparse.Namespace(control=str(self.control), summary_sha256='b'*64, confirm=True, checks=None, notes='')
        with patch.object(bridge, 'lecture_control', return_value=self.control), patch.object(bridge, 'local_note', return_value=self.note):
            with self.assertRaises(bridge.Refused):
                bridge.cmd_approve(args)

    def test_batch_rejects_duplicate_or_unapproved_lessons(self):
        item = {'control': str(self.control), 'summary_sha256': 'a'*64}
        with patch.object(bridge, 'STATE', self.root/'state'), patch.object(bridge, 'lecture_control', return_value=self.control), patch.object(bridge, 'local_note', return_value=self.note):
            with self.assertRaisesRegex(bridge.Refused, '重复'):
                bridge.cmd_push_many(argparse.Namespace(items=json.dumps([item, item]), email=False))
            self.note['approved'] = False
            with self.assertRaisesRegex(bridge.Refused, '审核内容'):
                bridge.cmd_push_many(argparse.Namespace(items=json.dumps([item]), email=False))

    def test_old_approval_stays_supported(self):
        args = argparse.Namespace(control=str(self.control), summary_sha256='a'*64, confirm=False,
                                  checks=json.dumps(['one', 'two', 'three', 'four']), notes='')
        with patch.object(bridge, 'lecture_control', return_value=self.control), patch.object(bridge, 'local_note', return_value=self.note), patch.object(bridge, 'out'):
            bridge.cmd_approve(args)
        self.assertEqual(bridge.read_json(self.control/'app-approval.json')['approval_mode'], 'checklist')

    def test_failed_push_with_a_journal_or_mail_reservation_is_not_reset(self):
        for journal, mail in [(True, False), (False, True)]:
            with self.subTest(journal=journal, mail=mail):
                (self.control/'notes-publication.json').unlink(missing_ok=True)
                if journal:
                    (self.control/'notes-publication.json').write_text('{}')
                bridge.write_json(self.control/'app-publication-status.json', {'status': 'failed', 'mail_reserved': mail})
                with self.assertRaises(bridge.Refused):
                    bridge.reset_failed_attempt(self.control)

    def test_batch_runs_each_selected_lesson_once_and_keeps_partial_failure(self):
        fake = self.root/'fake_bridge.py'
        fake.write_text('''import json,sys
from pathlib import Path
p=Path(sys.argv[sys.argv.index('--control')+1])
with (p/'calls').open('a') as log: log.write('push\\n')
ok=p.name=='good'
(p/'app-publication-status.json').write_text(json.dumps({'status':'completed' if ok else 'failed','error_type':None if ok else 'SyntheticFailure'}))
print(json.dumps({'ok':True,'pid':1}))
''')
        items = []
        for name in ('good', 'bad'):
            control = self.root/name; control.mkdir()
            items.append({'control': str(control), 'summary_sha256': 'a'*64, 'status': 'pending'})
        state = self.root/'push-batch.json'
        state.write_text(json.dumps({'items': items, 'email': False, 'bridge': str(fake)}))
        queue.run(state)
        result = json.loads(state.read_text())
        self.assertEqual(result['status'], 'finished_with_failures')
        self.assertEqual([item['status'] for item in result['items']], ['completed', 'failed'])
        self.assertEqual((self.root/'good/calls').read_text(), 'push\n')
        self.assertEqual((self.root/'bad/calls').read_text(), 'push\n')


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root/'remote.git'
        self.seed = self.root/'seed'
        self.command(['git', 'init', '--bare', '-q', str(self.remote)])
        self.command(['git', 'init', '-q', str(self.seed)])
        self.command(['git', 'config', 'user.name', 'Test'], cwd=self.seed)
        self.command(['git', 'config', 'user.email', 'test@example.invalid'], cwd=self.seed)
        (self.seed/'data').mkdir()
        (self.seed/'data/icourse-index.enc').write_bytes(b'before')
        self.command(['git', 'add', '.'], cwd=self.seed)
        self.command(['git', 'commit', '-qm', 'initial'], cwd=self.seed)
        self.command(['git', 'push', '-q', str(self.remote), 'HEAD:refs/heads/data'], cwd=self.seed)
        self.base = self.head()
        self.before = self.root/'before.db'; self.before.write_bytes(b'before')
        self.database = self.root/'candidate.db'; self.database.write_bytes(b'after')

        def shard(database, directory, key):
            Path(directory, 'shards').mkdir(parents=True)
            Path(directory, 'icourse-index.enc').write_bytes(Path(database).read_bytes())

        self.f = SimpleNamespace(ROOT=self.root, URL=str(self.remote), CID='1', SID='12', key='test-key',
            command=self.command, scope=Mock(), validate_database=Mock(), durable_backup=Mock(),
            shard_database=shard, auth_env=lambda: None,
            load_index=lambda path, key: path.read_bytes(),
            reassemble_database=lambda index, shards, restored, key: restored.write_bytes(index),
            verify_equivalent=lambda a, b: self.assertEqual(Path(a).read_bytes(), Path(b).read_bytes()),
            record=lambda name, record: (self.root/name).write_text(json.dumps(record)),
            load_remote=self.load_remote)

    def command(self, args, **kwargs):
        return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, **kwargs).stdout

    def head(self):
        return self.command(['git', 'ls-remote', str(self.remote), 'refs/heads/data']).decode().split()[0]

    def load_remote(self, destination):
        revision = self.head()
        destination.write_bytes(self.command(['git', '--git-dir', str(self.remote), 'show', revision+':data/icourse-index.enc']))
        return revision

    def advance(self):
        (self.seed/'data/icourse-index.enc').write_bytes(b'concurrent')
        self.command(['git', 'add', '.'], cwd=self.seed)
        self.command(['git', 'commit', '-qm', 'concurrent'], cwd=self.seed)
        self.command(['git', 'push', '-q', str(self.remote), 'HEAD:refs/heads/data'], cwd=self.seed)

    def test_fresh_checkout_ignores_an_old_failed_workspace(self):
        old = self.root/'notes-checkout'; old.mkdir(); (old/'keep').write_text('old')
        commit = support.publish_snapshot(self.f, self.database, self.before, self.base, 'notes')
        self.assertEqual(self.head(), commit)
        self.assertTrue(json.loads((self.root/'notes-publication.json').read_text())['remote_readback_verified'])
        self.assertEqual((old/'keep').read_text(), 'old')
        self.assertEqual(list(self.root.glob('notes-checkout-*')), [])

    def test_known_race_before_push_has_no_pending_journal(self):
        self.advance()
        with self.assertRaises(support.DataChanged):
            support.publish_snapshot(self.f, self.database, self.before, self.base, 'notes')
        self.assertFalse((self.root/'notes-publication.json').exists())

    def test_confirmed_non_fast_forward_can_merge_again(self):
        def race(args, **kwargs):
            if args[:2] == ['git', 'push']:
                self.advance()
            return self.command(args, **kwargs)
        self.f.command = race
        with self.assertRaises(support.DataChanged):
            support.publish_snapshot(self.f, self.database, self.before, self.base, 'notes')
        self.assertFalse((self.root/'notes-publication.json').exists())
        self.assertEqual(len(list(self.root.glob('notes-rejected-*.json'))), 1)

    def test_unknown_push_retains_journal_and_checkout(self):
        def interrupted(args, **kwargs):
            if args[:2] == ['git', 'push']:
                raise subprocess.CalledProcessError(128, args, stderr=b'connection lost')
            return self.command(args, **kwargs)
        self.f.command = interrupted
        with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
            support.publish_snapshot(self.f, self.database, self.before, self.base, 'notes')
        journal = json.loads((self.root/'notes-publication.json').read_text())
        self.assertEqual(journal['status'], 'push_pending')
        self.assertTrue((self.root/journal['retained_checkout']/'.git').exists())

    def test_bounded_remerge_rebuilds_the_candidate(self):
        build = Mock(return_value=(self.database, None))
        with patch.object(support, 'publish_snapshot', side_effect=[support.DataChanged(), support.DataChanged(), 'commit']):
            self.assertEqual(support.publish_notes(self.f, {}, {}, None, build, Mock()), ('commit', None))
        self.assertEqual(build.call_count, 3)


if __name__ == '__main__':
    unittest.main()
