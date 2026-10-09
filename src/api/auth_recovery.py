"""Bounded fresh-session authentication; never replay tickets or retain credentials."""
import time

import requests

from src.api.webvpn import AuthenticationError, WebVPNSession, authentication_failure


RETRYABLE_AUTH_REASONS = frozenset(('service_unavailable', 'cold_session',
                                  'cas_context_missing', 'api_verification_failed'))
PREFLIGHT_TRANSIENT_CODES = frozenset(('service_unavailable', 'auth_read_timeout',
    'auth_connect_timeout', 'auth_timeout', 'auth_connection_error'))


def retryable_auth_response(error):
    """Malformed transient replies restart the flow, never repeat its POST."""
    if not isinstance(error, requests.exceptions.JSONDecodeError): return False
    audit = authentication_failure(error)
    if audit['failure_phase'] not in ('webvpn_auth_methods', 'webvpn_public_key',
            'webvpn_auth_execute', 'icourse_auth_methods', 'icourse_public_key',
            'icourse_auth_execute'): return False
    reply = audit.get('response', {})
    if reply.get('challenge_hint') is not False or reply.get('redirected') is not False:
        return False
    status, kind = reply.get('http_status'), reply.get('body_kind')
    return (status in (408, 429, 500, 502, 503, 504) and kind in ('empty', 'html', 'json', 'other')
            or status == 200 and kind in ('empty', 'json'))


def authenticated_session(*, max_attempts=3, student_id=None, password=None,
                          factory=WebVPNSession, sleep=time.sleep, probe_attempts=1):
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise ValueError('Invalid bounded authentication attempts')
    if type(probe_attempts) is not int or not 1 <= probe_attempts <= 3:
        raise ValueError('Invalid bounded preflight attempts')
    history = []
    for attempt in range(max_attempts):
        vpn = factory()
        probe_completed = False
        try:
            # This retry is only a credential-free portal probe. It never
            # repeats password submission or follows a consumed CAS ticket.
            for probe in range(probe_attempts):
                vpn.auth_probe_attempts = probe+1
                vpn.auth_probe_transient_failures = probe
                try:
                    vpn.probe_login_service()
                    break
                except Exception as error:
                    temporary = (isinstance(error, AuthenticationError)
                                 and error.reason == 'service_unavailable'
                                 or isinstance(error, (requests.exceptions.Timeout,
                                                      requests.exceptions.ConnectionError))
                                 and not isinstance(error, requests.exceptions.SSLError))
                    if temporary: vpn.auth_probe_transient_failures = probe+1
                    if not temporary or probe == probe_attempts-1: raise
                    session = vpn.session
                    if isinstance(session, DeadlineSession):
                        remaining = session.deadline-time.monotonic()
                        if remaining <= 0 or session.cancelled.wait(min(1,remaining)):
                            raise AuthenticationError('media_auth_cancelled')
                    else: sleep(1)
            probe_completed = True
            if student_id is None and password is None:
                if getattr(vpn, 'requires_webvpn_login', True): vpn.login()
                verified = vpn.authenticate_icourse(strict=True)
            else:
                if getattr(vpn, 'requires_webvpn_login', True): vpn.login(student_id, password)
                verified = vpn.authenticate_icourse(student_id, password, strict=True)
            if verified is not True:
                raise AuthenticationError('api_verification_failed')
            return vpn
        except Exception as error:
            error.auth_failure_diagnostics = authentication_failure(error, vpn=vpn)
            # Local control-flow proof, independent of an exception's phase.
            error.auth_failure_diagnostics['precredential_failure'] = not probe_completed
            history.append({k: v for k, v in error.auth_failure_diagnostics.items()
                            if k not in ('auth_attempts', 'attempt_failures')})
            error.auth_failure_diagnostics.update(auth_attempts=attempt+1,
                                                  attempt_failures=list(history))
            vpn.session.close()
            retryable = (isinstance(error, AuthenticationError)
                         and error.reason in RETRYABLE_AUTH_REASONS
                         or isinstance(error, (requests.exceptions.Timeout,
                                               requests.exceptions.ConnectionError))
                         and not isinstance(error, requests.exceptions.SSLError)
                         or retryable_auth_response(error))
            if not retryable or attempt == max_attempts-1:
                raise
            # The next attempt gets a new Session and a new one-use ticket.
            # Outage probes run before credentials are submitted.
            sleep(min(5*(attempt+1), 10))


def initial_authenticated_session(*, max_attempts=3, student_id=None, password=None,
                                  factory=WebVPNSession, sleep=time.sleep):
    """Two delayed rounds only when every previous failure preceded login.

    At the default limit: nine portal probes at most, with 30/60-second
    cooldowns between three ordinary rounds. No extra round after any login
    flow begins; media reauthentication deliberately does not use this entry.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise ValueError('Invalid bounded authentication attempts')
    rounds = min(3, 10 // max_attempts)
    history, waited = [], 0
    for recovery in range(rounds):
        try:
            vpn = authenticated_session(max_attempts=max_attempts,
                student_id=student_id, password=password, factory=factory, sleep=sleep)
            if recovery:
                print(f'[Auth] iCourse identity verified after portal recovery '
                      f'round {recovery+1}/{rounds}.')
            return vpn
        except Exception as error:
            audit = authentication_failure(error)
            failures = audit.get('attempt_failures', [])
            eligible = (len(failures) == max_attempts
                and all(row.get('precredential_failure') is True
                    and row.get('failure_phase') == 'login_service_probe'
                    and row.get('failure') in PREFLIGHT_TRANSIENT_CODES
                    for row in failures))
            history.extend(failures)
            audit.update(auth_attempts=len(history), attempt_failures=list(history),
                         preflight_recovery_rounds=recovery+1,
                         preflight_recovery_wait_seconds=waited)
            error.auth_failure_diagnostics = audit
            if not eligible or recovery == rounds-1:
                raise
            delay = 30 * (recovery+1)
            print(f'[Auth] Portal unavailable before login; cooling down {delay}s '
                  f'before recovery round {recovery+2}/{rounds}.')
            sleep(delay)
            waited += delay


class DeadlineSession(requests.Session):
    """Each auth request/redirect checks the same cancellation and wall deadline."""
    def __init__(self, cancelled, deadline):
        super().__init__()
        self.cancelled, self.deadline = cancelled, deadline

    def send(self, request, **kwargs):
        remaining = self.deadline-time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise AuthenticationError('media_auth_cancelled')
        requested = kwargs.get('timeout') or (10, 10)
        if not isinstance(requested, tuple): requested = (requested, requested)
        kwargs['timeout'] = tuple(min(float(v or cap), cap, remaining)
                                  for v, cap in zip(requested, (10, 30)))
        return super().send(request, **kwargs)


def fresh_media_session(cancelled, deadline):
    """One new ticket flow from configured credentials, never a media Location."""
    def factory():
        vpn = WebVPNSession()
        vpn.session.close()
        vpn.session = DeadlineSession(cancelled, deadline)
        from src.runtime import config
        vpn.session.headers.update({'User-Agent': config.USER_AGENT})
        return vpn
    # One fresh login, with one bounded retry of the no-credential preflight
    # inside the existing 75-second deadline; no retry after login begins.
    return authenticated_session(max_attempts=1, factory=factory, probe_attempts=2)
