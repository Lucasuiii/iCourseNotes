#!/usr/bin/env python3
"""Explicit local campus benchmark. No ASR, mail, data writes or auto-dispatch.

Private selectors are CLI inputs, never output. All media lives in a temporary
private directory or memory; only whitelisted anonymous metrics are saved.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import io
import json
import os
import signal
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.api.auth_recovery import authenticated_session, DeadlineSession
from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession, authentication_failure
from src.runtime import config
from src.runtime.aac_ranges import (AACRangeTransport, Limits, VerifiedPackets,
    boxes, load_index, fetch_packets, MediaTransportError, full_payload)


class Quiet(io.TextIOBase):
    def write(self,value):return len(value)


def snapshot(reader):return reader.statistics()


def delta(before,after):
    keys=('range_requests','upstream_bytes','multipart_requests','multipart_body_bytes','retry_bytes')
    return {k:after[k]-before[k] for k in keys}


def continuous(reader,index,packets):
    # Exactly the legacy experiment baseline: contiguous audio/video byte span.
    # Not a measurement of whole-lecture AudioDownloader wall time.
    start=packets[0].offset;end=packets[-1].offset+packets[-1].size
    payloads=[];partial=bytearray();cursor=0
    for a in range(start,end,reader.limits.body_bytes):
        data=reader.read(a,min(reader.limits.body_bytes,end-a));block_end=a+len(data)
        while cursor<len(packets) and packets[cursor].offset<block_end:
            p=packets[cursor];left=max(a,p.offset);right=min(block_end,p.offset+p.size)
            partial.extend(data[left-a:right-a])
            if len(partial)<p.size:break
            if len(partial)!=p.size:raise MediaTransportError('baseline_packet_mismatch')
            payloads.append(bytes(partial));partial.clear();cursor+=1
    return VerifiedPackets(packets,tuple(payloads),index.timescale)


def prefix_movie(data,verified):
    """Decode fixture only: trim sample tables after independently probing them.

    This stops demuxing at the verified prefix instead of reading unfilled sparse
    tail packets. Original packet offsets, durations, ASC and edit list remain.
    """
    packets=verified.packets;n=len(packets);timing=[]
    for p in packets:
        if timing and timing[-1][1]==p.duration:timing[-1]=(timing[-1][0]+1,p.duration)
        else:timing.append((1,p.duration))
    def make(kind,payload):return struct.pack('>I4s',len(payload)+8,kind)+payload
    def full(kind,payload):return make(kind,b'\0'*4+payload)
    def visit(start,end):
        output=bytearray()
        for node in boxes(data,start,end):
            kind=node.kind;value=bytes(data[node.payload:node.end])
            if kind in (b'trak',b'mdia',b'minf',b'stbl'):
                output.extend(make(kind,visit(node.payload,node.end)));continue
            if kind==b'stts':value=b'\0'*4+struct.pack('>I',len(timing))+b''.join(struct.pack('>II',*v) for v in timing)
            elif kind in (b'stsz',b'stz2'):
                kind=b'stsz';value=b'\0'*4+struct.pack('>II',0,n)+b''.join(struct.pack('>I',p.size) for p in packets)
            elif kind in (b'stco',b'co64'):
                kind=b'co64';value=b'\0'*4+struct.pack('>I',n)+b''.join(struct.pack('>Q',p.offset) for p in packets)
            elif kind==b'stsc':value=b'\0'*4+struct.pack('>IIII',1,1,1,1)
            elif kind==b'mdhd':
                value=bytearray(value);duration=sum(p.duration for p in packets)
                struct.pack_into('>Q' if value[0]==1 else '>I',value,24 if value[0]==1 else 16,duration)
            elif kind==b'sbgp':
                original=full_payload(data,node);remaining=n;groups=[]
                for count,group in struct.iter_unpack('>II',original[8:]):
                    amount=min(count,remaining)
                    if amount:groups.append((amount,group));remaining-=amount
                    if not remaining:break
                value=b'\0'*4+bytes(original[:4])+struct.pack('>I',len(groups))+b''.join(struct.pack('>II',*v) for v in groups)
            output.extend(make(kind,value))
        return bytes(output)
    return visit(0,len(data))


def independent_probe(reader,index,verified):
    """Sparse original-layout MP4 with selected AAC and original metadata.

    Only metadata and selected AAC have storage allocated. Video is absent and
    its trak disabled; no decoding of zero-filled video is attempted. FFprobe
    reads the original audio tables independently, including PTS and duration.
    This is a prefix evidence fixture, never a complete-lecture download.
    """
    began=time.monotonic()
    def command_timeout():
        remaining=getattr(reader,'deadline',float('inf'))-time.monotonic()
        if remaining<=0:raise MediaTransportError('aac_deadline')
        return min(90,remaining)
    end_seconds=(verified.packets[-1].dts+verified.packets[-1].duration)/index.timescale
    with tempfile.TemporaryDirectory(prefix='aac-exploration-') as tmp:
        path=Path(tmp)/'prefix-evidence.mp4'
        raw=bytearray(index.moov_bytes)
        for track in boxes(raw):
            if track.kind!=b'trak':continue
            mdia=next(b for b in boxes(raw,track.payload,track.end) if b.kind==b'mdia')
            hdlr=next(b for b in boxes(raw,mdia.payload,mdia.end) if b.kind==b'hdlr')
            if raw[hdlr.payload+8:hdlr.payload+12]!=b'soun':raw[track.start+4:track.start+8]=b'free'
        with path.open('wb') as out:
            os.chmod(path,0o600);out.truncate(reader.total)
            for node in index.top_boxes:
                out.seek(node.start)
                # Retain exact original top-level lengths, including extended mdat.
                header=reader.read(node.start,node.payload-node.start)
                if node.kind not in (b'moov',b'mdat',b'ftyp'):
                    header=header[:4]+b'free'+header[8:]
                out.write(header)
                if node.kind==b'moov':
                    out.write(raw)
                    remainder=node.end-node.payload-len(raw)
                    if remainder:
                        if remainder<8:raise MediaTransportError('reference_layout_unsupported')
                        out.write(struct.pack('>I4s',remainder,b'free'))
                if node.kind==b'ftyp':out.write(reader.read(node.payload,node.end-node.payload))
            for p,payload in zip(verified.packets,verified.payloads):out.seek(p.offset);out.write(payload)
        packet_result=subprocess.run(['ffprobe','-v','error','-read_intervals',f'0%{end_seconds-.25/index.timescale:.9f}',
            '-select_streams','a:0','-show_packets','-show_entries','packet=pts,dts,duration,pos,size,data_hash',
            '-show_data_hash','sha256','-of','json',str(path)],capture_output=True,timeout=command_timeout())
        if packet_result.returncode or packet_result.stderr:raise MediaTransportError('reference_probe_failed')
        rows=json.loads(packet_result.stdout)['packets']
        if len(rows)!=len(verified.packets):raise MediaTransportError('reference_packet_count_mismatch')
        for p,payload,row in zip(verified.packets,verified.payloads,rows):
            actual=tuple(int(row[k]) for k in ('pos','size','pts','dts','duration'))
            if actual!=(p.offset,p.size,p.pts,p.dts,p.duration):raise MediaTransportError('reference_timeline_mismatch')
            if row['data_hash'].split(':')[1].lower()!=hashlib.sha256(payload).hexdigest():raise MediaTransportError('reference_packet_mismatch')
        # Decode a bounded prefix fixture after the original tables passed the
        # independent packet/PTS probe. No fabricated tail audio is decoded.
        compact=prefix_movie(raw,verified)
        moov=next(b for b in index.top_boxes if b.kind==b'moov')
        with path.open('r+b') as out:
            remainder=moov.end-moov.payload-len(compact)
            if remainder>=8:
                out.seek(moov.payload);out.write(compact)
                out.write(struct.pack('>I4s',remainder,b'free'))
            else:
                out.seek(moov.start+4);out.write(b'free')
                out.seek(0,2);out.write(struct.pack('>I4s',len(compact)+8,b'moov'));out.write(compact)
        # Decode same selected packets with retained MP4 edit timeline and ADTS.
        command=['ffmpeg','-v','error','-copyts','-i',str(path),'-t',f'{end_seconds:.9f}',
            '-af','aresample=async=1:first_pts=0','-ar','16000','-ac','1','-f','f32le','pipe:1']
        original=subprocess.run(command,capture_output=True,timeout=command_timeout())
        adts=subprocess.run(['ffmpeg','-v','error','-f','aac','-i','pipe:0','-af',f'asetpts=PTS+{index.presentation_offset/index.timescale:.12f}/TB,aresample=async=1:first_pts=0','-ar','16000','-ac','1',
            '-f','f32le','pipe:1'],input=verified.adts(index),capture_output=True,timeout=command_timeout())
        if original.returncode or original.stderr or adts.returncode or adts.stderr:raise MediaTransportError('decode_failed')
        common=min(len(original.stdout),len(adts.stdout))-128
        if not common or original.stdout[:common]!=adts.stdout[:common]:raise MediaTransportError('decode_pcm_mismatch')
        old_seconds=len(original.stdout)/64000;new_seconds=len(adts.stdout)/64000
        if max(abs(old_seconds-end_seconds),abs(new_seconds-end_seconds))>1024/index.sample_rate+.003:
            raise MediaTransportError('decode_duration_mismatch')
        return dict(reference_packets=len(rows),packet_hashes_equal=True,native_pts_dts_duration_equal=True,
            original_pcm_seconds=old_seconds,adts_pcm_seconds=new_seconds,pcm_prefix_equal_excluding_last_32_samples=True,
            pcm_end_difference_seconds=round(new_seconds-old_seconds,8),
            pcm_end_match=abs(new_seconds-old_seconds)<=1/16000,
            decode_seconds=round(time.monotonic()-began,4),temporary_media_removed=True,
            timeline_preserved=False,timeline_alignment_complete=False,reference_decode_container='verified_prefix_tables_with_original_edit_list')


def benchmark(reader,index,seconds):
    packets=index.packets(seconds=seconds,limits=reader.limits)
    if not packets or packets[-1].dts+packets[-1].duration<seconds*index.timescale:
        raise MediaTransportError('requested_window_incomplete')
    rows=[];reference=None
    for mode in ('multipart','continuous','continuous','multipart'):
        before=snapshot(reader);began=time.monotonic()
        verified=fetch_packets(reader,index,packets) if mode=='multipart' else continuous(reader,index,packets)
        elapsed=time.monotonic()-began
        digest=hashlib.sha256(b''.join(verified.payloads)).digest()
        if reference is None:reference=digest
        if digest!=reference:raise MediaTransportError('packet_equivalence_failed')
        rows.append(dict(mode=mode,fetch_seconds=round(elapsed,4),**delta(before,snapshot(reader))))
    try:evidence=independent_probe(reader,index,verified)
    except MediaTransportError as error:
        error.window_statistics=dict(requested_seconds=seconds,expected_samples=len(packets),packet_equivalence=True,runs=rows)
        raise
    return dict(requested_seconds=seconds,expected_samples=len(packets),verified_samples=len(verified.payloads),
        native_timeline_seconds=verified.seconds,presentation_start_seconds=packets[0].dts/index.timescale,
        presentation_end_seconds=(packets[-1].dts+packets[-1].duration)/index.timescale,audio_payload_bytes=sum(p.size for p in packets),
        packet_equivalence=True,runs=rows,**evidence)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course',required=True)
    parser.add_argument('--lecture',required=True)
    parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--credential-loader',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--network-byte-budget',type=int,default=700000000)
    parser.add_argument('--maximum-seconds',type=int,choices=(60,900),default=60)
    args=parser.parse_args()
    def total_deadline(signum,frame):raise MediaTransportError('experiment_deadline')
    signal.signal(signal.SIGALRM,total_deadline);signal.alarm(600)
    report=dict(schema=1,access_mode='campus_direct',concurrent_cloud_run=True,
                media_retained=False,windows=[],success=False,
                baseline='contiguous_AV_span_not_full_AudioDownloader',
                budgets=dict(network_bytes=args.network_byte_budget,range_requests=2000,total_seconds=600,
                             authentication_seconds=90,window_seconds=args.maximum_seconds))
    vpn=None;reader=None;began=time.monotonic();stop=threading.Event();phase='authentication'
    progress_output=sys.stdout
    try:
        sys.path.insert(0,str(args.credential_loader))
        from local_credentials import load_credentials
        student,password=load_credentials(args.credentials)
        def login(cancelled,deadline):
            def factory():
                value=WebVPNSession(access_mode='direct');value.session.close()
                value.session=DeadlineSession(cancelled,deadline)
                value.session.headers.update({'User-Agent':config.USER_AGENT});return value
            return authenticated_session(max_attempts=1,student_id=student,password=password,
                                         factory=factory,probe_attempts=1)
        with contextlib.redirect_stdout(Quiet()),contextlib.redirect_stderr(Quiet()):
            vpn=login(stop,began+90)
            # Request deadlines remain bounded throughout metadata retrieval.
            vpn.session.deadline=began+600
            client=ICourseClient(vpn,media_reauth_factory=lambda stopped,deadline:login(stopped,min(deadline,began+600)))
            client.get_userinfo()
            phase='metadata'
            with vpn.get(config.ICOURSE_BASE+'/courseapi/v3/portal-home-setting/get-sub-info',
                    params={'course_id':args.course,'sub_id':args.lecture},timeout=(5,8),stream=True,allow_redirects=False) as response:
                if response.status_code!=200:raise MediaTransportError('metadata_rejected')
                data=response.raw.read(100001,decode_content=False)
                if len(data)>100000:raise MediaTransportError('metadata_limit')
                payload=json.loads(data)
                if payload.get('code')!=0:raise MediaTransportError('metadata_rejected')
                signed=client.sign_video_url(payload['data']['content']['playback']['url'])
            reader=AACRangeTransport(client,signed,limits=Limits(seconds=max(1,began+600-time.monotonic()),network_bytes=args.network_byte_budget),
                                     timeout=(5,8),allow_session_refresh=True)
            phase='index';index=load_index(reader)
            report['index']=dict(bytes=reader.statistics()['index_bytes'],seconds=round(reader.statistics()['index_seconds'],4),
                                 samples=len(index.sizes),native_duration_seconds=index.duration/index.timescale,
                                 sample_rate=index.sample_rate,channels=index.channels,identity_edit=index.identity_edit,presentation_offset_seconds=index.presentation_offset/index.timescale)
            for seconds in (60,900) if args.maximum_seconds==900 else (60,):
                phase=f'window_{seconds}'
                report['windows'].append(benchmark(reader,index,seconds))
                print(json.dumps({'phase':phase,'verified':True,'anonymous_summary':report['windows'][-1]},ensure_ascii=False),file=progress_output,flush=True)
            report['success']=True
    except Exception as error:
        if hasattr(error,'window_statistics'):report['failed_window']=error.window_statistics
        report['failure_phase']=phase
        report['error_code']=error.code if isinstance(error,MediaTransportError) else (
            authentication_failure(error).get('failure','authentication_failed') if phase=='authentication' else 'experiment_failed')
    finally:
        if reader is not None:
            report['transport']=reader.statistics();reader.close()
        if vpn is not None:vpn.session.close()
        signal.alarm(0)
        report['wall_seconds']=round(time.monotonic()-began,4)
        # Whitelisted report contains no provider exceptions, URLs or selectors.
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['success'] else 1


if __name__=='__main__':raise SystemExit(main())
