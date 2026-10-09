"""
iCourse API client for Fudan University's smart teaching platform.

Provides access to course details, lecture lists, video URLs,
and video downloads through WebVPN.
"""

import copy
import threading
import hashlib
import os
import re
import time
import uuid
import requests
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

from src.runtime import config
from src.api.webvpn import WebVPNSession, get_vpn_url


_DATE_FROM_SUB_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _extract_date_from_sub(sub_title: str) -> str | None:
    """Extract YYYY-MM-DD from a sub_title like "2026-03-05第6-8节"."""
    if not sub_title:
        return None
    m = _DATE_FROM_SUB_RE.match(sub_title)
    return m.group(1) if m else None

def fetch_ppt_image(client: "ICourseClient", item: dict,
                    max_attempts: int = 2, timeout: int = 30) -> bytes | None:
    """Download a single PPT image. Returns bytes or None on persistent failure.

    Module-level (not a method on ICourseClient) so worker threads in the
    scheduler can call it without binding the function name at import time —
    that way tests can monkey-patch ``src.icourse.fetch_ppt_image`` and the
    scheduler will pick up the replacement on its next worker invocation.
    """
    url = item["pptimgurl"]
    for attempt in range(1, max_attempts + 1):
        try:
            if url.startswith(config.WEBVPN_BASE):
                resp = client.vpn.get_raw(url, timeout=timeout)
            else:
                resp = client.vpn.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            print(f"[PPTFetcher] download failed (attempt "
                  f"{attempt}/{max_attempts}): {type(e).__name__}")
            if attempt < max_attempts:
                time.sleep(1)
    return None


