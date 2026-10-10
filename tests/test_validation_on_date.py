import os
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import yaml
from scripts import production_qwen as pipeline


class ExactDateValidationTests(unittest.TestCase):
    def client(self):
        client = MagicMock()
        client.get_course_detail.return_value = dict(title='概率论', teacher='教师', lectures=[
            dict(sub_id='1', date='2026-09-18', sub_title='2026-09-18第2-5节'),
            dict(sub_id='2', date='2026-10-09', sub_title='2026-10-09第2-5节'),
            dict(sub_id='3', date='2026-10-16', sub_title='2026-10-16第2-5节')])
        db = MagicMock(); db.get_lecture.return_value = {}
        return client, db

    def test_unavailable_exact_date_never_substitutes_older_playable_lesson(self):
        client, db = self.client()
        client.get_video_url.side_effect = lambda course, sub: 'synthetic-url' if sub=='1' else None
        with self.assertRaises(ValueError):
            pipeline.latest_validation_task(client, db, '10', today='2026-10-09', on_date='2026-10-09')
        client.get_video_url.assert_called_once_with('10', '2')

    def test_exact_date_selection_is_frozen_and_future_date_is_not_probed(self):
        client, db = self.client(); client.get_video_url.return_value = 'synthetic-url'
        task, _ = pipeline.latest_validation_task(client, db, '10', today='2026-10-09', on_date='2026-10-09')
        self.assertEqual(task[2]['sub_id'], '2')
        self.assertEqual(task[2]['_validation']['on_date'], '2026-10-09')
        client.get_video_url.reset_mock()
        with self.assertRaises(ValueError):
            pipeline.latest_validation_task(client, db, '10', today='2026-10-09', on_date='2026-10-16')
        client.get_video_url.assert_not_called()

    def test_date_validation_requires_isolation_and_cannot_change_recovery_selection(self):
        env = dict(VALIDATION_COURSE_ID='10', COURSE_IDS='10', VALIDATION_ON_DATE='2026-10-09',
            VALIDATION_SOURCE_RUN_ID='', VALIDATION_SELECTION_RUN_ID='', VALIDATION_BEFORE_DATE='',
            VALIDATION_LECTURE_RANK='1', VALIDATION_LECTURE_RANKS='', PUBLISH_RESULTS='false', SEND_EMAIL='false')
        with patch.dict(os.environ, env):
            self.assertEqual(pipeline.validation_course(), '10')
            for changed in (dict(VALIDATION_COURSE_ID=''), dict(VALIDATION_SOURCE_RUN_ID='99'),
                            dict(VALIDATION_SELECTION_RUN_ID='99'), dict(PUBLISH_RESULTS='true')):
                with patch.dict(os.environ, changed), self.assertRaises(ValueError): pipeline.validation_course()
            for invalid in ('2026-02-30', '2026-1-9', 'private-value'):
                with patch.dict(os.environ, dict(VALIDATION_ON_DATE=invalid)), self.assertRaises(ValueError): pipeline.validation_on_date()

    def test_dispatch_and_reusable_workflow_pass_date_only_to_planning(self):
        workflow = yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/parallel_pilot.yml').read_text())
        for event in ('workflow_dispatch', 'workflow_call'):
            self.assertEqual(workflow['on'][event]['inputs']['validation_on_date']['default'], '')
        self.assertEqual(workflow['jobs']['plan']['env']['VALIDATION_ON_DATE'], '${{ inputs.validation_on_date }}')

    def test_restored_queue_must_match_both_requested_and_frozen_date_before_encoding(self):
        from scripts.production import planning
        for lecture_date, frozen_date in [('2026-09-18', '2026-10-09'), ('2026-10-09', '')]:
            with self.subTest(lecture=lecture_date, frozen=frozen_date):
                files = {'queue.json': json.dumps([['10', '课程', dict(sub_id='2', date=lecture_date,
                    _validation=dict(playable_rank=1, date=lecture_date, on_date=frozen_date))]]).encode()}
                runtime = SimpleNamespace(validation_course=lambda:'10', validation_ranks=lambda:[1],
                    validation_on_date=lambda:'2026-10-09', validation_before_date=lambda:'',
                    artifact=MagicMock(return_value=True), decode=lambda path, stage:files, root=lambda:Path('/unused'),
                    read_json=json.loads, MAX_TASKS=5, encode=MagicMock())
                with patch.dict(os.environ, dict(GITHUB_ACTIONS='false', HISTORY_REFRESH_TARGETS='',
                    VALIDATION_SOURCE_RUN_ID='', VALIDATION_SELECTION_RUN_ID='')):
                    with self.assertRaisesRegex(ValueError, 'requested recording'): planning.plan(runtime)
                runtime.encode.assert_not_called()
