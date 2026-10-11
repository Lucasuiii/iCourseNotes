"""Real FFmpeg/HTTP integration, lifecycle failures and independent board seeks."""
from dataclasses import replace
import hashlib
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_aac_ranges import Origin, MemoryReader, fixture, boxes, box, full
from src.runtime.aac_ranges import AACRangeTransport, load_index, MediaTransportError
from src.runtime.aac_audio import prepare_aac, FALLBACK_CODES
from src.runtime.audio_preparation import collect_decode_diagnostics, validate_prepared_audio, safe_transport_diagnostics
from src.runtime.scheduler import AudioDownloader


def add_empty_edit(data, seconds=.021):
    index = load_index(MemoryReader(data))
    movie = next(b for b in boxes(data) if b.kind == b'moov')
    header = next(b for b in boxes(data, movie.payload, movie.end) if b.kind == b'mvhd')
    scale = struct.unpack_from('>I', data, header.payload+12)[0]
    result = bytearray()
    # FFmpeg's default tail moov leaves all sample offsets unchanged.
    for node in boxes(data):
        if node.kind != b'moov':
            result.extend(data[node.start:node.end]); continue
        payload = bytearray()
        for child in boxes(data, node.payload, node.end):
            raw = data[child.start:child.end]
            if child.kind == b'trak':
                edit = full(b'elst', struct.pack('>I', 2)
                    +struct.pack('>Iihh', round(seconds*scale), -1, 1, 0)
                    +struct.pack('>Iihh', math.ceil(index.duration*scale/index.timescale), 0, 1, 0))
                raw = box(b'trak', data[child.payload:child.end]+box(b'edts', edit))
            payload.extend(raw)
        result.extend(box(b'moov', payload))
    return bytes(result)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class AACProductionTests(unittest.TestCase):
    def setUp(self):
        # Keep the integration on AAC even when the caller explicitly
        # selected the MP4 compatibility mode.
        from src.runtime import config
        scope = patch.object(config, 'AUDIO_ACQUISITION', 'aac_auto')
        scope.start(); self.addCleanup(scope.stop)

    def source(self, folder, *, video=False, seconds=3, edited=False):
        source = Path(folder)/'source.mp4'
        command = ['ffmpeg', '-v', 'error']
        if video:
            command += ['-f', 'lavfi', '-i', f'testsrc=size=192x96:rate=30:duration={seconds}']
        command += ['-f', 'lavfi', '-i', f'sine=sample_rate=48000:duration={seconds}']
        if video: command += ['-c:v', 'mpeg4', '-q:v', '8']
        command += ['-ac', '2', '-c:a', 'aac', '-use_editlist', '0', str(source)]
        subprocess.run(command, check=True)
        if edited: source.write_bytes(add_empty_edit(source.read_bytes()))
        return source

    def start(self, folder, origin):
        origin.client.get_video_url = lambda *args: origin.url
        downloader = AudioDownloader(str(Path(folder)/'pcm'), max_concurrent=1)
        downloader.schedule(origin.client, 'synthetic', '1', preserve_timestamps=True)
        handle = downloader.get('1', timeout=10)
        self.assertIsNotNone(handle, downloader.startup_failure('1'))
        return downloader, handle

    def specification(self, handle):
        diagnostic = collect_decode_diagnostics(handle)
        return dict(audio_seconds=diagnostic['audio_seconds'], media_seconds=diagnostic['media_seconds'],
                    audio_diagnostics=diagnostic)

    def test_default_production_stream_matches_original_decode_and_blackboard_still_seeks(self):
        from src.pipeline.homework_visual import collect_visual_evidence
        with tempfile.TemporaryDirectory(prefix='aac-production-test-') as tmp:
            source = self.source(tmp, video=True)
            with Origin(source.read_bytes()) as origin:
                downloader, handle = self.start(tmp, origin)
                try:
                    self.assertEqual(handle.process.wait(timeout=15), 0)
                    self.assertTrue(handle.timeline_preserved)
                    spec = self.specification(handle); validate_prepared_audio(spec)
                    audit = spec['audio_diagnostics']['source_transport']
                    self.assertEqual(audit['mode'], 'aac_ranges')
                    self.assertEqual(audit['aac_verified_samples'], audit['aac_expected_samples'])
                    self.assertTrue(audit['aac_full_packet_coverage'])
                    endpoint = audit['aac_media_seconds']
                    reference = subprocess.run(['ffmpeg', '-v', 'error', '-copyts', '-i', str(source),
                        '-vn', '-af', f'atrim=end={endpoint:.12f},aresample=async=1:first_pts=0',
                        '-ar', '16000', '-ac', '1', '-f', 'f32le', 'pipe:1'], capture_output=True, check=True)
                    self.assertEqual(hashlib.sha256(Path(handle.path).read_bytes()).digest(),
                                     hashlib.sha256(reference.stdout).digest())
                    # The unchanged visual module reopens original A/V, never
                    # the PCM or an audio-only reference, after audio completion.
                    calls = []
                    origin.client.get_ppt_list = lambda *args: []
                    origin.client.get_video_url = lambda *args: calls.append(args) or origin.url
                    evidence = collect_visual_evidence(origin.client, 'synthetic', '1',
                        [dict(id=0, quote='作业第1题', block_start=0, block_end=3)],
                        [dict(chunk_id=0, text='作业第1题', quote_start_ms=500, quote_end_ms=1000)],
                        audio_seconds=spec['audio_seconds'], frames_per_cue=6, delay_seconds=90,
                        ocr=lambda image: [dict(text='第1题', confidence=.99)])
                    self.assertTrue(calls)
                    self.assertTrue(evidence['frames'])
                    self.assertTrue(all(row['status']=='ok' for row in evidence['frames']),
                                    [(row['seconds'], row['status']) for row in evidence['frames']])
                finally:
                    path = Path(handle.path); downloader.shutdown()
                    self.assertFalse(path.exists())
            self.assertFalse(any(Path(tmp).rglob('*.aac')))

    def test_leading_edit_and_short_final_frame_preserve_original_endpoint(self):
        import numpy as np
        with tempfile.TemporaryDirectory(prefix='aac-production-test-') as tmp:
            source = self.source(tmp, edited=True)
            with Origin(source.read_bytes()) as origin:
                downloader, handle = self.start(tmp, origin)
                try:
                    self.assertEqual(handle.process.wait(timeout=15), 0)
                    spec = self.specification(handle); validate_prepared_audio(spec)
                    samples = np.fromfile(handle.path, dtype=np.float32)
                    self.assertEqual(float(np.max(np.abs(samples[:300]))), 0)
                    self.assertAlmostEqual(spec['audio_seconds'], spec['media_seconds'], places=3)
                    self.assertIn(b'Duration:', b''.join(handle.stderr_chunks))
                finally: downloader.shutdown()

    def test_unsupported_codec_falls_back_before_output_with_same_frozen_source(self):
        with tempfile.TemporaryDirectory(prefix='aac-production-test-') as tmp:
            source = Path(tmp)/'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'sine=sample_rate=48000:duration=2', '-c:a', 'alac', str(source)], check=True)
            with Origin(source.read_bytes()) as origin:
                downloader, handle = self.start(tmp, origin)
                try:
                    self.assertEqual(handle.process.wait(timeout=15), 0)
                    spec = self.specification(handle); validate_prepared_audio(spec)
                    audit = spec['audio_diagnostics']['source_transport']
                    self.assertEqual(audit['mode'], 'aac_mp4_fallback')
                    self.assertEqual(audit['aac_fallback_reason'], 'codec_unsupported')
                    self.assertEqual(handle.media_transport._source.etag, '"stable"')
                    self.assertTrue(all(condition=='"stable"' for ranges,condition,_ in origin.rows[1:]))
                finally: downloader.shutdown()

    def test_source_change_and_missing_parts_never_fall_back_or_spawn_decoder(self):
        for mode in ('changed', 'missing', 'html'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='aac-production-test-') as tmp:
                with Origin(fixture(), mode) as origin:
                    origin.client.get_video_url = lambda *args: origin.url
                    downloader = AudioDownloader(tmp, max_concurrent=1)
                    try:
                        with patch('src.runtime.scheduler.AACDecodeProcess.spawn') as spawn, \
                             patch.object(AACRangeTransport, 'start_mp4_fallback') as fallback:
                            downloader.schedule(origin.client, 'synthetic', '1', preserve_timestamps=True)
                            self.assertIsNone(downloader.get('1', timeout=10))
                            spawn.assert_not_called(); fallback.assert_not_called()
                        self.assertTrue(downloader.startup_failure('1'))
                    finally: downloader.shutdown()

    def test_failure_after_partial_pcm_never_reports_success_or_switches_backend(self):
        original = AACRangeTransport.fetch
        for code in ('source_changed', 'missing_or_extra_part'):
            with self.subTest(code=code), tempfile.TemporaryDirectory(prefix='aac-production-test-') as tmp:
                source = self.source(tmp, video=True, seconds=10)
                with Origin(source.read_bytes()) as origin:
                    calls = [0]
                    def fail_later(reader, ranges):
                        if len(ranges)>1:
                            calls[0] += 1
                            if calls[0] == 3: raise MediaTransportError(code)
                        return original(reader, ranges)
                    with patch.object(AACRangeTransport, 'fetch', fail_later), \
                         patch.object(AACRangeTransport, 'start_mp4_fallback') as fallback:
                        downloader, handle = self.start(tmp, origin)
                        try:
                            self.assertNotEqual(handle.process.wait(timeout=15), 0)
                            self.assertEqual(handle.media_transport.audit()['terminal_error_code'], code)
                            self.assertFalse(handle.media_transport.audit()['aac_full_packet_coverage'])
                            with self.assertRaises(ValueError): validate_prepared_audio(self.specification(handle))
                            fallback.assert_not_called()
                        finally: downloader.shutdown()

    def test_cancelled_index_fetch_stops_owned_transport_without_decoder_or_slot_leak(self):
        entered, stopped = threading.Event(), threading.Event()
        def blocked(reader):
            entered.set(); reader._stop.wait(3); stopped.set(); reader.check()
        with tempfile.TemporaryDirectory(prefix='aac-production-test-') as tmp, Origin(fixture()) as origin:
            origin.client.get_video_url = lambda *args: origin.url
            downloader = AudioDownloader(tmp, max_concurrent=1)
            with patch('src.runtime.scheduler.prepare_aac', blocked), \
                 patch('src.runtime.scheduler.AACDecodeProcess.spawn') as spawn:
                downloader.schedule(origin.client, 'synthetic', '1', preserve_timestamps=True)
                self.assertTrue(entered.wait(2)); downloader.release('1')
                self.assertTrue(stopped.wait(2)); spawn.assert_not_called()
                self.assertTrue(downloader._sem.acquire(timeout=2)); downloader._sem.release()
            downloader.shutdown(); self.assertEqual(downloader.startup_failure('1'), {})

    def test_cancelled_producer_closes_pipe_and_releases_concurrency_slot(self):
        original = AACRangeTransport.fetch
        entered = threading.Event()
        with tempfile.TemporaryDirectory(prefix='aac-production-test-') as tmp:
            source = self.source(tmp, video=True, seconds=10)
            with Origin(source.read_bytes()) as origin:
                calls = [0]
                def blocked(reader, ranges):
                    if len(ranges)>1:
                        calls[0] += 1
                        if calls[0] == 2:
                            entered.set(); reader._stop.wait(5); reader.check()
                    return original(reader, ranges)
                with patch.object(AACRangeTransport, 'fetch', blocked):
                    downloader, handle = self.start(tmp, origin)
                    try:
                        self.assertTrue(entered.wait(3)); path = Path(handle.path)
                        downloader.release('1')
                        self.assertFalse(path.exists())
                        self.assertNotEqual(handle.process.poll(), 0)
                        self.assertFalse(handle.process._thread.is_alive())
                        self.assertTrue(downloader._sem.acquire(timeout=2)); downloader._sem.release()
                    finally: downloader.shutdown()


