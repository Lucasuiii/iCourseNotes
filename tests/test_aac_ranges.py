"""Synthetic indexes and real loopback HTTP; never use account credentials."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import requests

from src.runtime.aac_ranges import (AACRangeTransport, Limits, MediaTransportError,
    VerifiedPackets, aac_config, boxes, fetch_packets, load_index, parse_multipart)


def box(kind, payload, wide=False):
    return (struct.pack('>I4sQ',1,kind,len(payload)+16) if wide else
            struct.pack('>I4s',len(payload)+8,kind))+payload


def full(kind,payload): return box(kind,b'\0'*4+payload)


def desc(tag,payload): return bytes([tag,len(payload)])+payload


def fixture(*,tail=False,wide=False,compact=0,edits=False,fragment=False,
            duplicate_track=False,wrong_count=False,bad_offset=False,codec=b'mp4a'):
    # Three samples, variable duration and multiple stsc runs. Video bytes sit between chunks.
    asc=bytes.fromhex('1190')
    esds=full(b'esds',desc(3,b'\0\1\0'+desc(4,b'\x40\x15'+b'\0'*11+desc(5,asc))+desc(6,b'\2')))
    entry=box(codec,b'\0'*6+struct.pack('>H',1)+b'\0'*8+struct.pack('>HHI',2,16,0)+struct.pack('>I',48000<<16)+esds)
    sizes=[3,4,5]
    def moov(offset):
        stsd=full(b'stsd',struct.pack('>I',1)+entry)
        if compact:
            packed=(b'\x34\x50' if compact==4 else bytes(sizes) if compact==8 else struct.pack('>3H',*sizes))
            sz=full(b'stz2',b'\0'*3+bytes([compact])+struct.pack('>I',3)+packed)
        else:sz=full(b'stsz',struct.pack('>II3I',0,4 if wrong_count else 3,*sizes))
        co=full(b'co64' if wide else b'stco',struct.pack('>I',2)+struct.pack('>2Q' if wide else '>2I',offset+500 if bad_offset else offset,offset+17))
        stsc=full(b'stsc',struct.pack('>I6I',2,1,2,1,2,1,1))
        stts=full(b'stts',struct.pack('>I4I',2,2,1024,1,1008))
        stbl=box(b'stbl',stsd+sz+co+stsc+stts)
        mdhd=full(b'mdhd',struct.pack('>4IHH',0,0,48000,3056,0,0))
        hdlr=full(b'hdlr',struct.pack('>I',0)+b'soun'+b'\0'*12)
        track=box(b'trak',(box(b'edts',full(b'elst',struct.pack('>I',0))) if edits else b'')+box(b'mdia',mdhd+hdlr+box(b'minf',stbl)))
        return box(b'moov',track*(2 if duplicate_track else 1)+(box(b'mvex',b'') if fragment else b''),wide)
    ftyp=box(b'ftyp',b'isom\0\0\0\0isom')
    payload=b'abcDEFG'+b'VIDEO-----'+b'hijkl'
    if tail:return ftyp+box(b'mdat',payload)+moov(len(ftyp)+8)
    metadata=moov(0)
    metadata=moov(len(ftyp)+len(metadata)+8)
    return ftyp+metadata+box(b'mdat',payload)


class MemoryReader:
    def __init__(self,data,limits=Limits()):self.data=data;self.total=len(data);self.limits=limits;self.stats={'index_bytes':0,'index_seconds':0};self.format_observations={}
    def read(self,start,size,index=False):
        if index:self.stats['index_bytes']+=size
        return self.data[start:start+size]
    def audit(self):return self.stats
    def _count(self,k,v):self.stats[k]+=v
    def check(self):pass


def multipart(data,ranges,mode='ok'):
    items=list(ranges)
    if mode=='reorder':items.reverse()
    if mode=='missing':items=items[:-1]
    if mode=='duplicate':items[-1]=items[0]
    if mode=='extra':items.append((0,0))
    result=bytearray()
    for a,b in items:
        total=len(data)+(1 if mode=='total' else 0)
        result.extend(f'--B\r\nContent-Type: video/mp4\r\nContent-Range: bytes {a}-{b}/{total}\r\n\r\n'.encode())
        result.extend(data[a:b+1]);result.extend(b'\r\n')
    result.extend(b'--B--\r\n')
    if mode=='boundary':return bytes(result).replace(b'--B',b'--C')
    if mode=='truncate':return bytes(result[:-10])
    return bytes(result)


class Origin:
    def __init__(self,data,mode='ok'):
        self.data=data;self.mode=mode;self.rows=[];self.calls=0;self.refresh=0
    def __enter__(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def do_GET(self):
                owner.calls+=1
                ranges=[(int(p.split('-')[0]), int(p.split('-')[1]) if p.split('-')[1] else len(owner.data)-1)
                        for p in self.headers['Range'][6:].split(',')]
                owner.rows.append((ranges,self.headers.get('If-Match'),self.headers.get('Accept-Encoding')))
                multi=len(ranges)>1;mode=owner.mode
                if multi and (mode=='auth' and owner.refresh==0 or mode=='auth_persistent' or mode=='auth_change' and owner.refresh==0):
                    self.send_response(401);self.send_header('Content-Length','0');self.end_headers();return
                status=(200 if mode=='ignored' and multi else 416 if mode=='416' and multi else 429 if mode=='429' and multi else 206)
                body=multipart(owner.data,ranges,mode) if multi else owner.data[ranges[0][0]:ranges[0][1]+1]
                self.send_response(status)
                self.send_header('ETag','"changed"' if (mode=='changed' or mode=='auth_change' and owner.refresh) and multi else '"stable"')
                self.send_header('Content-Length',str(len(body)))
                if multi:
                    self.send_header('Content-Type','text/html' if mode=='html' else 'multipart/byteranges; boundary="B"')
                else:self.send_header('Content-Range',f'bytes {ranges[0][0]}-{ranges[0][1]}/{len(owner.data)}')
                if mode=='gzip' and multi:self.send_header('Content-Encoding','gzip')
                self.end_headers()
                if mode=='drop' and multi and owner.calls==2:
                    self.wfile.write(body[:10]);self.wfile.flush();self.close_connection=True
                elif mode=='drip' and multi:
                    try:
                        for byte in body:
                            self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(.01)
                    except OSError:pass
                else:self.wfile.write(body)
        class Server(ThreadingHTTPServer):
            def handle_error(self,*args):pass
        self.server=Server(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/media'
        class Client:
            def renew_video_url(self,url,now=None):return url
            def get_stream_params(self,url):return url,''
            def refresh_media_session(self):owner.refresh+=1;return True
        self.client=Client();return self
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join(2)


class IndexTests(unittest.TestCase):
    def test_head_tail_wide_compact_variable_timing_and_chunks(self):
        for options in ({},{'tail':True},{'wide':True},{'compact':4},{'compact':8},{'compact':16}):
            with self.subTest(options=options):
                data=fixture(**options);index=load_index(MemoryReader(data));p=index.packets(seconds=index.duration/index.timescale)
                self.assertEqual([data[x.offset:x.offset+x.size] for x in p],[b'abc',b'DEFG',b'hijkl'])
                self.assertEqual([x.duration for x in p],[1024,1024,1008])
                self.assertEqual(p[-1].dts+p[-1].duration,index.duration)
                self.assertEqual(index.packets(start=1024/48000,seconds=.01)[0].number,1)

    def test_unsupported_and_damaged_indexes_fail_closed(self):
        cases=[({'edits':True},'edit_list_unsupported'),({'fragment':True},'fragmented_mp4_unsupported'),
               ({'duplicate_track':True},'ambiguous_audio_tracks'),({'wrong_count':True},'sample_count_mismatch'),
               ({'bad_offset':True},'invalid_sample_offset'),({'codec':b'enca'},'codec_unsupported')]
        for options,code in cases:
            with self.subTest(options=options),self.assertRaises(MediaTransportError) as e:load_index(MemoryReader(fixture(**options)))
            self.assertEqual(e.exception.code,code)
        for broken in (b'\0'*7,struct.pack('>I4s',7,b'moov'),struct.pack('>I4s',2**32-1,b'moov'),fixture()[:-1]):
            with self.assertRaises(MediaTransportError):load_index(MemoryReader(broken))

    def test_index_budget_and_count_limits(self):
        for limits in (Limits(index_bytes=32),Limits(samples=2)):
            with self.assertRaises(MediaTransportError):load_index(MemoryReader(fixture(),limits))
        with self.assertRaises(MediaTransportError):list(boxes(struct.pack('>I4s',1,b'moov')))

    def test_only_lc_standard_frame_config_supported(self):
        self.assertEqual(aac_config(bytes.fromhex('1190')),(48000,2,3))
        for asc in (b'',bytes.fromhex('2990'),bytes.fromhex('1194'),bytes.fromhex('1190ffff')):
            with self.assertRaises(MediaTransportError):aac_config(asc)
        with self.assertRaises(ValueError):Limits(seconds=float('nan'))

    def test_missing_duplicate_packets_rejected_even_if_decodable(self):
        data=fixture();index=load_index(MemoryReader(data));packets=index.packets(seconds=index.duration/index.timescale)
        payloads=tuple(data[p.offset:p.offset+p.size] for p in packets)
        for p,b in ((packets,payloads[:-1]),(packets,(*payloads[:-1],b'x')),
                    ((packets[0],packets[0]),(payloads[0],payloads[0])),
                    ((packets[0],packets[2]),(payloads[0],payloads[2]))):
            with self.assertRaises(MediaTransportError):VerifiedPackets(p,b,index.timescale)
        with self.assertRaises(MediaTransportError):index.packets(seconds=901)
        with self.assertRaises(MediaTransportError):index.packets(seconds=1,limits=Limits(packet_bytes=2))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_real_aac_packets_pts_and_decode_match_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fixture.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=sample_rate=48000:duration=2',
                '-ac','2','-c:a','aac','-use_editlist','0',str(path)],check=True)
            data=bytearray(path.read_bytes())
            # Make a supported fixture from FFmpeg's output by removing its
            # preroll groups, keeping box sizes/offsets and all packet bytes.
            def strip_groups(start,end):
                for node in boxes(data,start,end):
                    if node.kind in (b'sgpd',b'sbgp'):
                        data[node.start+4:node.start+8]=b'free'
                    elif node.kind in (b'moov',b'trak',b'mdia',b'minf',b'stbl'):
                        strip_groups(node.payload,node.end)
            grouped=load_index(MemoryReader(data));self.assertTrue(grouped.requires_preroll)
            with self.assertRaises(MediaTransportError):grouped.packets(start=.5,seconds=.5)
            strip_groups(0,len(data));path.write_bytes(data)
            index=load_index(MemoryReader(data));packets=index.packets(seconds=index.duration/index.timescale)
            probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_packets','-show_streams',
                '-select_streams','a','-of','json',str(path)]))
            self.assertEqual(len(packets),len(probe['packets']))
            for p,row in zip(packets,probe['packets']):
                self.assertEqual((p.offset,p.size,p.dts,p.duration),(int(row['pos']),int(row['size']),int(row['pts']),int(row['duration'])))
            result=VerifiedPackets(packets,tuple(data[p.offset:p.offset+p.size] for p in packets),index.timescale)
            a=subprocess.run(['ffmpeg','-v','error','-i',str(path),'-ar','16000','-ac','1','-f','f32le','pipe:1'],capture_output=True,check=True)
            b=subprocess.run(['ffmpeg','-v','error','-f','aac','-i','pipe:0','-ar','16000','-ac','1','-f','f32le','pipe:1'],input=result.adts(index),capture_output=True,check=True)
            self.assertFalse(a.stderr+b.stderr)
            # Different end padding affects only the resampler's final filter taps.
            common=min(len(a.stdout),len(b.stdout))-128
            self.assertGreater(common,0);self.assertEqual(a.stdout[:common],b.stdout[:common])
            # Encoded final packet can include padding; decode duration is independent evidence.
            self.assertLess(abs(len(b.stdout)/64000-result.seconds),1024/48000+.002)


class MultipartTests(unittest.TestCase):
    def test_reordering_and_binary_boundary_lookalike(self):
        data=b'abc\r\n--B\r\ndefghijkl';ranges=[(0,12),(13,len(data)-1)]
        self.assertEqual(parse_multipart(multipart(data,ranges,'reorder'),'multipart/byteranges; boundary=B',ranges,len(data)),[data[:13],data[13:]])

    def test_corrupt_multipart_variants(self):
        data=b'0123456789';ranges=[(0,2),(7,9)]
        for mode in ('missing','duplicate','extra','total','boundary','truncate'):
            with self.subTest(mode=mode),self.assertRaises(MediaTransportError):parse_multipart(multipart(data,ranges,mode),'multipart/byteranges; boundary=B',ranges,len(data))
        good=multipart(data,ranges)
        self.assertEqual(parse_multipart(b'\r\n'+good+b'\r\n','multipart/byteranges; boundary=B',ranges,len(data)),[b'012',b'789'])
        for changed in (good.replace(b'bytes 0-2',b'bytes 0-3'),good.replace(b'Content-Type: video/mp4',b'Content-Encoding: gzip')):
            with self.assertRaises(MediaTransportError):parse_multipart(changed,'multipart/byteranges; boundary=B',ranges,len(data))

    def test_loopback_complete_packets_and_conditional_requests(self):
        data=fixture()
        with Origin(data,'reorder') as origin,AACRangeTransport(origin.client,origin.url) as reader:
            index=load_index(reader);packets=index.packets(seconds=index.duration/index.timescale)
            result=fetch_packets(reader,index,packets)
            self.assertEqual(result.payloads,(b'abc',b'DEFG',b'hijkl'))
            self.assertEqual(reader.statistics()['multipart_requests'],1)
            self.assertTrue(all(row[2]=='identity' for row in origin.rows))
            self.assertTrue(all(row[1]=='"stable"' for row in origin.rows[1:]))
            for secret in ('http','stable','media','ETag'):self.assertNotIn(secret,json.dumps(reader.statistics()))

    def test_real_http_bad_responses_never_commit_a_batch(self):
        for mode in ('missing','duplicate','extra','total','boundary','truncate','html','changed','gzip','416','ignored'):
            with self.subTest(mode=mode),Origin(b'0123456789',mode) as origin,AACRangeTransport(origin.client,origin.url,attempts=1) as reader:
                reader.read(0,1)
                with self.assertRaises(MediaTransportError):reader.fetch([(0,2),(7,9)])
                self.assertLessEqual(origin.calls,2)
                if mode in ('html','changed','gzip','416','ignored'):self.assertEqual(reader.statistics()['upstream_bytes'],1)

    def test_interrupted_batch_retries_as_unit_without_duplicate_packets(self):
        with Origin(b'0123456789','drop') as origin,AACRangeTransport(origin.client,origin.url) as reader:
            reader.read(0,1)
            result=reader.fetch([(0,2),(7,9)])
            self.assertEqual(result,[b'012',b'789']);self.assertEqual(reader.statistics()['retries'],1)
            self.assertEqual(origin.rows[1][0],origin.rows[2][0])
            self.assertGreater(reader.statistics()['retry_bytes'],0)
            self.assertEqual(reader.statistics()['signature_renewals'],3)

    def test_session_recovery_once_and_source_change_stops(self):
        with Origin(b'0123456789','auth') as origin,AACRangeTransport(origin.client,origin.url,allow_session_refresh=True) as reader:
            reader.read(0,1)
            self.assertEqual(reader.fetch([(0,2),(7,9)]),[b'012',b'789'])
            self.assertEqual(origin.refresh,1);self.assertEqual(reader.statistics()['session_refresh_successes'],1)

    def test_budgets_cancel_and_deadline(self):
        for limits in (Limits(network_bytes=2),Limits(source_bytes=9),Limits(requests=1),Limits(body_bytes=3),Limits(header_bytes=15)):
            with Origin(b'0123456789') as origin,AACRangeTransport(origin.client,origin.url,limits=limits) as reader:
                with self.assertRaises(MediaTransportError):
                    reader.read(0,1);reader.fetch([(0,2),(7,9)])
                self.assertLessEqual(reader.statistics()['upstream_bytes'],limits.network_bytes)
        with Origin(b'0123456789') as origin,AACRangeTransport(origin.client,origin.url) as reader:
            reader._stop.set()
            with self.assertRaises(MediaTransportError):reader.read(0,1)
        with Origin(b'0123456789') as origin,AACRangeTransport(origin.client,origin.url) as reader:
            reader.deadline=0
            with self.assertRaises(MediaTransportError) as e:reader.read(0,1)
            self.assertEqual(e.exception.code,'aac_deadline')


class AdditionalIntegrityTests(unittest.TestCase):
    def test_shortened_or_forged_window_fails_before_network(self):
        data=fixture();index=load_index(MemoryReader(data));window=index.packets(seconds=index.duration/index.timescale)
        with Origin(data) as origin,AACRangeTransport(origin.client,origin.url) as reader:
            for bad in (replace(window,items=window.items[:-1]),
                        replace(window,items=(replace(window[0],duration=1),*window.items[1:])),
                        replace(window,items=(window[0],window[0],window[2]))):
                with self.assertRaises(MediaTransportError):fetch_packets(reader,index,bad)
            self.assertEqual(origin.calls,0)

    def test_overlaps_outside_file_and_body_cap_fail_before_network(self):
        with Origin(b'0123456789') as origin,AACRangeTransport(origin.client,origin.url) as reader:
            reader.read(0,1);before=origin.calls
            for ranges in ([(0,2),(2,3)],[(8,10)],[(0,1)]*65):
                with self.assertRaises(MediaTransportError):reader.fetch(ranges)
            self.assertEqual(origin.calls,before)

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_decodable_missing_tail_is_still_rejected(self):
        # A prefix can decode normally, but cannot satisfy the frozen request plan.
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=sample_rate=48000:duration=1',
                '-ac','2','-c:a','aac','-use_editlist','0',str(path)],check=True)
            data=path.read_bytes();index=load_index(MemoryReader(data));window=index.packets(seconds=index.duration/index.timescale)
            shortened=VerifiedPackets(window[:-1],tuple(data[p.offset:p.offset+p.size] for p in window[:-1]),index.timescale)
            decode=subprocess.run(['ffmpeg','-v','error','-f','aac','-i','pipe:0','-f','null','-'],input=shortened.adts(index),capture_output=True)
            self.assertEqual(decode.returncode,0);self.assertFalse(decode.stderr)
            with Origin(data) as origin,AACRangeTransport(origin.client,origin.url) as reader:
                with self.assertRaises(MediaTransportError):fetch_packets(reader,index,replace(window,items=window.items[:-1]))
                self.assertEqual(origin.calls,0)

    def test_gap_count_corruption_and_expanded_memory_budget(self):
        data=bytearray(fixture());pos=data.find(b'stts')
        # First duration doubles; mdhd remains unchanged, so table consistency fails.
        struct.pack_into('>I',data,pos+16,2048)
        with self.assertRaises(MediaTransportError):load_index(MemoryReader(data))
        with self.assertRaises(MediaTransportError) as e:load_index(MemoryReader(fixture(),Limits(index_memory_bytes=1)))
        self.assertEqual(e.exception.code,'index_memory_limit')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_real_leading_empty_edit_reference_and_decode(self):
        from scripts.explore_aac_ranges import independent_probe
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=sample_rate=48000:duration=2',
                '-ac','2','-c:a','aac','-use_editlist','0',str(path)],check=True)
            data=path.read_bytes();native=load_index(MemoryReader(data));output=bytearray()
            movie=next(b for b in boxes(data) if b.kind==b'moov')
            mvhd=next(b for b in boxes(data,movie.payload,movie.end) if b.kind==b'mvhd')
            movie_scale=struct.unpack_from('>I',data,mvhd.payload+12)[0]
            for node in boxes(data):
                if node.kind!=b'moov':output.extend(data[node.start:node.end]);continue
                payload=bytearray()
                for child in boxes(data,node.payload,node.end):
                    raw=data[child.start:child.end]
                    if child.kind==b'trak':
                        # Use the actual movie timescale for a 21ms empty edit.
                        edit=full(b'elst',struct.pack('>I',2)+struct.pack('>Iihh',round(movie_scale*.021),-1,1,0)
                            +struct.pack('>Iihh',round(native.duration*movie_scale/native.timescale),0,1,0))
                        raw=box(b'trak',data[child.payload:child.end]+box(b'edts',edit))
                    payload.extend(raw)
                output.extend(box(b'moov',payload))
            data=bytes(output);reader=MemoryReader(data);index=load_index(reader)
            self.assertEqual(index.presentation_offset,1008)
            window=index.packets(seconds=1)
            verified=VerifiedPackets(window,tuple(data[p.offset:p.offset+p.size] for p in window),index.timescale)
            evidence=independent_probe(reader,index,verified)
            self.assertTrue(evidence['native_pts_dts_duration_equal'])
            self.assertTrue(evidence['pcm_prefix_equal_excluding_last_32_samples'])
            self.assertFalse(evidence['timeline_preserved'])


class RecoveryBudgetTests(unittest.TestCase):
    def test_persistent_http_retries_are_finite(self):
        with Origin(b'0123456789','429') as origin,AACRangeTransport(origin.client,origin.url) as reader:
            reader.read(0,1)
            with self.assertRaises(MediaTransportError) as e:reader.fetch([(0,2),(7,9)])
            self.assertEqual(e.exception.code,'upstream_retries_exhausted')
            self.assertEqual(origin.calls,4);self.assertEqual(reader.statistics()['upstream_bytes'],1)

    def test_authentication_only_once_and_new_source_is_refused(self):
        for mode,code in [('auth_persistent','media_session_unavailable'),('auth_change','source_changed')]:
            with Origin(b'0123456789',mode) as origin,AACRangeTransport(origin.client,origin.url,allow_session_refresh=True) as reader:
                reader.read(0,1)
                with self.assertRaises(MediaTransportError) as e:reader.fetch([(0,2),(7,9)])
                self.assertEqual(e.exception.code,code);self.assertEqual(origin.refresh,1)
                self.assertEqual(reader.statistics()['session_refresh_successes'],0)
                self.assertEqual(reader.statistics()['upstream_bytes'],1)

    def test_drip_body_cannot_extend_total_deadline(self):
        with Origin(b'0123456789','drip') as origin,AACRangeTransport(origin.client,origin.url) as reader:
            reader.read(0,1);reader.deadline=time.monotonic()+.08;began=time.monotonic()
            with self.assertRaises(MediaTransportError) as e:reader.fetch([(0,2),(7,9)])
            self.assertEqual(e.exception.code,'aac_deadline')
            self.assertLess(time.monotonic()-began,.5)
            self.assertEqual(reader.statistics()['multipart_body_bytes'],0)
