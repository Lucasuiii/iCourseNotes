"""Retry/restart rules for failed local lectures, on a temporary project root with a test key.

- "Retry failed" never replays a lecture whose every ASR block is empty (it would stop again);
  the message depends on whether the checkpoint came from older code.
- Other failures still resume; a failed lecture restarted with current code is retired as
  `superseded` (files kept) so its old queue no longer runs it.
Run:  <python> macos-app/bridge/tests/test_retry_rules.py
"""
import importlib.util, json, os, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1]/'app_bridge.py'
REPO = Path(__file__).resolve().parents[3]


class RetryRuleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        runtime = self.root/'work/local-runtime'
        source = runtime/'source'; (source/'.local-history-refresh').mkdir(parents=True)
        for name in ('scripts', 'src'):
            (source/name).symlink_to(REPO/name, target_is_directory=True)
        (source/'.git').write_text('gitdir: test\n')  # marks a source checkout
        keyfile = self.root/'key'; keyfile.write_text('k'*40)
        (source/'.local-history-refresh/settings.json').write_text(json.dumps({'key_file': str(keyfile), 'env_files': []}))
        (runtime/'active-source.json').write_text(json.dumps({'path': str(source)}))
        os.environ['ICOURSE_PROJECT_ROOT'] = str(self.root)
        spec = importlib.util.spec_from_file_location('app_bridge_under_test', BRIDGE)
        self.b = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.b)
        self.queue = self.root/'work/local-latest-new-2026-11-06-000000'; self.queue.mkdir()
        (self.queue/'app_queue.py').write_text('')

    def tearDown(self):
        os.environ.pop('ICOURSE_PROJECT_ROOT', None)
        shutil.rmtree(self.root, ignore_errors=True)

    def lecture(self, sid, *, texts, status='failed', sha='old', diagnostics=None):
        control = self.root/f'work/local-latest-1-{sid}-2026-11-06-000000'; control.mkdir()
        self.b.write_json(control/'execution-status.json', {'status': status, 'phase': 'summary'})
        self.b.write_json(control/'source.json', {'sha': sha})
        state = {'stage': 'summary', 'rows': [{'chunk_id': i, 'text': t} for i, t in enumerate(texts)],
                 'spec': {'plan': {'blocks': texts}, 'audio_diagnostics': diagnostics or {}}}
        self.b.store(control/f'lecture-{sid}').save(f'1-{sid}.enc', state)
        return {'course_id': '1', 'sub_id': sid, 'course_title': '课程', 'date': '2026-11-04', 'control': str(control)}

    def resume(self, targets, sha='new'):
        self.b.write_json(self.queue/'targets.json', targets)
        launched = []
        with mock.patch.object(self.b, 'source_identity', return_value=(None, sha)), \
             mock.patch.object(self.b, 'worker_active', return_value=False), \
             mock.patch.object(self.b, 'launch_queue', side_effect=lambda c, e: launched.append(e) or 1), \
             mock.patch.object(self.b, 'out'):
            self.b.cmd_resume(mock.Mock(control=str(self.queue), retry_failed=True))
        return launched

    def test_empty_recognition_from_old_code_points_to_a_fresh_run(self):
        t = self.lecture('11', texts=['', ' '])
        self.assertTrue(self.b.empty_recognition(Path(t['control']), '1', '11'))
        with self.assertRaises(self.b.Refused) as caught:
            self.resume([t])
        self.assertIn('检查新回放', str(caught.exception))

    def test_empty_recognition_with_current_code_is_not_replayed(self):
        t = self.lecture('12', texts=[''], sha='new', diagnostics={'channel_mix': 'mid'})
        with self.assertRaises(self.b.Refused) as caught:
            self.resume([t])
        self.assertIn('没有识别到语音', str(caught.exception))

    def test_other_failures_still_resume(self):
        t = self.lecture('13', texts=['讲课内容', ''])
        self.assertFalse(self.b.empty_recognition(Path(t['control']), '1', '13'))
        self.assertEqual(self.resume([t]), [['--resume', '--retry-failed']])

    def test_superseded_lecture_is_retired_but_kept(self):
        t = self.lecture('14', texts=[''])
        control = Path(t['control']); before = sorted(p.name for p in control.rglob('*'))
        self.b.supersede_failed(control, self.root/'work/local-latest-1-14-2026-11-07-000000')
        status = self.b.read_json(control/'execution-status.json')
        self.assertEqual((status['status'], status['previous_status']), ('superseded', 'failed'))
        self.assertEqual(sorted(p.name for p in control.rglob('*')), before)
        self.assertEqual([r['status'] for r in self.b.local_controls('1', '14')], ['superseded'])
        row = self.b.lecture_status(t, self.b.store)
        self.assertEqual((row['status'], row['superseded_by']), ('superseded', 'local-latest-1-14-2026-11-07-000000'))
        self.assertEqual(row['failure_reason'], 'no_speech_old_downmix')
        # A superseded lecture is neither retried by its old queue nor blocks the retry of others.
        other = self.lecture('15', texts=['讲课内容'])
        self.assertEqual(self.resume([t, other]), [['--resume', '--retry-failed']])

    def test_scan_lets_failed_lectures_be_selected_again(self):
        scan = self.root/'work/local-latest-new-2026-11-06-010101'; scan.mkdir()
        rows = [{'course_id': '1', 'sub_id': sid, 'course_title': '课程', 'date': '2026-11-04'} for sid in ('21', '22', '23')]
        self.b.write_json(scan/'inventory.json', {'status': 'complete', 'unrecognized_available': rows})
        self.lecture('21', texts=[''])                        # failed: may restart
        self.lecture('22', texts=['讲课'], status='paused')    # paused: must resume instead
        ready = {r['sub_id']: r for r in self.b.summarize_inventory(scan)['ready']}
        self.assertEqual((ready['21']['selectable'], ready['21']['local_restart'], ready['21']['local_existing']), (True, True, False))
        self.assertEqual((ready['22']['selectable'], ready['22']['local_existing']), (False, True))
        self.assertEqual((ready['23']['selectable'], ready['23']['local_restart']), (True, False))


if __name__ == '__main__':
    unittest.main()
