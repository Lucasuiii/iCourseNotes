"""UIS second factor: a correct password may be answered with second=True (2026-11-09).

The flow completes it with the bound authenticator (TOTP) and never with SMS,
mail or QR, which need a person. No secret, code or token reaches diagnostics.
"""
import base64
import json
import unittest
from unittest.mock import patch

import test_webvpn_auth_diagnostics as fixtures
from src.api import webvpn
from src.api.webvpn import AuthenticationError, totp, next_totp

response = fixtures.response
SEED = base64.b32encode(b'12345678901234567890').decode()  # RFC 6238 SHA-1 test key
SECOND = {'code': 200, 'message': 'private-ok', 'loginToken': None, 'second': True, 'pageLevelNo': 2,
          'authChainCode': 'private-second-chain', 'requestNumber': 'private-request', 'userName': 'private-user',
          'moduleCodes': ['qr', 'userAndMail', 'userAndSms', 'userAndOtp'],
          'mobile': 'private-mobile', 'mail': 'private-mail'}


class TotpTests(unittest.TestCase):
    def test_rfc6238_vectors_and_formatting(self):
        self.assertEqual(totp(SEED, now=59), '287082')
        self.assertEqual(totp(SEED, now=1111111109), '081804')
        self.assertEqual(totp(SEED, now=1234567890), '005924')
        spaced = ' '.join(SEED[i:i+4] for i in range(0, len(SEED), 4)).lower()
        self.assertEqual(totp(spaced, now=59), '287082')
        for bad in ('', 'not base32!'):
            with self.assertRaises(AuthenticationError) as caught:
                totp(bad, now=59)
            self.assertEqual(caught.exception.reason, 'otp_secret_invalid')


class FakeClock:
    """Shared by the window guard: sleeping advances time instead of waiting."""
    def __init__(self, now=1000.0): self.now, self.slept = now, []
    def time(self): return self.now
    def sleep(self, seconds): self.slept.append(seconds); self.now += seconds


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(1000.0)
        for patcher in (patch.object(webvpn, '_otp_clock', self.clock.time), patch.object(webvpn, '_otp_sleep', self.clock.sleep),
                        patch.object(webvpn, '_OTP_LAST_WINDOW', [None]),
                        patch.object(webvpn, '_OTP_STATE_PATH', self.state_path())):
            patcher.start(); self.addCleanup(patcher.stop)

    def state_path(self):
        import tempfile
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        return folder.name + '/otp-window'

    def test_a_window_is_never_submitted_twice(self):
        first = next_totp(SEED)
        self.assertEqual(first, totp(SEED, now=1000.0))
        self.clock.now += 5  # a retry 5 s later is still in window 33
        second = next_totp(SEED)
        self.assertEqual(len(self.clock.slept), 1)
        self.assertGreaterEqual(self.clock.now, 34*30)
        self.assertEqual(second, totp(SEED, now=self.clock.now))
        self.assertNotEqual(int(1000.0 // 30), int(self.clock.now // 30))
        self.clock.now += 60
        next_totp(SEED); self.assertEqual(len(self.clock.slept), 1)  # a later window needs no wait


    def test_a_window_used_by_another_process_is_skipped(self):
        with open(webvpn._OTP_STATE_PATH, 'w') as f: f.write(str(int(1000.0 // 30)))
        code = next_totp(SEED)
        self.assertEqual(len(self.clock.slept), 1)
        self.assertEqual(code, totp(SEED, now=self.clock.now))
        with open(webvpn._OTP_STATE_PATH) as f: self.assertEqual(int(f.read()), int(self.clock.now // 30))


class SecondFactorFlowTests(WindowTests):
    def session(self, *replies):
        vpn = fixtures.AuthDiagnosticTests().session(execute=response(SECOND))
        posts = list(vpn.session.post.side_effect)
        vpn.session.post.side_effect = posts[:2] + list(replies) + posts[2:]
        return vpn

    def test_otp_completes_the_second_page_and_keeps_diagnostics_private(self):
        vpn = self.session(response({'code': 200, 'loginToken': 'private-token'}))
        with patch('src.runtime.config.UIS_OTP_SECRET', SEED), patch('src.api.webvpn.totp', return_value='123456'):
            self.assertTrue(vpn.authenticate_icourse('private-student', 'private-password', strict=True))
        first, second = vpn.session.post.call_args_list[1], vpn.session.post.call_args_list[2]
        self.assertIn('/authExecute', second.args[0])
        body = second.kwargs['json']
        self.assertEqual(body['authModuleCode'], 'userAndOtp')
        self.assertEqual(body['authChainCode'], 'private-second-chain')
        self.assertEqual({k: body[k] for k in ('lck', 'entityId', 'requestType')},
                         {k: first.kwargs['json'][k] for k in ('lck', 'entityId', 'requestType')})
        self.assertEqual(body['requestNumber'], 'private-request')
        self.assertEqual(body['authPara'], {'loginName': 'private-user', 'otpCode': '123456'})
        audit = json.dumps(vpn.auth_diagnostics)
        self.assertNotIn('private', audit); self.assertNotIn('123456', audit)
        self.assertTrue(any(x.get('second_factor') and x.get('otp_offered') for x in vpn.auth_diagnostics))

    def test_missing_seed_or_no_otp_module_stops_without_another_post(self):
        for seed, modules in ((SEED, ['qr', 'userAndSms']), ('', SECOND['moduleCodes'])):
            vpn = fixtures.AuthDiagnosticTests().session(execute=response(dict(SECOND, moduleCodes=modules)))
            with self.subTest(seed=bool(seed), modules=modules), patch('src.runtime.config.UIS_OTP_SECRET', seed):
                with self.assertRaises(AuthenticationError) as caught:
                    vpn.authenticate_icourse('id', 'password', strict=True)
                self.assertEqual(caught.exception.reason, 'second_factor_required')
                self.assertEqual(vpn.session.post.call_count, 2)

    def test_rejected_code_is_retried_once_with_the_next_window_then_reported(self):
        wrong = response({'code': 400, 'message': 'private-wrong-code'})
        vpn = self.session(wrong, wrong)
        with patch('src.runtime.config.UIS_OTP_SECRET', SEED):
            with self.assertRaises(AuthenticationError) as caught:
                vpn.authenticate_icourse('id', 'password', strict=True)
        self.assertEqual(caught.exception.reason, 'second_factor_rejected')
        self.assertEqual(vpn.session.post.call_count, 4)  # password, two codes, never a third
        codes = [c.kwargs['json']['authPara']['otpCode'] for c in vpn.session.post.call_args_list[2:4]]
        self.assertNotEqual(codes[0], codes[1])
        self.assertNotIn('private', json.dumps(vpn.auth_diagnostics))

    def test_second_code_on_the_same_page_completes_the_login(self):
        vpn = self.session(response({'code': 400, 'message': 'private-wrong-code'}),
                           response({'code': 200, 'loginToken': 'private-token'}))
        with patch('src.runtime.config.UIS_OTP_SECRET', SEED):
            self.assertTrue(vpn.authenticate_icourse('id', 'password', strict=True))
        retry = vpn.session.post.call_args_list[3].kwargs['json']
        self.assertEqual((retry['authChainCode'], retry['requestNumber']), ('private-second-chain', 'private-request'))


if __name__ == '__main__':
    unittest.main()
