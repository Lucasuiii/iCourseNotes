"""A failed local start must not leave lectures unstartable; data reads survive a brief outage."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('start_bridge', ROOT/'macos-app/bridge/app_bridge.py')
bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
from scripts import production_db


class PreparationFailureTests(unittest.TestCase):
    def test_failed_preparation_is_retired_and_restartable(self):
        with tempfile.TemporaryDirectory() as tmp:
            done, failed = Path(tmp)/'a', Path(tmp)/'b'
            done.mkdir(); failed.mkdir()
            bridge.mark_preparation_failed([done, failed], 'plan')
            for c in (done, failed):
                status = json.loads((c/'execution-status.json').read_text())
                self.assertEqual((status['status'], status['phase'], status['failed_step']), ('failed', 'preparation', 'plan'))
            # Directories left by older app versions: selection written, preparation never finished.
            left = Path(tmp)/'c'; left.mkdir()
            self.assertTrue(bridge.abandoned_preparation({'control': str(left), 'status': 'prepared'}))
            (left/'preparation-result.json').write_text('{}')
            self.assertFalse(bridge.abandoned_preparation({'control': str(left), 'status': 'prepared'}))
            self.assertFalse(bridge.abandoned_preparation({'control': str(left), 'status': 'running'}))


class InventoryTests(unittest.TestCase):
    def test_scan_list_offers_a_lecture_whose_previous_start_stopped_in_preparation(self):
        row = {'course_id': '1', 'course_title': 'c', 'sub_id': '2', 'title': 't', 'date': '2026-11-17'}
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp); (control/'inventory.json').write_text(json.dumps(
                {'status': 'complete', 'unrecognized_available': [row], 'deferred_unavailable': [], 'courses': []}))
            left = Path(tmp)/'left'; left.mkdir()
            controls = [{'control': str(left), 'status': 'prepared'}]
            with patch.object(bridge, 'local_controls', return_value=controls):
                ready = bridge.summarize_inventory(control)['ready'][0]
            self.assertEqual((ready['selectable'], ready['local_existing'], ready['local_restart']), (True, False, True))
            (left/'preparation-result.json').write_text('{}')  # a real prepared task still blocks
            with patch.object(bridge, 'local_controls', return_value=controls):
                ready = bridge.summarize_inventory(control)['ready'][0]
            self.assertEqual((ready['selectable'], ready['local_existing']), (False, True))


class DataReadRetryTests(unittest.TestCase):
    def test_transient_git_read_failures_are_retried_then_raised(self):
        error = subprocess.CalledProcessError(128, ['git'])
        waits = []
        with patch.object(production_db, 'command', side_effect=[error, error, b'ok']) as call:
            self.assertEqual(production_db.read_command(['git', 'ls-remote'], sleep=waits.append), b'ok')
        self.assertEqual((call.call_count, waits), (3, [5, 15]))
        with patch.object(production_db, 'command', side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                production_db.read_command(['git', 'fetch'], sleep=lambda _: None)


if __name__ == '__main__':
    unittest.main()
