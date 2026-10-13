import subprocess
from pathlib import Path
import unittest


class FrontendDeleteTransportTests(unittest.TestCase):
    def test_error_phase_and_dispatch_outcome(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', 'scripts/test_frontend_delete_transport.cjs'], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