class ICourseClient:
    """Client for the iCourse API, operating through WebVPN."""

    def __init__(self, vpn_session: WebVPNSession, *, media_reauth_factory=None):
        self.vpn = vpn_session
        self.base_url = config.ICOURSE_BASE
        self._userinfo = None
        self._media_reauth_factory = media_reauth_factory
        self._media_owned_session = None
        self.media_auth_audit = {}
        self._video_lookup_audits = {}
        self._video_lookup_lock = threading.Lock()

    def video_lookup_diagnostics(self, course_id, sub_id):
        with self._video_lookup_lock:
            return copy.deepcopy(self._video_lookup_audits.get((str(course_id), str(sub_id)), {}))

    def _record_video_lookup(self, course_id, sub_id, audit):
        with self._video_lookup_lock:
            self._video_lookup_audits[(str(course_id), str(sub_id))] = copy.deepcopy(audit)
            while len(self._video_lookup_audits) > 128:
                self._video_lookup_audits.pop(next(iter(self._video_lookup_audits)))

    def get_userinfo(self) -> dict:
        """Get current user info (id, tenant_id, phone, account).

        Caches the result for the session.
        """
        if self._userinfo is not None:
            return self._userinfo

        url = f"{self.base_url}/userapi/v1/infosimple"
        resp = self.vpn.get(url)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") not in (0, 200):
            raise RuntimeError(f"Failed to get userinfo: {data.get('msg')}")

        self._userinfo = data.get("params") or data.get("data", {})
        return self._userinfo

    def check_alive(self) -> bool:
        """Quick session health check (non-cached)."""
        try:
            resp = self.vpn.get(
                f"{self.base_url}/userapi/v1/infosimple", timeout=10
            )
            return resp.status_code == 200 and resp.json().get("code") in (0, 200)
        except Exception:
            return False

    def fork_for_media(self):
        # Authentication recovery belongs to this download, not concurrent PPT
        # requests or another lecture sharing the original client.
        value = copy.copy(self)
        value._userinfo = dict(self.get_userinfo())
        value._media_owned_session = None
        value.media_auth_audit = {}
        return value

    def close_media_session(self):
        if self._media_owned_session is not None:
            self._media_owned_session.close()
            self._media_owned_session = None

    def _verified_media_user(self, vpn, *, audit=None):
        audit = self.media_auth_audit if audit is None else audit
        response = vpn.get(f'{self.base_url}/userapi/v1/infosimple',
                           allow_redirects=False, timeout=10)
        try:
            audit['api_status'] = response.status_code
            if response.status_code != 200: return None
            payload = response.json()
            if payload.get('code') not in (0, 200): return None
            user = payload.get('params') or payload.get('data')
            if not isinstance(user, dict) or not user.get('id'): return None
            for key in ('id', 'tenant_id'):
                if (self._userinfo and self._userinfo.get(key) is not None
                        and str(self._userinfo[key]) != str(user.get(key))):
                    audit['failure'] = 'identity_mismatch'
                    return None
            return user
        finally:
            response.close()

    def refresh_media_session(self) -> bool:
        """Probe existing SSO only. Password recovery is separately opt-in."""
        self.media_auth_audit = {'stage': 'existing_session'}
        try:
            portal = config.ICOURSE_BASE if getattr(self.vpn, 'access_mode', None) == 'direct' else config.WEBVPN_BASE
            response = self.vpn.session.get(portal+'/', allow_redirects=False, timeout=5)
            try:
                self.media_auth_audit['portal_status'] = response.status_code
                if response.status_code != 200:
                    self.media_auth_audit['failure'] = 'portal_unavailable'
                    return False
            finally:
                response.close()
            user = self._verified_media_user(self.vpn)
            if user is None:
                self.media_auth_audit.setdefault('failure', 'api_verification_failed')
                return False
            self._userinfo = user
            return True
        except requests.exceptions.SSLError:
            self.media_auth_audit['failure'] = 'auth_tls_error'
            return False
        except Exception:
            self.media_auth_audit['failure'] = 'existing_session_exception'
            return False

    def reauthenticate_media_session(self, stopped, *, timeout=75):
        """Bounded fresh login; rejected/late sessions are never adopted."""
        if (self._media_reauth_factory is None or stopped.is_set()
                or self.media_auth_audit.get('failure') in ('identity_mismatch', 'auth_tls_error')):
            return False
        cancelled = threading.Event()
        deadline = time.monotonic()+timeout
        ready = threading.Event()
        lock = threading.Lock()
        result = {}
        previous_failure = self.media_auth_audit.pop('failure', None)
        if previous_failure: self.media_auth_audit['existing_failure'] = previous_failure
        self.media_auth_audit.update(stage='fresh_session', reauth_attempts=1,
                                     reauth_successes=0)
        def authenticate():
            candidate = None
            phase = 'fresh_session_factory'
            audit = {}
            try:
                candidate = self._media_reauth_factory(cancelled, deadline)
                if cancelled.is_set() or stopped.is_set() or time.monotonic() >= deadline: return
                phase = 'media_identity_verification'
                for key, attribute in (('probe_attempts','auth_probe_attempts'),
                                       ('probe_transient_failures','auth_probe_transient_failures')):
                    value = getattr(candidate,attribute,None)
                    if type(value) is int and 0 <= value <= 3: audit[key] = value
                user = self._verified_media_user(candidate, audit=audit)
                if user is None:
                    audit.setdefault('failure','api_verification_failed')
                    audit['failure_phase'] = phase
                if user is None: return
                with lock:
                    if not cancelled.is_set() and not stopped.is_set() and time.monotonic() < deadline:
                        result.update(vpn=candidate, user=user)
                        candidate = None
            except Exception as error:
                from src.api.webvpn import authentication_failure
                audit.update(authentication_failure(error, phase))
            finally:
                if candidate is not None: candidate.session.close()
                with lock:
                    # An abandoned auth worker cannot mutate the public audit
                    # after the caller has published timeout/cancellation.
                    if not cancelled.is_set() and not stopped.is_set() and time.monotonic() < deadline:
                        result['audit'] = audit
                ready.set()
        threading.Thread(target=authenticate, daemon=True).start()
        while not ready.wait(.05):
            if stopped.is_set() or time.monotonic() >= deadline: break
        with lock:
            cancelled.set()
            self.media_auth_audit.update(result.get('audit',{}))
            candidate = result.get('vpn')
            if candidate is None or stopped.is_set() or time.monotonic() >= deadline:
                if candidate is not None: candidate.session.close()
                self.media_auth_audit.setdefault('failure',
                    'media_auth_cancelled' if stopped.is_set() else
                    'media_auth_deadline' if time.monotonic() >= deadline else 'fresh_session_unavailable')
                self.media_auth_audit.setdefault('failure_phase','fresh_session_factory')
                return False
            # Session stays usable after recovery; keep its cancellation separate
            # from the finished auth wait and retain a lifetime request deadline.
            if hasattr(candidate.session, 'cancelled'):
                candidate.session.cancelled = stopped
                candidate.session.deadline = float('inf')
            self.vpn = candidate
            self._userinfo = result['user']
            self._media_owned_session = candidate.session
            self.media_auth_audit.pop('failure', None)
            self.media_auth_audit.update(stage='verified', reauth_successes=1)
            return True

    def trusted_media_login_urls(self) -> tuple[str, ...]:
        """Exact WebVPN SSO routes; no request or credential access here."""
        from src.runtime.media_protocol import LOGIN_PATHS
        route = (lambda url: url) if getattr(self.vpn, 'access_mode', None) == 'direct' else get_vpn_url
        return tuple(route(config.IDP_BASE + path) for path in LOGIN_PATHS
                     if not path.startswith('/wengine-vpn/'))

    def sign_video_url(
        self, video_url: str, now: int | None = None
    ) -> str:
        """Sign a video URL with CDN authentication parameters.

        Adds clientUUID and t parameters required for video download.
        The t parameter format: {user_id}-{timestamp}-{md5_hash}
        where md5_hash = md5(pathname + user_id + tenant_id + reversed_phone + timestamp)
        """
        userinfo = self.get_userinfo()
        user_id = userinfo.get("id", "")
        tenant_id = userinfo.get("tenant_id", "")
        phone = str(userinfo.get("phone", ""))

        if now is None:
            now = int(time.time())

        reversed_phone = phone[::-1]
        pathname = urlparse(video_url).path

        hash_input = f"{pathname}{user_id}{tenant_id}{reversed_phone}{now}"
        md5_hash = hashlib.md5(hash_input.encode()).hexdigest()
        t_param = f"{user_id}-{now}-{md5_hash}"

        client_uuid = str(uuid.uuid4())
        sep = "&" if "?" in video_url else "?"
        return f"{video_url}{sep}clientUUID={client_uuid}&t={t_param}"

    def renew_video_url(self, video_url: str, now: int | None = None) -> str:
        """Renew both authentication fields without reselecting the lesson."""
        parts = urlsplit(video_url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if (parts.scheme not in ('http','https') or not parts.netloc
                or sum(k == 't' for k,_ in query) != 1
                or sum(k == 'clientUUID' for k,_ in query) != 1):
            raise ValueError('Invalid existing media signature')
        base = urlunsplit(parts._replace(query=urlencode([(k,v) for k,v in query
                                                        if k not in ('t','clientUUID')])))
        return self.sign_video_url(base, now=now)

    def get_course_detail(self, course_id: str) -> dict:
        """Get course details including title, teacher, and lecture list.

        Returns dict with keys: title, teacher, lectures
        Each lecture has: sub_id, sub_title, lecturer_name, date, has_playback
        """
        url = f"{self.base_url}/courseapi/v3/multi-search/get-course-detail"
        resp = self.vpn.get(url, params={"course_id": course_id})
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(
                f"API error for course {course_id}: {data.get('msg')}"
            )

        course_data = data.get("data", {})
        title = course_data.get("title", "Unknown")
        teacher = course_data.get("realname", "Unknown")

        # Parse the nested sub_list: {year: {month: {day: [items]}}}
        lectures = []
        sub_list = course_data.get("sub_list", {})
        for year, months in sub_list.items():
            for month, days in months.items():
                for day, items in days.items():
                    for item in items:
                        if "id" in item:
                            sub_title = item.get("sub_title", "")
                            # Real lecture date is embedded in sub_title
                            # ("2026-03-05第6-8节" → "2026-03-05"); fall back
                            # to the server's year/month/day keys if missing.
                            # Zero-pad the fallback so SQLite ORDER BY works.
                            date = (
                                _extract_date_from_sub(sub_title)
                                or f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                            )
                            lectures.append(
                                {
                                    "sub_id": item["id"],
                                    "sub_title": sub_title,
                                    "lecturer_name": item.get(
                                        "lecturer_name", ""
                                    ),
                                    "date": date,
                                    "has_playback": str(item.get("playback_status")) == "1",
                                }
                            )

        return {"title": title, "teacher": teacher, "lectures": lectures}

    def get_ppt_list(self, course_id: str, sub_id: str,
                     per_page: int = 100) -> list[dict]:
        """Fetch PPT screenshot list for a lecture.

        Walks pagination until exhausted. Returns a flat list of items, each:
            {
              "id": int,                # row id
              "pptimgurl": str,         # full image URL (used for OCR)
              "pptthumb": str,          # thumbnail URL (kept for reference)
              "created_sec": int,       # offset within lecture, in seconds
              "created_ms": int,        # original epoch ms timestamp
              "taskid": str,
            }
        Sorted by created_sec ascending.
        """
        import json
        items = []
        page = 1
        while True:
            url = f"{self.base_url}/pptnote/v1/schedule/search-ppt"
            resp = self.vpn.get(
                url,
                params={
                    "course_id": course_id, "sub_id": sub_id,
                    "page": page, "per_page": per_page,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"search-ppt failed: {data.get('msg')}")
            page_items = data.get("list", [])
            if not page_items:
                break
            for raw in page_items:
                try:
                    content = json.loads(raw.get("content", "{}"))
                except (ValueError, TypeError):
                    continue
                img_url = content.get("pptimgurl")
                if not img_url:
                    continue
                items.append({
                    "id": raw.get("id"),
                    "pptimgurl": img_url,
                    "pptthumb": content.get("pptthumb", ""),
                    "created_sec": int(raw.get("created_sec", 0) or 0),
                    "created_ms": int(content.get("created", 0) or 0),
                    "taskid": content.get("taskid", ""),
                })
            if len(page_items) < per_page:
                break
            page += 1
        items.sort(key=lambda x: x["created_sec"])
        return items

    def get_course_list(
        self, term: str = "24", page: int = 1, per_page: int = 20
    ) -> dict:
        """Get a paginated list of courses for a given term.

        Returns dict with keys: total, courses (list of course dicts).
        Empty-string filter params are omitted so the API returns all
        courses rather than searching for "".
        """
        url = f"{self.base_url}/portal/courseapi/v3/multi-search/get-course-list"
        # Omitting empty-string params matters — some backends treat
        # ``title=""`` as "search for nothing" rather than "no filter".
        params: dict[str, str | int] = {
            "tenant": config.TENANT_CODE,
            "term": term,
            "page": page,
            "per_page": per_page,
        }
        for key in ("title", "kkxy_code", "course_type", "course_student_type"):
            val = getattr(config, key.upper(), "") if key.isupper() else ""
            if not val:
                continue
            params[key] = val
        resp = self.vpn.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"API error: {data.get('msg')}")

        result = data.get("data", {})
        return {
            "total": int(result.get("total", 0)),
            "courses": result.get("list", []),
        }

    def discover_terms(self, code_min: int = 10,
                       code_max: int = 35) -> list[dict]:
        """Scan term codes to discover all available semesters.

        Returns ``[{code, name, count}]`` sorted by code descending
        (newest first).  Only codes returning >0 courses are included.
        """
        results: list[dict] = []
        for code in range(code_min, code_max + 1):
            try:
                resp = self.get_course_list(
                    term=str(code), page=1, per_page=1,
                )
                total = resp.get("total", 0)
                if not total:
                    continue
                courses = resp.get("courses", [])
                name = (courses[0].get("term_name") if courses else None) or str(code)
                results.append({"code": str(code), "name": name,
                                "count": total})
            except Exception:
                continue
        return sorted(results, key=lambda x: -int(x["code"]))

    def list_semester_courses(self, term: str,
                              per_page: int = 500) -> list[dict]:
        """Walk every page of get-course-list for ``term``.

        Uses ``total`` from the first response to compute the exact page
        count — no hard-coded max.  (Caller must ensure the API hasn't
        silently capped ``per_page`` below the requested value.)

        Returns a flat list of ``{course_id, title, teacher, dept}`` dicts,
        deduped by course_id.
        """
        import math

        out: list[dict] = []
        seen: set[str] = set()

        # Page 1 — discover total
        result = self.get_course_list(
            term=term, page=1, per_page=per_page,
        )
        total_expected = result.get("total") or 0
        if not total_expected:
            return out
        total_pages = max(1, math.ceil(total_expected / per_page))

        def _process(page_items):
            for raw in page_items:
                cid = raw.get("id") or raw.get("course_id")
                if not cid:
                    continue
                cid = str(cid)
                if cid in seen:
                    continue
                seen.add(cid)
                dept = (
                    raw.get("kkxy_name") or raw.get("school_name")
                    or raw.get("dept_name") or raw.get("kkxy") or None
                )
                out.append({
                    "course_id": cid,
                    "title": raw.get("title") or "",
                    "teacher": raw.get("realname") or raw.get("teacher") or "",
                    "dept": dept,
                })

        page_items = result.get("courses", [])
        if not page_items:
            return out
        _process(page_items)

        # Remaining pages 2 .. total_pages
        for page in range(2, total_pages + 1):
            result = self.get_course_list(
                term=term, page=page, per_page=per_page,
            )
            page_items = result.get("courses", [])
            if not page_items:
                break
            _process(page_items)

        return out

    def get_lecture_detail(self, course_id: str, sub_id: str) -> dict:
        """Get details for a specific lecture, including video URL info.

        The video URL is typically embedded in the course detail's sub_list
        items. This method retrieves the full course detail and finds the
        matching lecture by sub_id.
        """
        detail = self.get_course_detail(course_id)
        for lecture in detail["lectures"]:
            if str(lecture["sub_id"]) == str(sub_id):
                return lecture
        raise ValueError(
            f"Lecture {sub_id} not found in course {course_id}"
        )

    def get_transcript(self, sub_id: str) -> str | None:
        """Get the transcript text for a lecture (flat string).

        Returns the full transcript text, empty string if no transcript,
        or None on error.
        """
        segments = self.get_transcript_segments(sub_id)
        if segments is None:
            return None
        if not segments:
            return ""
        return " ".join(s["text"] for s in segments if s["text"])

    def get_transcript_segments(self, sub_id: str) -> list[dict] | None:
        """Get transcript as timed segments.  Returns None on API error,
        empty list if no transcript exists.

        Each segment: {"start_ms": int, "end_ms": int, "text": str}
        Sorted by start_ms ascending.
        """
        url = f"{self.base_url}/courseapi/v3/web-socket/search-trans-result"
        resp = self.vpn.get(
            url, params={"sub_id": sub_id, "format": "json"}
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            return None

        result_list = data.get("list", [])
        if not result_list:
            return []

        all_content = result_list[0].get("all_content", [])
        if not all_content:
            return []

        return sorted(
            (
                {
                    "start_ms": int(seg.get("BeginSec", 0)) * 1000,
                    "end_ms": int(seg.get("EndSec", seg.get("BeginSec", 0))) * 1000,
                    "text": seg.get("Text", ""),
                }
                for seg in all_content
                if seg.get("Text", "").strip()
            ),
            key=lambda s: s["start_ms"],
        )

    def get_sub_detail(self, course_id: str, sub_id: str) -> dict:
        """Get detailed info for a specific lecture (unsigned URL).

        Returns the full sub-detail data from the API.
        Note: The video URL returned here is NOT signed for CDN auth.
        Use get_sub_info() instead for a signed/downloadable URL.
        """
        url = f"{self.base_url}/courseapi/v3/multi-search/get-sub-detail"
        from src.api.playback_diagnostics import playback_json
        data = playback_json(self.vpn, url, {"course_id":course_id, "sub_id":sub_id})

        if data.get("code") != 0:
            error = RuntimeError(
                f"API error for sub {sub_id}: {data.get('msg')}"
            )
            error.playback_api_code = data.get('code')
            raise error

        payload = data.get("data") or {}
        if not isinstance(payload, dict): raise ValueError('Invalid playback payload')
        return payload

    def get_sub_info(self, course_id: str, sub_id: str) -> dict:
        """Get lecture info including video URLs and timestamp.

        Returns the data payload from the API.

        Non-zero API codes that still ship a populated data payload
        (notably 7001 "视频未到开放时间", the school's 24h pre-release
        review gate) are returned as partial data so the caller can
        extract the video URL from nested content.playback.url — the
        gate scrubs top-level video_list/playurl but not the nested
        URL.  Only raises on HTTP failure or an entirely empty payload.
        """
        url = (
            f"{self.base_url}"
            f"/courseapi/v3/portal-home-setting/get-sub-info"
        )
        from src.api.playback_diagnostics import playback_json
        data = playback_json(self.vpn, url, {"course_id":course_id, "sub_id":sub_id})
        payload = data.get("data") or {}
        if not isinstance(payload, dict): raise ValueError('Invalid playback payload')

        if data.get("code") != 0 and not payload:
            error = RuntimeError(
                f"API error for sub-info {sub_id}: {data.get('msg')}"
            )
            error.playback_api_code = data.get('code')
            raise error

        return payload

    def get_video_url(self, course_id: str, sub_id: str) -> str | None:
        """Get a signed MP4 video URL for a specific lecture.

        Cascades through URL sources, most- to least-preferred:
          1. info.video_list[*].preview_url     — healthy lecture
          2. info.playurl[*]                    — healthy alternate
          3. info.content.playback.url          — review-gated (no extra call)
          4. get-sub-detail content.playback.url — last resort

        Sources 3 and 4 cover the school's pre-release review gate
        (sub-info code 7001 "视频未到开放时间"), which scrubs top-level
        video_list/playurl but leaves the URL in nested fields.  The
        CDN itself does not enforce the gate, so a signed URL from
        either source downloads successfully.

        Returns the signed video URL string, or None if no source yields one.
        """
        from src.api.playback_diagnostics import lookup_error, load_source
        audit = {'sources': [], 'url_found': False}
        self._record_video_lookup(course_id, sub_id, audit)
        info, row = load_source(lambda:self.get_sub_info(course_id, sub_id), 'sub_info')
        audit['sources'].append(row)
        self._record_video_lookup(course_id, sub_id, audit)

        # Get server timestamp for signing
        now = info.get("now")
        if isinstance(now, str):
            now = int(now)

        # Extract base video URL from playurl dict or video_list
        base_url = None

        # Try video_list first (has preview_url without /0/ prefix)
        video_list = info.get("video_list", {})
        if isinstance(video_list, dict):
            for _, v in video_list.items():
                if isinstance(v, dict):
                    preview = v.get("preview_url")
                    if preview and preview.endswith(".mp4"):
                        base_url = preview
                        break

        # Fallback: try playurl dict (has /0/ prefix, may need stripping)
        if not base_url:
            playurl = info.get("playurl", {})
            if isinstance(playurl, dict):
                for k, v in playurl.items():
                    if k == "now":
                        continue
                    if isinstance(v, str) and v.endswith(".mp4"):
                        base_url = v
                        break

        # Review-gate fallback: nested content.playback.url is preserved
        # even when code == 7001 scrubs the top-level fields above.
        if not base_url:
            playback = (info.get("content") or {}).get("playback") or {}
            nested = playback.get("url")
            if isinstance(nested, str) and nested.endswith(".mp4"):
                base_url = nested
                if not now:
                    content_now = (info.get("content") or {}).get("now")
                    if isinstance(content_now, (int, str)):
                        now = int(content_now)

        # Last resort: hit get-sub-detail (gate-free) directly.
        if not base_url:
            detail, row = load_source(lambda:self.get_sub_detail(course_id, sub_id), 'sub_detail')
            audit['sources'].append(row)
            try:
                content = detail.get("content", {})
                playback = content.get("playback", {})
                if playback and playback.get("url"):
                    base_url = playback["url"]
            except Exception as error:
                audit['sources'][-1] = {'source':'sub_detail', **lookup_error(error)}

        audit['url_found'] = bool(base_url)
        self._record_video_lookup(course_id, sub_id, audit)
        if not base_url:
            print("    No video URL found (tried all configured sources)")
            return None

        try:
            return self.sign_video_url(base_url, now=now)
        except Exception as error:
            audit['sources'].append({'source': 'signing', **lookup_error(error)})
            self._record_video_lookup(course_id, sub_id, audit)
            raise

    def get_stream_params(self, video_url: str) -> tuple[str, str]:
        """Get WebVPN URL and HTTP headers for direct streaming (e.g., ffmpeg).

        Returns:
            (vpn_url, http_headers) where http_headers is ffmpeg-compatible.
        """
        vpn_url = video_url if getattr(self.vpn, 'access_mode', None) == 'direct' else get_vpn_url(video_url)
        if getattr(self.vpn, 'access_mode', None) == 'direct':
            cookies = self.vpn.session.prepare_request(requests.Request('GET', video_url)).headers.get('Cookie', '')
        else:
            cookies = "; ".join(f"{c.name}={c.value}" for c in self.vpn.session.cookies)
        headers = f"Cookie: {cookies}\r\nUser-Agent: {config.USER_AGENT}\r\n"
        return vpn_url, headers

    def download_video(
        self,
        video_url: str,
        output_path: str,
        chunk_size: int = 8192,
    ) -> str:
        """Download a video file from the given URL.

        If video_url is a WebVPN URL, uses get_raw; otherwise uses get.
        Returns the output file path.
        """
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        tmp_path = output_path + ".tmp"
        t0 = time.time()

        if video_url.startswith(config.WEBVPN_BASE):
            resp = self.vpn.get_raw(video_url, stream=True, timeout=300)
        else:
            resp = self.vpn.get(video_url, stream=True, timeout=300)

        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0

        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    print(
                        f"\r    Downloading: {pct}% "
                        f"({downloaded // 1024 // 1024}MB/"
                        f"{total // 1024 // 1024}MB)",
                        end="",
                        flush=True,
                    )

        print()  # newline after progress

        if total and downloaded < total:
            os.remove(tmp_path)
            raise RuntimeError(
                f"Incomplete download: got {downloaded} of {total} bytes"
            )

        os.replace(tmp_path, output_path)
        elapsed = time.time() - t0
        size_mb = downloaded / (1024 * 1024)
        print(f"    Downloaded: {size_mb:.1f}MB in {elapsed:.0f}s")
        return output_path
