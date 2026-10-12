import subprocess
from pathlib import Path
import unittest


class FrontendCourseDeleteTests(unittest.TestCase):
    def test_whole_course_deletion_guards(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', 'scripts/test_frontend_course_delete.cjs'], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
