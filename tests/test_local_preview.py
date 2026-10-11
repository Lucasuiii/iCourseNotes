from argparse import Namespace
from datetime import date, timedelta
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch
from scripts.local_history_refresh import new_preview_target, make_plan
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

    def test_deleted_lecture_cannot_be_reintroduced_as_new_preview(self):
        from src.data.database import Database
        from scripts.local_history.storage import Store
        from src.runtime import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'baseline.db'
            db = Database(str(path))
            db.upsert_course('40329', 'offline', '')
            db.insert_lecture('678993', '40329', 'offline', '2026-10-09')
            db.suppress_lectures('40329', ['678993'])
            db.conn.close()
            store = Store(Path(tmp) / 'run', 'offline-test-key-' + '0' * 32)
            def remote(destination):
                shutil.copyfile(path, destination)
                return 'offline-revision'
            with patch('scripts.production_db.load_remote', side_effect=remote), patch.object(config, 'COURSE_IDS', ['40329']):
                with self.assertRaisesRegex(ValueError, 'permanently ignored'):
                    make_plan(store, self.args())
            self.assertFalse(store.exists('manifest.enc'))
            self.assertFalse(store.exists('progress.enc'))
