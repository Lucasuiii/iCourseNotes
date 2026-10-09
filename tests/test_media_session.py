"""Real loopback HTTP cookie rotation and bounded SSO refresh; no live login."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import requests
from src.api.icourse import ICourseClient
from src.api.webvpn import get_vpn_url
from src.runtime.media_transport import SignedRangeRelay, MediaTransportError


class SessionOrigin:
    DATA = bytes(range(256))*256

    def __init__(self, mode='rotate', *, connection_bound=False):
        self.mode=mode; self.token='first'; self.calls=[]; self.cookies=[]
        self.connection_bound=connection_bound; self.connection_mismatches=0

    def __enter__(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1' if owner.connection_bound else 'HTTP/1.0'
            def handle(self):
                try: super().handle()
                except (ConnectionResetError,BrokenPipeError): pass
            def log_message(self,*args): pass
            def reply(self,status,body=b'',headers=()):
                self.send_response(status)
                self.send_header('Content-Length',str(len(body)))
                for key,value in headers: self.send_header(key,value)
                self.end_headers()
                try: self.wfile.write(body)
                except OSError: pass
            def do_GET(self):
                path=urlsplit(self.path).path
                owner.calls.append(path)
                if path == '/':
                    if owner.mode == 'cold':
                        self.reply(302,headers=[('Location','/login?private-ticket')]);return
                    owner.token='refreshed'
                    self.reply(200,headers=[('Set-Cookie','media_token=refreshed; Path=/')]);return
                if path == '/api':
                    self.reply(200,b'{"code":0,"params":{"id":"cached-private-user","tenant_id":"fake","phone":"fake"}}');return
                if path != '/media':
                    self.reply(500);return
                cookie=self.headers.get('Cookie','')
                owner.cookies.append(cookie)
                if owner.connection_bound:
                    if not hasattr(self,'bound_cookie'): self.bound_cookie=cookie
                    if cookie != self.bound_cookie:
                        owner.connection_mismatches+=1
                        self.reply(302,headers=[('Location','/login?private-ticket')]);return
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers.get('Range',''))
                if not match: self.reply(400);return
                start=int(match[1]);end=min(int(match[2]) if match[2] else len(owner.DATA)-1,len(owner.DATA)-1)
                if start and owner.mode in ('foreign','same_media','wrapped_foreign'):
                    if owner.mode=='foreign':location='https://untrusted.invalid/login?private-ticket'
                    elif owner.mode=='wrapped_foreign':location=get_vpn_url('https://untrusted.invalid/cas/login')+'?private-ticket'
                    else:location='/media?t=untrusted-ticket'
                    self.reply(302,b'private-login-html',[('Location',location)]);return
                if start and owner.mode in ('login','cold','persistent','changed','wrapped_login',
                                           'control','control_persistent','control_changed','root'):
                    if owner.token!='refreshed' or owner.mode in ('persistent','control_persistent'):
                        location = (get_vpn_url('https://id.fudan.edu.cn/idp/authCenter/authenticate')
                                    if owner.mode=='wrapped_login' else '/login')
                        if owner.mode.startswith('control'):location='/wengine-vpn/session'
                        if owner.mode=='root':location='/'
                        self.reply(302,b'private-login-html',[('Location',location+'?private-ticket')]);return
                expected='' if owner.token is None else 'media_token='+owner.token
                if cookie!=expected:
                    self.reply(401,b'private-login-html');return
                headers=[('Content-Range',f'bytes {start}-{end}/{len(owner.DATA)}'),
                         ('ETag','"changed"' if start and owner.mode in ('changed','control_changed') else '"immutable"')]
                if start==0 and owner.mode=='rotate':
                    owner.token='second';headers.append(('Set-Cookie','media_token=second; Path=/'))
                elif start==0 and owner.mode=='delete':
                    owner.token=None;headers.append(('Set-Cookie','media_token=; Max-Age=0; Path=/'))
                self.reply(206,owner.DATA[start:end+1],headers)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.signed=self.base+'/media?t=old&clientUUID=old'
        owner=self
        class VPN:
            def __init__(self):
                self.session=requests.Session()
                self.session.cookies.set('media_token','first',domain='127.0.0.1',path='/')
                self.session.cookies.set('foreign','private-cookie',domain='untrusted.invalid',path='/')
                self.session.cookies.set('other_path','private-cookie',domain='127.0.0.1',path='/not-media')
                self.session.cookies.set('secure','private-cookie',domain='127.0.0.1',path='/',secure=True)
            def get(self,url,**kwargs): return self.session.get(owner.base+'/api',**kwargs)
        class Client(ICourseClient):
            counter=0
            def renew_video_url(self,url,now=None):
                self.counter+=1
                return owner.base+f'/media?t=fresh{self.counter}&clientUUID={self.counter}'
            def get_stream_params(self,url):
                # The old flattened header must be ignored when a real login
                # jar exists; path/domain/secure rules must select the cookies.
                value = self.vpn.session.cookies.get('media_token')
                return url,'Cookie: '+('media_token='+value if value else '')+'\r\n'
        self.client=Client(VPN());self.client._userinfo={'id':'cached-private-user'}
        self.config=patch('src.api.icourse.config.WEBVPN_BASE',self.base)
        self.config.start()
        return self

    def __exit__(self,*args):
        self.config.stop();self.client.vpn.session.close()
        self.server.shutdown();self.server.server_close();self.thread.join(2)


class MediaSessionTests(unittest.TestCase):
    def relay(self,origin,**kwargs):
        return SignedRangeRelay(origin.client,origin.signed,prefix_bytes=4096,
                                chunk_bytes=16384,**kwargs)

    def test_rotated_cookie_survives_ranges_and_reaches_the_login_session(self):
        with SessionOrigin() as origin, self.relay(origin) as relay:
            session=relay._session
            self.assertEqual(requests.get(relay.url,timeout=15).content,origin.DATA)
            self.assertIs(relay._session,session)
            self.assertEqual(origin.cookies[0],'media_token=first')
            self.assertTrue(all(c=='media_token=second' for c in origin.cookies[1:]))
            self.assertEqual(origin.client.vpn.session.cookies.get('media_token'),'second')
            audit=relay.audit();self.assertEqual(audit['cookie_updates'],1)
            self.assertEqual(audit['session_refresh_attempts'],0)
            self.assertNotIn('private',json.dumps(audit))

    def test_cookie_deletion_is_not_resurrected_by_the_old_header(self):
        with SessionOrigin('delete') as origin, self.relay(origin) as relay:
            self.assertEqual(requests.get(relay.url,timeout=15).content,origin.DATA)
            self.assertTrue(all(c=='' for c in origin.cookies[1:]))
            self.assertIsNone(origin.client.vpn.session.cookies.get('media_token'))
            self.assertEqual(relay.audit()['cookie_updates'],1)

    def test_known_login_redirect_refreshes_existing_cookies_once_and_resumes(self):
        with SessionOrigin('login') as origin, self.relay(origin,allow_session_refresh=True) as relay:
            self.assertEqual(requests.get(relay.url,timeout=15).content,origin.DATA)
            self.assertEqual(origin.calls.count('/'),1);self.assertEqual(origin.calls.count('/api'),1)
            self.assertNotIn('/login',origin.calls)
            self.assertEqual(origin.client._userinfo['id'],'cached-private-user')
            audit=relay.audit()
            self.assertEqual(audit['session_refresh_attempts'],1)
            self.assertEqual(audit['session_refresh_successes'],1)
            self.assertEqual(audit['session_identity_verifications'],1)
            self.assertEqual(audit['session_resume_attempts'],1)
            self.assertEqual(audit['redirect_counts'],{'login':1})
            self.assertEqual(audit['last_failure_offset'],4096)
            self.assertEqual(audit['upstream_bytes'],len(origin.DATA))
            self.assertNotIn('private',json.dumps(audit))
            audit['redirect_counts']['login']=999
            self.assertEqual(relay.audit()['redirect_counts'],{'login':1})

    def test_default_readonly_transport_never_refreshes_authentication(self):
        with SessionOrigin('login') as origin, self.relay(origin) as relay:
            with self.assertRaises(requests.RequestException): requests.get(relay.url,timeout=15)
            self.assertNotIn('/',origin.calls);self.assertNotIn('/api',origin.calls)
            self.assertEqual(relay.audit()['terminal_error_code'],'media_session_unavailable')

    def test_exact_wrapped_sso_route_refreshes_once_without_following_ticket(self):
        with SessionOrigin('wrapped_login') as origin, self.relay(origin,allow_session_refresh=True) as relay:
            self.assertEqual(requests.get(relay.url,timeout=15).content,origin.DATA)
            self.assertEqual(origin.calls.count('/'),1)
            self.assertEqual(origin.calls.count('/api'),1)
            audit = relay.audit()
            self.assertEqual(audit['session_refresh_attempts'],1)
            self.assertEqual(audit['last_redirect']['classification'],'login')
            self.assertEqual(audit['last_redirect']['route'],'vpn_wrapped')
            self.assertNotIn('private',json.dumps(audit))
            audit['last_redirect']['route']='modified'
            self.assertEqual(relay.audit()['last_redirect']['route'],'vpn_wrapped')

    def test_wrapped_foreign_login_suffix_cannot_trigger_refresh(self):
        with SessionOrigin('wrapped_foreign') as origin, self.relay(origin,allow_session_refresh=True) as relay:
            with self.assertRaises(requests.RequestException): requests.get(relay.url,timeout=15)
            audit = relay.audit()
            self.assertEqual(audit['terminal_error_code'],'media_redirect_untrusted')
            self.assertEqual(audit['last_redirect']['classification'],'other')
            self.assertEqual(audit['last_redirect']['route'],'vpn_wrapped')
            self.assertEqual(audit['session_refresh_attempts'],0)
            self.assertNotIn('/',origin.calls)
            self.assertNotIn('untrusted.invalid',json.dumps(audit))

    def test_native_control_redirect_only_probes_original_session_and_resumes_original_range(self):
        for mode in ('control','root'):
            with self.subTest(mode=mode), SessionOrigin(mode) as origin, self.relay(origin,allow_session_refresh=True) as relay:
                self.assertEqual(requests.get(relay.url,timeout=15).content,origin.DATA)
                audit=relay.audit()
                self.assertEqual(audit['session_refresh_attempts'],1)
                self.assertEqual(audit['session_refresh_successes'],1)
                self.assertEqual(audit['upstream_bytes'],len(origin.DATA))
                self.assertEqual(audit['last_failure_offset'],4096)
                self.assertEqual(audit['last_redirect']['classification'],'other')
                self.assertEqual(audit['last_redirect']['recovery'],'probe_original_session')
                self.assertNotIn('/wengine-vpn/session',origin.calls)
                self.assertNotIn('private',json.dumps(audit))

    def test_native_redirect_probe_cannot_override_source_change_or_repeat_forever(self):
        for mode,code in [('control_changed','source_changed'),('control_persistent','media_redirect_untrusted')]:
            with self.subTest(mode=mode), SessionOrigin(mode) as origin, self.relay(origin,allow_session_refresh=True) as relay:
                with self.assertRaises(requests.RequestException):requests.get(relay.url,timeout=15)
                audit=relay.audit()
                self.assertEqual(audit['terminal_error_code'],code)
                self.assertEqual(audit['session_refresh_attempts'],1)
                self.assertEqual(audit['upstream_bytes'],4096)
                self.assertNotIn('/wengine-vpn/session',origin.calls)

    def test_native_redirect_probe_refuses_a_different_verified_account(self):
        with SessionOrigin('control') as origin, self.relay(origin,allow_session_refresh=True) as relay:
            origin.client._userinfo={'id':'different-private-user'}
            with self.assertRaises(requests.RequestException):requests.get(relay.url,timeout=15)
            audit=relay.audit()
            self.assertEqual(audit['terminal_error_code'],'media_session_unavailable')
            self.assertEqual(audit['session_refresh_successes'],0)
            self.assertEqual(audit['upstream_bytes'],4096)

    def test_cold_or_repeated_login_redirect_does_not_loop_or_forward_html(self):
        for mode in ('cold','persistent'):
            with self.subTest(mode=mode), SessionOrigin(mode) as origin, self.relay(origin,allow_session_refresh=True) as relay:
                with self.assertRaises(requests.RequestException): requests.get(relay.url,timeout=15)
                self.assertEqual(origin.calls.count('/'),1)
                self.assertNotIn('/login',origin.calls)
                self.assertEqual(relay.audit()['upstream_bytes'],4096)
                self.assertEqual(relay.audit()['terminal_error_code'],'media_session_unavailable')
                self.assertEqual(relay.audit()['session_refresh_attempts'],1)

    def test_unknown_redirect_stops_without_following_or_refreshing(self):
        with SessionOrigin('foreign') as origin, self.relay(origin,allow_session_refresh=True) as relay:
            with self.assertRaises(requests.RequestException): requests.get(relay.url,timeout=15)
            audit=relay.audit()
            self.assertEqual(audit['terminal_error_code'],'media_redirect_untrusted')
            self.assertEqual(audit['redirect_counts'],{'other':1})
            self.assertEqual(audit['upstream_bytes'],4096)
            self.assertEqual(audit['session_refresh_attempts'],0)
            self.assertNotIn('private',json.dumps(audit));self.assertNotIn('untrusted.invalid',json.dumps(audit))

    def test_redirect_classification_does_not_accept_downgrade_or_credentials(self):
        relay=SignedRangeRelay(MagicMock(),'https://vpn.example/media?t=old&clientUUID=old')
        for location,expected in [('/login?ticket=private','login'),
                ('/wengine-vpn/login?ticket=private','login'),
                ('/media?t=private','same_media'),
                ('http://vpn.example/login','other'),
                ('https://private-user:private-pass@vpn.example/login','other'),
                ('https://untrusted.invalid/login','other'),
                ('https://id.fudan.edu.cn/cas/login?ticket=private','login'),
                ('https://id.fudan.edu.cn/other','other'),('', 'missing')]:
            response=SimpleNamespace(url='https://vpn.example/media',headers={'Location':location})
            self.assertEqual(relay._redirect_kind(response),expected)

    def test_same_media_redirect_remains_bounded_without_following_new_signature(self):
        with SessionOrigin('same_media') as origin, self.relay(origin,attempts=2) as relay:
            with self.assertRaises(requests.RequestException): requests.get(relay.url,timeout=15)
            self.assertEqual(relay.audit()['redirect_counts'],{'same_media':2})
            self.assertEqual(relay.audit()['terminal_error_code'],'range_not_honored')
            self.assertEqual(relay.audit()['upstream_bytes'],4096)

    def test_changed_source_after_session_refresh_is_not_spliced(self):
        with SessionOrigin('changed') as origin, self.relay(origin,allow_session_refresh=True) as relay:
            with self.assertRaises(requests.RequestException): requests.get(relay.url,timeout=15)
            self.assertEqual(relay.audit()['terminal_error_code'],'source_changed')
            self.assertEqual(relay.audit()['upstream_bytes'],4096)

    def test_refresh_requires_both_portal_and_icourse_verification(self):
        valid={'code':0,'params':{'id':'private-user','tenant_id':'fake','phone':'fake'}}
        for portal,api,expected in [(302,valid,False),(200,{'code':7001},False),
                (200,{'code':0},False),(200,{'code':0,'params':{'id':'another-user'}},False),
                (200,valid,True)]:
            vpn=MagicMock();client=ICourseClient(vpn);client._userinfo={'id':'private-user'}
            vpn.session.get.return_value=SimpleNamespace(status_code=portal,close=lambda:None)
            vpn.get.return_value=SimpleNamespace(status_code=200,json=lambda:api,close=lambda:None)
            success=client.refresh_media_session()
            self.assertEqual(success,expected)
            self.assertFalse(vpn.session.get.call_args.kwargs['allow_redirects'])
            if portal==200:self.assertFalse(vpn.get.call_args.kwargs['allow_redirects'])
            else:vpn.get.assert_not_called()
            vpn.login.assert_not_called();vpn.authenticate_icourse.assert_not_called()
            self.assertEqual(client._userinfo,valid['params'] if success else {'id':'private-user'})

    def test_fresh_login_resumes_same_offset_without_mutating_shared_client(self):
        with SessionOrigin('cold') as origin:
            old = origin.client.vpn
            def factory(cancelled, deadline):
                candidate = type(old)()
                origin.token = 'refreshed'
                candidate.session.cookies.set('media_token','refreshed',domain='127.0.0.1',path='/')
                return candidate
            origin.client._media_reauth_factory = MagicMock(side_effect=factory)
            with self.relay(origin,allow_session_refresh=True) as relay:
                self.assertEqual(requests.get(relay.url,timeout=15).content,origin.DATA)
                self.assertIs(origin.client.vpn,old)
                self.assertIsNot(relay.client,origin.client)
                audit = relay.audit()
                self.assertEqual(audit['last_failure_offset'],4096)
                self.assertEqual(audit['upstream_bytes'],len(origin.DATA))
                self.assertEqual(audit['media_auth']['reauth_successes'],1)
                origin.client._media_reauth_factory.assert_called_once()
                self.assertNotIn('/login',origin.calls)
                self.assertNotIn('private',json.dumps(audit))

    def test_fresh_login_cannot_accept_another_account_or_tenant(self):
        for user in ({'id':'another'}, {'id':'cached-private-user','tenant_id':'another'}):
            with self.subTest(user=user), SessionOrigin('cold') as origin:
                origin.client._userinfo['tenant_id']='fake'
                candidate=MagicMock()
                candidate.get.return_value=SimpleNamespace(status_code=200,json=lambda:{'code':0,'params':user},close=lambda:None)
                origin.client._media_reauth_factory=MagicMock(return_value=candidate)
                with self.relay(origin,allow_session_refresh=True) as relay:
                    with self.assertRaises(requests.RequestException):requests.get(relay.url,timeout=15)
                    self.assertEqual(relay.audit()['upstream_bytes'],4096)
                    self.assertEqual(relay.audit()['media_auth']['failure'],'identity_mismatch')
                    candidate.session.close.assert_called_once()

    def test_fresh_session_still_rejects_changed_source_or_persistent_login(self):
        for mode, code in [('changed','source_changed'),('persistent','media_session_unavailable')]:
            with self.subTest(mode=mode), SessionOrigin('cold') as origin:
                def factory(cancelled,deadline):
                    candidate=type(origin.client.vpn)()
                    origin.mode=mode;origin.token='refreshed'
                    candidate.session.cookies.set('media_token','refreshed',domain='127.0.0.1',path='/')
                    return candidate
                origin.client._media_reauth_factory=MagicMock(side_effect=factory)
                with self.relay(origin,allow_session_refresh=True) as relay:
                    with self.assertRaises(requests.RequestException):requests.get(relay.url,timeout=15)
                    self.assertEqual(relay.audit()['terminal_error_code'],code)
                    self.assertEqual(relay.audit()['upstream_bytes'],4096)
                    self.assertEqual(relay.audit()['session_identity_verifications'],1)
                    self.assertEqual(relay.audit()['session_refresh_successes'],0)
                    self.assertEqual(relay.audit()['session_recovery_events'][-1]['event'],'media_resume_failed')
                    origin.client._media_reauth_factory.assert_called_once()

    def test_fresh_login_replaces_connection_bound_pool_and_resumes_exact_bytes(self):
        with SessionOrigin('cold',connection_bound=True) as origin:
            old=origin.client.vpn
            def factory(cancelled,deadline):
                candidate=type(old)()
                origin.mode='connection_bound';origin.token='refreshed'
                candidate.session.cookies.set('media_token','refreshed',domain='127.0.0.1',path='/')
                return candidate
            origin.client._media_reauth_factory=MagicMock(side_effect=factory)
            pools=[]
            def pool_factory():
                pool=requests.Session();pool.close=MagicMock(wraps=pool.close)
                pools.append(pool);return pool
            with self.relay(origin,allow_session_refresh=True,session_factory=pool_factory) as relay:
                self.assertEqual(requests.get(relay.url,timeout=15).content,origin.DATA)
                self.assertEqual(len(pools),2);pools[0].close.assert_called_once()
                self.assertEqual(origin.connection_mismatches,0)
                audit=relay.audit()
                self.assertEqual(audit['last_failure_offset'],4096)
                self.assertEqual(audit['session_identity_verifications'],1)
                self.assertEqual(audit['session_refresh_successes'],1)
                self.assertEqual(audit['session_recovery_events'][-1]['event'],'media_resumed')
                self.assertEqual(audit['session_recovery_events'][-1]['offset'],4096)
                self.assertIs(origin.client.vpn,old)
                origin.client._media_reauth_factory.assert_called_once()
            pools[1].close.assert_called_once()

    def test_unknown_redirect_never_submits_credentials_even_with_factory(self):
        with SessionOrigin('foreign') as origin:
            origin.client._media_reauth_factory=MagicMock()
            with self.relay(origin,allow_session_refresh=True) as relay:
                with self.assertRaises(requests.RequestException):requests.get(relay.url,timeout=15)
                origin.client._media_reauth_factory.assert_not_called()

    def test_timeout_cancels_and_closes_late_authentication_without_adopting_it(self):
        candidate=MagicMock()
        candidate.get.return_value=SimpleNamespace(status_code=200,json=lambda:{'code':0,'params':{'id':'same'}},close=lambda:None)
        release=threading.Event(); finished=threading.Event()
        def factory(cancelled, deadline):
            release.wait(2)
            self.assertTrue(cancelled.is_set())
            finished.set()
            return candidate
        old=MagicMock();client=ICourseClient(old,media_reauth_factory=factory);client._userinfo={'id':'same'}
        self.assertFalse(client.reauthenticate_media_session(threading.Event(),timeout=.05))
        release.set();self.assertTrue(finished.wait(2))
        import time
        for _ in range(50):
            if candidate.session.close.called:break
            time.sleep(.01)
        candidate.session.close.assert_called_once()
        candidate.get.assert_not_called()
        self.assertIs(client.vpn,old)

    def test_readonly_transport_never_uses_fresh_authentication(self):
        with SessionOrigin('cold') as origin:
            origin.client._media_reauth_factory=MagicMock()
            with self.relay(origin) as relay:
                with self.assertRaises(requests.RequestException):requests.get(relay.url,timeout=15)
                origin.client._media_reauth_factory.assert_not_called()


if __name__=='__main__': unittest.main()
