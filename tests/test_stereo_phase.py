"""Polarity-inverted stereo must not cancel the lecture in the mono downmix (real FFmpeg)."""
import json
import math
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from src.runtime.audio_preparation import downmix_outputs, resolve_channel_phase, side_path

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


class CloudPrepareTests(unittest.TestCase):
    """The production prepare stage keeps the streamed VAD unless the recording is polarity-inverted."""

    def prepare(self, side_samples):
        from scripts import production_qwen as pipeline
        from test_qwen_production_pipeline import database
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'RUNNER_TEMP': tmp, 'GITHUB_RUN_ID': '99', 'COURSE_SLOT': '0', 'DB_ENCRYPTION_KEY': 'k'*32,
                'QWEN_PRODUCTION_TASK': 'true', 'AUTO_COURSE_TERMS': 'false', 'PUBLISH_RESULTS': 'false',
                'SHARD_MODE': 'shared', 'GITHUB_ACTIONS': 'false', 'POOL_ARTIFACTS': 'false'}):
            root = pipeline.root(); db = database(root/'fixture.db')
            speech = speech_like(4)
            raw = root/'source.raw'; (speech*1e-3).astype('<f4').tofile(raw)  # cancelled downmix
            if side_samples is not None:
                side_samples.astype('<f4').tofile(side_path(raw))
            process = MagicMock(); process.poll.return_value = 0; process.returncode = 0
            handle = SimpleNamespace(path=str(raw), process=process, timeline_preserved=True,
                                     stderr_chunks=[b'Duration: 00:00:04.00'])
            scheduler = MagicMock(); scheduler.audio_downloader.get.return_value = handle
            transcriber = MagicMock(); transcriber.prepare_pcm_stream.return_value = [(0, 4)]
            transcriber.last_audio_duration = 4; transcriber.last_media_duration = 4
            transcriber.last_vad_windows = [(0, 4)]; transcriber.last_prepare_stats = {'stream_eof': True}
            with patch.object(pipeline, 'artifact', return_value=False), \
                 patch.object(pipeline, 'task_files', return_value=(db, '10', '近代史', {'sub_id': '1', 'date': '2026-11-04'})), \
                 patch.object(pipeline, 'recover_preparation', return_value=None), \
                 patch.dict('sys.modules', {'main': SimpleNamespace(login_with_retry=lambda: None),
                     'src.pipeline.ppt_pipeline': SimpleNamespace(PPTPipeline=MagicMock()),
                     'src.api.icourse': SimpleNamespace(ICourseClient=MagicMock())}), \
                 patch('src.runtime.scheduler.Scheduler', return_value=scheduler), \
                 patch('src.ai.qwen_transcriber.QwenTranscriber', return_value=transcriber), \
                 patch('scripts.shared_asr_worker.initialize'), patch.object(pipeline, 'write_outputs'):
                pipeline.prepare()
            spec = json.loads(pipeline.decode(root/'out'/'prepared.enc', 'prepared')['specification.json'])
            return spec, transcriber, scheduler, level(raw), Path(side_path(raw)).exists()

    def test_inverted_recording_switches_to_side_and_segments_again(self):
        spec, transcriber, scheduler, final_level, side_left = self.prepare(speech_like(4)*0.5)
        self.assertIs(scheduler.audio_downloader.write_side_channel, True)
        self.assertEqual(spec['audio_diagnostics']['channel_mix'], 'side')
        self.assertEqual(transcriber.prepare_pcm_stream.call_count, 2)
        self.assertGreater(final_level, -30); self.assertFalse(side_left)
        self.assertEqual(spec['mode'], 'sharded')

    def test_normal_recording_keeps_the_streamed_segmentation(self):
        spec, transcriber, _, final_level, _ = self.prepare(None)
        self.assertEqual(spec['audio_diagnostics']['channel_mix'], 'mono_downmix')
        self.assertEqual(transcriber.prepare_pcm_stream.call_count, 1)
        self.assertLess(final_level, -50)


if __name__ == '__main__':
    unittest.main()
