"""Loopback byte-range transport for an immutable, freshly signed media source.

Only bounded upstream ranges are buffered. A failed range resumes at its next
unread byte; FFmpeg never sees duplicate bytes or an upstream signed URL.
Session recovery is opt-in; fresh authentication requires an explicit client factory.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlsplit

import requests

from src.runtime.media_protocol import (MediaTransportError, MediaSource,
    RangeRecoveryPolicy, RecoveryAction, VerifiedRangeBuffer, redirect_kind,
    media_identity, connection_failure_code, redirect_observation)


class SignedRangeRelay:
    def __init__(self, client, signed_url, *, chunk_bytes=8*1024*1024,
                 prefix_bytes=64*1024, attempts=3, max_upstream_bytes=None,
                 session_factory=requests.Session, timeout=(10, 15),
                 allow_session_refresh=False, cache_bytes=0,
                 allow_fresh_session_escalation=False):
        factory = getattr(client, "_media_reauth_factory", None)
        self._owns_client = callable(factory) and allow_session_refresh
        self.client = client.fork_for_media() if self._owns_client else client
        self.signed_url = signed_url
        self.chunk_bytes, self.prefix_bytes = chunk_bytes, prefix_bytes
        self.attempts, self.max_bytes = attempts, max_upstream_bytes
        self.session_factory, self.timeout = session_factory, timeout
        self.allow_session_refresh = allow_session_refresh
        self.allow_fresh_session_escalation = allow_fresh_session_escalation
        self._fresh_auth_used = False
        self._session = None
        self._source = MediaSource(signed_url)
        self._recovery = RangeRecoveryPolicy(attempts)
        self._buffer = VerifiedRangeBuffer(cache_bytes)
        self._session_refreshed = False
        self._resume_pending = False
        self._started = time.monotonic()
        self._prefix = b''
        self._stop = threading.Event()
        self._fetch_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._response_lock = threading.Lock()
        self._responses = set()
        self._last_initial_time = None
        self._server = self._thread = None
        self._closed = False
        self._path = '/'+secrets.token_urlsafe(32)
        self.url = None
        self._audit = dict(mode='signed_range_relay', range_requests=0,
                           range_verified=0, retries=0, signature_renewals=0,
                           upstream_bytes=0, source_total_bytes=None,
                           validator_kind=None, terminal_error_code=None,
                           upstream_status_counts={}, range_rejections=0,
                           redirect_counts={}, cookie_updates=0,
                           session_refresh_attempts=0, session_refresh_successes=0,
                           fresh_session_attempts=0,
                           session_identity_verifications=0, session_resume_attempts=0,
                           session_recovery_events=[],
                           last_failure_offset=None, last_error_code=None, state='idle',
                           cache_hits=0, cached_bytes_served=0, cache_bytes=0,
                           last_failure_stage=None, last_redirect={}, media_auth={})
        if type(chunk_bytes) is not int or not 4096 <= chunk_bytes <= 16*1024*1024:
            raise ValueError('Invalid bounded media transport limits')
        if not 1 <= prefix_bytes <= chunk_bytes:
            raise ValueError('Invalid media prefix limit')

    @property
    def total(self):
        return self._source.total

    def _transition(self, state, *, error=None, offset=None):
        with self._audit_lock:
            if self._audit['state'] == 'closed': return
            if error is not None: self._audit['last_failure_stage'] = self._audit['state']
            self._audit['state'] = state
            if error is not None: self._audit['last_error_code'] = error
            if offset is not None: self._audit['last_failure_offset'] = offset

    def audit(self):
        with self._audit_lock:
            return dict(self._audit,
                        upstream_status_counts=dict(self._audit['upstream_status_counts']),
                        redirect_counts=dict(self._audit['redirect_counts']),
                        last_redirect=dict(self._audit['last_redirect']),
                        media_auth=dict(self._audit['media_auth']),
                        session_recovery_events=[dict(row) for row in self._audit['session_recovery_events']])

    def _session_event(self, event, offset=None):
        with self._audit_lock:
            events = self._audit['session_recovery_events']
            if len(events) < (12 if self.allow_fresh_session_escalation else 8):
                events.append({'event':event, 'elapsed_seconds':round(time.monotonic()-self._started, 3),
                               'offset':offset if offset is not None else self._audit['last_failure_offset']})

    def _count(self, key, amount=1):
        with self._audit_lock:
            self._audit[key] += amount

    def _fail(self, code):
        if self._resume_pending:
            self._session_event('media_resume_failed')
            self._resume_pending = False
        if not self._stop.is_set():
            with self._audit_lock:
                self._audit['terminal_error_code'] = self._audit['terminal_error_code'] or code
                self._audit['state'] = 'failed'
        raise MediaTransportError(code)

    def _check_signing_budget(self):
        if self._stop.is_set(): raise MediaTransportError('stopped')

    def _signed_request(self, start, retry):
        self._check_signing_budget()
        now = int(time.time())
        # Initial-byte reuse may be rejected even with a new UUID. On retry,
        # wait for the actual clock to advance, never fabricate a future time.
        original = dict(parse_qsl(urlsplit(self.signed_url).query)).get('t','')
        try: original_time = int(original.rsplit('-',2)[-2])
        except (ValueError, IndexError): original_time = None
        if (start == 0 and now in (original_time, self._last_initial_time)) or retry:
            previous = now
            while now == previous:
                if self._stop.wait(.05): raise MediaTransportError('stopped')
                self._check_signing_budget()
                now = int(time.time())
        self._check_signing_budget()
        fresh = self.client.renew_video_url(self.signed_url, now=now)
        if self._stop.is_set(): raise MediaTransportError('stopped')
        if self._source.selected != media_identity(fresh):
            raise MediaTransportError('source_changed')
        target, raw_headers = self.client.get_stream_params(fresh)
        if self._stop.is_set(): raise MediaTransportError('stopped')
        self._source.bind_request(fresh, target)
        headers = dict(line.split(':',1) for line in raw_headers.split('\r\n') if ':' in line)
        headers = {key.strip(): value.strip() for key,value in headers.items()}
        headers['Accept-Encoding'] = 'identity'
        headers.update(self._source.conditional_headers())
        self._count('signature_renewals')
        if start == 0: self._last_initial_time = now
        return target, headers

    def _request_session(self, headers):
        if self._session is None:
            self._session = self.session_factory()
        login = getattr(getattr(self.client, 'vpn', None), 'session', None)
        if isinstance(login, requests.Session):
            # Share the scoped jar, including deletions/rotations. A manually
            # supplied Cookie header overrides Requests' fresh jar selection.
            self._session.cookies = login.cookies
            headers = {k:v for k,v in headers.items() if k.lower() != 'cookie'}
        return self._session, headers

    def _redirect_kind(self, response):
        routes = getattr(self.client, 'trusted_media_login_urls', None)
        login_urls = routes() if callable(routes) else ()
        return redirect_kind(response, login_urls=login_urls)

    def _can_refresh_session(self):
        return (self.allow_session_refresh
                and callable(getattr(self.client, 'refresh_media_session', None))
                and (not self._session_refreshed or
                     self.allow_fresh_session_escalation and self._owns_client
                     and not self._fresh_auth_used))

    def _refresh_session(self):
        refresh = getattr(self.client, 'refresh_media_session', None)
        if not self._can_refresh_session():
            self._fail('media_session_unavailable')
        escalate = self._session_refreshed
        self._session_refreshed = True
        self._count('session_refresh_attempts')
        self._session_event('authentication_started')
        if self._stop.is_set(): raise MediaTransportError('stopped')
        try:
            # An API-verified cookie can still fail on the media endpoint.
            # On its second login rejection, use one genuinely fresh login.
            success = False if escalate else refresh() is True
            reauthenticate = getattr(self.client, "reauthenticate_media_session", None)
            if not success and self._owns_client and callable(reauthenticate):
                self._fresh_auth_used = True
                self._count('fresh_session_attempts')
                timeout = getattr(self.client, '_media_reauth_timeout', None)
                options = {'timeout': timeout} if timeout is not None else {}
                success = reauthenticate(self._stop, **options) is True
        except Exception:
            success = False
        with self._audit_lock:
            self._audit['media_auth'] = dict(getattr(self.client, 'media_auth_audit', {}))
        if self._stop.is_set(): raise MediaTransportError('stopped')
        if self._audit['media_auth'].get('failure') == 'auth_tls_error': self._fail('upstream_tls_error')
        if not success: self._fail('media_session_unavailable')
        self._count('session_identity_verifications')
        self._session_event('identity_verified')
        # A verified new cookie jar must not inherit an upstream connection
        # authenticated under the old session. Keep the immutable source,
        # validator, buffered bytes and offset; only replace the HTTP pool.
        if self._session is not None:
            self._session.close()
            self._session = None
        self._resume_pending = True

    def _verify_response(self, response, start, end, *, open_ended=False):
        with self._audit_lock:
            counts=self._audit['upstream_status_counts']
            label=str(response.status_code)
            counts[label]=counts.get(label,0)+1
        if response.status_code == 412: raise MediaTransportError('source_changed')
        if response.status_code == 401:
            raise MediaTransportError('media_session_refresh_needed')
        if response.status_code in (403,408,429,500,502,503,504):
            raise MediaTransportError('upstream_retryable_http')
        if response.status_code != 206:
            self._count('range_rejections')
            # Never consume an ignored range or follow a login redirect. A
            # fresh signature can recover transient anti-replay responses.
            if response.status_code in (301,302,303,307,308):
                kind = self._redirect_kind(response)
                with self._audit_lock:
                    counts = self._audit['redirect_counts']
                    counts[kind] = counts.get(kind,0)+1
                    self._audit['last_redirect'] = dict(redirect_observation(response), classification=kind)
                if kind == 'login':
                    self._session_event('login_redirect', start)
                    raise MediaTransportError('media_session_refresh_needed')
                observed = redirect_observation(response)
                if (self._can_refresh_session()
                        and observed.get('authority') == 'same_origin'
                        and observed.get('route') in ('root', 'vpn_control')
                        and not observed.get('downgrade')
                        and not observed.get('credential_authority')
                        and callable(getattr(self.client, 'refresh_media_session', None))):
                    # An unknown native WebVPN route is never followed or
                    # trusted as media. Probe our original portal/API once;
                    # only a fresh 206 with the frozen validator can resume.
                    with self._audit_lock:
                        self._audit['last_redirect']['recovery'] = 'probe_original_session'
                    raise MediaTransportError('media_session_refresh_needed')
                if kind != 'same_media': raise MediaTransportError('media_redirect_untrusted')
                raise MediaTransportError('range_not_honored')
            if response.status_code == 200:
                raise MediaTransportError('range_not_honored')
            raise MediaTransportError('upstream_http_rejected')
        remaining = self._source.verify_range(response, start, end, open_ended=open_ended)
        with self._audit_lock:
            self._audit.update(source_total_bytes=self.total, validator_kind=self._source.validator_kind)
        self._count('range_verified')
        return remaining

    def _read_verified_body(self, response, remaining, data):
        while remaining:
            if self._stop.is_set(): raise MediaTransportError('stopped')
            size = min(64*1024, remaining)
            if self.max_bytes is not None:
                budget = self.max_bytes-self.audit()['upstream_bytes']
                if budget <= 0: raise MediaTransportError('diagnostic_byte_limit')
                size = min(size, budget)
            block = response.raw.read(size, decode_content=False)
            if not block: raise MediaTransportError('upstream_premature_eof')
            if len(block) > size: raise MediaTransportError('invalid_read_length')
            data.extend(block); remaining -= len(block)
            self._count('upstream_bytes', len(block))

    def _read_range(self, start, end, *, initial_probe=False):
        # One lock protects source binding, session recovery and verified cache.
        # Failed or closed relays never serve old cached bytes as a recovery.
        with self._fetch_lock:
            if self._stop.is_set(): raise MediaTransportError('stopped')
            if self.audit()['terminal_error_code']: raise MediaTransportError('transport_failed')
            cached = self._buffer.get(start, end)
            if cached is not None:
                self._count('cache_hits'); self._count('cached_bytes_served', len(cached))
                return cached
            data = bytearray()
            for attempt in range(self._recovery.attempts):
                if self._stop.is_set(): raise MediaTransportError('stopped')
                response = None
                error_code = None
                try:
                    offset = start+len(data)
                    self._transition('signing')
                    target, headers = self._signed_request(offset, attempt > 0)
                    session, headers = self._request_session(headers)
                    before_cookies = [(c.domain,c.path,c.name,c.value,c.expires) for c in session.cookies]
                    self._count('range_requests')
                    if self._resume_pending: self._count('session_resume_attempts')
                    open_ended = initial_probe and offset == 0
                    range_value = f'bytes={offset}-'+('' if open_ended else str(end))
                    self._transition('requesting')
                    if self._stop.is_set(): raise MediaTransportError('stopped')
                    response = session.get(target, headers={**headers,'Range':range_value},
                                           stream=True, timeout=self.timeout, allow_redirects=False)
                    with self._response_lock: self._responses.add(response)
                    after_cookies = [(c.domain,c.path,c.name,c.value,c.expires) for c in session.cookies]
                    if after_cookies != before_cookies: self._count('cookie_updates')
                    self._transition('validating')
                    remaining = self._verify_response(response, offset, end, open_ended=open_ended)
                    self._transition('reading')
                    self._read_verified_body(response, remaining, data)
                    if self._stop.is_set(): raise MediaTransportError('stopped')
                    if self._resume_pending:
                        self._count('session_refresh_successes')
                        self._session_event('media_resumed', offset)
                        self._resume_pending = False
                    result = bytes(data)
                    self._buffer.put(start, result)
                    with self._audit_lock: self._audit['cache_bytes'] = self._buffer.bytes
                    self._transition('ready')
                    return result
                except MediaTransportError as error:
                    error_code = error.code
                except (requests.RequestException, OSError) as error:
                    error_code = connection_failure_code(error)
                except Exception as error:
                    from urllib3.exceptions import HTTPError
                    error_code = connection_failure_code(error) if isinstance(error, HTTPError) else 'transport_internal_error'
                finally:
                    if response is not None:
                        with self._response_lock: self._responses.discard(response)
                        response.close()
                if self._stop.is_set(): raise MediaTransportError('stopped')
                self._transition('recovering', error=error_code, offset=start+len(data))
                # Header validation can already have marked a permanent failure.
                if self.audit()['terminal_error_code']: self._fail(error_code)
                can_refresh = self._can_refresh_session()
                action = self._recovery.decide(error_code, attempt, can_refresh=can_refresh)
                if action is RecoveryAction.STOP:
                    self._fail(self._recovery.terminal_code(error_code))
                if action is RecoveryAction.REFRESH:
                    self._transition('refreshing')
                    self._refresh_session()
                self._count('retries')
                self._transition('retrying')
                if self._stop.wait(self._recovery.delay(error_code, attempt)):
                    raise MediaTransportError('stopped')
            raise AssertionError('Bounded recovery must return or stop')

    def start(self):
        try:
            # Match the known-working initial bytes=0- form, but read only a
            # bounded prefix and close. Subsequent reads use closed ranges.
            self._prefix = self._read_range(0,self.prefix_bytes-1,initial_probe=True)
            owner = self
            class Handler(BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.1'
                def log_message(self,*args): pass
                def do_HEAD(self): self.serve(False)
                def do_GET(self): self.serve(True)
                def serve(self,body):
                    self.close_connection = True
                    if self.path != owner._path:
                        self.send_error(404); return
                    if owner._stop.is_set() or owner.audit()['terminal_error_code']:
                        self.send_error(503); return
                    value = self.headers.get('Range')
                    start,end = 0,owner.total-1
                    if value:
                        match = re.fullmatch(r'bytes=(\d+)-(\d*)',value)
                        if not match:
                            self.send_error(416);return
                        start = int(match[1]); end = min(int(match[2]) if match[2] else end,end)
                        if start > end:
                            self.send_error(416);return
                    self.send_response(206 if value else 200)
                    self.send_header('Accept-Ranges','bytes')
                    self.send_header('Content-Type','application/octet-stream')
                    self.send_header('Content-Length',str(end-start+1))
                    self.send_header('Connection','close')
                    if value: self.send_header('Content-Range',f'bytes {start}-{end}/{owner.total}')
                    self.end_headers()
                    if not body:return
                    try:
                        position = start
                        while position <= end and not owner._stop.is_set():
                            if owner.audit()['terminal_error_code']: return
                            if position < len(owner._prefix):
                                data = owner._prefix[position:min(len(owner._prefix),end+1)]
                            else:
                                finish = min(position+owner.chunk_bytes-1,end)
                                data = owner._read_range(position,finish)
                            self.wfile.write(data); self.wfile.flush()
                            position += len(data)
                    except (OSError,MediaTransportError):
                        # Closing a short response lets the existing FFmpeg EOF
                        # and duration gates reject it. No fabricated tail.
                        return
            class Server(ThreadingHTTPServer):
                daemon_threads = True
                def handle_error(self,*args): pass  # No private traceback.
            self._server = Server(('127.0.0.1',0),Handler)
            self.url = f'http://127.0.0.1:{self._server.server_port}{self._path}'
            self._thread = threading.Thread(target=self._server.serve_forever,daemon=True)
            self._thread.start()
            return self
        except Exception:
            self.close()
            raise

    def close(self):
        with self._close_lock:
            if self._closed:return
            self._closed = True; self._stop.set()
            with self._response_lock: responses = list(self._responses)
            for response in responses: response.close()
            if self._server is not None:
                if self._thread is not None:self._server.shutdown()
                self._server.server_close()
            if self._thread is not None:self._thread.join(timeout=2)
            if self._session is not None:self._session.close()
            if self._owns_client: self.client.close_media_session()
            self._buffer.close()
            with self._audit_lock: self._audit['cache_bytes'] = 0
            self._transition('closed')

    def __enter__(self): return self.start()
    def __exit__(self,*args): self.close()
