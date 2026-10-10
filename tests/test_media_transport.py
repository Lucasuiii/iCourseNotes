"""Real HTTP failures and FFmpeg decoding through the signed byte relay."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import parse_qs,urlsplit
from unittest.mock import MagicMock

import requests
from src.api.icourse import ICourseClient
from src.runtime.media_transport import SignedRangeRelay,MediaTransportError
from src.runtime.scheduler import AudioDownloader


class Origin:
    def __init__(self,data,*,drop_start=None,change=False,ignore=False,validator=True,expired=False,persistent=False,ignored_once_start=None,rejected_status=None):
        self.data=data;self.drop_start=drop_start;self.change=change
        self.ignore=ignore;self.validator=validator;self.expired=expired;self.persistent=persistent
        self.ignored_once_start=ignored_once_start;self.ignored_once=False
        self.rejected_status=rejected_status
        self.requests=[];self.dropped=False;self.tickets=set();self.error=None
    def __enter__(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                query=parse_qs(urlsplit(self.path).query)
                value=self.headers.get('Range','')
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',value)
                if not match:self.send_error(400);return
                start=int(match[1]);end=min(int(match[2]) if match[2] else len(owner.data)-1,len(owner.data)-1)
                owner.requests.append((start,end,query.get('t',[''])[0]))
                if owner.rejected_status is not None:
                    self.send_error(owner.rejected_status);return
                if self.headers.get('Cookie')!='fake-private-cookie':
                    owner.error='missing_cookie';self.send_error(403);return
                ticket=query.get('t',[''])[0]
                if start==0:
                    if ticket in owner.tickets:self.send_error(403);return
                    owner.tickets.add(ticket)
                if (owner.expired or owner.persistent) and start>0:
                    owner.expired=False;self.send_error(403);return
                ignored=(owner.ignore or start==owner.ignored_once_start and not owner.ignored_once)
                if ignored: owner.ignored_once=True
                self.send_response(200 if ignored else 206)
                self.send_header('Content-Length',str(end-start+1))
                self.send_header('Content-Range',f'bytes {start}-{end}/{len(owner.data)}')
                if owner.validator:
                    self.send_header('ETag','"changed"' if owner.change and start else '"immutable"')
                    self.send_header('Last-Modified','Tue, 06 Oct 2026 00:00:00 GMT')
                self.end_headers()
                data=owner.data[start:end+1]
                if start==owner.drop_start and not owner.dropped:
                    owner.dropped=True;data=data[:65536]
                    self.wfile.write(data);self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                try:self.wfile.write(data)
                except OSError:pass
        class Server(ThreadingHTTPServer):
            daemon_threads=True
            def handle_error(self,*args):pass
        self.server=Server(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}/media'
        self.signed=self.base+'?t=old&clientUUID=old'
        owner=self
        class Client:
            counter=0
            def get_video_url(self,*args):return owner.signed
            def renew_video_url(self,original,now=None):
                self.counter+=1
                return owner.base+f'?t=ticket{self.counter}&clientUUID={self.counter}'
            def get_stream_params(self,url):return url,'Cookie: fake-private-cookie\r\n'
        self.client=Client()
        return self
    def __exit__(self,*args):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)


class MediaTransportTests(unittest.TestCase):
    DATA=bytes(range(256))*4096

    def test_signature_renewal_keeps_selected_path_and_non_auth_query(self):
        client=ICourseClient(MagicMock())
        client._userinfo={'id':'fake-user','tenant_id':'fake-tenant','phone':'fake-phone'}
        url=client.renew_video_url('https://example.test/lesson.mp4?track=1&clientUUID=old&t=old',now=100)
        parts=urlsplit(url);query=parse_qs(parts.query)
        self.assertEqual(parts.path,'/lesson.mp4');self.assertEqual(query['track'],['1'])
        self.assertEqual(len(query['t']),1);self.assertNotEqual(query['t'],['old'])
        self.assertEqual(len(query['clientUUID']),1);self.assertNotEqual(query['clientUUID'],['old'])
        with self.assertRaises(ValueError):client.renew_video_url('https://example.test/x?t=a&t=b&clientUUID=x')

    def test_real_early_eof_resumes_after_buffered_bytes_without_duplicate_output(self):
        with Origin(self.DATA,drop_start=16384) as origin, \
             SignedRangeRelay(origin.client,origin.signed,chunk_bytes=128*1024,prefix_bytes=16384) as relay:
            response=requests.get(relay.url,timeout=20)
            self.assertEqual(response.content,self.DATA)
            audit=relay.audit()
            self.assertEqual(audit['retries'],1);self.assertIsNone(audit['terminal_error_code'])
            self.assertIn(81920,[row[0] for row in origin.requests])
            # Byte zero is served from the verified prefix, never a reused ticket.
            self.assertEqual(sum(row[0]==0 for row in origin.requests),1)
            self.assertIsNone(origin.error)
            for private in ('ticket','fake-private','http://','immutable'):
                self.assertNotIn(private,json.dumps(audit))

    def test_real_expired_signature_is_renewed_and_client_can_seek_or_reopen(self):
        with Origin(self.DATA,expired=True) as origin, \
             SignedRangeRelay(origin.client,origin.signed,chunk_bytes=128*1024,prefix_bytes=16384) as relay:
            for start,end in [(900000,900099),(0,199),(16384,17000)]:
                response=requests.get(relay.url,headers={'Range':f'bytes={start}-{end}'},timeout=20)
                self.assertEqual(response.status_code,206)
                self.assertEqual(response.content,self.DATA[start:end+1])
            self.assertEqual(relay.audit()['retries'],1)
            tickets=[x[2] for x in origin.requests]
            self.assertEqual(len(tickets),len(set(tickets)))
            self.assertEqual(requests.get(relay.url+'wrong',timeout=5).status_code,404)
            self.assertEqual(requests.get(relay.url,headers={'Range':'bytes=200-100'},timeout=5).status_code,416)

    def test_changed_same_size_source_never_splices_into_the_verified_prefix(self):
        with Origin(self.DATA,change=True) as origin, \
             SignedRangeRelay(origin.client,origin.signed,prefix_bytes=16384) as relay:
            with self.assertRaises(requests.RequestException):requests.get(relay.url,timeout=10)
            self.assertEqual(relay.audit()['terminal_error_code'],'source_changed')
            self.assertEqual(relay.audit()['upstream_bytes'],16384)

    def test_missing_validator_and_ignored_ranges_fail_before_forwarding(self):
        for options,code in [({'validator':False},'source_validator_missing'),({'ignore':True},'range_not_honored')]:
            with Origin(self.DATA,**options) as origin:
                relay=SignedRangeRelay(origin.client,origin.signed)
                with self.assertRaises(MediaTransportError) as raised:relay.start()
                self.assertEqual(raised.exception.code,code)
                self.assertEqual(relay.audit()['upstream_bytes'],0)

    def test_temporary_ignored_range_recovers_same_offset_without_forwarding_bad_response(self):
        with Origin(self.DATA,ignored_once_start=16384) as origin, \
             SignedRangeRelay(origin.client,origin.signed,chunk_bytes=128*1024,prefix_bytes=16384) as relay:
            response=requests.get(relay.url,timeout=20)
            self.assertEqual(response.content,self.DATA)
            audit=relay.audit()
            self.assertEqual(audit['range_rejections'],1);self.assertEqual(audit['retries'],1)
            self.assertIsNone(audit['terminal_error_code'])
            self.assertEqual(audit['upstream_bytes'],len(self.DATA))
            rows=[r for r in origin.requests if r[0]==16384]
            self.assertEqual(len(rows),2);self.assertNotEqual(rows[0][2],rows[1][2])
            self.assertEqual(audit['upstream_status_counts']['200'],1)
            self.assertGreater(audit['upstream_status_counts']['206'],1)

    def test_persistent_ignored_range_stays_bounded_and_forwards_no_unverified_bytes(self):
        with Origin(self.DATA,ignore=True) as origin:
            relay=SignedRangeRelay(origin.client,origin.signed,attempts=2)
            with self.assertRaises(MediaTransportError) as error:relay.start()
            self.assertEqual(error.exception.code,'range_not_honored')
            self.assertEqual(len(origin.requests),2)
            self.assertEqual(relay.audit()['upstream_bytes'],0)
            self.assertEqual(relay.audit()['upstream_status_counts'],{'200':2})

    def test_diagnostic_byte_budget_is_bounded_and_relay_closes(self):
        with Origin(self.DATA) as origin:
            relay=SignedRangeRelay(origin.client,origin.signed,prefix_bytes=4096,max_upstream_bytes=8192).start()
            port=relay._server.server_port
            with self.assertRaises(requests.RequestException):requests.get(relay.url,timeout=10)
            self.assertEqual(relay.audit()['terminal_error_code'],'diagnostic_byte_limit')
            self.assertEqual(relay.audit()['upstream_bytes'],8192)
            relay.close();relay.close()
            with socket.socket() as probe:self.assertNotEqual(probe.connect_ex(('127.0.0.1',port)),0)

    def test_persistent_rejection_stops_after_three_attempts(self):
        with Origin(self.DATA,persistent=True) as origin, \
             SignedRangeRelay(origin.client,origin.signed,prefix_bytes=4096) as relay:
            with self.assertRaises(requests.RequestException):requests.get(relay.url,timeout=15)
            self.assertEqual(relay.audit()['range_requests'],4)  # Prefix + three failed reads.
            self.assertEqual(relay.audit()['retries'],2)
            self.assertEqual(relay.audit()['terminal_error_code'],'upstream_retries_exhausted')

    def test_terminal_transport_error_blocks_even_a_zero_decoder_exit(self):
        from scripts.production_qwen import validate_prepared_audio
        spec={'audio_seconds':100,'media_seconds':100,'audio_diagnostics':{
            'stderr_complete':True,'decode_return_code':0,'decode_error_counts':{},
            'source_transport':{'terminal_error_code':'source_changed'}}}
        with self.assertRaisesRegex(ValueError,'read or decode'):
            validate_prepared_audio(spec)

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_actual_downloader_recovers_eof_and_preserves_gap_pcm_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);source=tmp/'gap.mkv';baseline=tmp/'baseline.raw'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                'sine=frequency=440:sample_rate=16000:duration=20','-af',
                r'asetpts=PTS+if(gte(T\,10)\,40/TB\,0)','-c:a','pcm_f32le',str(source)],check=True)
            subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(source),'-af',
                'aresample=async=1:first_pts=0','-ar','16000','-ac','1','-f','f32le',str(baseline)],check=True)
            with Origin(source.read_bytes(),drop_start=65536) as origin:
                downloader=AudioDownloader(str(tmp/'audio'),max_concurrent=1, audio_mode="mp4")
                try:
                    downloader.schedule(origin.client,'course','lesson',preserve_timestamps=True)
                    handle=downloader.get('lesson',timeout=20)
                    self.assertIsNotNone(handle)
                    self.assertEqual(handle.process.wait(timeout=30),0)
                    self.assertTrue(handle.stderr_done.wait(5))
                    actual=Path(handle.path).read_bytes()
                    self.assertEqual(hashlib.sha256(actual).digest(),hashlib.sha256(baseline.read_bytes()).digest())
                    self.assertEqual(len(actual)/64000,60)
                    self.assertEqual(handle.decode_error_counts,{})
                    self.assertEqual(handle.media_transport.audit()['retries'],1)
                    self.assertIsNone(handle.media_transport.audit()['terminal_error_code'])
                    self.assertIn(131072,[row[0] for row in origin.requests])
                    self.assertNotIn(b'fake-private',b''.join(handle.stderr_chunks))
                finally:downloader.shutdown()


if __name__=='__main__':unittest.main()
