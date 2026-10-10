"""Bounded, native-resolution board OCR with delayed frame coverage."""
import io
import math
from pathlib import Path
import subprocess
import tempfile

from src.ai.homework_review import MAX_FOCUS, nearby_pages
from src.ai.homework_visual_evidence import assess_visual, candidate_key, references

MAX_VIDEO_FRAMES = 48
FRAMES_PER_CUE = 12


def visual_window(candidate, interval, audio_seconds=None, *, delay_seconds=180):
    if interval:
        start = max(0, interval['quote_start_ms']/1000-10)
        end = interval['quote_end_ms']/1000+delay_seconds
    else:
        # A rejected word alignment must not disable board reading. The ASR
        # block is only a coarse search range, never a precise quote timestamp.
        start, block_end = candidate.get('block_start'), candidate.get('block_end')
        if (any(type(t) not in (int, float) or not math.isfinite(t)
                for t in (start, block_end)) or start < 0 or block_end <= start):
            return None
        end = block_end+delay_seconds
    limit = audio_seconds if isinstance(audio_seconds, (int, float)) and math.isfinite(audio_seconds) else candidate['block_end']
    end = min(end, limit)
    if end <= start:
        return None
    window = {'start_ms': round(start*1000), 'end_ms': round(end*1000)}
    block_end = candidate.get('block_end')
    if (limit >= 600 and isinstance(audio_seconds, (int, float))
            and math.isfinite(audio_seconds) and type(block_end) in (int, float)
            and math.isfinite(block_end) and 0 <= limit-block_end <= 300):
        # Late instructions can precede the completed board. Reserve samples
        # near the lecture end within the existing per-cue image budget.
        window.update(end_ms=round(limit*1000), includes_lecture_end=True)
    return window


def frame_times(interval, window, count=FRAMES_PER_CUE):
    start, end = window['start_ms']/1000, window['end_ms']/1000
    last = max(start, end-.1)
    if interval:
        anchor_end = interval['quote_end_ms']/1000
        offsets = [20, 40, 60, 90] if count == 6 else [10, 20, 35, 50, 70, 90, 110, 130, 155, 180]
        samples = [start, anchor_end]+[anchor_end+offset for offset in offsets]
    else:
        samples = [start+(last-start)*i/(count-1) for i in range(count)]
    if window.get('includes_lecture_end'):
        samples = samples[:count-2]+[max(start, end-30), last]
    times = {round(min(max(start, t), last), 3) for t in samples}
    return sorted(times)


def image_views(image):
    from PIL import Image
    with Image.open(io.BytesIO(image)) as source:
        source.load(); original = source.convert('RGB')
    w, h = original.size
    # Overlapping halves cover either board side without claiming a fixed
    # region is the blackboard. Keep native full-frame pixels available.
    views = [('full', original)]
    if w >= 640 and h >= 360:
        for name, box in [('board_left', (0, 0, math.ceil(w*2/3), h)),
                          ('board_right', (math.floor(w/3), 0, w, h))]:
            crop = original.crop(box)
            factor = max(1, min(2, 2560/max(crop.size)))
            if factor > 1:
                crop = crop.resize(tuple(round(s*factor) for s in crop.size), Image.Resampling.LANCZOS)
            views.append((name, crop))
    result = []
    for name, view in views:
        buf = io.BytesIO(); view.save(buf, format='PNG')
        result.append((name, buf.getvalue(), view.size))
    return result


def read_frame(image, ocr):
    if not image:
        return {'status': 'capture_failed', 'text': '', 'references': [], 'views': []}
    try:
        views = image_views(image)
    except Exception:
        return {'status': 'image_decode_failed', 'text': '', 'references': [], 'views': []}
    texts, refs, audits = [], {}, []
    for name, data, size in views:
        try:
            blocks = ocr(data)
            # Legacy string OCR stays visible but cannot establish confidence.
            if isinstance(blocks, str):
                blocks = [{'text': blocks, 'confidence': None}] if blocks else []
            view_text = []
            for block in blocks:
                text = block.get('text', '') if isinstance(block, dict) else block.text
                score = block.get('confidence') if isinstance(block, dict) else block.confidence
                if not text.strip():
                    continue
                view_text.append(text)
                if isinstance(score, (int, float)) and math.isfinite(score):
                    for ref in references(text):
                        refs[ref] = max(refs.get(ref, 0), score)
            texts.extend(view_text)
            audits.append({'region': name, 'size': list(size), 'status': 'ok' if view_text else 'no_text'})
        except Exception as error:
            audits.append({'region': name, 'size': list(size), 'status': 'ocr_failed', 'error_type': type(error).__name__})
    text = '\n'.join(dict.fromkeys(texts))[:4000]
    return {'status': 'ok' if text else 'ocr_failed' if any(v['status'] == 'ocr_failed' for v in audits) else 'no_text',
            'text': text, 'references': [{'text': t, 'confidence': s} for t, s in refs.items()], 'views': audits}


