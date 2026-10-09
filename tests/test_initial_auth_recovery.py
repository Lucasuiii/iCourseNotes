"""Delayed precredential recovery, with real HTTP header timeouts and no account."""
import contextlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import MagicMock

import requests
from src.api.auth_recovery import authenticated_session, initial_authenticated_session
from src.api.webvpn import AuthenticationError, WebVPNSession, authentication_failure


class InitialAuthRecoveryTests(unittest.TestCase):
    def vpn(self, error=None, *, after_probe=False):
        vpn = MagicMock(auth_phase='login_service_probe')
        vpn.authenticate_icourse.return_value = True
        if error is not None:
            (vpn.login if after_probe else vpn.probe_login_service).side_effect = error
        return vpn

    def test_outage_recovers_after_cooldown_without_extra_password_submissions(self):
        failed = [self.vpn(requests.exceptions.ReadTimeout('private')) for _ in range(3)]
        recovered = self.vpn()
        factory, sleep = MagicMock(side_effect=failed+[recovered]), MagicMock()
        self.assertIs(initial_authenticated_session(factory=factory, sleep=sleep), recovered)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10, 30])
        for vpn in failed:
            vpn.login.assert_not_called(); vpn.authenticate_icourse.assert_not_called()
            vpn.session.close.assert_called_once()
        recovered.login.assert_called_once()
        recovered.authenticate_icourse.assert_called_once_with(strict=True)
        recovered.session.close.assert_not_called()

    def test_persistent_outage_is_bounded_and_safe_audit_keeps_all_nine_attempts(self):
        sessions = [self.vpn(requests.exceptions.ReadTimeout('private-url')) for _ in range(9)]
        factory, sleep = MagicMock(side_effect=sessions), MagicMock()
        with self.assertRaises(requests.exceptions.ReadTimeout) as caught:
            initial_authenticated_session(factory=factory, sleep=sleep)
        self.assertEqual(factory.call_count, 9)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10, 30, 5, 10, 60, 5, 10])
        audit = authentication_failure(caught.exception)
        self.assertEqual(audit['auth_attempts'], 9)
        self.assertEqual(len(audit['attempt_failures']), 9)
        self.assertEqual(audit['preflight_recovery_rounds'], 3)
        self.assertEqual(audit['preflight_recovery_wait_seconds'], 90)
        self.assertTrue(all(r['precredential_failure'] for r in audit['attempt_failures']))
        self.assertNotIn('private', json.dumps(audit))
        for vpn in sessions:
            vpn.login.assert_not_called(); vpn.session.close.assert_called_once()

    def test_every_supported_transient_probe_failure_can_recover(self):
        for kind in (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout,
                     requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            with self.subTest(kind=kind):
                failed, good = self.vpn(kind('private')), self.vpn()
                self.assertIs(initial_authenticated_session(max_attempts=1,
                    factory=MagicMock(side_effect=[failed, good]), sleep=MagicMock()), good)
        failed, good = self.vpn(AuthenticationError('service_unavailable')), self.vpn()
        self.assertIs(initial_authenticated_session(max_attempts=1,
            factory=MagicMock(side_effect=[failed, good]), sleep=MagicMock()), good)

    def test_permanent_failures_never_get_the_extra_round(self):
        for error in (requests.exceptions.SSLError('private'),
                      AuthenticationError('service_redirect_untrusted'),
                      AuthenticationError('service_http_rejected'),
                      AuthenticationError('authentication_rejected'),
                      AuthenticationError('password_method_missing')):
            with self.subTest(reason=type(error).__name__):
                vpn = self.vpn(error); factory, sleep = MagicMock(return_value=vpn), MagicMock()
                with self.assertRaises(type(error)):
                    initial_authenticated_session(factory=factory, sleep=sleep)
                factory.assert_called_once(); sleep.assert_not_called(); vpn.login.assert_not_called()

    def test_phase_alone_cannot_enable_extra_recovery_after_login_started(self):
        # An exception still claiming probe phase must not conceal control flow.
        sessions = [self.vpn(requests.exceptions.ReadTimeout('private-ticket'), after_probe=True)
                    for _ in range(3)]
        factory, sleep = MagicMock(side_effect=sessions), MagicMock()
        with self.assertRaises(requests.exceptions.ReadTimeout) as caught:
            initial_authenticated_session(factory=factory, sleep=sleep)
        self.assertEqual(factory.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])
        self.assertFalse(authentication_failure(caught.exception)['precredential_failure'])
        for vpn in sessions: vpn.login.assert_called_once()

    def test_mixed_probe_and_login_failures_do_not_extend_authentication_budget(self):
        sessions = [self.vpn(requests.exceptions.ReadTimeout('private')),
                    self.vpn(requests.exceptions.ReadTimeout('private'), after_probe=True),
                    self.vpn(requests.exceptions.ReadTimeout('private'))]
        factory, sleep = MagicMock(side_effect=sessions), MagicMock()
        with self.assertRaises(requests.exceptions.ReadTimeout):
            initial_authenticated_session(factory=factory, sleep=sleep)
        self.assertEqual(factory.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])

    def test_recovered_portal_still_requires_strict_identity_verification(self):
        failed, good = self.vpn(requests.exceptions.ReadTimeout('private')), self.vpn()
        good.authenticate_icourse.return_value = False
        factory = MagicMock(side_effect=[failed, good])
        with self.assertRaises(AuthenticationError):
            initial_authenticated_session(max_attempts=1, factory=factory, sleep=MagicMock())
        self.assertEqual(factory.call_count, 2)
        good.authenticate_icourse.assert_called_once_with(strict=True)
        good.session.close.assert_called_once()

    def test_later_round_login_failure_cannot_unlock_the_final_cooldown(self):
        sessions = [self.vpn(requests.exceptions.ReadTimeout('private')) for _ in range(3)]
        sessions += [self.vpn(requests.exceptions.ReadTimeout('private'), after_probe=True),
                     self.vpn(requests.exceptions.ReadTimeout('private')),
                     self.vpn(requests.exceptions.ReadTimeout('private'))]
        factory, sleep = MagicMock(side_effect=sessions), MagicMock()
        with self.assertRaises(requests.exceptions.ReadTimeout) as caught:
            initial_authenticated_session(factory=factory, sleep=sleep)
        self.assertEqual(factory.call_count, 6)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10, 30, 5, 10])
        self.assertEqual(authentication_failure(caught.exception)['auth_attempts'], 6)

    def test_prepare_audit_retains_recovery_evidence_without_provider_text(self):
        import tempfile
        from unittest.mock import patch
        from scripts import production_qwen as pipeline
        sessions = [self.vpn(requests.exceptions.ReadTimeout('private-cookie')) for _ in range(9)]
        with self.assertRaises(requests.exceptions.ReadTimeout) as caught:
            initial_authenticated_session(factory=MagicMock(side_effect=sessions), sleep=MagicMock())
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'RUNNER_TEMP':tmp}):
            pipeline.preparation_failure_audit({'prepare_phase':'login'}, {}, caught.exception)
            audit = json.loads(pipeline.out('prepare-failure.json').read_text())
        self.assertEqual(audit['authentication']['auth_attempts'], 9)
        self.assertEqual(audit['authentication']['preflight_recovery_rounds'], 3)
        self.assertEqual(audit['authentication']['preflight_recovery_wait_seconds'], 90)
        self.assertNotIn('private', json.dumps(audit))

    def test_invalid_limits_and_large_limits_cannot_expand_history_unboundedly(self):
        for value in (0, 11, True, 2.5):
            factory = MagicMock()
            with self.assertRaises(ValueError): initial_authenticated_session(max_attempts=value, factory=factory)
            factory.assert_not_called()
        sessions = [self.vpn(requests.exceptions.ReadTimeout('private')) for _ in range(10)]
        factory = MagicMock(side_effect=sessions)
        with self.assertRaises(requests.exceptions.ReadTimeout):
            initial_authenticated_session(max_attempts=10, factory=factory, sleep=MagicMock())
        self.assertEqual(factory.call_count, 10)

    def test_untrusted_audit_fields_and_types_are_removed(self):
        error = requests.exceptions.ReadTimeout('private')
        error.auth_failure_diagnostics = {'precredential_failure':'private',
            'preflight_recovery_rounds':999, 'preflight_recovery_wait_seconds':True,
            'attempt_failures':[{'precredential_failure':'private', 'url':'private'}]}
        self.assertEqual(authentication_failure(error)['attempt_failures'], [{}])
        self.assertNotIn('private', json.dumps(authentication_failure(error)))


