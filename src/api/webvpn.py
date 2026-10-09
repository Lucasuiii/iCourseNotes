"""
WebVPN URL encoding and authentication for Fudan University.

Handles:
- AES-128-CFB URL encoding/decoding for WebVPN proxy URLs
- Full 7-step IDP authentication flow against id.fudan.edu.cn
"""

import html as html_mod
import re
from binascii import hexlify, unhexlify
from urllib.parse import urlparse, urlencode, quote, urljoin, urlsplit

import requests
from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
import base64

from src.runtime import config


class AuthenticationError(RuntimeError):
    """Fixed reason code; no credential, response body or ticket in diagnostics."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


AUTH_PHASES = frozenset(('login_service_probe', 'credentials', 'webvpn_context',
    'webvpn_auth_methods', 'webvpn_public_key', 'webvpn_password_encrypt',
    'webvpn_auth_execute', 'webvpn_ticket', 'webvpn_ticket_follow',
    'webvpn_session_probe', 'icourse_portal_warmup', 'icourse_cas_context',
    'icourse_auth_methods', 'icourse_public_key', 'icourse_password_encrypt',
    'icourse_auth_execute', 'icourse_cas_ticket', 'icourse_ticket_follow',
    'icourse_api_verification', 'fresh_session_factory', 'media_identity_verification',
    'unknown'))
AUTH_FAILURE_CODES = frozenset(('service_unavailable', 'cold_session',
    'cas_context_missing', 'api_verification_failed', 'authentication_rejected',
    'password_method_missing', 'service_redirect_untrusted', 'service_http_rejected',
    'public_key_missing', 'login_token_missing', 'cas_ticket_missing',
    'ticket_destination_untrusted', 'media_auth_cancelled', 'auth_read_timeout',
    'auth_connect_timeout', 'auth_timeout', 'auth_tls_error', 'auth_connection_error',
    'auth_invalid_response', 'auth_invalid_value', 'auth_internal_error'))
AUTH_ERROR_TYPES = frozenset(('AuthenticationError', 'ReadTimeout', 'ConnectTimeout',
    'Timeout', 'SSLError', 'ConnectionError', 'JSONDecodeError', 'ValueError',
    'TypeError', 'RuntimeError', 'Exception'))


def safe_auth_response(value):
    """Carry only response shape, never headers, body, URL or auth context."""
    if not isinstance(value, dict): return {}
    clean = {}
    status = value.get('http_status')
    if type(status) is int and 100 <= status <= 599: clean['http_status'] = status
    kind = value.get('body_kind')
    if isinstance(kind, str) and kind in ('empty', 'html', 'json', 'other'):
        clean['body_kind'] = kind
    for key in ('challenge_hint', 'redirected'):
        if type(value.get(key)) is bool: clean[key] = value[key]
    return clean


def authentication_failure(error, phase='unknown', vpn=None):
    """Safe fixed fields, including a request that failed before any response."""
    kinds = ((AuthenticationError, 'AuthenticationError', 'auth_internal_error'),
             (requests.exceptions.ReadTimeout, 'ReadTimeout', 'auth_read_timeout'),
             (requests.exceptions.ConnectTimeout, 'ConnectTimeout', 'auth_connect_timeout'),
             (requests.exceptions.Timeout, 'Timeout', 'auth_timeout'),
             (requests.exceptions.SSLError, 'SSLError', 'auth_tls_error'),
             (requests.exceptions.ConnectionError, 'ConnectionError', 'auth_connection_error'),
             (requests.exceptions.JSONDecodeError, 'JSONDecodeError', 'auth_invalid_response'),
             (ValueError, 'ValueError', 'auth_invalid_value'),
             (TypeError, 'TypeError', 'auth_internal_error'),
             (RuntimeError, 'RuntimeError', 'auth_internal_error'))
    error_type, code = 'Exception', 'auth_internal_error'
    for kind, name, reason in kinds:
        if isinstance(error, kind): error_type, code = name, reason; break
    if isinstance(error, AuthenticationError) and isinstance(error.reason,str) and error.reason in AUTH_FAILURE_CODES:
        code = error.reason
    current = getattr(vpn, 'auth_phase', phase) if vpn is not None else phase
    result = {'failure_phase': current if isinstance(current,str) and current in AUTH_PHASES else 'unknown',
              'error_type': error_type, 'failure': code}
    # Only the whitelisted snapshot is carried out of a discarded login Session.
    inherited = getattr(error, 'auth_failure_diagnostics', None)
    if isinstance(inherited, dict):
        for key in ('precredential_failure',):
            if type(inherited.get(key)) is bool: result[key] = inherited[key]
        for key, limit in (('preflight_recovery_rounds', 3),
                           ('preflight_recovery_wait_seconds', 90)):
            value = inherited.get(key)
            if type(value) is int and 0 <= value <= limit: result[key] = value
        for key, allowed in (('failure_phase',AUTH_PHASES), ('error_type',AUTH_ERROR_TYPES),
                             ('failure',AUTH_FAILURE_CODES)):
            value = inherited.get(key)
            if isinstance(value,str) and value in allowed: result[key] = value
        for key in ('probe_attempts', 'probe_transient_failures'):
            value = inherited.get(key)
            if type(value) is int and 0 <= value <= 3: result[key] = value
        response = safe_auth_response(inherited.get('response'))
        if response: result['response'] = response
        attempts = inherited.get('auth_attempts')
        if type(attempts) is int and 1 <= attempts <= 10:
            result['auth_attempts'] = attempts
        history = inherited.get('attempt_failures')
        if isinstance(history, list) and len(history) <= 10:
            safe_history = []
            for row in history:
                if not isinstance(row, dict): continue
                clean = {k: row[k] for k, allowed in (
                    ('failure_phase', AUTH_PHASES), ('error_type', AUTH_ERROR_TYPES),
                    ('failure', AUTH_FAILURE_CODES)) if isinstance(row.get(k), str) and row[k] in allowed}
                for name in ('probe_attempts', 'probe_transient_failures'):
                    value = row.get(name)
                    if type(value) is int and 0 <= value <= 3: clean[name] = value
                if type(row.get('precredential_failure')) is bool:
                    clean['precredential_failure'] = row['precredential_failure']
                response = safe_auth_response(row.get('response'))
                if response: clean['response'] = response
                safe_history.append(clean)
            result['attempt_failures'] = safe_history
    if vpn is not None:
        for key, attribute in (('probe_attempts','auth_probe_attempts'),
                               ('probe_transient_failures','auth_probe_transient_failures')):
            value = getattr(vpn,attribute,None)
            if type(value) is int and 0 <= value <= 3: result[key] = value
    return result


def auth_observation(stage, response, data=None, **flags):
    """Whitelisted transport/flow evidence, never provider text or URLs."""
    row = {'stage':stage, 'http_status':response.status_code,
           'redirect_count':len(response.history or [])}
    # Page hints alone do not prove that a challenge is required.
    text = response.text[:100_000].lower()
    row['captcha_page_hint'] = any(s in text for s in ('captcha', '验证码'))
    row['mfa_page_hint'] = any(s in text for s in ('二次验证', '动态口令', '短信验证码'))
    if isinstance(data, dict):
        value = str(data.get('code', ''))
        if re.fullmatch(r'-?\d{1,6}',value): row['response_code'] = int(value)
        for key in ('needVerifyCode', 'needCaptcha', 'needMfa', 'needOtp'):
            if type(data.get(key)) is bool: row[key] = data[key]
    allowed = {'context_found', 'password_method_found', 'public_key_found',
               'login_token_found', 'ticket_found', 'verified'}
    row.update({k:v for k,v in flags.items() if k in allowed and type(v) is bool})
    return row


def encrypt_host(hostname: str) -> str:
    """Encrypt hostname using AES-128-CFB for WebVPN URL encoding.

    Returns the hex-encoded ciphertext of the hostname.
    """
    key = config.WEBVPN_AES_KEY
    iv = config.WEBVPN_AES_IV
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    plaintext = hostname.encode("utf-8")
    encrypted = cipher.encrypt(plaintext)
    return hexlify(encrypted).decode("ascii")


def decrypt_host(ciphertext_hex: str) -> str:
    """Decrypt a WebVPN-encoded hostname."""
    key = config.WEBVPN_AES_KEY
    iv = config.WEBVPN_AES_IV
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    decrypted = cipher.decrypt(unhexlify(ciphertext_hex))
    return decrypted.decode("utf-8")


def get_vpn_url(url: str) -> str:
    """Convert a regular URL to its WebVPN proxy URL.

    Example:
        https://icourse.fudan.edu.cn/courseapi/v3/...
        ->
        https://webvpn.fudan.edu.cn/https/77726476706e69737468656265737421f9f44e.../courseapi/v3/...
    """
    parsed = urlparse(url)
    protocol = parsed.scheme
    hostname = parsed.hostname
    port = parsed.port
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query
    if parsed.fragment:
        path += "#" + parsed.fragment

    # Remove leading slash from path for concatenation
    path = path.lstrip("/")

    encrypted = encrypt_host(hostname)
    iv_hex = hexlify(config.WEBVPN_AES_IV).decode("ascii")

    # Include non-standard port
    port_suffix = ""
    if port and not (
        (protocol == "http" and port == 80)
        or (protocol == "https" and port == 443)
    ):
        port_suffix = f"-{port}"

    vpn_url = f"{config.WEBVPN_BASE}/{protocol}{port_suffix}/{iv_hex}{encrypted}"
    if path:
        vpn_url += f"/{path}"
    return vpn_url


def get_ordinary_url(vpn_url: str) -> str:
    """Convert a WebVPN URL back to the original URL."""
    parsed = urlparse(vpn_url)
    path_parts = parsed.path.strip("/").split("/", 2)
    if len(path_parts) < 2:
        raise ValueError(f"Invalid WebVPN URL: {vpn_url}")

    protocol_part = path_parts[0]  # e.g. "https" or "https-8080"
    encoded_host = path_parts[1]  # IV + ciphertext
    rest = path_parts[2] if len(path_parts) > 2 else ""

    # Parse protocol and optional port
    if "-" in protocol_part:
        protocol, port_str = protocol_part.rsplit("-", 1)
        port = f":{port_str}"
    else:
        protocol = protocol_part
        port = ""

    # Strip the 32-char IV hex prefix
    iv_hex_len = 32
    ciphertext_hex = encoded_host[iv_hex_len:]
    hostname = decrypt_host(ciphertext_hex)

    original = f"{protocol}://{hostname}{port}"
    if rest:
        original += f"/{rest}"
    if parsed.query:
        original += f"?{parsed.query}"
    return original


class WebVPNSession:
    """Manages a WebVPN session with full IDP authentication."""

    def __init__(self, *, access_mode='webvpn'):
        if access_mode not in ('webvpn', 'direct'):
            raise ValueError('Invalid iCourse access mode')
        self.access_mode = access_mode
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.logged_in = False
        self.auth_diagnostics = []
        self.auth_phase = 'unknown'

    @property
    def requires_webvpn_login(self):
        return self.access_mode == 'webvpn'

    def route_url(self, url):
        return get_vpn_url(url) if self.requires_webvpn_login else url

    @property
    def portal_url(self):
        return (config.WEBVPN_BASE if self.requires_webvpn_login else config.ICOURSE_BASE)+'/'

    def _record_icourse_auth(self, stage, response, data=None, **flags):
        self.auth_diagnostics.append(auth_observation(stage,response,data,**flags))
        self.auth_diagnostics = self.auth_diagnostics[-32:]

    def _auth_json(self, response):
        try:
            return response.json()
        except requests.exceptions.JSONDecodeError as error:
            text = response.text[:100_000].strip().lower()
            kind = ('empty' if not text else 'html' if text.startswith('<')
                    else 'json' if text.startswith(('{', '[')) else 'other')
            observed = auth_observation('invalid_json', response)
            challenge = (observed['captcha_page_hint'] or observed['mfa_page_hint']
                or bool(re.search(r'"(?:needverifycode|needmfa|needotp)"\s*:\s*true', text))
                or any(hint in text for hint in ('one-time password', 'two-factor')))
            audit = authentication_failure(error, vpn=self)
            audit['response'] = safe_auth_response({
                'http_status': response.status_code, 'body_kind': kind,
                'challenge_hint': challenge,
                'redirected': bool(response.history)})
            error.auth_failure_diagnostics = audit
            raise

    def probe_login_service(self):
        """Reachability before sending credentials; HTTP 200 is not login proof."""
        from src.runtime.media_protocol import redirect_kind, redirect_observation
        self.auth_phase = 'login_service_probe'
        response = self.session.get(self.portal_url, allow_redirects=False,
                                    timeout=(5, 5))
        try:
            self._record_icourse_auth('login_service_probe', response)
            if response.status_code in (408, 429, 500, 502, 503, 504):
                raise AuthenticationError('service_unavailable')
            if response.status_code == 200:
                return
            if response.status_code in (301, 302, 303, 307, 308):
                observed = redirect_observation(response)
                if redirect_kind(response) == 'login' or (
                        observed.get('authority') == 'same_origin'
                        and observed.get('route') == 'vpn_control'
                        and not observed.get('downgrade')
                        and not observed.get('credential_authority')):
                    return
                raise AuthenticationError('service_redirect_untrusted')
            raise AuthenticationError('service_http_rejected')
        finally:
            response.close()

    def login(self, student_id: str = None, password: str = None) -> bool:
        """Execute the full 7-step IDP authentication flow.

        Returns True on success, raises on failure.
        """
        if not self.requires_webvpn_login:
            raise AuthenticationError('direct_mode_requires_icourse_auth')
        self.auth_phase = 'credentials'
        student_id = student_id or config.STUDENT_ID
        password = password or config.PASSWORD

        if not student_id or not password:
            raise ValueError(
                "Student ID and password are required. "
                "Set STUID and UISPsw environment variables."
            )

        print("[1/7] Getting authentication context...")
        lck, entity_id = self._get_auth_context()

        print("[2/7] Querying authentication methods...")
        auth_chain_code, request_type = self._query_auth_methods(lck, entity_id)

        print("[3/7] Getting RSA public key...")
        pub_key_pem = self._get_public_key()

        print("[4/7] Encrypting password...")
        self.auth_phase = 'webvpn_password_encrypt'
        encrypted_password = self._encrypt_password(password, pub_key_pem)

        print("[5/7] Executing authentication...")
        login_token = self._auth_execute(
            student_id,
            encrypted_password,
            lck,
            entity_id,
            auth_chain_code,
            request_type,
        )

        print("[6/7] Getting CAS ticket...")
        ticket_url = self._get_cas_ticket(login_token)

        print("[7/7] Establishing WebVPN session...")
        self._establish_session(ticket_url)

        self.logged_in = True
        print("[*] WebVPN login successful!")
        return True

    def authenticate_icourse(
        self, student_id: str = None, password: str = None, *, strict: bool = False
    ) -> bool:
        """Authenticate to iCourse via CAS/IDP through WebVPN.

        Mimics the browser flow:
        1. Access casapi login URL (like clicking "校内用户登录")
        2. Follow redirect to IDP authenticate (casapi generates correct
           service URL with forward param and r=auth/login)
        3. IDP auth steps through WebVPN
        4. Follow ticket back to iCourse through WebVPN

        strict=True requires successful final API verification. The default
        keeps the existing caller's fallback behavior for unavailable APIs.
        """
        student_id = student_id or config.STUDENT_ID
        password = password or config.PASSWORD

        route_label = "WebVPN" if self.requires_webvpn_login else "direct campus access"
        print(f"[*] Starting iCourse CAS authentication ({route_label})...")

        # Pre-flight: probe the WebVPN portal.  A cold session redirects to
        # /login instantly (status 302); a hot session returns 200.  Fail
        # fast on cold — login_with_retry() in main.py will re-login.
        self.auth_phase = 'icourse_portal_warmup'
        warmup = self.session.get(
            self.portal_url, allow_redirects=False, timeout=5,
        )
        self._record_icourse_auth('portal_warmup',warmup)
        if warmup.status_code != 200:
            raise AuthenticationError('cold_session')

        idp_vpn_base = self.route_url(config.IDP_BASE)

        # Step 1: Initiate CAS login via casapi.  Use allow_redirects=True
        # so requests follows the full redirect chain like a browser.
        print(f"[1/7] Initiating CAS login via casapi...")
        casapi_url = (
            f"{config.ICOURSE_BASE}/casapi/index.php"
            f"?r=auth/login&school_login=1"
            f"&tenant_code={config.TENANT_CODE}"
            f"&forward={quote(config.ICOURSE_BASE + '/', safe='')}"
        )
        vpn_url = self.route_url(casapi_url)

        self.auth_phase = 'icourse_cas_context'
        resp = self.session.get(vpn_url, allow_redirects=True, timeout=60)
        lck = None
        for source in [resp.url] + [
            h.headers.get("Location", "") for h in (resp.history or [])
        ]:
            m = re.search(r'lck=([^&#"]+)', source)
            if m:
                lck = m.group(1)
                break
        if not lck:
            m = re.search(r'lck=([^&#"]+)', resp.text[:5000])
            if m:
                lck = m.group(1)
        if not lck:
            self._record_icourse_auth('cas_context',resp,context_found=False)
            raise AuthenticationError('cas_context_missing')
        self._record_icourse_auth('cas_context',resp,context_found=True)

        entity_id = config.ICOURSE_BASE
        print("    lck: OK")

        # Step 2: Query auth methods (through WebVPN)
        print(f"[2/7] Querying auth methods ({route_label})...")
        url = self.route_url(f"{config.IDP_BASE}/idp/authn/queryAuthMethods")
        self.auth_phase = 'icourse_auth_methods'
        resp = self.session.post(
            url,
            json={"lck": lck, "entityId": entity_id},
            headers={
                "Content-Type": "application/json",
                "Referer": f"{idp_vpn_base}/ac/",
                "Origin": config.WEBVPN_BASE if self.requires_webvpn_login else config.IDP_BASE,
            },
            timeout=60,
        )
        self._record_icourse_auth('auth_methods_http',resp)
        data = self._auth_json(resp)
        auth_method_list = data.get("data", [])
        request_type = data.get("requestType", "chain_type")

        auth_chain_code = ""
        for method in auth_method_list:
            if method.get("moduleCode") == "userAndPwd":
                auth_chain_code = method.get("authChainCode", "")
                break
        if not auth_chain_code:
            self._record_icourse_auth('auth_methods',resp,data,password_method_found=False)
            raise AuthenticationError('password_method_missing')
        self._record_icourse_auth('auth_methods',resp,data,password_method_found=True)
        print("    authChainCode: OK")

        # Step 3: Get RSA public key (through WebVPN)
        print(f"[3/7] Getting RSA public key ({route_label})...")
        url = self.route_url(f"{config.IDP_BASE}/idp/authn/getJsPublicKey")
        self.auth_phase = 'icourse_public_key'
        resp = self.session.get(
            url,
            headers={"Referer": f"{idp_vpn_base}/ac/"},
            timeout=60,
        )
        self._record_icourse_auth('public_key_http',resp)
        data = self._auth_json(resp)
        pub_key_b64 = data.get("data", "")
        if not pub_key_b64:
            self._record_icourse_auth('public_key',resp,data,public_key_found=False)
            raise AuthenticationError('public_key_missing')
        self._record_icourse_auth('public_key',resp,data,public_key_found=True)
        print("    Got RSA public key")

        # Step 4: Encrypt password
        print(f"[4/7] Encrypting password...")
        self.auth_phase = 'icourse_password_encrypt'
        encrypted_password = self._encrypt_password(password, pub_key_b64)

        # Step 5: Execute authentication (through WebVPN)
        print(f"[5/7] Executing authentication ({route_label})...")
        url = self.route_url(f"{config.IDP_BASE}/idp/authn/authExecute")
        self.auth_phase = 'icourse_auth_execute'
        payload = {
            "authModuleCode": "userAndPwd",
            "authChainCode": auth_chain_code,
            "entityId": entity_id,
            "requestType": request_type,
            "lck": lck,
            "authPara": {
                "loginName": student_id,
                "password": encrypted_password,
                "verifyCode": "",
            },
        }
        resp = self.session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Referer": f"{idp_vpn_base}/ac/",
                "Origin": config.WEBVPN_BASE if self.requires_webvpn_login else config.IDP_BASE,
            },
            timeout=60,
        )
        self._record_icourse_auth('auth_execute_http',resp)
        data = self._auth_json(resp)
        self._record_icourse_auth('auth_execute',resp,data,
                                 login_token_found=bool(data.get('loginToken')))

        if str(data.get("code")) != "200":
            raise AuthenticationError('authentication_rejected')

        login_token = data.get("loginToken", "")
        if not login_token:
            raise AuthenticationError('login_token_missing')
        print("    loginToken: OK")

        # Step 6: Get CAS ticket (through WebVPN)
        print(f"[6/7] Getting CAS ticket ({route_label})...")
        url = self.route_url(f"{config.IDP_BASE}/idp/authCenter/authnEngine")
        self.auth_phase = 'icourse_cas_ticket'
        resp = self.session.post(
            url,
            data={"loginToken": login_token},
            headers={
                "Referer": f"{idp_vpn_base}/ac/",
                "Origin": config.WEBVPN_BASE if self.requires_webvpn_login else config.IDP_BASE,
            },
            timeout=60,
        )
        html = resp.text

        # Extract ticket URL from the authnEngine response
        # The URL may already be rewritten to a WebVPN URL by the proxy
        ticket_match = re.search(
            r'locationValue\s*=\s*"([^"]*ticket=[^"]*)"', html
        )
        if not ticket_match:
            ticket_match = re.search(
                r'(https?://[^\s"\'<>]*ticket=[^\s"\'<>]*)', html
            )
        if not ticket_match:
            self._record_icourse_auth('cas_ticket',resp,ticket_found=False)
            raise AuthenticationError('cas_ticket_missing')
        self._record_icourse_auth('cas_ticket',resp,ticket_found=True)

        ticket_url = html_mod.unescape(ticket_match.group(1))
        print("    Ticket extracted.")

        if not self.requires_webvpn_login:
            target, expected = urlsplit(ticket_url), urlsplit(config.ICOURSE_BASE)
            if (target.username or target.password
                    or (target.scheme, target.netloc) != (expected.scheme, expected.netloc)):
                raise AuthenticationError('ticket_destination_untrusted')

        # Step 7: Follow ticket to iCourse (through WebVPN)
        print(f"[7/7] Following ticket to iCourse ({route_label})...")
        if self.requires_webvpn_login and not ticket_url.startswith(config.WEBVPN_BASE):
            ticket_url = self.route_url(ticket_url)

        self.auth_phase = 'icourse_ticket_follow'
        resp = self.session.get(
            ticket_url, allow_redirects=True, timeout=90
        )
        self._record_icourse_auth('ticket_follow',resp)
        print(f"    Status: {resp.status_code}")

        # Verify by making a test API call
        test_url = self.route_url(
            f"{config.ICOURSE_BASE}/userapi/v1/infosimple"
        )
        self.auth_phase = 'icourse_api_verification'
        resp = self.session.get(test_url, timeout=60)
        self._record_icourse_auth('api_verification_http',resp)
        if resp.status_code == 200:
            try:
                user_data = resp.json()
                verified = str(user_data.get('code')) in ('0','200')
                self._record_icourse_auth('api_verification',resp,user_data,verified=verified)
                if verified:
                    self.logged_in = True
                    print("    Verified: login OK")
                    print(f"[*] iCourse authentication successful!")
                    return True
            except Exception:
                pass

        if strict: raise AuthenticationError('api_verification_failed')
        print("    Could not verify iCourse auth via API, proceeding...")
        return True

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET request through WebVPN. Converts URL automatically."""
        vpn_url = self.route_url(url)
        kwargs.setdefault("timeout", 60)
        return self.session.get(vpn_url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """POST request through WebVPN. Converts URL automatically."""
        vpn_url = self.route_url(url)
        kwargs.setdefault("timeout", 60)
        return self.session.post(vpn_url, **kwargs)

    def get_raw(self, url: str, **kwargs) -> requests.Response:
        """GET request without URL conversion (for already-converted URLs)."""
        kwargs.setdefault("timeout", 30)
        return self.session.get(url, **kwargs)

    def post_raw(self, url: str, **kwargs) -> requests.Response:
        """POST request without URL conversion."""
        kwargs.setdefault("timeout", 30)
        return self.session.post(url, **kwargs)

    # --- Private authentication steps ---

    def _get_auth_context(self) -> tuple[str, str]:
        """Step 1: GET authenticate endpoint, extract lck from redirect."""
        self.auth_phase = 'webvpn_context'
        service_url = f"{config.WEBVPN_BASE}/login?cas_login=true"
        url = (
            f"{config.IDP_BASE}/idp/authCenter/authenticate"
            f"?service={quote(service_url, safe='')}"
        )
        resp = self.session.get(url, allow_redirects=False, timeout=60)

        # Follow redirects manually to extract lck
        location = resp.headers.get("Location", "")
        while resp.status_code in (301, 302) and "lck=" not in location:
            resp = self.session.get(location, allow_redirects=False, timeout=60)
            location = resp.headers.get("Location", "")

        if resp.status_code in (301, 302):
            location = resp.headers.get("Location", "")

        # Extract lck parameter
        lck_match = re.search(r"[?&]lck=([^&]+)", location)
        self._record_icourse_auth('webvpn_context',resp,context_found=bool(lck_match))
        if not lck_match:
            raise AuthenticationError('cas_context_missing')

        lck = lck_match.group(1)
        entity_id = config.WEBVPN_BASE
        print("    lck: OK")
        return lck, entity_id

    def _query_auth_methods(
        self, lck: str, entity_id: str
    ) -> tuple[str, str]:
        """Step 2: Query available authentication methods."""
        self.auth_phase = 'webvpn_auth_methods'
        url = f"{config.IDP_BASE}/idp/authn/queryAuthMethods"
        resp = self.session.post(
            url,
            json={"lck": lck, "entityId": entity_id},
            headers={
                "Content-Type": "application/json",
                "Referer": f"{config.IDP_BASE}/ac/",
                "Origin": config.IDP_BASE,
            },
            timeout=60,
        )
        self._record_icourse_auth('webvpn_auth_methods_http',resp)
        data = self._auth_json(resp)
        # data["data"] is a list of auth methods; pick the userAndPwd one
        # authChainCode for userAndPwd is in the list items;
        # requestType is at the top level
        auth_method_list = data.get("data", [])
        request_type = data.get("requestType", "chain_type")

        auth_chain_code = ""
        for method in auth_method_list:
            if method.get("moduleCode") == "userAndPwd":
                auth_chain_code = method.get("authChainCode", "")
                break

        self._record_icourse_auth('webvpn_auth_methods',resp,data,password_method_found=bool(auth_chain_code))
        if not auth_chain_code:
            raise AuthenticationError('password_method_missing')

        print("    authChainCode: OK")
        return auth_chain_code, request_type

    def _get_public_key(self) -> str:
        """Step 3: Get RSA public key for password encryption."""
        self.auth_phase = 'webvpn_public_key'
        url = f"{config.IDP_BASE}/idp/authn/getJsPublicKey"
        resp = self.session.get(
            url,
            headers={
                "Referer": f"{config.IDP_BASE}/ac/",
            },
            timeout=60,
        )
        data = self._auth_json(resp)
        pub_key_b64 = data.get("data", "")
        self._record_icourse_auth('webvpn_public_key',resp,data,public_key_found=bool(pub_key_b64))
        if not pub_key_b64:
            raise AuthenticationError('public_key_missing')

        print("    Got RSA public key")
        return pub_key_b64

    def _encrypt_password(self, password: str, pub_key_b64: str) -> str:
        """Step 4: RSA-encrypt the password with PKCS1_v1_5."""
        # Construct PEM format
        pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            + pub_key_b64
            + "\n-----END PUBLIC KEY-----"
        )
        rsa_key = RSA.import_key(pem)
        cipher = PKCS1_v1_5.new(rsa_key)
        encrypted = cipher.encrypt(password.encode("utf-8"))
        return base64.b64encode(encrypted).decode("ascii")

    def _auth_execute(
        self,
        student_id: str,
        encrypted_password: str,
        lck: str,
        entity_id: str,
        auth_chain_code: str,
        request_type: str,
    ) -> str:
        """Step 5: Execute authentication and get loginToken."""
        self.auth_phase = 'webvpn_auth_execute'
        url = f"{config.IDP_BASE}/idp/authn/authExecute"
        payload = {
            "authModuleCode": "userAndPwd",
            "authChainCode": auth_chain_code,
            "entityId": entity_id,
            "requestType": request_type,
            "lck": lck,
            "authPara": {
                "loginName": student_id,
                "password": encrypted_password,
                "verifyCode": "",
            },
        }
        resp = self.session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Referer": f"{config.IDP_BASE}/ac/",
                "Origin": config.IDP_BASE,
            },
            timeout=60,
        )
        data = self._auth_json(resp)
        self._record_icourse_auth('webvpn_auth_execute',resp,data,
                                 login_token_found=bool(data.get('loginToken')))
        if str(data.get("code")) != "200":
            raise AuthenticationError('authentication_rejected')
        # loginToken is at top level, not nested under "data"
        login_token = data.get("loginToken", "")
        if not login_token:
            raise AuthenticationError('login_token_missing')

        print("    loginToken: OK")
        return login_token

    def _get_cas_ticket(self, login_token: str) -> str:
        """Step 6: Exchange loginToken for a CAS ticket URL."""
        self.auth_phase = 'webvpn_ticket'
        url = f"{config.IDP_BASE}/idp/authCenter/authnEngine"
        resp = self.session.post(
            url,
            data={"loginToken": login_token},
            headers={
                "Referer": f"{config.IDP_BASE}/ac/",
                "Origin": config.IDP_BASE,
            },
            timeout=60,
        )

        # The response is HTML containing a JS redirect with the ticket URL
        html = resp.text

        # Extract the locationValue from the JavaScript
        ticket_match = re.search(
            r'locationValue\s*=\s*"([^"]*ticket=[^"]*)"', html
        )
        if not ticket_match:
            # Fallback: any URL with ticket= parameter
            ticket_match = re.search(
                r'(https?://[^\s"\'<>]*ticket=[^\s"\'<>]*)', html
            )

        self._record_icourse_auth('webvpn_ticket',resp,ticket_found=bool(ticket_match))
        if not ticket_match:
            raise AuthenticationError('cas_ticket_missing')

        ticket_url = ticket_match.group(1)
        # Unescape HTML entities (e.g., &amp; -> &)
        ticket_url = html_mod.unescape(ticket_url)
        print("    Ticket extracted.")
        return ticket_url

    def _establish_session(self, ticket_url: str):
        """Follow a one-use CAS ticket once; recovery requires a fresh ticket."""
        self.auth_phase = 'webvpn_ticket_follow'
        try:
            resp = self.session.get(ticket_url, allow_redirects=True, timeout=60)
        except requests.exceptions.Timeout:
            has_ticket = any("wengine_vpn_ticket" in c.name for c in self.session.cookies)
            self.auth_diagnostics.append({'stage':'webvpn_ticket_follow',
                'transport_error':'timeout','attempt':1,'session_cookie_present':has_ticket})
            self.auth_diagnostics = self.auth_diagnostics[-32:]
            if has_ticket:
                self._verify_webvpn_session()
                print("    Session verified despite ticket request timeout.")
                return
            # CAS service tickets may already have been consumed even when
            # the response timed out. The caller must obtain a fresh ticket.
            raise
        self._record_icourse_auth('webvpn_ticket_follow',resp)
        if resp.status_code == 200:
            self._verify_webvpn_session()
            print("    Session established.")
            return
        raise AuthenticationError('cold_session')

    def _verify_webvpn_session(self):
        self.auth_phase = 'webvpn_session_probe'
        # A ticket endpoint may return a login page with HTTP 200. Neither
        # that status nor the mere presence of a stale cookie proves login.
        try:
            resp=self.session.get(config.WEBVPN_BASE+'/',allow_redirects=False,timeout=5)
        except requests.exceptions.Timeout:
            self.auth_diagnostics.append({'stage':'webvpn_session_probe','transport_error':'timeout'})
            self.auth_diagnostics=self.auth_diagnostics[-32:]
            raise
        self._record_icourse_auth('webvpn_session_probe',resp)
        if resp.status_code != 200: raise AuthenticationError('cold_session')
