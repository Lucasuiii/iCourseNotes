"""Safe decoder diagnostics and the audio completeness gate, independent of ASR."""
from __future__ import annotations

import math
from pathlib import Path
import re

PCM_RATE = 16000
PCM_SAMPLE_BYTES = 4
PCM_BYTES_PER_SECOND = PCM_RATE*PCM_SAMPLE_BYTES
PREPARE_STREAM_TIMEOUT = 90*60
PREPARE_IDLE_TIMEOUT = 5*60
ERROR_PATTERNS = {
    'premature_eof': (b'stream ends prematurely', b'partial file'),
    'input_read_error': (b'input/output error', b'error during demuxing',
                         b'error opening input', b'connection timed out',
                         b'connection reset by peer'),
    'decode_error': (b'error while decoding', b'error decoding',
                     b'corrupt input packet', b'packet corrupt'),
}


def record_decode_errors(chunk, counts):
    text = chunk.lower()
    for code, needles in ERROR_PATTERNS.items():
        if any(needle in text for needle in needles):
            counts[code] = min(1_000_000, counts.get(code, 0)+1)


class DecodeErrorScanner:
    """Count fixed error categories even across reads or a rotated stderr tail."""
    def __init__(self, counts):
        self.counts = counts
        self._tail = b''
        self._overlap = max(len(n) for needles in ERROR_PATTERNS.values() for n in needles)-1

    def feed(self, chunk):
        text = self._tail+chunk.lower()
        boundary = len(self._tail)
        for code, needles in ERROR_PATTERNS.items():
            found = False
            for needle in needles:
                position = text.find(needle)
                while position >= 0:
                    if position+len(needle) > boundary:
                        found = True; break
                    position = text.find(needle, position+1)
                if found: break
            if found:
                self.counts[code] = min(1_000_000, self.counts.get(code, 0)+1)
        self._tail = text[-self._overlap:]


def collect_decode_diagnostics(handle, *, media_seconds=None, interrupted=False, retained=False):
    done = getattr(handle, 'stderr_done', None)
    complete = done is None or done.wait(timeout=5)
    pcm = Path(handle.path)
    size = pcm.stat().st_size if pcm.exists() else 0
    if media_seconds is None:
        stderr = b''.join(handle.stderr_chunks).decode(errors='replace')
        match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', stderr)
        if match:
            media_seconds = int(match[1])*3600+int(match[2])*60+float(match[3])
    duration = size/PCM_BYTES_PER_SECOND
    diagnostics = {'pcm_bytes': size, 'pcm_sample_aligned': size % PCM_SAMPLE_BYTES == 0,
        'audio_seconds': duration, 'media_seconds': media_seconds,
        'duration_gap_seconds': max(0, media_seconds-duration) if media_seconds else None,
        'decode_return_code': handle.process.returncode, 'decode_interrupted': interrupted,
        'timeline_preserved': bool(getattr(handle, 'timeline_preserved', False)),
        'stderr_complete': complete,
        'decode_error_counts': {code: count for code, count in getattr(handle, 'decode_error_counts', {}).copy().items()
            if code in {*ERROR_PATTERNS, 'stderr_read_error'} and type(count) is int and 0 < count <= 1_000_000},
        'audio_retained': retained}
    transport = getattr(handle, 'media_transport', None)
    if transport is not None: diagnostics['source_transport'] = transport.audit()
    return diagnostics


def validate_prepared_audio(specification):
    """Decoder exit zero alone is never evidence of complete input."""
    diagnostics = specification['audio_diagnostics']
    if not diagnostics.get('stderr_complete', True):
        raise ValueError('Production audio diagnostics are incomplete')
    if (diagnostics.get('decode_error_counts') or diagnostics.get('decode_return_code') != 0
            or diagnostics.get('decode_interrupted')
            or diagnostics.get('source_transport', {}).get('terminal_error_code')):
        raise ValueError('Production audio has read or decode errors')
    duration, media = specification['audio_seconds'], specification.get('media_seconds')
    timing = specification.get('preparation_timing', {})
    if media is None and timing and timing.get('stream_eof') is not True:
        raise ValueError('Production audio diagnostics are incomplete')
    if (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
            or media is not None and (type(media) not in (int, float) or not math.isfinite(media) or media <= 0)
            or diagnostics.get('pcm_sample_aligned') is False):
        raise ValueError('Production audio has invalid sample metadata')
    if media and duration < media-max(120, media*.05):
        raise ValueError('Production audio is incomplete')


