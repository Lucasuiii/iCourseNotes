import io
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import requests
from src.api.icourse import ICourseClient
from src.api.playback_diagnostics import lookup_error, attach_lookup
from src.runtime.scheduler import AudioDownloader, _PendingSpawn


class PlaybackDiagnosticTests(unittest.TestCase):
    def client(self):
        client = ICourseClient(MagicMock()); client.sign_video_url = MagicMock(return_value='signed-private-url')
        return client

    def test_fallback_preserves_success_and_keeps_only_error_categories(self):
        client = self.client(); client.get_sub_info = MagicMock(side_effect=requests.exceptions.ReadTimeout('private-response-url'))
        client.get_sub_detail = MagicMock(return_value={'content': {'playback': {'url': 'private-base.mp4'}}})
        with patch('sys.stdout', io.StringIO()): self.assertEqual(client.get_video_url('10', '1'), 'signed-private-url')
        audit = client.video_lookup_diagnostics('10', '1')
        self.assertTrue(audit['url_found']); self.assertEqual(audit['sources'][0]['failure'], 'timeout')
        self.assertNotIn('private', json.dumps(audit)); client.get_sub_detail.assert_called_once()

    def test_two_failed_sources_bound_transient_retries_and_never_start_ffmpeg(self):
        client = self.client(); response = MagicMock(status_code=503)
        client.get_sub_info = MagicMock(side_effect=requests.exceptions.HTTPError('private-body', response=response))
        client.get_sub_detail = MagicMock(side_effect=requests.exceptions.JSONDecodeError('private', 'private-body', 0))
        with tempfile.TemporaryDirectory() as tmp, patch('sys.stdout', io.StringIO()), patch('src.runtime.scheduler.subprocess.Popen') as spawn:
            downloader = AudioDownloader(tmp, max_concurrent=1, audio_mode="mp4"); pending = _PendingSpawn(); downloader._active['1'] = pending
            downloader._spawn_when_ready(client, '10', '1', pending, True)
            audit = downloader.startup_failure('1'); spawn.assert_not_called()
            self.assertIsNone(downloader.get('1'))
        self.assertEqual(audit['error_code'], 'media_url_unavailable')
        info,detail=audit['playback_lookup']['sources']
        self.assertEqual(info['attempt_count'],3)
        self.assertEqual(info['attempts'],[{'result':'failed','failure':'http_error','http_status':503}]*3)
        self.assertEqual(detail,{'source':'sub_detail','result':'failed','failure':'invalid_json'})
        self.assertEqual(client.get_sub_info.call_count,3); client.get_sub_detail.assert_called_once()
        self.assertNotIn('private', json.dumps(audit)); client.sign_video_url.assert_not_called()

    def test_api_code_and_empty_valid_payload_are_distinguishable(self):
        client = self.client(); response = MagicMock(); response.json.return_value = {'code': 7001, 'msg': 'private', 'data': {}}
        client.vpn.get.return_value = response
        with patch('sys.stdout', io.StringIO()): self.assertIsNone(client.get_video_url('10', '1'))
        audit = client.video_lookup_diagnostics('10', '1')
        self.assertEqual([r['api_code'] for r in audit['sources']], [7001, 7001])
        response.json.return_value = {'code': 0, 'data': {}}
        with patch('sys.stdout', io.StringIO()): self.assertIsNone(client.get_video_url('10', '1'))
        self.assertEqual(client.video_lookup_diagnostics('10', '1')['sources'], [
            {'source': 'sub_info', 'result': 'payload'}, {'source': 'sub_detail', 'result': 'payload'}])

    def test_tls_signing_and_diagnostic_failures_do_not_change_original_behavior(self):
        self.assertEqual(lookup_error(requests.exceptions.SSLError('private'))['failure'], 'tls_error')
        client = self.client(); client.get_sub_info = MagicMock(return_value={'video_list': {'0': {'preview_url': 'private.mp4'}}})
        error = requests.exceptions.ReadTimeout('private'); client.sign_video_url.side_effect = error
        with self.assertRaises(requests.exceptions.ReadTimeout) as caught: client.get_video_url('10', '1')
        self.assertIs(caught.exception, error)
        self.assertEqual(client.video_lookup_diagnostics('10', '1')['sources'][-1]['source'], 'signing')
        broken = MagicMock(); broken.video_lookup_diagnostics.side_effect = RuntimeError('private')
        self.assertEqual(attach_lookup({'phase': 'media_lookup'}, broken, '10', '1'), {'phase': 'media_lookup'})
        broken.video_lookup_diagnostics.side_effect = None
        broken.video_lookup_diagnostics.return_value = {'url_found': False, 'private': 'private', 'sources': [
            {'source': 'sub_info', 'result': 'failed', 'failure': 'private', 'http_status': 'private', 'body': 'private'}]}
        public = attach_lookup({}, broken, '10', '1')
        self.assertNotIn('private', json.dumps(public))
        self.assertEqual(public['playback_lookup']['sources'][0]['failure'], 'other_error')

    def test_snapshots_are_bounded_detached_and_do_not_keep_stale_results(self):
        client = self.client(); client.get_sub_info = MagicMock(return_value={}); client.get_sub_detail = MagicMock(return_value={})
        with patch('sys.stdout', io.StringIO()):
            for n in range(130): client.get_video_url('10', str(n))
        self.assertEqual(len(client._video_lookup_audits), 128); self.assertEqual(client.video_lookup_diagnostics('10', '0'), {})
        snapshot = client.video_lookup_diagnostics('10', '129'); snapshot['sources'].clear()
        self.assertEqual(len(client.video_lookup_diagnostics('10', '129')['sources']), 2)
        client.get_sub_info.return_value = {'now': 'invalid-private'}
        with self.assertRaises(ValueError): client.get_video_url('10', '129')
        self.assertEqual(client.video_lookup_diagnostics('10', '129')['sources'], [{'source': 'sub_info', 'result': 'payload'}])
