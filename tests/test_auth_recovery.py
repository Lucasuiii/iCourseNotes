"""Bounded login decisions and strict API proof, without live credentials."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import requests
from src.api.auth_recovery import authenticated_session
from src.api.webvpn import AuthenticationError, WebVPNSession
from src.api.icourse import ICourseClient


class AuthRecoveryTests(unittest.TestCase):
    def session(self):
        vpn=MagicMock();vpn.authenticate_icourse.return_value=True
        return vpn

    def test_outage_before_credentials_uses_bounded_new_sessions(self):
        sessions=[self.session() for _ in range(3)]
        for vpn in sessions:vpn.probe_login_service.side_effect=AuthenticationError('service_unavailable')
        factory=MagicMock(side_effect=sessions);sleep=MagicMock()
        with self.assertRaises(AuthenticationError):authenticated_session(factory=factory,sleep=sleep)
        self.assertEqual(factory.call_count,3)
        self.assertEqual(sleep.call_count,2)
        for vpn in sessions:
            vpn.login.assert_not_called();vpn.authenticate_icourse.assert_not_called()
            vpn.session.close.assert_called_once()

    def test_ticket_timeout_gets_a_fresh_session_and_strictly_verified_api(self):
        first,second=self.session(),self.session()
        first.login.side_effect=requests.exceptions.ReadTimeout('private-ticket')
        factory=MagicMock(side_effect=[first,second]);sleep=MagicMock()
        result=authenticated_session(factory=factory,sleep=sleep,student_id='private-student',password='private-password')
        self.assertIs(result,second)
        first.session.close.assert_called_once();second.session.close.assert_not_called()
        second.authenticate_icourse.assert_called_once_with('private-student','private-password',strict=True)
        first.login.assert_called_once();second.login.assert_called_once()

    def test_rejection_challenge_tls_and_code_errors_are_not_retried(self):
        for error in (AuthenticationError('authentication_rejected'),
                      AuthenticationError('password_method_missing'),
                      requests.exceptions.SSLError('private'), ValueError('private')):
            with self.subTest(error=type(error).__name__):
                vpn=self.session();vpn.login.side_effect=error;factory=MagicMock(return_value=vpn)
                with self.assertRaises(type(error)):authenticated_session(factory=factory,sleep=MagicMock())
                self.assertEqual(factory.call_count,1);vpn.session.close.assert_called_once()

    def test_false_verification_cannot_return_a_session(self):
        vpn=self.session();vpn.authenticate_icourse.return_value=False
        with self.assertRaises(AuthenticationError):authenticated_session(factory=lambda:vpn,max_attempts=1)
        vpn.authenticate_icourse.assert_called_once_with(strict=True);vpn.session.close.assert_called_once()

    def test_probe_closes_response_and_does_not_treat_200_as_authenticated(self):
        for status in (200,502):
            vpn=WebVPNSession();vpn.session.close();vpn.session=MagicMock()
            vpn._probe_sso_context=MagicMock(side_effect=AuthenticationError('service_unavailable'))
            response=MagicMock(status_code=status,text='',history=[])
            vpn.session.get.return_value=response
            if status==502:
                with self.assertRaises(AuthenticationError):vpn.probe_login_service()
            else:vpn.probe_login_service()
            response.close.assert_called_once()
            self.assertFalse(vpn.logged_in)
            self.assertFalse(vpn.session.get.call_args.kwargs['allow_redirects'])

    def test_untrusted_probe_redirect_never_starts_password_login(self):
        vpn=WebVPNSession();vpn.session.close();vpn.session=MagicMock()
        response=SimpleNamespace(status_code=302,text='',history=[],url='https://webvpn.fudan.edu.cn/',
                                 headers={'Location':'https://foreign.invalid/login'},close=MagicMock())
        vpn.session.get.return_value=response;vpn.login=MagicMock()
        with self.assertRaises(AuthenticationError):authenticated_session(factory=lambda:vpn)
        vpn.login.assert_not_called();response.close.assert_called_once()

    def test_explicit_default_port_login_is_same_origin_but_other_ports_are_not(self):
        from src.runtime.media_protocol import redirect_kind, redirect_observation
        for location, trusted in (
                ('https://webvpn.fudan.edu.cn:443/login', True),
                ('https://webvpn.fudan.edu.cn:444/login', False),
                ('https://webvpn.fudan.edu.cn:443/wengine-vpn/user/session', True),
                ('https://webvpn.fudan.edu.cn:444/wengine-vpn/user/session', False),
                ('https://webvpn.fudan.edu.cn.foreign.invalid:443/login', False),
                ('https://private@webvpn.fudan.edu.cn:443/login', False),
                ('http://webvpn.fudan.edu.cn:80/login', False)):
            with self.subTest(location=location):
                vpn=WebVPNSession();vpn.session.close();vpn.session=MagicMock()
                response=SimpleNamespace(status_code=302,text='',history=[],
                    url='https://webvpn.fudan.edu.cn/',headers={'Location':location},close=MagicMock())
                vpn.session.get.return_value=response
                if trusted:vpn.probe_login_service()
                else:
                    with self.assertRaises(AuthenticationError):vpn.probe_login_service()
                response.close.assert_called_once()
                self.assertFalse(vpn.logged_in)
                if location.endswith(':443/login') and trusted:
                    self.assertEqual(redirect_kind(response),'login')
                    self.assertEqual(redirect_observation(response)['authority'],'same_origin')

    def test_configured_wrapped_sso_accepts_only_equivalent_default_port(self):
        from src.api.webvpn import get_vpn_url
        from src.runtime.media_protocol import redirect_kind
        known=get_vpn_url('https://id.fudan.edu.cn/idp/authCenter/authenticate')
        for port,expected in ((443,'login'),(444,'other')):
            location=known.replace('webvpn.fudan.edu.cn/',f'webvpn.fudan.edu.cn:{port}/')
            response=SimpleNamespace(url='https://webvpn.fudan.edu.cn/media',headers={'Location':location})
            self.assertEqual(redirect_kind(response,login_urls=(known,)),expected)

    def test_invalid_attempt_limits_fail_before_constructing_session(self):
        for value in (0,11,True,2.5):
            factory=MagicMock()
            with self.assertRaises(ValueError):authenticated_session(max_attempts=value,factory=factory)
            factory.assert_not_called()

    def test_direct_authentication_skips_webvpn_password_login(self):
        vpn=self.session();vpn.requires_webvpn_login=False
        result=authenticated_session(factory=lambda:vpn,student_id='private-student',password='private-password')
        self.assertIs(result,vpn);vpn.login.assert_not_called()
        vpn.authenticate_icourse.assert_called_once_with('private-student','private-password',strict=True)

    def test_direct_cas_routes_only_native_urls_and_keeps_strict_verification(self):
        from test_webvpn_auth_diagnostics import AuthDiagnosticTests,response
        vpn=AuthDiagnosticTests().session();vpn.access_mode='direct'
        replies=list(vpn.session.post.side_effect)
        replies[-1]=response(text='locationValue="https://icourse.fudan.edu.cn/casapi/index.php?ticket=private"')
        vpn.session.post.side_effect=replies
        self.assertTrue(vpn.authenticate_icourse('private-student','private-password',strict=True))
        urls=[call.args[0] for call in vpn.session.get.call_args_list+vpn.session.post.call_args_list]
        self.assertTrue(any('icourse.fudan.edu.cn/casapi/' in url for url in urls))
        self.assertTrue(any('id.fudan.edu.cn/idp/authn/' in url for url in urls))
        self.assertFalse(any('webvpn.fudan.edu.cn' in url for url in urls))
        self.assertTrue(vpn.logged_in)
        with self.assertRaises(AuthenticationError):vpn.login('id','password')

    def test_direct_cas_refuses_a_ticket_for_a_different_destination(self):
        from test_webvpn_auth_diagnostics import AuthDiagnosticTests
        vpn=AuthDiagnosticTests().session();vpn.access_mode='direct'
        with self.assertRaises(AuthenticationError) as error:
            vpn.authenticate_icourse('private-student','private-password',strict=True)
        self.assertEqual(error.exception.reason,'ticket_destination_untrusted')
        self.assertFalse(any(call.args[0].startswith('https://private/cas')
                             for call in vpn.session.get.call_args_list))

    def test_direct_media_cookie_header_respects_domain_and_path(self):
        vpn=WebVPNSession(access_mode='direct')
        try:
            vpn.session.cookies.set('sso_secret','private-sso',domain='id.fudan.edu.cn',path='/')
            vpn.session.cookies.set('api_secret','private-api',domain='icourse.fudan.edu.cn',path='/userapi/')
            vpn.session.cookies.set('media_cookie','allowed',domain='icourse.fudan.edu.cn',path='/media/')
            client=ICourseClient(vpn)
            url,headers=client.get_stream_params('https://icourse.fudan.edu.cn/media/lecture')
            self.assertEqual(url,'https://icourse.fudan.edu.cn/media/lecture')
            self.assertIn('media_cookie=allowed',headers)
            self.assertNotIn('private-sso',headers);self.assertNotIn('private-api',headers)
            self.assertFalse(vpn.requires_webvpn_login)
        finally:vpn.session.close()


class MediaAuthDeadlineTests(unittest.TestCase):
    def test_cancelled_or_expired_session_never_sends_a_request(self):
        import threading
        import time
        from src.api.auth_recovery import DeadlineSession
        for cancelled, deadline in ((True,time.monotonic()+5),(False,time.monotonic()-1)):
            stopped=threading.Event()
            if cancelled:stopped.set()
            session=DeadlineSession(stopped,deadline)
            with patch('requests.Session.send') as send:
                with self.assertRaises(AuthenticationError): session.get('https://example.invalid/')
                send.assert_not_called()
            session.close()

    def test_timeout_is_clamped_for_every_redirect(self):
        import threading
        import time
        from src.api.auth_recovery import DeadlineSession
        session=DeadlineSession(threading.Event(),time.monotonic()+2)
        with patch('requests.Session.send') as send:
            session.send(object(),timeout=(60,90))
            self.assertTrue(all(0 < v <= 2 for v in send.call_args.kwargs['timeout']))
        session.close()