@contextlib.contextmanager
def slow_headers_portal():
    state = {'requests':0, 'methods':[], 'credentials':0}
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            with lock:
                state['requests'] += 1
                number = state['requests']
                state['methods'].append(self.command)
                state['credentials'] += int(bool(self.headers.get('Cookie') or self.headers.get('Authorization')))
            if number <= 3: time.sleep(.15)
            self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda:server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}/'
    sessions = []
    class LocalVPN(WebVPNSession):
        @property
        def portal_url(self): return base
        def __init__(self):
            super().__init__(); self.session.trust_env = False
            original = self.session.get
            def get(url, **kwargs):
                kwargs['timeout'] = (.1, .04)
                return original(url, **kwargs)
            self.session.get = get
            self.login = MagicMock()
            self.authenticate_icourse = MagicMock(return_value=True)
            sessions.append(self)
    try: yield state, sessions, LocalVPN
    finally:
        for vpn in sessions: vpn.session.close()
        server.shutdown(); server.server_close(); thread.join(timeout=1)


class RealHeaderTimeoutTests(unittest.TestCase):
    def test_old_three_attempt_policy_exhausts_before_portal_recovery(self):
        with slow_headers_portal() as (state, sessions, factory):
            with self.assertRaises(requests.exceptions.ReadTimeout):
                authenticated_session(factory=factory, sleep=MagicMock())
            self.assertEqual(state['requests'], 3)
            for vpn in sessions: vpn.login.assert_not_called()
            self.assertEqual(state['credentials'], 0)

    def test_delayed_recovery_survives_the_same_real_http_header_timeouts(self):
        with slow_headers_portal() as (state, sessions, factory):
            sleep = MagicMock()
            result = initial_authenticated_session(factory=factory, sleep=sleep)
            self.assertEqual(state['requests'], 4)
            self.assertEqual(state['methods'], ['GET']*4)
            self.assertEqual(state['credentials'], 0)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10, 30])
            self.assertIs(result, sessions[-1])
            for vpn in sessions[:-1]: vpn.login.assert_not_called()
            result.login.assert_called_once()
            result.authenticate_icourse.assert_called_once_with(strict=True)
