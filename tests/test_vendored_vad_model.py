"""Cloud runs install the Silero VAD model from the repository, never from a release download."""
import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA256 = '9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6'


class VendoredVadModelTests(unittest.TestCase):
    def test_vendored_model_matches_the_pinned_release_asset(self):
        data = (ROOT/'assets/models/silero_vad.onnx').read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), SHA256)
        self.assertIn(SHA256, (ROOT/'scripts/install_vad_model.sh').read_text())

    def test_workflows_do_not_download_the_model(self):
        # 2026-11-08/09: GitHub release downloads returned 503/504 and failed whole lectures.
        for path in list((ROOT/'.github').rglob('*.yml')):
            text = path.read_text()
            with self.subTest(path=path.name):
                self.assertIsNone(re.search(r'releases/download/[^\s]*silero_vad', text))
