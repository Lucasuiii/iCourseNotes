"""Bounded WebVPN media concurrency probe.

Each session logs in once, then reads small byte ranges of one lecture video at a
fixed pace and records what the media route answers. It never re-authenticates,
never downloads a whole lecture, and touches no database, model or email.

Output is limited to status codes, redirect classes, byte counts and timings:
no URLs, identifiers, cookies or response bodies.

Usage: media_concurrency_probe.py SESSIONS LABEL START_AT STAGGER STOP_AT OUT
  Sessions start at START_AT + i*STAGGER (epoch seconds) and stop at STOP_AT.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import sys
import threading
import time

import requests

from src.api.icourse import ICourseClient
from src.api.webvpn import AuthenticationError, WebVPNSession
from src.runtime import config
from src.runtime.media_protocol import redirect_kind

READ_BYTES = int(float(os.environ.get('PROBE_READ_MB', '1')) * (1 << 20))
INTERVAL = float(os.environ.get('PROBE_INTERVAL_SECONDS', '5'))
API_EVERY = 3
MAX_READS = 400
_quiet = threading.Lock()


def quiet(call):
    """Library calls print progress; keep every byte of it out of public logs."""
    with _quiet, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return call()


def pick_video(client):
    """Same deterministic choice in every session: newest playable lecture."""
    for course in config.COURSE_IDS:
        lectures = quiet(lambda: client.get_course_detail(course))['lectures']
        for lecture in sorted((x for x in lectures if x['has_playback']),
                              key=lambda x: (x['date'], str(x['sub_id'])), reverse=True)[:3]:
            url = quiet(lambda: client.get_video_url(course, lecture['sub_id']))
            if url:
                return url
    raise RuntimeError('No playable lecture')


def media_headers(raw):
    headers = {k.strip(): v.strip() for k, v in (line.split(':', 1) for line in raw.split('\r\n') if ':' in line)}
    headers = {k: v for k, v in headers.items() if k.lower() != 'cookie'}
    headers['Accept-Encoding'] = 'identity'
    return headers


def session(index, label, start_at, stop_at, record):
    record.update(label=label, index=index, events=[])
    time.sleep(max(0, start_at - time.time()))
    began = time.time()
    vpn = WebVPNSession()
    try:
        quiet(vpn.login)
        quiet(lambda: vpn.authenticate_icourse(strict=True))
    except Exception as error:
        record['login'] = {'ok': False, 'at': round(began, 2), 'seconds': round(time.time() - began, 2),
                           'type': type(error).__name__,
                           'reason': error.reason if isinstance(error, AuthenticationError) else None}
        return
    record['login'] = {'ok': True, 'at': round(began, 2), 'seconds': round(time.time() - began, 2)}
    client = ICourseClient(vpn)
    try:
        signed = pick_video(client)
    except Exception as error:
        record['video'] = {'ok': False, 'type': type(error).__name__}
        return
    record['video'] = {'ok': True}
    login_urls = client.trusted_media_login_urls()
    media = requests.Session()
    total, n = None, 0
    while time.time() < stop_at and n < MAX_READS:
        n += 1
        started = time.time()
        # Spread reads over the file (deterministic per session) instead of re-reading one range.
        offset = 0 if total is None else ((index * 97 + n * 13) * READ_BYTES) % max(READ_BYTES, total - READ_BYTES)
        event = {'kind': 'media', 'at': round(started, 2)}
        try:
            fresh = client.renew_video_url(signed)
            target, raw = client.get_stream_params(fresh)
            media.cookies = vpn.session.cookies
            response = media.get(target, headers={**media_headers(raw), 'Range': f'bytes={offset}-{offset + READ_BYTES - 1}'},
                                 stream=True, timeout=60, allow_redirects=False)
            event['status'] = response.status_code
            if response.status_code in (301, 302, 303, 307, 308):
                event['redirect'] = redirect_kind(response, login_urls=login_urls)
            elif response.status_code == 206:
                size = 0
                for block in response.iter_content(64 * 1024):
                    size += len(block)
                event['bytes'] = size
                content_range = response.headers.get('Content-Range', '')
                if '/' in content_range and content_range.rsplit('/', 1)[1].isdigit():
                    total = int(content_range.rsplit('/', 1)[1])
            response.close()
        except Exception as error:
            event['error'] = type(error).__name__
        event['seconds'] = round(time.time() - started, 2)
        record['events'].append(event)
        if n % API_EVERY == 0 or event.get('status') != 206:
            started = time.time()
            alive = quiet(client.check_alive)
            record['events'].append({'kind': 'api', 'at': round(started, 2), 'ok': alive,
                                     'seconds': round(time.time() - started, 2)})
        time.sleep(max(0, INTERVAL - (time.time() - started)))
    media.close()
    quiet(vpn.session.close)


def summarize(record):
    media = [e for e in record.get('events', []) if e['kind'] == 'media']
    api = [e for e in record.get('events', []) if e['kind'] == 'api']
    statuses = {}
    for e in media:
        key = str(e.get('status', e.get('error')))
        if e.get('redirect'):
            key += ':' + e['redirect']
        statuses[key] = statuses.get(key, 0) + 1
    first_bad = next((e['at'] for e in media if e.get('status') != 206), None)
    return {'label': record.get('label'), 'login': record.get('login'), 'video': record.get('video'),
            'media_reads': len(media), 'media_bytes': sum(e.get('bytes', 0) for e in media),
            'media_statuses': statuses, 'first_non_206_at': first_bad,
            'api_checks': len(api), 'api_failures': sum(not e['ok'] for e in api)}


def main(argv):
    count, label, start_at, stagger, stop_at, out = int(argv[0]), argv[1], float(argv[2]), float(argv[3]), float(argv[4]), argv[5]
    if not 1 <= count <= 5 or stop_at - start_at > 1800:
        raise ValueError('Probe bounds exceeded')
    records = [{} for _ in range(count)]
    threads = [threading.Thread(target=session, args=(i, f'{label}-s{i}', start_at + i * stagger, stop_at, records[i]))
               for i in range(count)]
    for t in threads: t.start()
    for t in threads: t.join()
    with open(out, 'w') as f:
        json.dump({'label': label, 'start_at': start_at, 'stagger': stagger, 'stop_at': stop_at,
                   'read_bytes': READ_BYTES, 'interval': INTERVAL, 'sessions': records}, f)
    for record in records:
        print(json.dumps(summarize(record)))


if __name__ == '__main__':
    main(sys.argv[1:])
