"""UIS second factor: a correct password may be answered with second=True (2026-11-09).

The flow completes it with the bound authenticator (TOTP) and never with SMS,
mail or QR, which need a person. No secret, code or token reaches diagnostics.
"""
import base64
import json
import unittest
from unittest.mock import patch

import test_webvpn_auth_diagnostics as fixtures
from src.api.webvpn import AuthenticationError, totp

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


class SecondFactorFlowTests(unittest.TestCase):
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

    def test_rejected_code_is_reported_and_never_resubmitted(self):
        vpn = self.session(response({'code': 400, 'message': 'private-wrong-code'}))
        with patch('src.runtime.config.UIS_OTP_SECRET', SEED):
            with self.assertRaises(AuthenticationError) as caught:
                vpn.authenticate_icourse('id', 'password', strict=True)
        self.assertEqual(caught.exception.reason, 'second_factor_rejected')
        self.assertEqual(vpn.session.post.call_count, 3)
        self.assertNotIn('private', json.dumps(vpn.auth_diagnostics))


if __name__ == '__main__':
    unittest.main()
