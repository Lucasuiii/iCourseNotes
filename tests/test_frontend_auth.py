import subprocess
from pathlib import Path
import unittest


class FrontendAuthTests(unittest.TestCase):
    def test_public_read_and_private_file_unlock(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', 'scripts/test_frontend_auth.cjs'], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