def collect_visual_evidence(client, course_id, sub_id, candidates, intervals, *, audio_seconds=None,
                            screenshot_fetcher=None, ocr=None, frame_observer=None, vision_reader=None,
                            frames_per_cue=FRAMES_PER_CUE, delay_seconds=180):
    if frames_per_cue not in (6, 12) or delay_seconds not in (90, 180):
        raise ValueError('Unsupported bounded visual profile')
    if screenshot_fetcher is None:
        from src.api.icourse import fetch_ppt_image
        screenshot_fetcher = fetch_ppt_image
    if ocr is None:
        # A successful direct image read never initializes the local OCR engine.
        def ocr(image):
            from src.ai.ocr import ocr_image_strict
            return ocr_image_strict(image)
    try:
        pages = client.get_ppt_list(course_id, sub_id)
    except Exception:
        pages = []
    results, seen, windows, ids = [], set(), [], []
    video_count = 0
    for candidate in candidates[:MAX_FOCUS]:
        cid = candidate_key(candidate['id'], candidate['quote']); ids.append(cid)
        interval = next((row for row in intervals if row.get('chunk_id') == candidate['id']
                         and row.get('text') == candidate['quote']), None)
        window = visual_window(candidate, interval, audio_seconds, delay_seconds=delay_seconds)
        windows.append({'candidate_id': cid, 'range': window, 'aligned': bool(interval),
                        'anchor': 'forced_alignment' if interval else 'asr_block'})
        shots = nearby_pages(pages, window or candidate)
        if isinstance(audio_seconds, (int, float)) and math.isfinite(audio_seconds):
            shots = [shot for shot in shots if 0 <= shot['created_sec'] <= audio_seconds]
        captured = []
        for shot in shots:
            identity = (cid, shot.get('id'), shot['created_sec'])
            if identity in seen:
                continue
            seen.add(identity)
            image, error = None, None
            try:
                image = screenshot_fetcher(client, shot, max_attempts=1, timeout=15)
            except Exception as exc:
                error = type(exc).__name__
            captured.append({'image': image, 'source': 'platform_screenshot',
                             'seconds': shot['created_sec'], 'candidate_id': cid, 'capture_error_code': error})
        if window:
            for seconds in frame_times(interval, window, frames_per_cue)[:frames_per_cue]:
                if video_count >= MAX_VIDEO_FRAMES:
                    break
                video_count += 1
                params = None
                try:
                    url = client.get_video_url(course_id, sub_id)  # unchanged fallback chain; fresh signature per seek
                    if url:
                        params = client.get_stream_params(url)
                except Exception:
                    pass
                response = video_frame(params, seconds, diagnostic=True) if params else None
                image = response.get('image') if isinstance(response, dict) else response
                error = response.get('error_code') if isinstance(response, dict) else None
                captured.append({'image': image, 'source': 'video_frame', 'seconds': seconds,
                                 'candidate_id': cid, 'capture_error_code': error})
        valid = [row for row in captured if row['image']]
        readings, vision_error = None, None
        if callable(vision_reader) and valid:
            try:
                readings = vision_reader(valid)
                if not isinstance(readings, list) or len(readings) != len(valid):
                    raise ValueError('Incomplete image readings')
            except Exception as error:
                readings = None; vision_error = type(error).__name__
        index = 0
        for capture in captured:
            image = capture['image']
            if image and readings is not None:
                row = readings[index]; index += 1
                row = dict(row, vision_status='complete')
            else:
                row = dict(read_frame(image, ocr), reader='local_ocr',
                           vision_status='failed' if vision_error else 'unavailable')
                if vision_error:
                    row['vision_error_type'] = vision_error
            row.update({k: v for k, v in capture.items() if k != 'image' and v is not None})
            results.append(row)
            if callable(frame_observer):
                frame_observer(image, row)
    valid = [r for r in results if r['status'] in ('ok', 'no_text')]
    complete = results and len(valid) == len(results) and all(
        all(v['status'] in ('ok', 'no_text') for v in r['views']) for r in valid)
    capture = 'complete' if complete else 'partial' if valid else 'unavailable'
    return assess_visual({'schema_version': 3, 'capture_status': capture, 'candidate_ids': ids,
                          'windows': windows, 'frames': results})


def video_frame(params, seconds, *, diagnostic=False):
    url, headers = params
    with tempfile.TemporaryDirectory(prefix='icourse-homework-frame-') as tmp:
        path = Path(tmp)/'frame.png'
        try:
            # Keep original resolution. Credentials/errors stay out of logs.
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-rw_timeout', '15000000',
                            '-headers', headers, '-ss', str(seconds), '-i', url,
                            '-frames:v', '1', '-y', str(path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=30)
            data = path.read_bytes()
            return {'image': data, 'error_code': None} if diagnostic else data
        except (OSError, subprocess.SubprocessError) as error:
            # Inspect only in memory; never return stderr, private URL or body.
            body = (getattr(error, 'stderr', None) or b'').decode(errors='ignore').lower()
            if isinstance(error, subprocess.TimeoutExpired): code = 'capture_timeout'
            elif '401' in body and ('http' in body or 'server' in body): code = 'http_401'
            elif '403' in body and ('http' in body or 'server' in body): code = 'http_403'
            elif '404' in body and ('http' in body or 'server' in body): code = 'http_404'
            elif 'timed out' in body: code = 'transport_timeout'
            elif isinstance(error, FileNotFoundError): code = 'no_frame_or_runtime_missing'
            else: code = 'capture_failed'
            return {'image': None, 'error_code': code} if diagnostic else None
