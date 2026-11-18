"""Pipelined app queue: two overlapping lectures at most, one model on the GPU."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from scripts.local_history.gpu_lock import exclusive_gpu

TEMPLATE = Path(__file__).resolve().parents[1]/'macos-app/bridge/templates/app_queue.py'

# Walks the real stage names; records start/end so the test can check overlap.
FAKE_WORKER = r'''
import json, sys, time
from pathlib import Path
R = Path(__file__).resolve().parent; sid = R.name
events = R.parent/'events.jsonl'
def log(kind): events.open('a').write(json.dumps({'sid': sid, 'kind': kind, 'time': time.time()})+'\n')
def stage(name):
    (R/('lecture-'+sid)).mkdir(exist_ok=True)
    (R/('lecture-'+sid)/'stage.json').write_text(json.dumps({'stage': name, 'time': time.time()}))
    log(name)
log('start')
for name in ['authentication', 'acquisition']: stage(name); time.sleep(0.4)
if (R.parent/'pause-during-acquisition').exists():
    (R/'execution-status.json').write_text(json.dumps({'status': 'paused'})); log('end'); sys.exit(3)
for name in ['asr', 'summary']: stage(name); time.sleep(0.6)
(R/'execution-status.json').write_text(json.dumps({'status': 'awaiting_content_review'}))
log('end')
'''


class GpuLockTests(unittest.TestCase):
    def test_second_holder_waits_and_reports_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'gpu.lock'; order = []; waited = []
            def second():
                with exclusive_gpu(lambda: waited.append(True), poll=0.02, path=path):
                    order.append('second')
            with exclusive_gpu(path=path, poll=0.02) as first_waited:
                worker = threading.Thread(target=second); worker.start()
                time.sleep(0.2); order.append('first')
            worker.join(5)
            self.assertFalse(first_waited)
            self.assertEqual(order, ['first', 'second'])
            self.assertEqual(waited, [True])


class PipelinedQueueTests(unittest.TestCase):
    def build(self, tmp, count):
        root = Path(tmp)
        env_bin = root/'qwen-local-asr-test-env/bin'; env_bin.mkdir(parents=True)
        (env_bin/'python').symlink_to(sys.executable)
        queue = root/'queue'; queue.mkdir()
        shutil.copyfile(TEMPLATE, queue/'app_queue.py')
        targets = []
        for i in range(count):
            control = root/f'l{i}'; control.mkdir()
            (control/'app_run_once.py').write_text(FAKE_WORKER)
            targets.append({'control': str(control), 'sub_id': f'l{i}', 'course_title': 'c', 'date': '2026-11-18',
                            'output': str(root/'out')})
        (queue/'targets.json').write_text(json.dumps(targets))
        return root, queue

    def run_queue(self, queue):
        env = dict(os.environ, ICOURSE_QUEUE_POLL='0.05')
        subprocess.run([sys.executable, str(queue/'app_queue.py')], env=env, check=True,
                       capture_output=True, timeout=60)
        log = queue.parent/'events.jsonl'
        events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return json.loads((queue/'queue-status.json').read_text()), events

    def test_next_lecture_overlaps_after_acquisition_with_at_most_two_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, queue = self.build(tmp, 3)
            status, events = self.run_queue(queue)
            self.assertEqual(status['status'], 'awaiting_content_review')
            self.assertEqual(status['active_sub_ids'], [])
            at = {(e['sid'], e['kind']): e['time'] for e in events}
            # Lecture 2 starts only after lecture 1 left acquisition, and before lecture 1 ends.
            self.assertGreater(at[('l1', 'start')], at[('l0', 'asr')])
            self.assertLess(at[('l1', 'start')], at[('l0', 'end')])
            self.assertGreater(at[('l2', 'start')], at[('l1', 'asr')])
            running = peak = 0
            for e in sorted(events, key=lambda e: e['time']):
                running += {'start': 1, 'end': -1}.get(e['kind'], 0); peak = max(peak, running)
            self.assertEqual(peak, 2)
            self.assertEqual(set(status['lectures']), {'l0', 'l1', 'l2'})

    def test_pause_request_stops_new_launches_and_waits_for_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, queue = self.build(tmp, 3)
            (queue/'app-pause-request.json').write_text('{}')
            status, events = self.run_queue(queue)
            self.assertEqual(status['status'], 'paused')
            self.assertEqual(events, [])
            self.assertFalse((queue/'app-pause-request.json').exists())

    def test_a_paused_lecture_ends_the_queue_after_the_other_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, queue = self.build(tmp, 3)
            (root/'pause-during-acquisition').touch()
            status, events = self.run_queue(queue)
            self.assertEqual(status['status'], 'paused')
            self.assertEqual(status['lectures']['l0']['status'], 'paused')
            self.assertNotIn(('l2', 'start'), {(e['sid'], e['kind']) for e in events})


if __name__ == '__main__':
    unittest.main()
