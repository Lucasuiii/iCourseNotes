"""No live login: bounded preflight retry, exact phases and immutable late audits."""
import copy
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import requests
from src.api.auth_recovery import authenticated_session, fresh_media_session
from src.api.icourse import ICourseClient
from src.api.webvpn import AuthenticationError, WebVPNSession, authentication_failure
from src.runtime.audio_preparation import startup_diagnostics
import test_webvpn_auth_diagnostics as auth_fixtures


class AuthFailureTests(unittest.TestCase):
    def test_error_classes_and_phase_are_safe_fixed_fields(self):
        errors=((requests.exceptions.ReadTimeout('private-url'),'ReadTimeout','auth_read_timeout'),
                (requests.exceptions.ConnectTimeout('private-url'),'ConnectTimeout','auth_connect_timeout'),
                (requests.exceptions.SSLError('private-cookie'),'SSLError','auth_tls_error'),
                (requests.exceptions.ConnectionError('private-url'),'ConnectionError','auth_connection_error'),
                (requests.exceptions.JSONDecodeError('private-body','private',0),'JSONDecodeError','auth_invalid_response'),
                (ValueError('private-value'),'ValueError','auth_invalid_value'),
                (AuthenticationError('ticket_destination_untrusted'),'AuthenticationError','ticket_destination_untrusted'))
        for error, kind, code in errors:
            with self.subTest(kind=kind):
                result=authentication_failure(error,'webvpn_ticket_follow')
                self.assertEqual(result,{'failure_phase':'webvpn_ticket_follow','error_type':kind,'failure':code})
                self.assertNotIn('private',json.dumps(result))
        error=ValueError('private')
        error.auth_failure_diagnostics={'failure_phase':'private-url','error_type':'private-password',
            'failure':'private-token','probe_attempts':'private','extra':'private-cookie'}
        result=authentication_failure(error,'private-url')
        self.assertEqual(result['failure_phase'],'unknown')
        self.assertNotIn('private',json.dumps(result))

    def test_credential_free_preflight_recovers_without_repeating_login(self):
        vpn=MagicMock(auth_phase='login_service_probe')
        vpn.probe_login_service.side_effect=[requests.exceptions.ReadTimeout('private-url'),None]
        vpn.authenticate_icourse.return_value=True
        sleep=MagicMock();factory=MagicMock(return_value=vpn)
        self.assertIs(authenticated_session(max_attempts=1,probe_attempts=2,factory=factory,sleep=sleep),vpn)
        factory.assert_called_once();self.assertEqual(vpn.probe_login_service.call_count,2)
        sleep.assert_called_once_with(1);vpn.login.assert_called_once();vpn.authenticate_icourse.assert_called_once()
        self.assertEqual(vpn.auth_probe_attempts,2);self.assertEqual(vpn.auth_probe_transient_failures,1)

    def test_preflight_exhaustion_preserves_reason_without_submitting_password(self):
        vpn=MagicMock(auth_phase='login_service_probe')
        vpn.probe_login_service.side_effect=requests.exceptions.ReadTimeout('private-url')
        with self.assertRaises(requests.exceptions.ReadTimeout) as raised:
            authenticated_session(max_attempts=1,probe_attempts=2,factory=lambda:vpn,sleep=MagicMock())
        vpn.login.assert_not_called();vpn.authenticate_icourse.assert_not_called()
        self.assertEqual(vpn.probe_login_service.call_count,2);vpn.session.close.assert_called_once()
        result=raised.exception.auth_failure_diagnostics
        self.assertEqual(result['failure'],'auth_read_timeout');self.assertEqual(result['failure_phase'],'login_service_probe')
        self.assertEqual(result['probe_transient_failures'],2)
        self.assertEqual(startup_diagnostics('authentication',raised.exception)['authentication_failure'],result)
        self.assertNotIn('private',json.dumps(result))

    def test_preflight_never_retries_tls_or_untrusted_redirect(self):
        for error in (requests.exceptions.SSLError('private'),AuthenticationError('service_redirect_untrusted')):
            vpn=MagicMock(auth_phase='login_service_probe');vpn.probe_login_service.side_effect=error
            with self.assertRaises(type(error)):
                authenticated_session(max_attempts=1,probe_attempts=2,factory=lambda:vpn,sleep=MagicMock())
            vpn.probe_login_service.assert_called_once();vpn.login.assert_not_called()

    def test_ticket_timeout_is_not_retried_by_the_preflight_policy(self):
        vpn=MagicMock(auth_phase='webvpn_ticket_follow');vpn.login.side_effect=requests.exceptions.ReadTimeout('private-ticket')
        factory=MagicMock(return_value=vpn)
        with self.assertRaises(requests.exceptions.ReadTimeout) as raised:
            authenticated_session(max_attempts=1,probe_attempts=2,factory=factory,sleep=MagicMock())
        factory.assert_called_once();vpn.probe_login_service.assert_called_once();vpn.login.assert_called_once()
        self.assertEqual(raised.exception.auth_failure_diagnostics['failure_phase'],'webvpn_ticket_follow')

    def test_every_icourse_network_step_is_marked_before_a_timeout(self):
        targets={'get':('icourse_portal_warmup','icourse_cas_context','icourse_public_key',
                        'icourse_ticket_follow','icourse_api_verification'),
                 'post':('icourse_auth_methods','icourse_auth_execute','icourse_cas_ticket')}
        for method,phases in targets.items():
            for index,phase in enumerate(phases):
                with self.subTest(phase=phase):
                    vpn=auth_fixtures.AuthDiagnosticTests().session()
                    call=getattr(vpn.session,method)
                    sequence=list(call.side_effect);sequence[index]=requests.exceptions.ReadTimeout('private-ticket')
                    call.side_effect=sequence
                    with self.assertRaises(requests.exceptions.ReadTimeout) as raised:
                        vpn.authenticate_icourse('private-student','private-password',strict=True)
                    self.assertEqual(authentication_failure(raised.exception,vpn=vpn)['failure_phase'],phase)
                    vpn.session.close()

    def test_webvpn_network_steps_are_marked_before_request_failure(self):
        for method,phase,args in (
            ('probe_login_service','login_service_probe',()),('_get_auth_context','webvpn_context',()),
            ('_query_auth_methods','webvpn_auth_methods',('private','private')),
            ('_get_public_key','webvpn_public_key',()),
            ('_auth_execute','webvpn_auth_execute',('private',)*6),
            ('_get_cas_ticket','webvpn_ticket',('private',)),
            ('_establish_session','webvpn_ticket_follow',('private',)),
            ('_verify_webvpn_session','webvpn_session_probe',())):
            with self.subTest(phase=phase):
                vpn=WebVPNSession();vpn.session.close();vpn.session=MagicMock();vpn.session.cookies=[]
                vpn.session.get.side_effect=requests.exceptions.ReadTimeout('private')
                vpn.session.post.side_effect=requests.exceptions.ReadTimeout('private')
                with self.assertRaises(requests.exceptions.ReadTimeout) as raised:getattr(vpn,method)(*args)
                self.assertEqual(authentication_failure(raised.exception,vpn=vpn)['failure_phase'],phase)

    def test_fresh_factory_keeps_one_login_and_three_delayed_bounded_preflights(self):
        with patch('src.api.auth_recovery.authenticated_session') as authenticate:
            fresh_media_session(threading.Event(),time.monotonic()+75)
            self.assertEqual(authenticate.call_args.kwargs['max_attempts'],1)
            self.assertEqual(authenticate.call_args.kwargs['probe_attempts'],3)
            self.assertEqual(authenticate.call_args.kwargs['probe_backoff'],(5,15))

    def test_failure_is_carried_out_of_discarded_factory_and_no_session_is_adopted(self):
        failure=requests.exceptions.ReadTimeout('private-ticket')
        failure.auth_failure_diagnostics={'failure_phase':'webvpn_ticket_follow',
            'error_type':'ReadTimeout','failure':'auth_read_timeout','probe_attempts':1}
        old=MagicMock();factory=MagicMock(side_effect=failure)
        client=ICourseClient(old,media_reauth_factory=factory)
        self.assertFalse(client.reauthenticate_media_session(threading.Event()))
        self.assertEqual(client.media_auth_audit['failure'],'auth_read_timeout')
        self.assertEqual(client.media_auth_audit['failure_phase'],'webvpn_ticket_follow')
        self.assertEqual(client.media_auth_audit['error_type'],'ReadTimeout')
        self.assertIs(client.vpn,old);factory.assert_called_once()
        self.assertNotIn('private',json.dumps(client.media_auth_audit))

    def test_identity_request_exception_has_its_own_phase(self):
        candidate=MagicMock();candidate.get.side_effect=requests.exceptions.ReadTimeout('private-api')
        client=ICourseClient(MagicMock(),media_reauth_factory=lambda *args:candidate)
        self.assertFalse(client.reauthenticate_media_session(threading.Event()))
        self.assertEqual(client.media_auth_audit['failure_phase'],'media_identity_verification')
        candidate.session.close.assert_called_once()

    def test_late_identity_check_cannot_overwrite_deadline_audit(self):
        entered=threading.Event();release=threading.Event();closed=threading.Event()
        def get(*args,**kwargs):
            entered.set();release.wait(2)
            raise requests.exceptions.ReadTimeout('private-api')
        candidate=MagicMock();candidate.get.side_effect=get;candidate.session.close.side_effect=closed.set
        old=MagicMock();client=ICourseClient(old,media_reauth_factory=lambda *args:candidate)
        self.assertFalse(client.reauthenticate_media_session(threading.Event(),timeout=.1))
        self.assertTrue(entered.is_set());self.assertEqual(client.media_auth_audit['failure'],'media_auth_deadline')
        snapshot=copy.deepcopy(client.media_auth_audit)
        release.set();self.assertTrue(closed.wait(2));time.sleep(.02)
        self.assertEqual(client.media_auth_audit,snapshot);self.assertIs(client.vpn,old)

    def test_cancelled_preflight_backoff_never_submits_credentials(self):
        from src.api.auth_recovery import DeadlineSession
        stopped=threading.Event();vpn=WebVPNSession();vpn.session.close()
        vpn.session=DeadlineSession(stopped,time.monotonic()+75)
        def probe():
            vpn.auth_phase='login_service_probe';stopped.set()
            raise requests.exceptions.ReadTimeout('private-url')
        vpn.probe_login_service=probe;vpn.login=MagicMock()
        with self.assertRaises(AuthenticationError) as raised:
            authenticated_session(max_attempts=1,probe_attempts=2,factory=lambda:vpn)
        self.assertEqual(raised.exception.reason,'media_auth_cancelled');vpn.login.assert_not_called()

    def test_invalid_preflight_limit_fails_before_session_creation(self):
        for value in (0,4,True,2.5):
            factory=MagicMock()
            with self.assertRaises(ValueError):authenticated_session(probe_attempts=value,factory=factory)
            factory.assert_not_called()


if __name__=='__main__': unittest.main()
