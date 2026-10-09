"""Fixed lookup error vocabulary; no provider messages, response bodies or URLs."""
import re
import time
import requests
FAILURES = {'tls_error', 'timeout', 'connection_error', 'http_error', 'invalid_json',
            'api_error', 'invalid_payload', 'other_error', 'login_required',
            'challenge_required', 'redirect_rejected'}


class PlaybackResponseError(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.playback_failure = reason


def safe_response(value):
    if type(value) is not dict: return {}
    clean = {}
    status = value.get('http_status')
    if type(status) is int and 100 <= status <= 599: clean['http_status'] = status
    kind = value.get('body_kind')
    if type(kind) is str and kind in ('empty', 'html', 'json', 'other'): clean['body_kind'] = kind
    for key in ('login_hint', 'challenge_hint', 'redirected'):
        if type(value.get(key)) is bool: clean[key] = value[key]
    return clean


def playback_json(vpn, url, params):
    """One bounded GET; never follow a login page or retain its body/Location."""
    response = vpn.get(url, params=params, allow_redirects=False, timeout=(5, 10))
    # The prefix is used only to classify the reply and is never attached to errors.
    raw = response.content
    prefix = raw[:8192].decode(errors='replace').strip().lower() if isinstance(raw, bytes) else ''
    shape = ('empty' if not prefix else 'html' if prefix.startswith('<')
             else 'json' if prefix.startswith(('{', '[')) else 'other')
    location = response.headers.get('Location', '')
    location = location.lower() if isinstance(location, str) else ''
    login = any(x in location for x in ('/login', '/cas/', '/idp/')) or any(
        x in prefix for x in ('type="password"', "type='password'", '/cas/login', '/idp/auth', '统一身份认证'))
    challenge = any(x in prefix for x in ('captcha', '验证码', '二次验证', 'one-time password')) or bool(
        re.search(r'"(?:needverifycode|needcaptcha|needmfa|needotp)"\s*:\s*true', prefix))
    observation = safe_response({'http_status':response.status_code, 'body_kind':shape,
        'login_hint':login, 'challenge_hint':challenge,
        'redirected':bool(response.history) or response.status_code in (301,302,303,307,308)})
    try:
        if challenge: raise PlaybackResponseError('challenge_required')
        if login or response.status_code == 401: raise PlaybackResponseError('login_required')
        if response.status_code in (301,302,303,307,308): raise PlaybackResponseError('redirect_rejected')
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict): raise ValueError('Invalid playback payload')
        if any(payload.get(key) is True for key in ('needVerifyCode', 'needCaptcha', 'needMfa', 'needOtp')):
            observation['challenge_hint'] = True
            raise PlaybackResponseError('challenge_required')
        return payload
    except Exception as error:
        error.playback_response = observation
        raise
    finally:
        response.close()


def retryable_lookup(error):
    row = lookup_error(error)
    if row['failure'] in ('timeout', 'connection_error'): return True
    response = row.get('response', {})
    if any(response.get(key) for key in ('login_hint', 'challenge_hint', 'redirected')): return False
    status = row.get('http_status', response.get('http_status'))
    return (row['failure'] == 'http_error' and status in (408,429,500,502,503,504)
            or row['failure'] == 'invalid_json' and status == 200
            and response.get('body_kind') in ('empty', 'json'))


def load_source(getter, source):
    """At most three idempotent GET attempts; preserve the fallback order."""
    history = []
    for attempt in range(3):
        try:
            payload = getter()
            row = {'result':'payload'}
        except Exception as error:
            row = lookup_error(error)
            history.append(row)
            if attempt < 2 and retryable_lookup(error):
                time.sleep(.25*(attempt+1))
                continue
            payload = {}
        else:
            history.append(row)
        result = {'source':source, **row}
        if len(history) > 1: result.update(attempt_count=len(history), attempts=history)
        return payload, result


def lookup_error(error):
    classes = ((requests.exceptions.SSLError, 'tls_error'),
               (requests.exceptions.Timeout, 'timeout'),
               (requests.exceptions.ConnectionError, 'connection_error'),
               (requests.exceptions.HTTPError, 'http_error'),
               (requests.exceptions.JSONDecodeError, 'invalid_json'),
               (RuntimeError, 'api_error'), (ValueError, 'invalid_payload'))
    result = {'result': 'failed', 'failure': next((code for cls, code in classes
                                                  if isinstance(error, cls)), 'other_error')}
    fixed = getattr(error, 'playback_failure', None)
    if isinstance(fixed, str) and fixed in FAILURES: result['failure'] = fixed
    status = getattr(getattr(error, 'response', None), 'status_code', None)
    if type(status) is int and 100 <= status <= 599: result['http_status'] = status
    code = getattr(error, 'playback_api_code', None)
    if type(code) is int and -1_000_000 <= code <= 1_000_000: result['api_code'] = code
    response = safe_response(getattr(error, 'playback_response', None))
    if response: result['response'] = response
    return result


def safe_result(row):
    if type(row) is not dict or row.get('result') not in ('payload', 'failed'): return None
    clean = {'result':row['result']}
    if row['result'] == 'failed':
        failure = row.get('failure')
        clean['failure'] = failure if isinstance(failure, str) and failure in FAILURES else 'other_error'
        for key, low, high in (('http_status',100,599), ('api_code',-1_000_000,1_000_000)):
            if type(row.get(key)) is int and low <= row[key] <= high: clean[key] = row[key]
        response = safe_response(row.get('response'))
        if response: clean['response'] = response
    return clean


def attach_lookup(audit, client, course, sub):
    """A best-effort diagnostic cannot replace a downloader failure."""
    try:
        getter = getattr(client, 'video_lookup_diagnostics', None)
        value = getter(course, sub) if callable(getter) else None
        if (type(value) is dict and type(value.get('url_found')) is bool
                and type(value.get('sources')) is list and len(value['sources']) <= 3):
            rows = []
            for row in value['sources']:
                if type(row) is not dict or row.get('source') not in ('sub_info', 'sub_detail', 'signing'): continue
                result = safe_result(row)
                if result is None: continue
                clean = {'source':row['source'], **result}
                history = row.get('attempts')
                if (type(row.get('attempt_count')) is int and 2 <= row['attempt_count'] <= 3
                        and type(history) is list and len(history) == row['attempt_count']):
                    attempts = [safe_result(item) for item in history]
                    if all(item is not None for item in attempts):
                        clean.update(attempt_count=len(attempts), attempts=attempts)
                rows.append(clean)
            audit['playback_lookup'] = {'sources': rows, 'url_found': value['url_found']}
    except Exception:
        pass
    return audit
