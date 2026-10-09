"""Media outage probes recover without increasing password or deadline budgets."""
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests
from src.api.auth_recovery import authenticated_session, fresh_media_session
from src.api.webvpn import AuthenticationError, WebVPNSession, authentication_failure
from src.runtime.audio_preparation import safe_transport_diagnostics
from test_initial_auth_recovery import slow_headers_portal


class MediaPreflightRecoveryTests(unittest.TestCase):
    def vpn(self, failures):
        vpn = WebVPNSession()
        vpn.probe_login_service = MagicMock(side_effect=failures)
        vpn.login = MagicMock()
        vpn.authenticate_icourse = MagicMock(return_value=True)
        return vpn

    def test_real_header_timeouts_exhaust_old_two_probes(self):
        with slow_headers_portal(failures=2) as (state, sessions, factory):
            with self.assertRaises(requests.exceptions.ReadTimeout):
                authenticated_session(max_attempts=1, probe_attempts=2,
                                      factory=factory, sleep=MagicMock())
            self.assertEqual(state['requests'], 2)
            for vpn in sessions: vpn.login.assert_not_called()

    def test_real_header_timeouts_recover_on_third_probe_with_one_login(self):
        with slow_headers_portal(failures=2) as (state, sessions, factory):
            sleep = MagicMock()
            vpn = authenticated_session(max_attempts=1, probe_attempts=3,
                                        probe_backoff=(5,15), factory=factory, sleep=sleep)
            self.assertEqual(state['requests'], 3)
            self.assertEqual(state['credentials'], 0)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [5,15])
            vpn.login.assert_called_once()
            vpn.authenticate_icourse.assert_called_once_with(strict=True)

    def test_fresh_media_factory_obeys_existing_deadline_and_one_login(self):
        clock = [0.0]; stopped = threading.Event()
        def wait(delay): clock[0] += delay; return False
        vpn = self.vpn([requests.exceptions.ReadTimeout(), requests.exceptions.ReadTimeout(), None])
        with patch('src.api.auth_recovery.WebVPNSession', return_value=vpn), \
             patch('src.api.auth_recovery.time.monotonic', side_effect=lambda:clock[0]), \
             patch.object(stopped, 'wait', side_effect=wait) as waits:
            self.assertIs(fresh_media_session(stopped, 75), vpn)
        self.assertEqual([c.args[0] for c in waits.call_args_list], [5,15])
        self.assertEqual(vpn.session.deadline, 75)
        self.assertEqual(vpn.auth_probe_attempts, 3)
        vpn.login.assert_called_once(); vpn.authenticate_icourse.assert_called_once_with(strict=True)
        vpn.session.close()

    def test_deadline_during_backoff_prevents_another_probe_or_password(self):
        clock = [0.0]; stopped = threading.Event()
        def wait(delay): clock[0] += delay; return False
        vpn = self.vpn([requests.exceptions.ReadTimeout(), None])
        with patch('src.api.auth_recovery.WebVPNSession', return_value=vpn), \
             patch('src.api.auth_recovery.time.monotonic', side_effect=lambda:clock[0]), \
             patch.object(stopped, 'wait', side_effect=wait) as waits:
            with self.assertRaises(AuthenticationError) as caught: fresh_media_session(stopped, 2)
        waits.assert_called_once_with(2)
        self.assertEqual(caught.exception.reason, 'media_auth_cancelled')
        vpn.probe_login_service.assert_called_once(); vpn.login.assert_not_called()

    def test_cancellation_during_backoff_prevents_another_probe_or_password(self):
        stopped = threading.Event()
        def wait(delay): stopped.set(); return True
        vpn = self.vpn([requests.exceptions.ReadTimeout(), None])
        with patch('src.api.auth_recovery.WebVPNSession', return_value=vpn), \
             patch('src.api.auth_recovery.time.monotonic', return_value=0), \
             patch.object(stopped, 'wait', side_effect=wait):
            with self.assertRaises(AuthenticationError): fresh_media_session(stopped, 75)
        vpn.probe_login_service.assert_called_once(); vpn.login.assert_not_called()

    def test_third_timeout_stops_without_password_or_additional_auth_flow(self):
        stopped = threading.Event()
        vpn = self.vpn([requests.exceptions.ReadTimeout('private') for _ in range(3)])
        with patch('src.api.auth_recovery.WebVPNSession', return_value=vpn), \
             patch('src.api.auth_recovery.time.monotonic', return_value=0), \
             patch.object(stopped, 'wait', return_value=False):
            with self.assertRaises(requests.exceptions.ReadTimeout) as caught: fresh_media_session(stopped,75)
        self.assertEqual(vpn.probe_login_service.call_count,3); vpn.login.assert_not_called()
        audit = authentication_failure(caught.exception)
        self.assertEqual(audit['auth_attempts'],1); self.assertEqual(audit['probe_attempts'],3)

    def test_public_media_authentication_audit_reveals_phase_without_sensitive_fields(self):
        auth = {'failure_phase':'login_service_probe','error_type':'ReadTimeout',
                'failure':'auth_read_timeout','probe_attempts':2,'probe_transient_failures':2,
                'auth_attempts':1,'cookie':'private','url':'private',
                'attempt_failures':[{'failure_phase':'login_service_probe',
                    'error_type':'ReadTimeout','failure':'auth_read_timeout','url':'private'}]}
        public = safe_transport_diagnostics({'media_auth':auth})
        self.assertEqual(public['media_authentication']['failure_phase'],'login_service_probe')
        self.assertEqual(public['media_authentication']['probe_attempts'],2)
        self.assertNotIn('private',json.dumps(public))
        self.assertEqual(auth['cookie'],'private')
        self.assertEqual(safe_transport_diagnostics({'media_auth':{'failure':[]}}),{})

    def test_invalid_backoff_never_constructs_a_session(self):
        for delay in ((1,), (1,True), (1,float('nan')), (1,float('inf')), (0,1), (1,31), [1,1]):
            factory = MagicMock()
            with self.assertRaises(ValueError): authenticated_session(factory=factory,probe_backoff=delay)
            factory.assert_not_called()
