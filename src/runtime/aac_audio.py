"""Verified AAC batches -> streaming PCM with an aggregate process lifecycle."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading

from src.runtime.aac_ranges import (AAC_FRAME_SAMPLES, MediaTransportError, iter_track_packets,
    load_index, needs_pcm_retiming, packet_ranges, stream_full_track, track_batches)


# Only unsupported format/resource shapes qualify, never damaged data, source
# changes, authentication failures or malformed HTTP/multipart responses.
FALLBACK_CODES = frozenset({
    'fragmented_mp4_unsupported', 'ambiguous_audio_tracks', 'codec_unsupported',
    'encrypted_audio_unsupported', 'unsupported_aac_config', 'unsupported_aac_extension',
    'sample_description_unsupported', 'edit_list_unsupported',
    'nonintegral_edit_offset_unsupported', 'full_edit_coverage_unsupported',
    'composition_timing_unsupported', 'aac_timescale_unsupported',
    'aac_sample_gap_unsupported', 'aac_sample_groups_unsupported',
    'unsupported_aac_packet_size', 'index_byte_limit', 'index_memory_limit',
    'full_track_payload_limit'})
MAX_AUDIO_BYTES = 500_000_000


def adts_frame(index, payload):
    size = len(payload)+7
    ch, freq = index.channels, index.frequency_index
    return bytes((255, 241, 64|(freq<<2)|(ch>>2), ((ch&3)<<6)|(size>>11),
                  (size>>3)&255, ((size&7)<<5)|31, 252))+payload


def prepare_aac(reader):
    """Reject unsupported formats and validate the first batch before spawning."""
    index = load_index(reader, allow_variable_timing=True)
    if not 0 < sum(index.sizes) <= MAX_AUDIO_BYTES:
        raise MediaTransportError('full_track_payload_limit')
    next(iter_track_packets(index))  # Includes full edit coverage checks.
    first = next(track_batches(index, max_ranges=reader.limits.batch_ranges))
    ranges = packet_ranges(first)
    payloads = reader.fetch(ranges)
    with reader._audit_lock:
        reader._audit.update(mode='aac_ranges', aac_expected_samples=len(index.sizes),
            aac_verified_samples=0, aac_full_packet_coverage=False,
            aac_payload_bytes=0, aac_timeline_complete=False,
            aac_media_seconds=(index.presentation_offset+index.duration)/index.timescale)
    return index, ranges, payloads


def duration_header(index):
    seconds = (index.presentation_offset+index.duration)/index.timescale
    hours = int(seconds//3600)
    minutes = int(seconds%3600//60)
    return f'Duration: {hours:02d}:{minutes:02d}:{seconds%60:09.6f}\n'.encode('ascii')


class _InitialBatch:
    def __init__(self, reader, ranges, data):
        self.reader, self.ranges, self.data = reader, ranges, data

    def __getattr__(self, name):
        return getattr(self.reader, name)

    def fetch(self, ranges):
        if self.data is not None:
            if ranges != self.ranges:
                raise MediaTransportError('packet_coverage_incomplete')
            result, self.data = self.data, None
            return result
        return self.reader.fetch(ranges)


def copy_timed_pcm(source, destination, index):
    """Match each decoded frame to its exact duration in the source time table."""
    stride = index.channels * 4
    frame_bytes = AAC_FRAME_SAMPLES * stride
    silence = bytes(frame_bytes)
    for count, duration in index.timing:
        if duration == AAC_FRAME_SAMPLES:
            while count:
                frames = min(count, 64)
                block = source.read(frames * frame_bytes)
                if len(block) != frames * frame_bytes:
                    raise MediaTransportError('aac_decoder_failed')
                destination.write(block)
                count -= frames
        else:
            for _ in range(count):
                frame = source.read(frame_bytes)
                if len(frame) != frame_bytes:
                    raise MediaTransportError('aac_decoder_failed')
                destination.write(frame[:duration * stride])
                if duration > AAC_FRAME_SAMPLES:
                    destination.write(silence[:(duration - AAC_FRAME_SAMPLES) * stride])
    if source.read(1):
        raise MediaTransportError('aac_decoder_failed')


class TimedAACDecoder:
    """Decode continuous AAC, restore native sample timing, then render PCM."""

    def __init__(self, command, outputs, index):
        read_fd, write_fd = os.pipe()
        self.stderr = os.fdopen(read_fd, 'rb')
        self.decoder = self.renderer = None
        try:
            self.decoder = subprocess.Popen(
                [*command, '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=write_fd)
            self.renderer = subprocess.Popen(
                ['ffmpeg', '-nostdin', '-y', '-f', 'f32le', '-ar', str(index.sample_rate),
                 '-ac', str(index.channels), '-i', 'pipe:0', *outputs],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=write_fd)
            self.stdin = self.decoder.stdin
            self.result = None
            self.thread = threading.Thread(target=self.render, args=(index,),
                                           name='aac-pcm-timing', daemon=True)
            self.thread.start()
        except BaseException:
            for process in (self.decoder, self.renderer):
                if process is None:
                    continue
                process.kill()
                process.wait()
                process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
            self.stderr.close()
            raise
        finally:
            os.close(write_fd)

    def render(self, index):
        try:
            with self.decoder.stdout, self.renderer.stdin:
                copy_timed_pcm(self.decoder.stdout, self.renderer.stdin, index)
        except (OSError, MediaTransportError):
            self.kill()
            self.result = 1
        finally:
            decode_code = self.decoder.wait()
            render_code = self.renderer.wait()
            self.result = self.result or decode_code or render_code

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired('aac_pcm_timing', timeout)
        return self.result

    def poll(self):
        return self.result if not self.thread.is_alive() else None

    def terminate(self):
        self.decoder.terminate()
        self.renderer.terminate()

    def kill(self):
        self.decoder.kill()
        self.renderer.kill()


class AACDecodeProcess:
    """Popen-compatible completion includes producer coverage and PCM endpoint.

    FFmpeg exit zero after a truncated stdin must never look like success.
    terminate/kill stop both network production and decoder. No AAC files are
    retained: only bounded batches and the existing caller-owned PCM scratch.
    """
    def __init__(self, decoder, reader, prepared, path):
        self.decoder, self.reader, self.path = decoder, reader, Path(path)
        self.index, ranges, payloads = prepared
        self.stderr = decoder.stderr
        self._done = threading.Event()
        self._result = None
        self._thread = threading.Thread(target=self._produce,
            args=(_InitialBatch(reader, ranges, payloads),), name='aac-pcm-producer', daemon=True)
        self._thread.start()

    @classmethod
    def spawn(cls, reader, prepared, path, *, with_side=False):
        index = prepared[0]
        offset = index.presentation_offset/index.timescale
        end = (index.presentation_offset+index.duration)/index.timescale
        # Restore the edit offset and trim final AAC padding. The variable
        # timing path applies per-frame corrections before resampling.
        filters = (f'asetpts=N/SR/TB+{offset:.12f}/TB,atrim=end={end:.12f},'
                   'aresample=async=1:first_pts=0')
        from src.runtime.audio_preparation import downmix_outputs
        command = ['ffmpeg', '-nostdin', '-y', '-xerror', '-f', 'aac', '-i', 'pipe:0', '-vn']
        outputs = downmix_outputs(path, prefix_filters=filters+',', with_side=with_side)
        if needs_pcm_retiming(index.timing):
            decoder = TimedAACDecoder(command, outputs, index)
        else:
            decoder = subprocess.Popen([*command, *outputs], stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            return cls(decoder, reader, prepared, path)
        except BaseException:
            decoder.kill(); decoder.wait(timeout=5)
            decoder.stdin.close(); decoder.stderr.close()
            raise

    def _produce(self, initial):
        try:
            def commit(packet, payload):
                self.reader.check()
                self.decoder.stdin.write(adts_frame(self.index, payload))
            def progress(row):
                with self.reader._audit_lock:
                    self.reader._audit.update(aac_verified_samples=row['verified_samples'],
                        aac_payload_bytes=row['audio_payload_bytes'])
            evidence = stream_full_track(initial, self.index, commit,
                max_payload_bytes=MAX_AUDIO_BYTES, progress=progress)
            self.decoder.stdin.close()
            code = self.decoder.wait()
            if code != 0:
                raise MediaTransportError('aac_decoder_failed')
            self.reader.check()
            expected = evidence['presentation_end_seconds']
            size = self.path.stat().st_size
            # One millisecond allows integer sample rounding and resampling.
            if not size or size%4 or abs(size/64000-expected) > .001:
                raise MediaTransportError('aac_pcm_endpoint_mismatch')
            with self.reader._audit_lock:
                self.reader._audit.update(aac_verified_samples=evidence['verified_samples'],
                    aac_payload_bytes=evidence['audio_payload_bytes'],
                    aac_full_packet_coverage=True, aac_timeline_complete=True)
            self._result = 0
        except BaseException as error:
            code = error.code if isinstance(error, MediaTransportError) else 'aac_producer_failed'
            with self.reader._audit_lock:
                self.reader._audit['terminal_error_code'] = self.reader._audit['terminal_error_code'] or code
            self._result = 1
            if self.decoder.poll() is None:
                self.decoder.kill()
            self.decoder.wait()
        finally:
            try:
                if not self.decoder.stdin.closed:
                    self.decoder.stdin.close()
            except OSError:
                pass
            finally:
                self._done.set()

    @property
    def returncode(self):
        return self._result if self._done.is_set() else None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired('aac_pcm_stream', timeout)
        self._thread.join()
        return self._result

    def terminate(self):
        self.decoder.terminate() if self.decoder.poll() is None else None
        self.reader.close()

    def kill(self):
        self.decoder.kill() if self.decoder.poll() is None else None
        self.reader.close()
