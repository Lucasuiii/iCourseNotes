"""Opt-in, bounded non-fragmented MP4/AAC exploration. No scheduler integration.

Only complete validated batches are returned. Packet timestamps are native track
 ticks; ADTS is a decode probe, never evidence of preserved presentation timing.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from email.message import Message
import math
import re
import struct
import time
from types import SimpleNamespace
from requests.structures import CaseInsensitiveDict

import requests
from urllib3.exceptions import HTTPError

from src.runtime.media_protocol import (MediaTransportError, RecoveryAction,
                                       connection_failure_code)
from src.runtime.media_transport import SignedRangeRelay


def fail(code):
    raise MediaTransportError(code)


@dataclass(frozen=True)
class Limits:
    seconds: float = 300
    network_bytes: int = 700_000_000
    requests: int = 2000
    batch_ranges: int = 64
    header_bytes: int = 8192
    body_bytes: int = 16 * 1024 * 1024
    index_bytes: int = 16 * 1024 * 1024
    samples: int = 1_000_000
    window_seconds: float = 900
    packet_bytes: int = 32 * 1024 * 1024
    index_memory_bytes: int = 128 * 1024 * 1024
    source_bytes: int = 64 * 1024 * 1024 * 1024

    def __post_init__(self):
        caps = {'network_bytes': 1_000_000_000, 'requests': 5000,
                'batch_ranges': 64, 'header_bytes': 16384,
                'body_bytes': 32*1024*1024, 'index_bytes': 32*1024*1024,
                'samples': 1_000_000, 'packet_bytes': 64*1024*1024, 'index_memory_bytes': 256*1024*1024, 'source_bytes': 64*1024*1024*1024}
        for name, cap in caps.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError('invalid_aac_limits')
        for name, cap in [('seconds', 1800), ('window_seconds', 900)]:
            v = getattr(self, name)
            if type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= cap:
                raise ValueError('invalid_aac_limits')


def multipart_boundary(content_type):
    message = Message()
    message['content-type'] = content_type
    if message.get_content_type().lower() != 'multipart/byteranges':
        fail('multipart_unsupported')
    boundary = message.get_param('boundary')
    if (not isinstance(boundary, str) or not 1 <= len(boundary) <= 70
            or not re.fullmatch(r"[0-9A-Za-z'()+_,./:=? -]+", boundary)
            or boundary.endswith(' ')):
        fail('invalid_multipart_boundary')
    return boundary.encode('ascii')


def parse_multipart(body, content_type, ranges, total):
    """Length-directed binary parser: boundary-looking packet bytes stay data."""
    boundary = b'--' + multipart_boundary(content_type)
    wanted = set(ranges)
    if len(wanted) != len(ranges): fail('duplicate_requested_range')
    result = {}
    cursor = body.find(boundary,0,1024+len(boundary))
    if cursor<0 or body[:cursor].strip(b' \t\r\n'):fail('invalid_multipart_preamble')
    for _ in range(len(ranges)):
        if body[cursor:cursor+len(boundary)+2] != boundary+b'\r\n':
            fail('invalid_multipart_boundary')
        cursor += len(boundary)+2
        end = body.find(b'\r\n\r\n', cursor, cursor+4096)
        if end < 0: fail('invalid_part_headers')
        headers = {}
        for row in body[cursor:end].split(b'\r\n'):
            key, sep, value = row.partition(b':')
            if not sep or not re.fullmatch(rb'[A-Za-z0-9-]+', key): fail('invalid_part_headers')
            name = key.decode('ascii').lower()
            if name in headers: fail('invalid_part_headers')
            headers[name] = value.strip()
        match = re.fullmatch(rb'bytes (\d+)-(\d+)/(\d+)', headers.get('content-range', b''))
        if not match: fail('invalid_content_range')
        first, last, source_total = map(int, match.groups())
        if source_total != total: fail('source_changed')
        key = (first, last)
        if key not in wanted or key in result: fail('unexpected_or_duplicate_part')
        if headers.get('content-encoding', b'identity').lower() not in (b'', b'identity'):
            fail('encoded_range')
        if 'content-type' in headers and headers['content-type'].lower() not in (
                b'application/octet-stream', b'video/mp4', b'audio/mp4'):
            fail('invalid_part_type')
        size = last-first+1
        if 'content-length' in headers and headers['content-length'] != str(size).encode():
            fail('invalid_content_length')
        cursor = end+4
        payload = body[cursor:cursor+size]
        if len(payload) != size: fail('upstream_premature_eof')
        cursor += size
        if body[cursor:cursor+2] != b'\r\n': fail('invalid_part_length')
        cursor += 2
        result[key] = payload
    ending=body[cursor:]
    if not ending.startswith(boundary+b'--') or len(ending)>len(boundary)+2+1024 or ending[len(boundary)+2:].strip(b' \t\r\n'):
        fail('missing_or_extra_part')
    return [result[key] for key in ranges]


class AACRangeTransport(SignedRangeRelay):
    """Reuse relay source/signature/session recovery; no loopback server needed.

    Each failed multipart read discards the entire uncommitted batch. A lifetime
    watchdog cancels the same stop event used by signing and authentication.
    """
    def __init__(self, client, signed_url, *, limits=Limits(), **kwargs):
        super().__init__(client, signed_url, max_upstream_bytes=limits.network_bytes, **kwargs)
        self.limits = limits
        self.format_observations={}
        self.deadline = time.monotonic()+limits.seconds
        self._audit.update(multipart_requests=0, multipart_body_bytes=0,
                           retry_bytes=0, index_bytes=0, index_seconds=0.0,
                           range_count=0)
        import threading
        self._deadline_timer = threading.Timer(limits.seconds, self._stop.set)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def __enter__(self): return self

    def close(self):
        self._deadline_timer.cancel()
        super().close()

    def check(self):
        if time.monotonic() >= self.deadline: fail('aac_deadline')
        if self._stop.is_set(): fail('stopped')
        if self.audit()['terminal_error_code']: fail('transport_failed')

    def read(self, start, size, *, index=False):
        data = self.fetch([(start, start+size-1)])[0]
        if index: self._count('index_bytes', len(data))
        return data

    def fetch(self, ranges):
        with self._fetch_lock:
            self.check()
            if not 1 <= len(ranges) <= self.limits.batch_ranges: fail('range_count_limit')
            previous = -1
            for first, last in ranges:
                if (type(first) is not int or type(last) is not int or first < 0
                        or last < first or first <= previous
                        or self.total is not None and last >= self.total):
                    fail('invalid_requested_range')
                previous = last
            value = 'bytes='+','.join(f'{a}-{b}' for a,b in ranges)
            if len(value) > self.limits.header_bytes: fail('range_header_limit')
            payload_size = sum(b-a+1 for a,b in ranges)
            # At most 4KiB part headers, 70-byte boundary and delimiters per part.
            cap = payload_size + (len(ranges)*4200+2128 if len(ranges)>1 else 0)
            if cap > self.limits.body_bytes: fail('response_body_limit')
            for attempt in range(self._recovery.attempts):
                response = None
                before = self.audit()['upstream_bytes']
                try:
                    self.check()
                    target, headers = self._signed_request(ranges[0][0], attempt>0)
                    self.check()
                    session, headers = self._request_session(headers)
                    # Both auth headers and Range count toward the request cap.
                    headers = {**headers, 'Range': value}
                    if (sum(len(k)+len(v)+4 for k,v in headers.items())
                            +sum(len(k)+len(v)+4 for k,v in session.headers.items())
                            +sum(len(c.name)+len(c.value)+2 for c in session.cookies)) > self.limits.header_bytes:
                        fail('range_header_limit')
                    if self.audit()['range_requests'] >= self.limits.requests: fail('request_limit')
                    self._count('range_requests')
                    self._count('range_count', len(ranges))
                    if len(ranges)>1: self._count('multipart_requests')
                    if self._resume_pending: self._count('session_resume_attempts')
                    remaining = max(.001, self.deadline-time.monotonic())
                    timeout = tuple(min(t, remaining) for t in self.timeout)
                    response = session.get(target, headers=headers, stream=True,
                                           timeout=timeout, allow_redirects=False)
                    with self._response_lock: self._responses.add(response)
                    self.check()
                    if len(ranges)==1 or response.status_code != 206:
                        expected = self._verify_response(response, *ranges[0])
                        if expected != payload_size: fail('invalid_content_range')
                    else:
                        self._count_status(response.status_code)
                        multipart_boundary(response.headers.get('Content-Type',''))
                        if self.total is None: fail('multipart_requires_frozen_source')
                        # Verify outer source/encoding against the same frozen source.
                        proxy_headers = CaseInsensitiveDict(response.headers)
                        proxy_headers.pop('Content-Length', None)
                        proxy_headers['Content-Range'] = f'bytes {ranges[0][0]}-{ranges[0][1]}/{self.total}'
                        self._source.verify_range(SimpleNamespace(headers=proxy_headers), *ranges[0])
                    if self.total is not None and self.total>self.limits.source_bytes:fail('source_size_limit')
                    length = response.headers.get('Content-Length')
                    if length is not None and (not length.isdigit() or int(length)>cap):
                        fail('response_body_limit')
                    body = bytearray()
                    read_once=getattr(response.raw,'read1',None)
                    if not callable(read_once):fail('bounded_raw_read_unsupported')
                    while True:
                        self.check()
                        budget = self.limits.network_bytes-self.audit()['upstream_bytes']
                        if budget <= 0: fail('diagnostic_byte_limit')
                        size = min(65536, cap+1-len(body), budget)
                        block = read_once(size, decode_content=False)
                        if not block: break
                        if len(block)>size: fail('invalid_read_length')
                        self._count('upstream_bytes', len(block))
                        body.extend(block)
                        if len(body)>cap: fail('response_body_limit')
                    self.check()
                    if length is not None and len(body)!=int(length): fail('upstream_premature_eof')
                    if len(ranges)==1:
                        if len(body)!=payload_size: fail('upstream_premature_eof')
                        output = [bytes(body)]
                    else:
                        output = parse_multipart(bytes(body), response.headers['Content-Type'], ranges, self.total)
                        self._count('multipart_body_bytes', len(body))
                        self._count('range_verified', len(ranges))
                    if self._resume_pending:
                        self._count('session_refresh_successes')
                        self._session_event('media_resumed', ranges[0][0])
                        self._resume_pending = False
                    return output
                except MediaTransportError as error:
                    code = error.code
                except (requests.RequestException, OSError, HTTPError) as error:
                    code = connection_failure_code(error)
                finally:
                    if response is not None:
                        with self._response_lock: self._responses.discard(response)
                        response.close()
                self._count('retry_bytes', self.audit()['upstream_bytes']-before)
                self.check()
                action = self._recovery.decide(code, attempt, can_refresh=(
                    self.allow_session_refresh and not self._session_refreshed
                    and callable(getattr(self.client, 'refresh_media_session', None))))
                if action is RecoveryAction.STOP: self._fail(self._recovery.terminal_code(code))
                if action is RecoveryAction.REFRESH: self._refresh_session()
                self._count('retries')
                if self._stop.wait(self._recovery.delay(code, attempt)): self.check()
            raise AssertionError('bounded recovery exhausted')

    def _count_status(self, status):
        with self._audit_lock:
            counts = self._audit['upstream_status_counts']
            counts[str(status)] = counts.get(str(status), 0)+1

    def statistics(self):
        # Public experiment stats never include source identity, validator or offsets.
        audit = self.audit()
        keys = ('range_requests', 'range_verified', 'signature_renewals', 'retries',
                'upstream_bytes', 'multipart_requests', 'multipart_body_bytes',
                'retry_bytes', 'index_bytes', 'index_seconds', 'range_count',
                'session_refresh_attempts', 'session_refresh_successes',
                'session_identity_verifications', 'terminal_error_code')
        return {**{k:audit[k] for k in keys},'format_observations':dict(self.format_observations)}


@dataclass(frozen=True)
class Box:
    kind: bytes
    start: int
    payload: int
    end: int


def boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    count = 0
    while start<end:
        count += 1
        if count>10000 or end-start<8: fail('invalid_mp4_box')
        size, kind = struct.unpack_from('>I4s', data, start)
        header = 8
        if size==1:
            if end-start<16: fail('invalid_mp4_box')
            size = struct.unpack_from('>Q', data, start+8)[0]; header=16
        if size==0: size=end-start
        if size<header or size>end-start: fail('invalid_mp4_box')
        yield Box(kind, start, start+header, start+size)
        start += size


def unique(nodes, kind):
    rows = [b for b in nodes if b.kind==kind]
    if len(rows)!=1: fail('missing_or_duplicate_mp4_table')
    return rows[0]


def full_payload(data, box, version=0):
    value = memoryview(data)[box.payload:box.end]
    if len(value)<4 or value[0]!=version or bytes(value[1:4])!=b'\0\0\0':
        fail('unsupported_mp4_version_or_flags')
    return value[4:]


def table(data, box, width, limit):
    value = full_payload(data, box)
    if len(value)<4: fail('invalid_mp4_table')
    count = struct.unpack_from('>I', value)[0]
    if not 0<count<=limit or len(value)!=4+count*width: fail('invalid_mp4_table')
    return value[4:]


def descriptors(data):
    cursor=0
    while cursor<len(data):
        tag=data[cursor]; cursor+=1; length=0
        for n in range(4):
            if cursor>=len(data): fail('invalid_aac_config')
            b=data[cursor];cursor+=1;length=(length<<7)|(b&127)
            if b<128: break
        else: fail('invalid_aac_config')
        if length>len(data)-cursor: fail('invalid_aac_config')
        yield tag, bytes(data[cursor:cursor+length]);cursor+=length


def asc_from_esds(payload):
    roots=list(descriptors(payload))
    if len(roots)!=1 or roots[0][0]!=3: fail('unsupported_aac_config')
    es=roots[0][1]
    if len(es)<3 or es[2]!=0: fail('unsupported_aac_config')
    decoder=[v for k,v in descriptors(es[3:]) if k==4]
    if len(decoder)!=1 or len(decoder[0])<13: fail('invalid_aac_config')
    dec=decoder[0]
    if dec[0]!=0x40 or dec[1]>>2!=5: fail('unsupported_aac_config')
    configs=[v for k,v in descriptors(dec[13:]) if k==5]
    if len(configs)!=1: fail('invalid_aac_config')
    return configs[0]


def aac_config(asc):
    bits=''.join(f'{b:08b}' for b in asc)
    if len(bits)<16: fail('invalid_aac_config')
    obj=int(bits[:5],2); freq=int(bits[5:9],2); channels=int(bits[9:13],2)
    rates=(96000,88200,64000,48000,44100,32000,24000,22050,16000,12000,11025,8000,7350)
    if obj!=2 or freq>=len(rates) or channels not in (1,2) or bits[13:16]!='000':
        fail('unsupported_aac_config')
    tail=bits[16:]
    # FFmpeg AAC-LC sometimes includes a backwards-compatible SBR-absent extension.
    if tail and '1' in tail:
        if len(tail)<17 or int(tail[:11],2)!=0x2b7 or int(tail[11:16],2)!=5 or tail[16]!='0' or '1' in tail[17:]:
            fail('unsupported_aac_extension')
    return rates[freq], channels, freq


@dataclass(frozen=True)
class Packet:
    number: int
    offset: int
    size: int
    dts: int
    duration: int
    media_dts: int | None = None

    @property
    def pts(self): return self.dts  # Supported audio has no composition offsets.


@dataclass(frozen=True)
class PacketWindow:
    items: tuple
    start: float
    requested_seconds: float

    def __len__(self):return len(self.items)
    def __iter__(self):return iter(self.items)
    def __getitem__(self,key):return self.items[key]


@dataclass
class AACIndex:
    sizes: array
    offsets: array
    timing: tuple
    timescale: int
    duration: int
    sample_rate: int
    channels: int
    frequency_index: int
    asc: bytes
    top_boxes: tuple = ()
    moov_bytes: bytes = b''
    identity_edit: bool = False
    presentation_offset: int = 0
    presentation_end: int | None = None
    requires_preroll: bool = False

    def packets(self, start=0, seconds=60, *, limits=Limits()):
        if (not math.isfinite(start) or not math.isfinite(seconds) or start<0
                or not 0<seconds<=limits.window_seconds): fail('window_limit')
        if self.requires_preroll and start!=0:fail('aac_preroll_seek_unsupported')
        left, right = start*self.timescale, (start+seconds)*self.timescale
        if self.presentation_end is not None and right>self.presentation_end+1e-6:fail('edit_window_unsupported')
        dts=0; number=0; output=[]; size=0
        for count, delta in self.timing:
            for _ in range(count):
                if dts+self.presentation_offset<right and dts+delta+self.presentation_offset>left:
                    size+=self.sizes[number]
                    if size>limits.packet_bytes: fail('packet_memory_limit')
                    output.append(Packet(number, self.offsets[number], self.sizes[number], dts+self.presentation_offset, delta, dts))
                dts+=delta;number+=1
                if dts+self.presentation_offset>=right: return PacketWindow(tuple(output),start,seconds)
        return PacketWindow(tuple(output),start,seconds)


def load_index(reader):
    began=time.monotonic(); limits=reader.limits
    def read(offset, size):
        if reader.audit()['index_bytes']+size>limits.index_bytes: fail('index_byte_limit')
        return reader.read(offset,size,index=True)
    top=[]; cursor=0
    # First tiny closed range freezes total/validators without reading video.
    first=read(0,8)
    while cursor<reader.total:
        reader.check()
        if len(top)>=10000 or reader.total-cursor<8: fail('invalid_mp4_box')
        head=first if cursor==0 else read(cursor,8)
        size,kind=struct.unpack('>I4s',head);header=8
        if size==1: size=struct.unpack('>Q',read(cursor+8,8))[0];header=16
        if size==0: size=reader.total-cursor
        if size<header or size>reader.total-cursor: fail('invalid_mp4_box')
        top.append(Box(kind,cursor,cursor+header,cursor+size));cursor+=size
    if any(b.kind in (b'moof',b'mfra') for b in top): fail('fragmented_mp4_unsupported')
    moov=unique(top,b'moov')
    # Walk only box headers; do not download video sample tables inside moov.
    def remote_boxes(start,end):
        n=0
        while start<end:
            reader.check();n+=1
            if n>10000 or end-start<8:fail('invalid_mp4_box')
            size,kind=struct.unpack('>I4s',read(start,8));header=8
            if size==1:size=struct.unpack('>Q',read(start+8,8))[0];header=16
            if size==0:size=end-start
            if size<header or size>end-start:fail('invalid_mp4_box')
            yield Box(kind,start,start+header,start+size)
            start+=size
    moov_nodes=list(remote_boxes(moov.payload,moov.end))
    if any(b.kind==b'mvex' for b in moov_nodes):fail('fragmented_mp4_unsupported')
    selected=[]
    for trak in [b for b in moov_nodes if b.kind==b'trak']:
        children=list(remote_boxes(trak.payload,trak.end))
        mdia=unique(children,b'mdia')
        m=list(remote_boxes(mdia.payload,mdia.end));handler=unique(m,b'hdlr')
        if handler.end-handler.payload<12:fail('invalid_mp4_handler')
        h=read(handler.payload,12)
        if h[:4]!=b'\0'*4:fail('unsupported_mp4_version_or_flags')
        if h[8:12]==b'soun':selected.append(trak)
    if len(selected)!=1:fail('ambiguous_audio_tracks')
    trak=selected[0]
    if trak.end-trak.start>limits.index_bytes:fail('index_byte_limit')
    mvhd=[b for b in moov_nodes if b.kind==b'mvhd']
    if len(mvhd)>1:fail('invalid_mp4_duration')
    movie=read(mvhd[0].start,mvhd[0].end-mvhd[0].start) if mvhd else b''
    data=movie+read(trak.start,trak.end-trak.start)
    nodes=list(boxes(data))
    if any(b.kind==b'mvex' for b in nodes): fail('fragmented_mp4_unsupported')
    audio=[]
    for trak in [b for b in nodes if b.kind==b'trak']:
        children=list(boxes(data,trak.payload,trak.end))
        mdia=unique(children,b'mdia'); m=list(boxes(data,mdia.payload,mdia.end))
        handler=full_payload(data,unique(m,b'hdlr'))
        if len(handler)<8: fail('invalid_mp4_handler')
        if bytes(handler[4:8])==b'soun': audio.append((children,m))
    if len(audio)!=1: fail('ambiguous_audio_tracks')
    children,m=audio[0]
    edts=[b for b in children if b.kind==b'edts']
    identity_edit=False;empty_duration=0;has_edit=False
    # One rate-1 zero-media-time edit, optionally preceded by one empty edit.
    # No media trimming, priming, non-unit rates or multiple audio intervals.
    if edts:
        if len(edts)!=1 or not mvhd:fail('edit_list_unsupported')
        edit=unique(list(boxes(data,edts[0].payload,edts[0].end)),b'elst')
        version=data[edit.payload]
        if version not in (0,1):fail('edit_list_unsupported')
        value=full_payload(data,edit,version);width=20 if version==1 else 12
        n=struct.unpack_from('>I',value)[0] if len(value)>=4 else 0
        reader.format_observations={'edit_entries':n}
        if n not in (1,2) or len(value)!=4+n*width:fail('edit_list_unsupported')
        edits=tuple(struct.iter_unpack('>Qqhh' if version else '>Iihh',value[4:]))
        reader.format_observations.update(edit_media_ticks=[v[1] for v in edits],
            edit_segment_movie_ticks=[v[0] for v in edits])
        if any((v[2],v[3])!=(1,0) for v in edits):fail('edit_list_unsupported')
        if n==2:
            if edits[0][1]!=-1:fail('edit_list_unsupported')
            empty_duration=edits[0][0]
        segment,media_time,_,_=edits[-1]
        if media_time!=0 or segment<=0:fail('edit_list_unsupported')
        movie_box=unique(list(boxes(data)),b'mvhd');movie_version=data[movie_box.payload]
        if movie_version not in (0,1):fail('edit_list_unsupported')
        movie_header=full_payload(data,movie_box,movie_version)
        if len(movie_header)<(28 if movie_version else 16):fail('invalid_mp4_duration')
        movie_scale=struct.unpack_from('>I',movie_header,16 if movie_version else 8)[0]
        if not 1<=movie_scale<=1_000_000:fail('invalid_mp4_duration')
        has_edit=True;identity_edit=empty_duration==0
    mdhd=unique(m,b'mdhd');version=data[mdhd.payload]
    if version not in (0,1): fail('unsupported_mp4_version_or_flags')
    header=full_payload(data,mdhd,version)
    if len(header)!=(20 if version==0 else 32): fail('invalid_mp4_duration')
    timescale,duration=struct.unpack_from('>II' if version==0 else '>IQ',header,8 if version==0 else 16)
    if not 1<=timescale<=1_000_000 or duration<=0: fail('invalid_mp4_duration')
    presentation_offset=0;presentation_end=duration
    if has_edit:
        reader.format_observations.update(movie_timescale=movie_scale,track_timescale=timescale,track_duration_ticks=duration)
        if empty_duration*timescale%movie_scale:fail('nonintegral_edit_offset_unsupported')
        presentation_offset=empty_duration*timescale//movie_scale
        # A prefix must fit both encoded track coverage and the edit interval.
        presentation_end=presentation_offset+min(duration,segment*timescale//movie_scale)
    minf=unique(m,b'minf'); stbl=unique(list(boxes(data,minf.payload,minf.end)),b'stbl')
    st=list(boxes(data,stbl.payload,stbl.end))
    if any(b.kind in (b'senc',b'saiz',b'saio') for b in st):
        fail('encrypted_audio_unsupported')
    composition=[b for b in st if b.kind==b'ctts']
    if composition:fail('composition_timing_unsupported')
    stsd=full_payload(data,unique(st,b'stsd'))
    if len(stsd)<4 or struct.unpack_from('>I',stsd)[0]!=1: fail('sample_description_unsupported')
    entries=list(boxes(stsd,4))
    if len(entries)!=1 or entries[0].kind!=b'mp4a': fail('codec_unsupported')
    e=entries[0]; entry=stsd[e.payload:e.end]
    if len(entry)<28 or bytes(entry[:6])!=b'\0'*6 or struct.unpack_from('>H',entry,6)[0]!=1 or bytes(entry[8:16])!=b'\0'*8:
        fail('sample_description_unsupported')
    channel,sample_size=struct.unpack_from('>HH',entry,16)
    rate=struct.unpack_from('>I',entry,24)[0]
    desc=list(boxes(entry,28));esds=unique(desc,b'esds')
    if any(b.kind==b'sinf' for b in desc): fail('encrypted_audio_unsupported')
    asc=asc_from_esds(full_payload(entry,esds));sample_rate,channels,freq=aac_config(asc)
    if channel!=channels or sample_size!=16 or rate!=sample_rate<<16: fail('invalid_aac_config')
    timing_table=table(data,unique(st,b'stts'),8,limits.samples)
    if len(data)+len(timing_table)//8*96>limits.index_memory_bytes:fail('index_memory_limit')
    timing=tuple(struct.iter_unpack('>II',timing_table))
    if any(c<=0 or d<=0 for c,d in timing): fail('invalid_sample_timing')
    count=sum(c for c,d in timing)
    if count>limits.samples or sum(c*d for c,d in timing)!=duration: fail('invalid_sample_timing')
    if timescale!=sample_rate:fail('aac_timescale_unsupported')
    if (any(d!=1024 for c,d in timing[:-1]) or not 0<timing[-1][1]<=1024
            or timing[-1][1]!=1024 and timing[-1][0]!=1):fail('aac_sample_gap_unsupported')
    groups=[b for b in st if b.kind in (b'sgpd',b'sbgp')];requires_preroll=False
    if groups:
        sgpd=unique(groups,b'sgpd');sbgp=unique(groups,b'sbgp')
        version=data[sgpd.payload]
        if version not in (0,1):fail('aac_sample_groups_unsupported')
        value=full_payload(data,sgpd,version)
        header=12 if version else 8
        if len(value)<header or bytes(value[:4])!=b'roll':fail('aac_sample_groups_unsupported')
        if version and struct.unpack_from('>I',value,4)[0]!=2:fail('aac_sample_groups_unsupported')
        entries=struct.unpack_from('>I',value,8 if version else 4)[0]
        if entries!=1 or len(value)!=header+2:fail('aac_sample_groups_unsupported')
        distance=struct.unpack_from('>h',value,header)[0]
        if distance not in (-1,0):fail('aac_sample_groups_unsupported')
        grouping=full_payload(data,sbgp)
        if len(grouping)<8 or bytes(grouping[:4])!=b'roll':fail('aac_sample_groups_unsupported')
        n=struct.unpack_from('>I',grouping,4)[0]
        if not 0<n<=count or len(grouping)!=8+8*n:fail('invalid_aac_sample_groups')
        total=0
        for c,g in struct.iter_unpack('>II',grouping[8:]):
            if c<=0 or g not in (0,1):fail('invalid_aac_sample_groups')
            total+=c;requires_preroll|=(g==1 and distance==-1)
        if total!=count:fail('invalid_aac_sample_groups')
        reader.format_observations.update(roll_distance=distance,prefix_only=requires_preroll)
    size_boxes=[b for b in st if b.kind in (b'stsz',b'stz2')]
    if len(size_boxes)!=1: fail('missing_or_duplicate_mp4_table')
    sb=size_boxes[0]; value=full_payload(data,sb)
    if len(value)<8: fail('invalid_sample_sizes')
    default,n=struct.unpack_from('>II',value)
    if n!=count: fail('sample_count_mismatch')
    if len(data)+len(timing)*96+count*12>limits.index_memory_bytes:fail('index_memory_limit')
    sizes=array('I')
    if sb.kind==b'stsz':
        if default:
            if len(value)!=8: fail('invalid_sample_sizes')
            sizes=array('I',[default])*count
        else:
            if len(value)!=8+count*4: fail('invalid_sample_sizes')
            sizes=array('I',(v[0] for v in struct.iter_unpack('>I',value[8:])))
    else:
        field=value[3]
        if bytes(value[:3])!=b'\0'*3 or field not in (4,8,16): fail('invalid_sample_sizes')
        if len(value)!=8+(count*field+7)//8: fail('invalid_sample_sizes')
        raw=value[8:]
        if field==4: sizes=array('I', ((raw[i//2]>>(4 if i%2==0 else 0))&15 for i in range(count)))
        elif field==8: sizes=array('I',raw)
        else: sizes=array('I',(v[0] for v in struct.iter_unpack('>H',raw)))
    if any(not 0<s<=8184 for s in sizes): fail('unsupported_aac_packet_size')
    cb=[b for b in st if b.kind in (b'stco',b'co64')]
    if len(cb)!=1: fail('missing_or_duplicate_mp4_table')
    width=8 if cb[0].kind==b'co64' else 4
    chunks=array('Q',(v[0] for v in struct.iter_unpack('>Q' if width==8 else '>I',table(data,cb[0],width,limits.samples))))
    mapping_table=table(data,unique(st,b'stsc'),12,limits.samples)
    if (len(data)+len(timing)*96+count*12+len(chunks)*8+len(mapping_table)//12*128)>limits.index_memory_bytes:fail('index_memory_limit')
    mapping=tuple(struct.iter_unpack('>III',mapping_table))
    if mapping[0][0]!=1: fail('invalid_chunk_mapping')
    previous=0
    for first, samples, description in mapping:
        if not previous<first<=len(chunks) or samples<=0 or description!=1: fail('invalid_chunk_mapping')
        previous=first
    mdats=[(b.payload,b.end) for b in top if b.kind==b'mdat']
    offsets=array('Q');sample=0;run=0;last_end=-1;mdat_index=0
    for number,offset in enumerate(chunks,1):
        while run+1<len(mapping) and mapping[run+1][0]<=number:run+=1
        n=mapping[run][1]
        if sample+n>count:fail('sample_count_mismatch')
        length=sum(sizes[sample:sample+n])
        while mdat_index<len(mdats) and mdats[mdat_index][1]<=offset: mdat_index+=1
        if (offset<last_end or mdat_index==len(mdats)
                or offset<mdats[mdat_index][0] or offset+length>mdats[mdat_index][1]):
            fail('invalid_sample_offset')
        last_end=offset+length
        for s in sizes[sample:sample+n]:offsets.append(offset);offset+=s
        sample+=n
    if sample!=count:fail('sample_count_mismatch')
    reader._count('index_seconds',time.monotonic()-began)
    return AACIndex(sizes,offsets,timing,timescale,duration,sample_rate,channels,freq,asc,tuple(top),data,identity_edit,presentation_offset,presentation_end,requires_preroll)


@dataclass(frozen=True)
class VerifiedPackets:
    packets: tuple
    payloads: tuple
    timescale: int

    def __post_init__(self):
        object.__setattr__(self,'packets',tuple(self.packets))
        if not self.packets or len(self.packets)!=len(self.payloads): fail('packet_coverage_incomplete')
        for i,(packet,payload) in enumerate(zip(self.packets,self.payloads)):
            if len(payload)!=packet.size:fail('packet_coverage_incomplete')
            if i and (packet.number!=self.packets[i-1].number+1 or packet.dts!=self.packets[i-1].dts+self.packets[i-1].duration):
                fail('packet_coverage_incomplete')

    @property
    def seconds(self): return sum(p.duration for p in self.packets)/self.timescale

    def adts(self,index):
        result=bytearray()
        for payload in self.payloads:
            size=len(payload)+7; ch=index.channels; freq=index.frequency_index
            result.extend(bytes((255,241,(1<<6)|(freq<<2)|(ch>>2),((ch&3)<<6)|(size>>11),
                                 (size>>3)&255,((size&7)<<5)|31,252)))
            result.extend(payload)
        return bytes(result)


def packet_ranges(packets):
    ranges=[]
    for p in packets:
        if ranges and p.offset==ranges[-1][1]+1:ranges[-1]=(ranges[-1][0],p.offset+p.size-1)
        else:ranges.append((p.offset,p.offset+p.size-1))
    return ranges


def fetch_packets(reader,index,packets):
    if not packets:fail('empty_packet_window')
    # Re-derive the immutable plan: losing the final packet is not a shorter success.
    if (not isinstance(packets,PacketWindow) or
            packets.items!=index.packets(packets.start,packets.requested_seconds,limits=reader.limits).items):
        fail('packet_coverage_incomplete')
    for i,p in enumerate(packets):
        if (not 0<=p.number<len(index.sizes) or p.offset!=index.offsets[p.number]
                or p.size!=index.sizes[p.number]
                or i and (p.number!=packets[i-1].number+1 or p.dts!=packets[i-1].dts+packets[i-1].duration)):
            fail('packet_coverage_incomplete')
    ranges=packet_ranges(packets);payloads=[];packet_cursor=0
    for b in range(0,len(ranges),reader.limits.batch_ranges):
        batch=ranges[b:b+reader.limits.batch_ranges]
        checked=reader.fetch(batch)  # Whole batch validation precedes committing packets.
        for (start,end),data in zip(batch,checked):
            cursor=0
            while packet_cursor<len(packets) and packets[packet_cursor].offset<=end:
                p=packets[packet_cursor]
                if p.offset!=start+cursor:fail('packet_coverage_incomplete')
                payloads.append(data[cursor:cursor+p.size]);cursor+=p.size;packet_cursor+=1
            if cursor!=len(data):fail('packet_coverage_incomplete')
    if packet_cursor!=len(packets):fail('packet_coverage_incomplete')
    return VerifiedPackets(packets,tuple(payloads),index.timescale)
