"""Pausing a pipelined queue stops the lecture on the GPU and lets a cloud-stage one finish."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pause_bridge', ROOT/'macos-app/bridge/app_bridge.py')
bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)


class PipelinedPauseTests(unittest.TestCase):
    def run_pause(self, stages, force=False):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp); (control/'app_queue.py').write_text('')
            targets = []
            for i, sid in enumerate(stages):
                lecture = control/sid; lecture.mkdir()
                (lecture/'execution-status.json').write_text(json.dumps({'worker_pid': 100+i}))
                targets.append({'control': str(lecture), 'sub_id': sid})
            (control/'targets.json').write_text(json.dumps(targets))
            (control/'queue-status.json').write_text(json.dumps({'pid': 1, 'current_sub_id': list(stages)[-1],
                                                                 'active_sub_ids': list(stages)}))
            status = lambda t, store: {'stage': stages[t['sub_id']],
                                       'safe_to_pause': stages[t['sub_id']] in bridge.LOCAL_STAGES}
            printed, killed = [], []
            with patch.object(bridge, 'queue_control', lambda p: control), patch.object(bridge, 'pid_alive', lambda *a: True), \
                 patch.object(bridge, 'lecture_status', status), patch.object(bridge, 'store', None, create=True), \
                 patch.object(bridge, 'out', printed.append), patch.object(bridge.os, 'kill', lambda pid, sig: killed.append(pid)):
                bridge.cmd_pause(SimpleNamespace(control=str(control), after_current=False, force=force))
            return printed[-1], killed, (control/'app-pause-request.json').exists()

    def test_only_the_local_stage_lecture_is_interrupted(self):
        result, killed, requested = self.run_pause({'a': 'summary', 'b': 'asr'})
        self.assertTrue(result['ok']); self.assertEqual(killed, [101]); self.assertTrue(requested)
        self.assertEqual(result['finishing_cloud_stage'], 1)

    def test_gpu_wait_counts_as_local(self):
        result, killed, _ = self.run_pause({'a': 'summary', 'b': 'gpu_wait'})
        self.assertEqual(killed, [101])

    def test_cloud_only_still_asks_for_confirmation(self):
        result, killed, requested = self.run_pause({'a': 'summary'})
        self.assertTrue(result['needs_confirmation']); self.assertEqual(killed, []); self.assertFalse(requested)
        result, killed, requested = self.run_pause({'a': 'summary'}, force=True)
        self.assertEqual(killed, [100]); self.assertTrue(requested)


if __name__ == '__main__':
    unittest.main()
