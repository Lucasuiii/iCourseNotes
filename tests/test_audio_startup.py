"""Pre-PCM failures must keep safe evidence without retrying or exposing URLs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from src.runtime.scheduler import AudioDownloader, _PendingSpawn
from src.runtime.media_protocol import MediaTransportError
from src.runtime.audio_preparation import startup_diagnostics
from scripts import production_resource_fetch as resource


class AudioStartupTests(unittest.TestCase):
    def pending(self,tmp):
        downloader=AudioDownloader(tmp,max_concurrent=1, audio_mode="mp4")
        pending=_PendingSpawn();downloader._active['1']=pending
        return downloader,pending

    def test_initial_range_failure_keeps_its_transport_audit_and_releases_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader,pending=self.pending(tmp)
            client=MagicMock();client.get_video_url.return_value='private-url'
            with patch('src.runtime.scheduler.SignedRangeRelay') as relay,patch('src.runtime.scheduler.subprocess.Popen') as spawn:
                relay.return_value.start.side_effect=MediaTransportError('media_session_unavailable')
                relay.return_value.audit.return_value={'upstream_status_counts':{'302':1},'upstream_bytes':0,'terminal_error_code':'media_session_unavailable'}
                downloader._spawn_when_ready(client,'10','1',pending,True)
                spawn.assert_not_called();relay.return_value.start.assert_called_once();relay.return_value.close.assert_called_once()
            self.assertIsNone(downloader.get('1'))
            failure=downloader.startup_failure('1')
            self.assertEqual(failure['phase'],'media_transport_start')
            self.assertEqual(failure['error_code'],'media_session_unavailable')
            self.assertEqual(failure['source_transport']['upstream_status_counts'],{'302':1})
            self.assertTrue(downloader._sem.acquire(blocking=False));downloader._sem.release()
            failure['source_transport']['upstream_bytes']=100
            self.assertEqual(downloader.startup_failure('1')['source_transport']['upstream_bytes'],0)
            self.assertNotIn('private',json.dumps(failure))
            downloader.shutdown();self.assertEqual(downloader.startup_failure('1'),{})

    def test_missing_playback_is_distinct_from_an_api_exception(self):
        for error in (None,ValueError('private-url cookie private-secret')):
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as tmp:
                downloader,pending=self.pending(tmp);client=MagicMock()
                client.get_video_url.return_value=None;client.get_video_url.side_effect=error
                downloader._spawn_when_ready(client,'10','1',pending,True)
                self.assertIsNone(downloader.get('1'))
                failure=downloader.startup_failure('1')
                self.assertEqual(failure['phase'],'media_lookup')
                self.assertEqual(failure['error_code'],'audio_startup_exception' if error else 'media_url_unavailable')
                self.assertNotIn('private',json.dumps(failure));downloader.shutdown()

    def test_decoder_spawn_failure_does_not_become_an_empty_lecture(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader,pending=self.pending(tmp);client=MagicMock();client.get_video_url.return_value='private-url'
            with patch('src.runtime.scheduler.SignedRangeRelay') as relay,patch('src.runtime.scheduler.subprocess.Popen',side_effect=FileNotFoundError('private-command')):
                relay.return_value.audit.return_value={'upstream_bytes':65536}
                downloader._spawn_when_ready(client,'10','1',pending,True)
            self.assertIsNone(downloader.get('1'))
            failure=downloader.startup_failure('1')
            self.assertEqual(failure['phase'],'decoder_spawn');self.assertEqual(failure['error_type'],'FileNotFoundError')
            self.assertNotIn('private',json.dumps(failure));downloader.shutdown()

    def test_unrecognized_exception_codes_are_not_published(self):
        self.assertEqual(startup_diagnostics('media_lookup',MediaTransportError('private-url-cookie'))['error_code'],'audio_startup_exception')

    def test_resource_startup_and_auth_failures_remain_distinct(self):
        selected={'task_slot':0,'course_id':'1','status':'selected','task':['1','private course',{'sub_id':'11','_validation':{'date':'2026-10-01'}}]}
        import os
        for login_error in (False,True):
            with self.subTest(login_error=login_error),tempfile.TemporaryDirectory() as tmp:
                downloader=MagicMock();downloader.get.return_value=None
                failure={'phase':'media_transport_start','error_type':'MediaTransportError','error_code':'source_changed'}
                downloader.startup_failure.return_value=failure
                with patch.object(resource,'check_request'),patch.dict(os.environ,{'COURSE_SLOT':'0'}), \
                     patch.object(resource.pipeline,'artifact'),patch.object(resource.shards,'unseal',return_value={'selections.json':json.dumps([selected]).encode()}), \
                     patch.object(resource.pipeline,'root',return_value=Path(tmp)), \
                     patch.object(resource,'authenticated_session') as login,patch.object(resource,'AudioDownloader',return_value=downloader),patch.object(resource,'save_result') as save:
                    if login_error:login.side_effect=ValueError('private credentials')
                    self.assertFalse(resource.fetch())
                audit=save.call_args.args[0]['audit']
                self.assertEqual(audit['phase'],'authentication' if login_error else 'audio_startup')
                self.assertFalse(audit['audio_retained']);self.assertFalse(audit['publication']);self.assertFalse(audit['emailed'])
                if not login_error:
                    self.assertEqual(audit['audio_startup_diagnostics'],failure)
                    self.assertEqual(audit['error_code'],'audio_startup_failed')
                else:downloader.schedule.assert_not_called()
                self.assertNotIn('private',json.dumps(audit))
