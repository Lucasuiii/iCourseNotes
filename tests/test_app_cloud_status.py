"""Application cloud-status command: health classification from run states only."""
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cloud_bridge', ROOT/'macos-app/bridge/app_bridge.py')
bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
NOW = 1_800_000_000


def run(status='completed', conclusion='success', age=3600):
    return {'status': status, 'conclusion': conclusion, 'updated': NOW-age}


class HealthTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(bridge.cloud_health([run()], NOW), ('ok', 0, NOW-3600))
        self.assertEqual(bridge.cloud_health([run('in_progress', None), run(conclusion='failure')], NOW)[0], 'running')
        self.assertEqual(bridge.cloud_health([run(conclusion='failure'), run(conclusion='failure', age=90000), run(age=180000)], NOW),
                         ('failing', 2, NOW-180000))
        # A cancelled run neither fails nor recovers the schedule.
        self.assertEqual(bridge.cloud_health([run(conclusion='cancelled'), run()], NOW)[0], 'ok')
        # Success too long ago means the schedule stopped running or never completes.
        self.assertEqual(bridge.cloud_health([run(age=27*3600)], NOW)[0], 'stale')
        self.assertEqual(bridge.cloud_health([], NOW), ('stale', 0, None))

    def test_partial_runs_are_not_failures(self):
        partial = dict(run(conclusion='failure'), partial=True)
        self.assertEqual(bridge.cloud_health([partial, run(conclusion='failure', age=90000)], NOW), ('partial', 0, NOW-3600))
        self.assertEqual(bridge.cloud_health([run(conclusion='failure'), partial], NOW), ('failing', 1, NOW-3600))
        self.assertEqual(bridge.cloud_health([dict(partial, updated=NOW-27*3600)], NOW)[0], 'stale')

    def test_command_marks_a_run_partial_when_plan_and_finalize_succeeded(self):
        runs = {'workflow_runs': [{'id': i, 'status': 'completed', 'conclusion': 'failure', 'event': 'schedule',
                                   'created_at': '2026-11-17T18:47:38Z', 'updated_at': '2026-11-17T21:52:59Z',
                                   'html_url': 'u'} for i in (2, 1)]}
        partial_jobs = {'jobs': [{'name': 'check / plan', 'conclusion': 'success'},
                                 {'name': 'check / 课堂并发进度（获取 → 识别 → 汇总）', 'conclusion': 'failure'},
                                 {'name': 'check / finalize', 'conclusion': 'success'}]}
        failed_jobs = {'jobs': [{'name': 'check / plan', 'conclusion': 'failure'}]}
        fake = lambda path: partial_jobs if '/runs/2/' in path else failed_jobs if '/runs/1/' in path else runs
        with patch.object(bridge, 'github_get', side_effect=fake), redirect_stdout(io.StringIO()) as output:
            bridge.cmd_cloud(None)
        result = json.loads(output.getvalue())
        self.assertEqual((result['health'], result['consecutive_failures']), ('partial', 0))
        self.assertEqual([r.get('partial') for r in result['runs']], [True, False])

    def test_command_exposes_only_states_and_failed_job_names(self):
        runs = {'workflow_runs': [{'id': 7, 'status': 'completed', 'conclusion': 'failure', 'event': 'schedule',
                                   'created_at': '2026-11-16T19:53:38Z', 'updated_at': '2026-11-16T19:56:12Z',
                                   'html_url': 'https://github.com/x/y/actions/runs/7', 'head_commit': {'message': 'private'}}]}
        jobs = {'jobs': [{'name': 'check / plan', 'conclusion': 'failure', 'steps': [{'name': 'private'}]},
                         {'name': 'check / finalize', 'conclusion': 'skipped'}]}
        calls = []
        def fake(path):
            calls.append(path); return jobs if path.endswith('/jobs?per_page=30') else runs
        with patch.object(bridge, 'github_get', side_effect=fake), redirect_stdout(io.StringIO()) as output:
            bridge.cmd_cloud(None)
        result = json.loads(output.getvalue())
        self.assertEqual((result['health'], result['consecutive_failures']), ('failing', 1))
        self.assertEqual(result['latest']['failed_jobs'], ['plan'])
        self.assertNotIn('private', output.getvalue())
        self.assertTrue(all(c.startswith('repos/Lucasuiii/iCourseNotes/actions/') for c in calls))

    def test_unreachable_github_is_a_readable_refusal(self):
        with patch.object(bridge, 'github_get', side_effect=OSError('private host')):
            with self.assertRaises(bridge.Refused) as caught:
                bridge.cmd_cloud(None)
        self.assertNotIn('private', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
