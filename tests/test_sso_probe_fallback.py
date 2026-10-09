"""Only trusted credential-free SSO context can replace transient portal failure."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import requests
from src.api.auth_recovery import authenticated_session, initial_authenticated_session
from src.api.webvpn import AuthenticationError, WebVPNSession, authentication_failure
from src.runtime import config


def response(status=302, location='/ac/login?lck=private-context'):
    return SimpleNamespace(status_code=status, headers={'Location':location},url='https://webvpn.fudan.edu.cn/',
                           history=[],text='',close=MagicMock())


class SSOFallbackTests(unittest.TestCase):
    def vpn(self, replies):
        vpn=WebVPNSession();vpn.session.close();vpn.session=MagicMock()
        vpn.session.get.side_effect=replies
        return vpn

    def test_transient_homepage_timeout_can_use_sso_context_and_consume_it_once(self):
        reply=response()
        vpn=self.vpn([requests.exceptions.ReadTimeout('private-url'),reply,response(location='/ac/login?lck=next-context')])
        vpn.probe_login_service()
        self.assertFalse(vpn.logged_in)
        self.assertEqual(vpn._get_auth_context(),('private-context',config.WEBVPN_BASE))
        self.assertIsNone(vpn._preflight_context)
        self.assertEqual(vpn.session.get.call_count,2)
        self.assertEqual(vpn._get_auth_context(),('next-context',config.WEBVPN_BASE))
        self.assertEqual(vpn.session.get.call_count,3)
        reply.close.assert_called_once()
        self.assertTrue(vpn.auth_sso_probe_succeeded)
        self.assertNotIn('private',json.dumps(vpn.auth_diagnostics))

    def test_transient_http_failure_can_fall_back_with_no_body_read(self):
        portal=response(status=503);sso=response()
        # A headers-only fallback must not read or parse arbitrary provider pages.
        del sso.text
        vpn=self.vpn([portal,sso]);vpn.probe_login_service()
        self.assertTrue(vpn.auth_sso_probe_succeeded)
        self.assertEqual(vpn.session.get.call_args.kwargs['timeout'],(5,10))
        self.assertTrue(vpn.session.get.call_args.kwargs['stream'])
        self.assertFalse(vpn.session.get.call_args.kwargs['allow_redirects'])
        portal.close.assert_called_once();sso.close.assert_called_once()
        vpn.session.post.assert_not_called()

    def test_homepage_tls_rejection_and_untrusted_redirect_do_not_use_fallback(self):
        for item in (requests.exceptions.SSLError('private'),response(status=403),
                     response(location='https://foreign.invalid/ac/login?lck=private')):
            with self.subTest(item=type(item).__name__):
                vpn=self.vpn([item]);vpn._probe_sso_context=MagicMock()
                with self.assertRaises((AuthenticationError,requests.exceptions.SSLError)):
                    vpn.probe_login_service()
                vpn._probe_sso_context.assert_not_called();vpn.session.post.assert_not_called()

    def test_bad_sso_targets_cannot_submit_password_or_be_followed(self):
        for location in ('https://foreign.invalid/ac/login?lck=private',
                         'http://id.fudan.edu.cn/ac/login?lck=private',
                         'https://id.fudan.edu.cn:444/ac/login?lck=private',
                         'https://private@id.fudan.edu.cn/ac/login?lck=private',
                         '/other/login?lck=private',
                         '/ac/login?ticket=private&lck=private',
                         '/ac/login?lck=private#fragment'):
            with self.subTest(location=location):
                reply=response(location=location)
                vpn=self.vpn([requests.exceptions.ReadTimeout(),reply])
                with self.assertRaises(AuthenticationError) as caught:
                    authenticated_session(max_attempts=1,factory=lambda:vpn,sleep=MagicMock())
                self.assertEqual(caught.exception.reason,'service_redirect_untrusted')
                self.assertEqual(vpn.session.get.call_count,2);vpn.session.post.assert_not_called()
                self.assertIsNone(vpn._preflight_context);reply.close.assert_called_once()

    def test_sso_tls_rejection_is_not_retried_even_after_homepage_timeout(self):
        vpn=self.vpn([requests.exceptions.ReadTimeout(),requests.exceptions.SSLError('private')])
        factory,sleep=MagicMock(return_value=vpn),MagicMock()
        with self.assertRaises(requests.exceptions.SSLError):
            initial_authenticated_session(factory=factory,sleep=sleep)
        factory.assert_called_once();sleep.assert_not_called();vpn.session.post.assert_not_called()

    def test_sso_redirect_loops_are_bounded_and_responses_closed(self):
        replies=[response(location='/ac/login') for _ in range(3)]
        vpn=self.vpn([requests.exceptions.ReadTimeout(),*replies])
        with self.assertRaises(AuthenticationError) as caught: vpn.probe_login_service()
        self.assertEqual(caught.exception.reason,'cas_context_missing')
        self.assertEqual(vpn.session.get.call_count,4)
        for reply in replies: reply.close.assert_called_once()
        vpn.session.post.assert_not_called()

    def test_sso_missing_or_duplicate_context_never_permits_password_submission(self):
        for reply in (response(status=200),response(location='/ac/login?lck=a&lck=b'),
                      response(location='/ac/login?lck=&lck=b'),
                      response(location='/ac/login?lck=a&%6cck=b'),
                      response(location='/ac/login?lck='+'x'*4097)):
            vpn=self.vpn([requests.exceptions.ReadTimeout(),reply])
            with self.assertRaises(AuthenticationError):
                authenticated_session(max_attempts=1,factory=lambda:vpn,sleep=MagicMock())
            vpn.session.post.assert_not_called()

    def test_relative_trusted_redirect_and_equivalent_https_port_are_supported(self):
        first=response(location='/ac/start');second=response(location='https://id.fudan.edu.cn:443/ac/login?lck=private')
        vpn=self.vpn([requests.exceptions.ReadTimeout(),first,second])
        vpn.probe_login_service()
        self.assertEqual(vpn._get_auth_context(),('private',config.WEBVPN_BASE))
        self.assertEqual(vpn.session.get.call_count,3)
        first.close.assert_called_once();second.close.assert_called_once()

    def test_actual_fudan_spa_fragment_context_is_supported_only_on_known_route(self):
        vpn=self.vpn([requests.exceptions.ReadTimeout(),
                      response(location='/ac/#/index?lck=private&authType=synthetic')])
        vpn.probe_login_service()
        self.assertEqual(vpn._get_auth_context(),('private',config.WEBVPN_BASE))
        self.assertNotIn('private',json.dumps(vpn.auth_diagnostics))
        for location in ('/ac/#/other?lck=private',
                         '/ac/login#/index?lck=private',
                         '/ac/#/index?lck=private&ticket=private',
                         '/ac/#/index?lck=private&%74icket=private',
                         '/ac/?lck=a#/index?lck=b'):
            vpn=self.vpn([requests.exceptions.ReadTimeout(),response(location=location)])
            with self.assertRaises(AuthenticationError): vpn.probe_login_service()
            self.assertIsNone(vpn._preflight_context);vpn.session.post.assert_not_called()

    def test_context_cannot_be_transferred_to_a_different_session(self):
        vpn=self.vpn([requests.exceptions.ReadTimeout(),response()]);vpn.probe_login_service()
        vpn.session=MagicMock();vpn.session.get.return_value=response(location='/ac/login?lck=new-context')
        self.assertEqual(vpn._get_auth_context(),('new-context',config.WEBVPN_BASE))
        vpn.session.get.assert_called_once()

    def test_both_endpoints_timeout_keep_bounded_precredential_recovery_and_safe_audit(self):
        sessions=[self.vpn([requests.exceptions.ReadTimeout('private-portal'),
                           requests.exceptions.ReadTimeout('private-sso')]) for _ in range(9)]
        factory=MagicMock(side_effect=sessions)
        with self.assertRaises(requests.exceptions.ReadTimeout) as caught:
            initial_authenticated_session(factory=factory,sleep=MagicMock())
        self.assertEqual(factory.call_count,9)
        audit=authentication_failure(caught.exception)
        self.assertEqual(audit['failure_phase'],'login_sso_probe')
        self.assertEqual(audit['portal_probe_failure'],'auth_read_timeout')
        self.assertTrue(audit['sso_probe_attempted']);self.assertFalse(audit['sso_probe_succeeded'])
        self.assertEqual(len(audit['attempt_failures']),9)
        self.assertNotIn('private',json.dumps(audit))
        for vpn in sessions: vpn.session.post.assert_not_called()

    def test_media_deadline_before_sso_fallback_prevents_any_further_request(self):
        from src.api.auth_recovery import DeadlineSession
        clock=[0.0];vpn=WebVPNSession();vpn.session.close()
        vpn.session=DeadlineSession(threading.Event(),75)
        def send(request,**kwargs):
            clock[0]=76
            raise requests.exceptions.ReadTimeout('private')
        with patch('src.api.auth_recovery.time.monotonic',side_effect=lambda:clock[0]), \
             patch('requests.Session.send',side_effect=send) as sends:
            with self.assertRaises(AuthenticationError) as caught: vpn.probe_login_service()
        self.assertEqual(caught.exception.reason,'media_auth_cancelled')
        sends.assert_called_once();self.assertIsNone(vpn._preflight_context)
        vpn.session.close()


@contextlib.contextmanager
def portal_timeout_sso_available():
    counts={'portal':0,'sso':0,'password_posts':0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path=='/':
                counts['portal']+=1;time.sleep(.15)
                self.send_response(200)
            else:
                counts['sso']+=1
                self.send_response(302);self.send_header('Location','/ac/login?lck=synthetic-context')
            self.send_header('Content-Length','0');self.end_headers()
        def do_POST(self):counts['password_posts']+=1;self.send_response(403);self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    vpn=WebVPNSession();vpn.session.trust_env=False
    get=vpn.session.get
    def short_get(url,**kwargs):kwargs['timeout']=(.1,.04);return get(url,**kwargs)
    vpn.session.get=short_get
    try:
        with patch.object(config,'WEBVPN_BASE',base),patch.object(config,'IDP_BASE',base):
            yield vpn,counts
    finally:vpn.session.close();server.shutdown();server.server_close();thread.join(timeout=1)


class RealSSOFallbackTests(unittest.TestCase):
    def test_real_homepage_timeout_no_longer_stops_a_trusted_sso_login(self):
        with portal_timeout_sso_available() as (vpn,counts):
            vpn._query_auth_methods=MagicMock(return_value=('synthetic-chain','synthetic-type'))
            vpn._get_public_key=MagicMock(return_value='synthetic-key')
            vpn._encrypt_password=MagicMock(return_value='synthetic-password')
            vpn._auth_execute=MagicMock(return_value='synthetic-token')
            vpn._get_cas_ticket=MagicMock(return_value='synthetic-ticket')
            vpn._establish_session=MagicMock()
            vpn.authenticate_icourse=MagicMock(return_value=True)
            result=authenticated_session(max_attempts=1,factory=lambda:vpn,
                student_id='synthetic',password='synthetic',sleep=MagicMock())
            self.assertIs(result,vpn)
            self.assertEqual(counts,{'portal':1,'sso':1,'password_posts':0})
            self.assertEqual(vpn._auth_execute.call_count,1)
            self.assertEqual(vpn._auth_execute.call_args.args[2],'synthetic-context')
            vpn.authenticate_icourse.assert_called_once_with('synthetic','synthetic',strict=True)
            self.assertIsNone(vpn._preflight_context)
