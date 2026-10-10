"""Local failures only: TLS classification, cancelled acquisition and ASR deadlines."""
import contextlib
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
import requests
import yaml
from urllib3.exceptions import SSLError as UrllibSSLError, ProtocolError
from src.runtime.media_protocol import connection_failure_code, MediaTransportError
from src.runtime.media_transport import SignedRangeRelay
from src.runtime.scheduler import AudioDownloader, _PendingSpawn
from src.api.icourse import ICourseClient
from src.ai.qwen_transcriber import QwenTranscriber, RATE

MODULES={'torch':SimpleNamespace(inference_mode=contextlib.nullcontext),
         'transformers':SimpleNamespace(StoppingCriteriaList=list)}


class RuntimeAuditTests(unittest.TestCase):
    def test_manual_resources_share_batch_lock_and_push_registration_is_isolated(self):
        root=Path(__file__).resolve().parents[1]
        resource=yaml.load((root/'.github/workflows/qwen_production_resources.yml').read_text(),Loader=yaml.BaseLoader)
        daily=yaml.load((root/'.github/workflows/check.yml').read_text(),Loader=yaml.BaseLoader)
        pilot=yaml.load((root/'.github/workflows/parallel_pilot.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(daily['concurrency']['group'],'icourse-data-${{ github.repository }}')
        for workflow in (resource,pilot):
            lock=workflow['concurrency']
            self.assertIn("format('icourse-data-{0}', github.repository)",lock['group'])
            self.assertEqual(lock['cancel-in-progress'],'false');self.assertEqual(lock['queue'],'max')
        self.assertIn("github.event_name == 'push'",resource['concurrency']['group'])
        self.assertIn("format('icourse-resource-registration-{0}', github.run_id)",resource['concurrency']['group'])
        self.assertIn("github.event_name == 'workflow_dispatch'",resource['jobs']['plan']['if'])
        self.assertNotIn('concurrency',resource['jobs']['fetch'])  # No nested parent-lock deadlock.
        self.assertEqual(resource['jobs']['fetch']['strategy']['max-parallel'],'4')

    def test_direct_and_wrapped_tls_errors_are_permanent(self):
        for error in (requests.exceptions.SSLError('private-url'),ssl.SSLError('private-certificate'),
                      ProtocolError('private',UrllibSSLError('private'))):
            with self.subTest(kind=type(error).__name__):
                self.assertEqual(connection_failure_code(error),'upstream_tls_error')

    def test_tls_range_failure_does_not_retry_or_refresh(self):
        client=MagicMock();client._media_reauth_factory=None
        client.renew_video_url.return_value='https://private.invalid/media'
        client.get_stream_params.return_value=('https://private.invalid/media','')
        session=requests.Session();session.get=MagicMock(side_effect=requests.exceptions.SSLError('private'))
        relay=SignedRangeRelay(client,'https://private.invalid/media',session_factory=lambda:session,
                               allow_session_refresh=True)
        with self.assertRaises(MediaTransportError) as error:relay.start()
        self.assertEqual(error.exception.code,'upstream_tls_error')
        self.assertEqual(session.get.call_count,1)
        client.refresh_media_session.assert_not_called()
        self.assertEqual(relay.audit()['retries'],0)
        self.assertNotIn('private',json.dumps(relay.audit()))

    def test_existing_session_tls_failure_never_submits_a_fresh_login(self):
        vpn=MagicMock();vpn.session.get.side_effect=requests.exceptions.SSLError('private')
        factory=MagicMock();client=ICourseClient(vpn,media_reauth_factory=factory)
        client._userinfo={'id':'synthetic'}
        relay=SignedRangeRelay(client,'https://private.invalid/media',allow_session_refresh=True)
        try:
            with self.assertRaises(MediaTransportError) as raised:relay._refresh_session()
            self.assertEqual(raised.exception.code,'upstream_tls_error');factory.assert_not_called()
            self.assertEqual(relay.audit()['media_auth']['failure'],'auth_tls_error')
            self.assertEqual(relay.audit()['session_refresh_successes'],0)
            self.assertFalse(relay.client.reauthenticate_media_session(threading.Event()))
            factory.assert_not_called()
        finally:relay.close()

    def test_cancelled_slot_wait_does_not_lookup_or_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader=AudioDownloader(tmp,max_concurrent=1, audio_mode="mp4");pending=_PendingSpawn()
            downloader._active['1']=pending;downloader._sem.acquire()
            client=MagicMock();client.get_video_url.return_value=None
            worker=threading.Thread(target=downloader._spawn_when_ready,args=(client,'10','1',pending,True))
            worker.start();downloader.release('1')
            # Give the cancelled waiter its slot; it must not touch the account.
            downloader._sem.release();worker.join(2)
            self.assertFalse(worker.is_alive());client.get_video_url.assert_not_called()
            self.assertEqual(downloader.startup_failure('1'),{})
            self.assertTrue(downloader._sem.acquire(blocking=False));downloader._sem.release()

    def test_cancelled_waiter_exits_while_the_slot_is_still_occupied(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader=AudioDownloader(tmp,max_concurrent=1, audio_mode="mp4");pending=_PendingSpawn()
            downloader._active['1']=pending;downloader._sem.acquire();client=MagicMock()
            worker=threading.Thread(target=downloader._spawn_when_ready,args=(client,'10','1',pending,True))
            worker.start()
            try:
                downloader.release('1');worker.join(1)
                self.assertFalse(worker.is_alive());client.get_video_url.assert_not_called()
                self.assertFalse(downloader._sem.acquire(blocking=False))
            finally: downloader._sem.release();worker.join(2)

    def test_cancel_during_lookup_never_starts_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader=AudioDownloader(tmp,max_concurrent=1, audio_mode="mp4");pending=_PendingSpawn();downloader._active['1']=pending
            client=MagicMock()
            def lookup(*args):downloader.release('1');return 'private-url'
            client.get_video_url.side_effect=lookup
            with patch('src.runtime.scheduler.SignedRangeRelay') as relay,patch('src.runtime.scheduler.subprocess.Popen') as spawn:
                downloader._spawn_when_ready(client,'10','1',pending,True)
                relay.assert_not_called();spawn.assert_not_called()
            self.assertTrue(downloader._sem.acquire(blocking=False));downloader._sem.release()

    def test_orphan_cleanup_cannot_delete_same_lecture_replacement_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader=AudioDownloader(tmp,max_concurrent=2, audio_mode="mp4");pending=_PendingSpawn();downloader._active['1']=pending
            client=MagicMock();client.get_video_url.return_value='synthetic-url'
            client.get_stream_params.return_value=('synthetic-url','')
            old,new=MagicMock(),MagicMock()
            for proc in (old,new):proc.stderr=[];proc.poll.return_value=0
            paths=[]
            def spawn(cmd,**kw):
                path=Path(cmd[-1]);paths.append(path);path.write_bytes(b'synthetic PCM')
                if len(paths)==1:
                    downloader.release('1');replacement=_PendingSpawn();downloader._active['1']=replacement
                    downloader._spawn_when_ready(client,'10','1',replacement,False)
                    return old
                return new
            with patch('src.runtime.scheduler.subprocess.Popen',side_effect=spawn):
                downloader._spawn_when_ready(client,'10','1',pending,False)
            handle=downloader.get('1');self.assertIs(handle.process,new)
            self.assertNotEqual(paths[0],paths[1]);self.assertFalse(paths[0].exists());self.assertTrue(paths[1].exists())
            self.assertEqual(paths[1].read_bytes(),b'synthetic PCM');downloader.shutdown()
            self.assertFalse(paths[1].exists())

    def test_display_failure_keeps_decoder_monitor_and_slot_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            reporter=MagicMock();reporter.audio_prefetch_start.side_effect=RuntimeError('synthetic display')
            downloader=AudioDownloader(tmp,max_concurrent=1, audio_mode="mp4",reporter=reporter)
            pending=_PendingSpawn();downloader._active['1']=pending;client=MagicMock()
            client.get_stream_params.return_value=('synthetic-url','')
            proc=MagicMock();proc.stderr=[];proc.poll.return_value=0
            ended=threading.Event();proc.wait.side_effect=lambda **kw:ended.wait(2)
            try:
                with patch('src.runtime.scheduler.subprocess.Popen',return_value=proc):
                    downloader._spawn_when_ready(client,'10','1',pending,False)
                self.assertIs(downloader.get('1').process,proc)
                self.assertFalse(downloader._sem.acquire(blocking=False))
                reporter.audio_prefetch_failed.assert_not_called()
                ended.set();self.assertTrue(downloader._sem.acquire(timeout=1));downloader._sem.release()
            finally:ended.set();downloader.shutdown()

    def test_cancel_during_prefix_fetch_closes_transport_and_never_spawns(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloader=AudioDownloader(tmp,max_concurrent=1, audio_mode="mp4");pending=_PendingSpawn();downloader._active['1']=pending
            client=MagicMock();client.get_video_url.return_value='private-url'
            entered=threading.Event();closed=threading.Event()
            relay=MagicMock()
            def start():
                entered.set()
                if not closed.wait(2):raise TimeoutError('synthetic prefix remained active')
                return relay
            relay.start.side_effect=start;relay.close.side_effect=closed.set
            with patch('src.runtime.scheduler.SignedRangeRelay',return_value=relay),patch('src.runtime.scheduler.subprocess.Popen') as spawn:
                worker=threading.Thread(target=downloader._spawn_when_ready,args=(client,'10','1',pending,True))
                worker.start();self.assertTrue(entered.wait(1));downloader.release('1')
                self.assertTrue(closed.wait(.2));worker.join(2);self.assertFalse(worker.is_alive());spawn.assert_not_called()
            self.assertEqual(downloader.startup_failure('1'),{})
            self.assertTrue(downloader._sem.acquire(blocking=False));downloader._sem.release()

    def test_normal_decode_deadline_discards_text_and_stops_followup_generations(self):
        now=[0.0]
        class Backend:
            def generate(self,**kwargs):
                now[0]=11.0
                criteria=kwargs.get('stopping_criteria',[])
                for check in criteria:check(None,None)
                return SimpleNamespace(sequences=np.zeros((1,5)))
        backend=Backend();original=backend.generate
        model=SimpleNamespace(model=backend,max_new_tokens=2048,
                              processor=SimpleNamespace(tokenizer=SimpleNamespace(encode=lambda *a,**kw:[1])))
        calls=[]
        def transcribe(**kw):
            calls.append(kw)
            backend.generate(input_ids=np.zeros((1,3)),max_new_tokens=model.max_new_tokens)
            return [SimpleNamespace(text='deadline truncated text')]
        model.transcribe=transcribe
        t=QwenTranscriber();t._model=model
        with patch.dict(sys.modules,MODULES),patch('src.ai.qwen_transcriber.time.monotonic',side_effect=lambda:now[0]):
            row=t._recognize_resilient(np.zeros(RATE),{'start':0,'end':1},10.0)
        self.assertEqual(row['text'],'')
        self.assertEqual(row['missing_intervals'][0]['error_code'],'worker_deadline')
        self.assertEqual(len(calls),1)
        self.assertEqual(model.max_new_tokens,2048)
        self.assertNotIn('generate',backend.__dict__)

    def test_echo_retry_respects_remaining_worker_time_and_restores_wrappers(self):
        now=[0.0];calls=[];criteria=[]
        class Backend:
            def generate(self,**kwargs):
                now[0]=8.0 if not criteria else 11.0
                checks=kwargs.get('stopping_criteria',[]);criteria.append(checks)
                self.stopped=any(check(None,None) for check in checks)
                return SimpleNamespace(sequences=np.zeros((1,5)))
        backend=Backend()
        model=SimpleNamespace(model=backend,max_new_tokens=2048,
            processor=SimpleNamespace(tokenizer=SimpleNamespace(encode=lambda *a,**kw:[1])))
        texts=iter(['术语：矩阵、范数、扰动、条件数、逆矩阵','truncated echo retry'])
        def transcribe(**kw):
            calls.append(kw);backend.generate(input_ids=np.zeros((1,3)),max_new_tokens=model.max_new_tokens)
            return [SimpleNamespace(text=next(texts))]
        model.transcribe=transcribe;t=QwenTranscriber();t._model=model
        t.set_terms(['矩阵','范数','扰动','条件数','逆矩阵'])
        with patch.dict(sys.modules,MODULES),patch('src.ai.qwen_transcriber.time.monotonic',side_effect=lambda:now[0]):
            row=t._recognize_resilient(np.zeros(RATE),{'start':0,'end':1},10.0)
        self.assertEqual(len(calls),2);self.assertTrue(backend.stopped)
        self.assertEqual(row['text'],'');self.assertEqual(row['missing_intervals'][0]['error_code'],'worker_deadline')
        self.assertEqual(model.max_new_tokens,2048);self.assertNotIn('generate',backend.__dict__)


if __name__=='__main__':unittest.main()
