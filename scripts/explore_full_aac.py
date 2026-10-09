#!/usr/bin/env python3
"""Explicit campus full-AAC experiment; owned temporary media, no ASR or publication."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.explore_aac_ranges import Quiet
from src.api.auth_recovery import authenticated_session,DeadlineSession
from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession,authentication_failure
from src.runtime import config
from src.runtime.aac_ranges import (AACRangeTransport,Limits,MediaTransportError,
                                   iter_track_packets,load_index,stream_full_track)


def adts_frame(index,payload):
    size=len(payload)+7;ch=index.channels;freq=index.frequency_index
    return bytes((255,241,64|(freq<<2)|(ch>>2),((ch&3)<<6)|(size>>11),
                  (size>>3)&255,((size&7)<<5)|31,252))+payload


def write_original_metadata(reader,index,output):
    output.truncate(reader.total)
    for node in index.top_boxes:
        output.seek(node.start)
        header=reader.read(node.start,node.payload-node.start)
        if node.kind not in (b'moov',b'mdat',b'ftyp'):header=header[:4]+b'free'+header[8:]
        output.write(header)
        if node.kind==b'moov':
            output.write(index.moov_bytes)
            remainder=node.end-node.payload-len(index.moov_bytes)
            if remainder:
                if remainder<8:raise MediaTransportError('reference_layout_unsupported')
                output.write(struct.pack('>I4s',remainder,b'free'))
        elif node.kind==b'ftyp':output.write(reader.read(node.payload,node.end-node.payload))


def checked_process(command,reader,error_path):
    """Caller must close/kill on every path; stderr stays private and bounded."""
    reader.check()
    error_file=error_path.open('wb');os.chmod(error_path,0o600)
    try:process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=error_file)
    except BaseException:error_file.close();raise
    return process,error_file


def finish_process(process,error_file,error_path,*,failed=False):
    if process.poll() is None and failed:process.kill()
    try:code=process.wait(timeout=5)
    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5);raise MediaTransportError('decoder_deadline')
    finally:
        process.stdout.close();error_file.close()
    if not failed and (code!=0 or error_path.stat().st_size):raise MediaTransportError('reference_decode_failed')


def probe_all_packets(path,hash_path,index,reader,error_path):
    began=time.monotonic();expected=iter(iter_track_packets(index));verified=0
    process,errors=checked_process(['ffprobe','-v','error','-select_streams','a:0','-show_packets',
        '-show_entries','packet=pts,dts,duration,pos,size,data_hash','-show_data_hash','sha256',
        '-of','compact=p=0',str(path)],reader,error_path)
    failed=True
    try:
        with hash_path.open('rb') as hashes:
            for line in process.stdout:
                reader.check()
                if verified%1024==0 and error_path.stat().st_size>1024*1024:raise MediaTransportError('stderr_output_limit')
                if len(line)>8192:raise MediaTransportError('reference_output_limit')
                row=dict(field.split('=',1) for field in line.decode('ascii').strip().split('|') if '=' in field)
                if 'pos' not in row:continue
                packet=next(expected,None)
                if packet is None:raise MediaTransportError('reference_extra_packet')
                actual=tuple(int(row[k]) for k in ('pos','size','pts','dts','duration'))
                if actual!=(packet.offset,packet.size,packet.pts,packet.dts,packet.duration):
                    raise MediaTransportError('reference_timeline_mismatch')
                if bytes.fromhex(row['data_hash'].split(':',1)[1])!=hashes.read(32):
                    raise MediaTransportError('reference_packet_mismatch')
                verified+=1
            if next(expected,None) is not None or hashes.read(1):raise MediaTransportError('reference_missing_packet')
        failed=False
    finally:finish_process(process,errors,error_path,failed=failed)
    return dict(reference_packets=verified,all_packet_hashes_equal=True,
                all_native_pts_dts_durations_equal=True,probe_seconds=round(time.monotonic()-began,3))


def decode_digest(path,index,reader,error_path,*,adts=False):
    began=time.monotonic()
    command=['ffmpeg','-v','error']+(['-f','aac'] if adts else ['-copyts'])+['-i',str(path)]
    filter_text=('asetpts=PTS+'+f'{index.presentation_offset/index.timescale:.12f}'+'/TB,' if adts else '')+'aresample=async=1:first_pts=0'
    command+=['-vn','-af',filter_text,'-ar','16000','-ac','1','-f','f32le','pipe:1']
    process,errors=checked_process(command,reader,error_path);failed=True;size=0;digest=hashlib.sha256()
    try:
        while True:
            reader.check();block=process.stdout.read1(65536)
            if not block:break
            size+=len(block)
            if error_path.stat().st_size>1024*1024:raise MediaTransportError('stderr_output_limit')
            if size>1_200_000_000:raise MediaTransportError('pcm_output_limit')
            digest.update(block)
        if not size or size%4:raise MediaTransportError('invalid_pcm_output')
        failed=False
    finally:finish_process(process,errors,error_path,failed=failed)
    return dict(seconds=size/64000,pcm_bytes=size,sha256=digest.hexdigest(),
                decode_seconds=round(time.monotonic()-began,3),decode_return_code=0,decode_stderr_empty=True)


def run_full(reader,index,directory,progress):
    if shutil.disk_usage(directory).free<1_000_000_000:raise MediaTransportError('temporary_disk_budget')
    sparse=directory/'complete-audio-layout.mp4';adts=directory/'complete.aac';hash_path=directory/'packets.sha256'
    with sparse.open('w+b') as original,adts.open('wb') as audio,hash_path.open('wb') as hashes:
        for path in (sparse,adts,hash_path):os.chmod(path,0o600)
        write_original_metadata(reader,index,original)
        def commit(packet,payload):
            original.seek(packet.offset);original.write(payload)
            audio.write(adts_frame(index,payload));hashes.write(hashlib.sha256(payload).digest())
        evidence=stream_full_track(reader,index,commit,progress=progress)
        original.flush();audio.flush();hashes.flush()
    progress({'phase':'full_payload_verified',**evidence},force=True)
    evidence.update(probe_all_packets(sparse,hash_path,index,reader,directory/'probe.err'))
    progress({'phase':'full_packet_reference_verified','reference_packets':evidence['reference_packets']},force=True)
    mp4=decode_digest(sparse,index,reader,directory/'mp4.err')
    aac=decode_digest(adts,index,reader,directory/'adts.err',adts=True)
    expected=evidence['presentation_end_seconds'];tolerance=1024/index.sample_rate+.003
    if max(abs(mp4['seconds']-expected),abs(aac['seconds']-expected))>tolerance:
        raise MediaTransportError('full_decode_duration_mismatch')
    # Digests exclude media/voice content; the public report only records equality.
    equal=mp4.pop('sha256')==aac.pop('sha256')
    evidence.update(mp4_decode=mp4,adts_decode=aac,full_pcm_hash_equal=equal,
                    pcm_end_difference_seconds=round(aac['seconds']-mp4['seconds'],8),
                    timeline_alignment_complete=equal,timeline_preserved=False)
    return evidence


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('course','lecture','credentials','credential-loader','report'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args();began=time.monotonic();stop=threading.Event();output=sys.stdout
    def deadline(*unused):raise MediaTransportError('experiment_deadline')
    signal.signal(signal.SIGALRM,deadline);signal.alarm(1800)
    report=dict(schema=1,access_mode='campus_direct',success=False,media_retained=False,
        budgets=dict(total_seconds=1800,media_body_bytes=400000000,range_requests=10000,audio_payload_bytes=200000000),
        baseline_full_mp4_downloaded=False,progress={},full_track=None)
    reader=None;vpn=None;phase='authentication';last_progress=0
    def progress(row,force=False):
        nonlocal last_progress
        report['progress']=row
        if row.get('phase')=='full_payload_verified':report['fetch_evidence']=dict(row)
        if row.get('phase')=='full_packet_reference_verified':report['reference_evidence']=dict(row)
        now=time.monotonic()
        if force or now-last_progress>=30:
            last_progress=now
            print(json.dumps({'elapsed_seconds':round(now-began,2),**row},ensure_ascii=False),file=output,flush=True)
    try:
        sys.path.insert(0,args.credential_loader)
        from local_credentials import load_credentials
        student,password=load_credentials(Path(args.credentials))
        def login(cancelled,end):
            def factory():
                value=WebVPNSession(access_mode='direct');value.session.close();value.session=DeadlineSession(cancelled,end)
                value.session.headers.update({'User-Agent':config.USER_AGENT});return value
            return authenticated_session(max_attempts=1,student_id=student,password=password,factory=factory)
        with contextlib.redirect_stdout(Quiet()),contextlib.redirect_stderr(Quiet()):
            vpn=login(stop,began+90);vpn.session.deadline=began+1800
            client=ICourseClient(vpn,media_reauth_factory=lambda cancelled,end:login(cancelled,min(end,began+1800)))
            client.get_userinfo();phase='metadata'
            with vpn.get(config.ICOURSE_BASE+'/courseapi/v3/portal-home-setting/get-sub-info',
                params={'course_id':args.course,'sub_id':args.lecture},timeout=(5,8),stream=True,allow_redirects=False) as response:
                if response.status_code!=200:raise MediaTransportError('metadata_rejected')
                data=response.raw.read(100001,decode_content=False)
                if len(data)>100000:raise MediaTransportError('metadata_limit')
                payload=json.loads(data)
                if payload.get('code')!=0:raise MediaTransportError('metadata_rejected')
                signed=client.sign_video_url(payload['data']['content']['playback']['url'])
            reader=AACRangeTransport(client,signed,limits=Limits(seconds=max(1,began+1800-time.monotonic()),
                network_bytes=400000000,requests=10000),timeout=(5,8),allow_session_refresh=True)
            phase='index';index=load_index(reader)
            report['index']=dict(samples=len(index.sizes),audio_payload_bytes=sum(index.sizes),
                native_seconds=index.duration/index.timescale,presentation_offset_seconds=index.presentation_offset/index.timescale)
            progress({'phase':'full_track_fetch_start',**report['index']},force=True)
            phase='full_track'
            with tempfile.TemporaryDirectory(prefix='aac-full-exploration-') as tmp:
                directory=Path(tmp);os.chmod(directory,0o700)
                report['full_track']=run_full(reader,index,directory,progress)
            report['success']=True
    except Exception as error:
        report.update(failure_phase=phase,error_code=error.code if isinstance(error,MediaTransportError) else
            authentication_failure(error)['failure'] if phase=='authentication' else 'full_experiment_failed')
    finally:
        if reader is not None:report['transport']=reader.statistics();reader.close()
        if vpn is not None:vpn.session.close()
        signal.alarm(0);report['wall_seconds']=round(time.monotonic()-began,3);report['temporary_media_removed']=True
        path=Path(args.report);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False),file=output,flush=True)
    return 0 if report['success'] else 1


if __name__=='__main__':raise SystemExit(main())
