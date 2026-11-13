import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import media_concurrency_probe as probe


class FakeResponse:
    def __init__(self, status, url, headers=None, body=b''):
        self.status_code, self.url, self.headers, self.body = status, url, headers or {}, body

    def iter_content(self, size):
        yield self.body

    def close(self): pass


class FakeVpn:
    logins = 0

    def __init__(self):
        import requests
        self.session = requests.Session()
        self.access_mode = 'webvpn'

    def login(self):
        print('private progress'); FakeVpn.logins += 1

    def authenticate_icourse(self, strict=False): return True


class FakeClient:
    def __init__(self, vpn): self.vpn = vpn
    def get_course_detail(self, course): return {'lectures': [{'sub_id': 1, 'date': '2026-11-01', 'has_playback': True}]}
    def get_video_url(self, course, sub): return 'https://media.example/v.mp4?clientUUID=a&t=1-1-x'
    def renew_video_url(self, url): return url
    def get_stream_params(self, url): return 'https://webvpn.example/v.mp4', 'Cookie: secret\r\nUser-Agent: x\r\n'
    def trusted_media_login_urls(self): return ('https://webvpn.example/login',)
    def check_alive(self): return True


class ProbeTests(unittest.TestCase):
    def test_records_only_codes_and_timings_and_never_reauthenticates(self):
        calls = []

        def get(self, target, headers, **kw):
            calls.append(headers)
            if len(calls) == 3:
                return FakeResponse(302, target, {'Location': 'https://webvpn.example/login'})
            return FakeResponse(206, target, {'Content-Range': 'bytes 0-9/100000000'}, b'0123456789')

        FakeVpn.logins = 0
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(probe, 'WebVPNSession', FakeVpn), patch.object(probe, 'ICourseClient', FakeClient), \
                patch.object(probe.config, 'COURSE_IDS', ['1']), patch.object(probe, 'INTERVAL', 0), \
                patch('requests.Session.get', get), patch.object(probe, 'MAX_READS', 5):
            out = os.path.join(tmp, 'r.json'); now = time.time()
            probe.main(['2', 'same', str(now), '0', str(now + 30), out])
            with open(out) as f: saved = json.load(f)
        self.assertEqual(FakeVpn.logins, 2)
        self.assertTrue(all('Cookie' not in h and h['Range'].startswith('bytes=') for h in calls))
        text = json.dumps(saved)
        for private in ('secret', 'media.example', 'webvpn.example', 'clientUUID'):
            self.assertNotIn(private, text)
        statuses = [e.get('status') for s in saved['sessions'] for e in s['events'] if e['kind'] == 'media']
        self.assertEqual(len(statuses), 10); self.assertEqual(statuses.count(302), 1)
        redirect = next(e for s in saved['sessions'] for e in s['events'] if e.get('status') == 302)
        self.assertEqual(redirect['redirect'], 'login')

    def test_bounds(self):
        with self.assertRaises(ValueError):
            probe.main(['6', 'x', '0', '0', '1', '/dev/null'])
        with self.assertRaises(ValueError):
            probe.main(['1', 'x', '0', '0', '4000', '/dev/null'])


if __name__ == '__main__':
    unittest.main()