STARTUP_ERROR_CODES = frozenset({
    'source_changed', 'source_validator_missing', 'invalid_content_length',
    'invalid_content_range', 'invalid_read_length', 'media_redirect_untrusted',
    'media_session_unavailable', 'media_session_refresh_needed', 'range_not_honored',
    'upstream_timeout', 'upstream_connection_error', 'upstream_premature_eof', 'upstream_tls_error',
    'upstream_retries_exhausted', 'encoded_range',
    'upstream_http_rejected', 'upstream_retryable_http', 'transport_internal_error',
    'transport_failed', 'diagnostic_byte_limit', 'stopped', 'service_unavailable',
    'cold_session', 'cas_context_missing', 'api_verification_failed',
    'authentication_rejected', 'password_method_missing', 'service_redirect_untrusted',
    'service_http_rejected', 'public_key_missing', 'login_token_missing',
    'cas_ticket_missing', 'ticket_destination_untrusted', 'media_auth_cancelled'})


def safe_transport_diagnostics(value):
    """Public failure evidence excludes URLs, cookies, validators and source identity."""
    if type(value) is not dict: return {}
    clean = {}
    code = value.get('terminal_error_code')
    if isinstance(code, str) and code in STARTUP_ERROR_CODES: clean['terminal_error_code'] = code
    for key in ('range_requests', 'range_verified', 'retries', 'range_rejections',
                'session_refresh_attempts', 'session_identity_verifications',
                'session_resume_attempts', 'session_refresh_successes'):
        count = value.get(key)
        if type(count) is int and 0 <= count <= 1_000_000: clean[key] = count
    statuses = value.get('upstream_status_counts')
    if type(statuses) is dict:
        clean['upstream_status_counts'] = {key:count for key, count in statuses.items()
            if type(key) is str and re.fullmatch(r'[1-5][0-9]{2}', key)
            and type(count) is int and 0 <= count <= 1_000_000}
    events = value.get('session_recovery_events')
    if type(events) is list and len(events) <= 8:
        clean['session_recovery_events'] = []
        for row in events:
            if type(row) is not dict: continue
            event, elapsed = row.get('event'), row.get('elapsed_seconds')
            if (type(event) is str and event in ('login_redirect', 'authentication_started',
                    'identity_verified', 'media_resumed', 'media_resume_failed')
                    and type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 1_000_000):
                clean['session_recovery_events'].append({'event':event, 'elapsed_seconds':elapsed})
    auth = value.get('media_auth')
    if type(auth) is dict:
        from src.api.webvpn import (AUTH_PHASES, AUTH_ERROR_TYPES, AUTH_FAILURE_CODES,
                                    authentication_failure)
        if all(isinstance(auth.get(key), str) and auth[key] in allowed
               for key, allowed in (('failure_phase', AUTH_PHASES),
                                    ('error_type', AUTH_ERROR_TYPES),
                                    ('failure', AUTH_FAILURE_CODES))):
            error = RuntimeError()
            error.auth_failure_diagnostics = auth
            clean['media_authentication'] = authentication_failure(error)
    return clean


def startup_diagnostics(phase, error=None, transport=None):
    """Keep pre-PCM failures without storing exception text or a traceback."""
    from src.runtime.media_protocol import MediaTransportError
    from src.api.webvpn import AuthenticationError
    code = (error.code if isinstance(error, MediaTransportError) else
            error.reason if isinstance(error, AuthenticationError) else None)
    value = {'phase': phase, 'error_type': type(error).__name__ if error is not None else 'NoPlayableURL',
             'error_code': code if code in STARTUP_ERROR_CODES else
                'media_url_unavailable' if error is None else 'audio_startup_exception'}
    if phase == 'authentication' and error is not None:
        from src.api.webvpn import authentication_failure
        value['authentication_failure'] = authentication_failure(error)
    if transport is not None: value['source_transport'] = transport.audit()
    return value
