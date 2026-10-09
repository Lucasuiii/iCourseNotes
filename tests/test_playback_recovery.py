"""Real HTTP lookup recovery, with no campus login, media, or provider calls."""
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import requests
from src.api.icourse import ICourseClient
from src.api.playback_diagnostics import attach_lookup


GOOD={'code':0,'data':{'video_list':{'0':{'preview_url':'https://private.invalid/lecture.mp4'}}}}


class PlaybackOrigin:
    def __init__(self, info, detail=None):
        self.replies={'get-sub-info':list(info),'get-sub-detail':list(detail or info)}
        self.calls=[]

    def __enter__(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                name=urlsplit(self.path).path.rsplit('/',1)[-1]
                owner.calls.append(name)
                replies=owner.replies.get(name)
                if replies is None:
                    self.send_error(500);return
                status,body,headers=replies[0]
                if len(replies)>1: replies.pop(0)
                if isinstance(body,dict): body=json.dumps(body).encode()
                self.send_response(status);self.send_header('Content-Length',str(len(body)))
                for key,value in headers.items(): self.send_header(key,value)
                self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.session=requests.Session()
        self.vpn=MagicMock()
        def get(url,**kwargs):
            path=urlsplit(url).path
            return self.session.get(f'http://127.0.0.1:{self.server.server_port}'+path,**kwargs)
        self.vpn.get.side_effect=get
        self.client=ICourseClient(self.vpn)
        self.client.sign_video_url=MagicMock(return_value='signed-private-url')
        self.output=patch('sys.stdout',io.StringIO());self.output.start()
        self.delay=patch('src.api.playback_diagnostics.time.sleep');self.delay.start()
        return self

    def lookup(self): return self.client.get_video_url('private-course','private-lecture')

    def public_audit(self): return attach_lookup({},self.client,'private-course','private-lecture')

    def __exit__(self,*args):
        self.delay.stop();self.output.stop();self.session.close()
        self.server.shutdown();self.server.server_close();self.thread.join(2)


class PlaybackRecoveryTests(unittest.TestCase):
    def test_empty_then_malformed_json_recovers_without_second_endpoint_or_login(self):
        with PlaybackOrigin([(200,b'',{}),(200,b'{"private":',{}),(200,GOOD,{})]) as origin:
            self.assertEqual(origin.lookup(),'signed-private-url')
            self.assertEqual(origin.calls,['get-sub-info']*3)
            row=origin.public_audit()['playback_lookup']['sources'][0]
            self.assertEqual(row['result'],'payload');self.assertEqual(row['attempt_count'],3)
            self.assertEqual([x['response']['body_kind'] for x in row['attempts'][:2]],['empty','json'])
            self.assertNotIn('private',json.dumps(origin.public_audit()))
            origin.vpn.login.assert_not_called()

    def test_six_empty_replies_exhaust_bounded_gets_and_keep_fallback_order(self):
        with PlaybackOrigin([(200,b'',{})]) as origin:
            self.assertIsNone(origin.lookup())
            self.assertEqual(origin.calls,['get-sub-info']*3+['get-sub-detail']*3)
            for row in origin.public_audit()['playback_lookup']['sources']:
                self.assertEqual(row['attempt_count'],3);self.assertEqual(row['failure'],'invalid_json')
            origin.client.sign_video_url.assert_not_called()

    def test_transient_gateway_response_retries_then_recovers(self):
        with PlaybackOrigin([(503,b'<html>temporary gateway</html>',{}),(200,GOOD,{})]) as origin:
            self.assertEqual(origin.lookup(),'signed-private-url')
            row=origin.public_audit()['playback_lookup']['sources'][0]
            self.assertEqual(row['attempt_count'],2);self.assertEqual(row['attempts'][0]['http_status'],503)

    def test_login_challenge_redirect_tls_permission_and_invalid_payload_do_not_retry(self):
        cases=[(200,b'<input type="password" value="private">',{},'login_required'),
               (200,b'<html>private captcha</html>',{},'challenge_required'),
               (503,b'private captcha',{},'challenge_required'),
               (200,{'code':0,'needOtp':True,'data':{}},{},'challenge_required'),
               (302,b'',{'Location':'/login?private-ticket'},'login_required'),
               (302,b'',{'Location':'https://private.invalid/elsewhere'},'redirect_rejected'),
               (401,b'private',{},'login_required'),(403,b'private',{},'http_error'),
               (200,b'<html>unclassified private page</html>',{},'invalid_json'),
               (200,{'code':0,'data':['private']},{},'invalid_payload')]
        for status,body,headers,failure in cases:
            with self.subTest(failure=failure,status=status), PlaybackOrigin([(status,body,headers)]) as origin:
                self.assertIsNone(origin.lookup())
                self.assertEqual(Counter(origin.calls),{'get-sub-info':1,'get-sub-detail':1})
                rows=origin.public_audit()['playback_lookup']['sources']
                # sub-detail rejects the non-zero/invalid API payload separately.
                self.assertEqual(rows[0]['failure'],failure)
                self.assertNotIn('private',json.dumps(origin.public_audit()))
                self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in origin.vpn.get.call_args_list))
                origin.vpn.login.assert_not_called()
        client=ICourseClient(MagicMock())
        client.vpn.get.side_effect=requests.exceptions.SSLError('private')
        with patch('sys.stdout',io.StringIO()): self.assertIsNone(client.get_video_url('x','y'))
        self.assertEqual(client.vpn.get.call_count,2)

    def test_timeout_retries_but_no_response_json_failure_does_not(self):
        client=ICourseClient(MagicMock())
        client.get_sub_info=MagicMock(side_effect=[requests.exceptions.ReadTimeout('private'),GOOD['data']])
        client.get_sub_detail=MagicMock();client.sign_video_url=MagicMock(return_value='signed')
        with patch('src.api.playback_diagnostics.time.sleep'):
            self.assertEqual(client.get_video_url('x','y'),'signed')
        self.assertEqual(client.get_sub_info.call_count,2);client.get_sub_detail.assert_not_called()

    def test_review_gate_partial_payload_still_uses_existing_url(self):
        payload={'code':7001,'data':{'content':{'now':100,'playback':{'url':'https://private.invalid/x.mp4'}}}}
        with PlaybackOrigin([(200,payload,{})]) as origin:
            self.assertEqual(origin.lookup(),'signed-private-url')
            self.assertEqual(origin.calls,['get-sub-info'])
            origin.client.sign_video_url.assert_called_once_with('https://private.invalid/x.mp4',now=100)

    def test_public_retry_history_is_strictly_whitelisted(self):
        client=MagicMock();client.video_lookup_diagnostics.return_value={'url_found':False,'sources':[
            {'source':'sub_info','result':'failed','failure':'invalid_json','attempt_count':2,'attempts':[
                {'result':'failed','failure':'invalid_json','body':'private','response':{
                    'http_status':200,'body_kind':'empty','headers':'private','login_hint':False}},
                {'result':'failed','failure':[],'response':{'body_kind':[],'url':'private'}}]}]}
        audit=attach_lookup({},client,'x','y')
        self.assertNotIn('private',json.dumps(audit))
        self.assertEqual(audit['playback_lookup']['sources'][0]['attempts'][1]['failure'],'other_error')


if __name__=='__main__': unittest.main()
