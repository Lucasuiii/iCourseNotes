from argparse import Namespace
from datetime import date, timedelta
import unittest
from unittest.mock import patch
from scripts.local_history_refresh import new_preview_target
from scripts.local_history.publication import apply


class LocalPreviewTests(unittest.TestCase):
    def args(self, **changes):
        args = dict(course_id='40329', new_lecture='678993', date='2026-10-09', lecture_id=None, limit=None)
        return Namespace(**(args | changes))

    def test_explicit_preview_scope_has_no_historical_replacement_identity(self):
        target = new_preview_target(self.args(), ['40329'])
        self.assertEqual((target['course_id'], target['sub_id'], target['date']), ('40329', '678993', '2026-10-09'))
        self.assertTrue(target['preview_only']); self.assertIsNone(target['before_hash'])

    def test_future_foreign_course_and_mixed_history_request_rejected(self):
        for change in [dict(course_id='999'), dict(new_lecture='other'), dict(date='2099-01-01'),
                       dict(date='2026-02-31'), dict(lecture_id=['1']), dict(limit=1)]:
            with self.assertRaises(ValueError): new_preview_target(self.args(**change), ['40329'])

    def test_preview_cannot_enter_publication_even_with_forged_approval(self):
        target = new_preview_target(self.args(), ['40329'])
        with patch('scripts.production_db.load_remote') as remote:
            with self.assertRaisesRegex(ValueError, 'cannot publish'):
                apply(None, {'targets': [target]}, {}, 'anything')
            remote.assert_not_called()
