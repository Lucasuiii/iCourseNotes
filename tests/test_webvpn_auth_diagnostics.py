"""Synthetic auth responses only: never access a real account or URL."""
import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import yaml
from src.api.webvpn import AuthenticationError,WebVPNSession,auth_observation


def response(data=None, *, text='', url='https://private/?lck=private-context', status=200):
    return SimpleNamespace(status_code=status,history=[],text=text,url=url,headers={},json=lambda:data or {})


class AuthDiagnosticTests(unittest.TestCase):
    def test_portal_timeout_does_not_replay_a_consumed_ticket(self):
        import requests
        vpn=WebVPNSession();vpn.session=MagicMock();vpn.session.cookies=[]
        vpn.session.get.side_effect=[response(),requests.exceptions.ReadTimeout('private')]
        with self.assertRaises(requests.exceptions.ReadTimeout):vpn._establish_session('private-ticket')
        self.assertEqual(vpn.session.get.call_count,2)
        self.assertEqual(vpn.auth_diagnostics[-1],{'stage':'webvpn_session_probe','transport_error':'timeout'})
        vpn.session.close()

    def test_ticket_200_and_cookie_after_timeout_do_not_prove_authenticated_session(self):
        import requests
        for first,cookies in [(response(),[]),
                (requests.exceptions.ReadTimeout('private'),[SimpleNamespace(name='wengine_vpn_ticket')])]:
            vpn=WebVPNSession();vpn.session=MagicMock();vpn.session.cookies=cookies
            vpn.session.get.side_effect=[first,response(status=302)]
            with self.assertRaises(AuthenticationError) as error:vpn._establish_session('private-ticket')
            self.assertEqual(error.exception.reason,'cold_session')
            self.assertEqual(vpn.auth_diagnostics[-1]['stage'],'webvpn_session_probe')
            self.assertEqual(vpn.auth_diagnostics[-1]['http_status'],302)
            self.assertNotIn('private',json.dumps(vpn.auth_diagnostics));vpn.session.close()

    def test_ticket_success_requires_portal_200(self):
        vpn=WebVPNSession();vpn.session=MagicMock()
        vpn.session.get.side_effect=[response(),response()]
        vpn._establish_session('private-ticket')
        self.assertEqual(vpn.session.get.call_count,2)
        self.assertEqual(vpn.auth_diagnostics[-1]['stage'],'webvpn_session_probe');vpn.session.close()

    def test_session_ticket_timeout_never_replays_ticket_or_exposes_cookie_or_url(self):
        import requests
        vpn=WebVPNSession();vpn.session=MagicMock();vpn.session.cookies=[]
        vpn.session.get.side_effect=requests.exceptions.ReadTimeout('private-ticket-url')
        with self.assertRaises(requests.exceptions.ReadTimeout):vpn._establish_session('private-ticket-url')
        self.assertEqual(vpn.session.get.call_count,1)
        self.assertEqual(vpn.session.get.call_args.kwargs['timeout'],60)
        self.assertEqual([row['attempt'] for row in vpn.auth_diagnostics],[1])
        self.assertTrue(all(row['transport_error']=='timeout' for row in vpn.auth_diagnostics))
        self.assertNotIn('private',json.dumps(vpn.auth_diagnostics));vpn.session.close()

    def session(self, *, api=None, execute=None, methods=None, cas=None, warmup=None):
        vpn=WebVPNSession();vpn.session=MagicMock();vpn._encrypt_password=MagicMock(return_value='private-encrypted-password')
        vpn.session.get.side_effect=[warmup or response(),cas or response(),response({'data':'private-public-key'}),
                                     response(),api or response({'code':0,'data':{'name':'private-name'}})]
        vpn.session.post.side_effect=[methods or response({'data':[{'moduleCode':'userAndPwd','authChainCode':'private-chain'}]}),
                                     execute or response({'code':200,'loginToken':'private-token'}),
                                     response(text='locationValue="https://private/cas?ticket=private-ticket"')]
        return vpn

    def test_verified_flow_records_presence_only_and_accepts_numeric_string_code(self):
        vpn=self.session(api=response({'code':'0','data':{'name':'private-name'}}))
        self.assertTrue(vpn.authenticate_icourse('private-student','private-password',strict=True))
        self.assertTrue(vpn.auth_diagnostics[-1]['verified'])
        audit=json.dumps(vpn.auth_diagnostics)
        self.assertNotIn('private',audit);self.assertNotIn('ticket=',audit)
        self.assertTrue(any(x.get('login_token_found') for x in vpn.auth_diagnostics))
        vpn.session.close()

    def test_cold_session_and_missing_redirect_context_are_distinct(self):
        for reason,options in [('cold_session',{'warmup':response(status=302)}),
                               ('cas_context_missing',{'cas':response(url='https://private/login',text='captcha form')})]:
            vpn=self.session(**options)
            with self.assertRaises(AuthenticationError) as error:vpn.authenticate_icourse('id','password',strict=True)
            self.assertEqual(error.exception.reason,reason)
            self.assertNotIn('private',json.dumps(vpn.auth_diagnostics));vpn.session.close()

    def test_password_method_and_explicit_challenge_are_distinct(self):
        vpn=self.session(methods=response({'data':[{'moduleCode':'otp'}]}))
        with self.assertRaises(AuthenticationError) as error:vpn.authenticate_icourse('id','password',strict=True)
        self.assertEqual(error.exception.reason,'password_method_missing');vpn.session.close()
        vpn=self.session(execute=response({'code':401,'needVerifyCode':True,'msg':'private-password'}))
        with self.assertRaises(AuthenticationError) as error:vpn.authenticate_icourse('id','password',strict=True)
        self.assertEqual(error.exception.reason,'authentication_rejected')
        self.assertTrue(vpn.auth_diagnostics[-1]['needVerifyCode'])
        self.assertEqual(vpn.auth_diagnostics[-1]['response_code'],401);vpn.session.close()

    def test_diagnostic_strict_mode_refuses_unverified_api_without_changing_default(self):
        for strict in (True,False):
            vpn=self.session(api=response({'code':401}))
            if strict:
                with self.assertRaises(AuthenticationError) as error:vpn.authenticate_icourse('id','password',strict=True)
                self.assertEqual(error.exception.reason,'api_verification_failed')
            else:self.assertTrue(vpn.authenticate_icourse('id','password'))
            self.assertFalse(vpn.auth_diagnostics[-1]['verified']);vpn.session.close()

    def test_safe_observation_does_not_export_provider_text_or_unknown_fields(self):
        row=auth_observation('auth_execute',response(text='验证码 private-student'),
                             {'code':'private-id','needCaptcha':True,'loginToken':'private-token'},
                             secret='private-password',verified=False)
        self.assertTrue(row['captcha_page_hint']);self.assertTrue(row['needCaptcha'])
        self.assertNotIn('response_code',row);self.assertNotIn('private',json.dumps(row))

    def test_auth_only_failure_is_encrypted_and_has_no_media_database_or_model_secrets(self):
        from scripts import production_auth_inspection as inspection
        from scripts.production_result_export import decrypt
        key=X25519PrivateKey.generate();public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        private=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RUNNER_TEMP':tmp,'SOURCE_RUN_ID':'99',
                'SOURCE_SLOT':'0','RECIPIENT_PUBLIC_KEY':base64.b64encode(public).decode()}):
            vpn=MagicMock();vpn.auth_diagnostics=[{'stage':'cas_context','http_status':200,'context_found':False}]
            vpn.authenticate_icourse.side_effect=AuthenticationError('cas_context_missing')
            with patch.object(inspection,'WebVPNSession',return_value=vpn):
                with self.assertRaises(AuthenticationError):inspection.inspect()
            blob=(Path(tmp)/'qwen-shards/out/auth-inspection.enc').read_bytes()
            audit=json.loads(decrypt(blob,private,'99',0))
            self.assertEqual(audit['failure_reason'],'cas_context_missing')
            self.assertFalse(audit['verified']);self.assertEqual(audit['attempts'],1)
            self.assertEqual(audit['media_requests'],0);self.assertEqual(audit['model_calls'],0)
            vpn.authenticate_icourse.assert_called_once_with(strict=True)
            vpn.session.close.assert_called_once()
        workflow=yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/qwen_production_validation.yml').read_text())
        job=workflow['jobs']['inspect-authentication']
        self.assertEqual(job['permissions'],{'contents':'read'})
        self.assertEqual({k for k,v in job['env'].items() if 'secrets.' in v},{'StuId','UISPsw','UIS_OTP_SECRET'})
        for name in ('export','inspect-audio','inspect-source-metadata'):
            self.assertIn('!inputs.inspect_authentication',workflow['jobs'][name]['if'])


if __name__=='__main__':unittest.main()
