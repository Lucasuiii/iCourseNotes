"""Polarity-inverted stereo must not cancel the lecture in the mono downmix (real FFmpeg)."""
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from src.runtime.audio_preparation import downmix_outputs, resolve_channel_phase, side_path
from scripts.local_history.runtime import stale_silent_downmix, supersede_acquisition
from scripts.local_history.storage import Store

RATE = 16000


def speech_like(seconds=8):
    t = np.arange(seconds*RATE)/RATE
    envelope = 0.5+0.5*np.sin(2*math.pi*3*t)  # syllable-rate amplitude modulation
    return (0.2*envelope*(np.sin(2*math.pi*220*t)+0.5*np.sin(2*math.pi*440*t))).astype(np.float32)


def level(path):
    x = np.fromfile(path, dtype='<f4').astype(np.float64)
    return 10*math.log10(max(float(np.dot(x, x))/len(x), 1e-20))


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is required')
class StereoPhaseTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def decode(self, left, right=None):
        source = self.dir/'source.f32'
        channels = 1 if right is None else 2
        data = left if right is None else np.stack([left, right], axis=1).reshape(-1)
        data.astype('<f4').tofile(source)
        out = self.dir/'audio.raw'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'f32le', '-ar', str(RATE),
                        '-ac', str(channels), '-i', str(source), '-vn',
                        *downmix_outputs(out, prefix_filters='aresample=async=1:first_pts=0,', with_side=True)],
                       check=True)
        return out

    def test_inverted_stereo_uses_the_side_signal(self):
        s = speech_like()
        out = self.decode(s, -s)
        self.assertLess(level(out), -60)  # the plain downmix cancels the lecture
        result = resolve_channel_phase(out)
        self.assertEqual(result['channel_mix'], 'side')
        self.assertGreater(level(out), -25)
        self.assertFalse(Path(side_path(out)).exists())
        self.assertEqual(out.stat().st_size, len(s)*4)
        json.dumps(result)

    def test_normal_one_sided_and_mono_recordings_keep_the_mid_signal(self):
        s = speech_like()
        for name, right in (('in-phase', s), ('left only', np.zeros_like(s)), ('mono', None)):
            with self.subTest(name):
                out = self.decode(s, right)
                before = out.read_bytes()
                result = resolve_channel_phase(out)
                self.assertEqual(result['channel_mix'], 'mid')
                self.assertEqual(out.read_bytes(), before)
                self.assertFalse(Path(side_path(out)).exists())

    def test_silent_recording_is_not_flipped(self):
        noise = (np.random.default_rng(1).standard_normal(4*RATE)*1e-4).astype(np.float32)
        out = self.decode(noise, -noise)
        self.assertEqual(resolve_channel_phase(out)['channel_mix'], 'mid')

    def test_without_side_output_arguments_are_unchanged(self):
        self.assertEqual(downmix_outputs('a.raw', prefix_filters='x,'),
                         ['-af', 'x', '-ac', '1', '-ar', '16000', '-f', 'f32le', 'a.raw'])
        self.assertEqual(downmix_outputs('a.raw'), ['-ac', '1', '-ar', '16000', '-f', 'f32le', 'a.raw'])
        self.assertEqual(resolve_channel_phase(self.dir/'missing.raw'), {'channel_mix': 'mono_downmix'})


class StaleCheckpointRetryTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def state(self, text='', **diagnostics):
        return {'spec': {'plan': {'blocks': [1, 2]}, 'audio_diagnostics': diagnostics},
                'rows': [{'chunk_id': 0, 'text': text}, {'chunk_id': 1, 'text': ' '}],
                'final_rows': [{'chunk_id': 0, 'text': text}, {'chunk_id': 1, 'text': ''}],
                'review': {'attempts': []}, 'glossary_snapshot': {'terms': ['秩']}, 'ppt_complete': True,
                'access_mode': 'direct', 'stage': 'summary', 'last_error_type': 'RuntimeError'}

    def test_only_empty_pre_fix_acquisitions_are_redone(self):
        self.assertTrue(stale_silent_downmix(self.state()))
        self.assertFalse(stale_silent_downmix(self.state(text='讲课内容')))
        self.assertFalse(stale_silent_downmix(self.state(channel_mix='mid')))
        self.assertFalse(stale_silent_downmix({'rows': [], 'review': {}}))

    def test_supersede_archives_audio_and_checkpoint_then_restarts(self):
        store = Store(self.dir/'run', 'k'*40)
        root = self.dir/'run'/'1-2'; root.mkdir(parents=True)
        audio = root/'audio.raw'; audio.write_bytes(b'old-audio')
        state = self.state()
        supersede_acquisition(store, '1-2', root, state, audio)
        self.assertFalse(audio.exists())
        self.assertEqual((root/'superseded-1'/'audio.raw').read_bytes(), b'old-audio')
        self.assertEqual(store.read('1-2.superseded-1.enc')['stage'], 'summary')
        self.assertEqual(state, {'glossary_snapshot': {'terms': ['秩']}, 'ppt_complete': True,
                                 'access_mode': 'direct', 'rows': [], 'review': {},
                                 'superseded_acquisitions': 1})
        audio.write_bytes(b'second')
        supersede_acquisition(store, '1-2', root, self.state(), audio)
        self.assertTrue((root/'superseded-2'/'audio.raw').exists())


if __name__ == '__main__':
    unittest.main()
