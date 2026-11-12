"""The same media recovery policy reaches local, cloud and scoped image clients."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from src.api.media_client import create_media_client
from src.runtime.audio_preparation import configure_audio_downloader


class MediaClientTests(unittest.TestCase):
    def test_routes_share_budgets_and_use_their_own_access_mode(self):
        for mode in ('direct','webvpn'):
            with self.subTest(mode=mode), patch('src.api.auth_recovery.fresh_media_session') as fresh:
                vpn=SimpleNamespace()
                client=create_media_client(vpn,access_mode=mode)
                self.assertIs(client.vpn,vpn)
                self.assertEqual(client._media_reauth_timeout,180)
                self.assertEqual(client._media_max_fresh_sessions,3)
                self.assertEqual(client._media_max_total_fresh_sessions,8)
                stop=threading.Event();client._media_reauth_factory(stop,123)
                fresh.assert_called_once_with(stop,123,read_timeout=60,max_attempts=2,access_mode=mode)

    def test_fork_retains_limits_and_isolates_download_ownership(self):
        client=create_media_client(SimpleNamespace())
        client._userinfo={'id':'fixture'}
        fork=client.fork_for_media()
        self.assertIsNot(fork,client)
        self.assertEqual(fork._media_max_total_fresh_sessions,client._media_max_total_fresh_sessions)
        self.assertIs(fork._media_reauth_factory,client._media_reauth_factory)
        self.assertIsNot(fork._userinfo,client._userinfo)
        self.assertIsNone(fork._media_owned_session)

    def test_downloader_preserves_explicit_transport_and_enables_phase_check(self):
        for mode in ('aac_auto','mp4'):
            downloader=SimpleNamespace()
            configure_audio_downloader(downloader,audio_mode=mode)
            self.assertEqual(downloader.audio_mode,mode)
            self.assertTrue(downloader.write_side_channel)
            self.assertTrue(downloader.allow_fresh_session_escalation)
        with self.assertRaises(ValueError):configure_audio_downloader(SimpleNamespace(),audio_mode='unknown')
        with self.assertRaises(ValueError):create_media_client(SimpleNamespace(),access_mode='unknown')