class AACGateTests(unittest.TestCase):
    def test_zero_exit_and_near_complete_pcm_still_require_exact_packet_coverage(self):
        source = dict(mode='aac_ranges', aac_expected_samples=100, aac_verified_samples=99,
            aac_full_packet_coverage=False, aac_timeline_complete=True, aac_media_seconds=100)
        spec = dict(audio_seconds=100, media_seconds=100,
            audio_diagnostics=dict(decode_return_code=0, source_transport=source))
        with self.assertRaisesRegex(ValueError, 'packet coverage'): validate_prepared_audio(spec)
        source.update(aac_verified_samples=100, aac_full_packet_coverage=True)
        spec['audio_seconds']=99.98
        with self.assertRaisesRegex(ValueError, 'timeline'): validate_prepared_audio(spec)
        spec['audio_seconds']=100; validate_prepared_audio(spec)

    def test_fallback_allowlist_excludes_transport_and_corruption_and_audit_redacts(self):
        for code in ('source_changed', 'missing_or_extra_part', 'invalid_sample_offset',
                     'range_not_honored', 'upstream_timeout', 'media_session_unavailable'):
            self.assertNotIn(code, FALLBACK_CODES)
        self.assertEqual(safe_transport_diagnostics(dict(mode='aac_ranges',
            aac_fallback_reason={'url':'private'}, aac_verified_samples=10, private='secret')),
            {'mode':'aac_ranges', 'aac_verified_samples':10})
