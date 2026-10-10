"""Conservative exercise references; repeated OCR is evidence, not an assignment."""
import hashlib
import math
import re

MIN_CONFIDENCE = .85
NUMBER = r'(?:\d{1,3}|[一二三四五六七八九十百零〇两]{1,5})'
ITEM = NUMBER + r'(?:\s*[（(]\s*' + NUMBER + r'\s*[）)]){0,6}'
LIST = ITEM + r'(?:\s*[、,，及和至到\-—]\s*' + ITEM + r'){0,12}'
REFERENCE = re.compile(
    rf'(?:第\s*{LIST}\s*题|(?:第\s*)?{NUMBER}\s*页|'
    rf'(?:习题|练习)\s*{NUMBER}(?:[.．]\d{{1,3}}){{0,2}}'
    rf'\s*[:：]\s*{LIST})')


def candidate_key(chunk_id, quote):
    return f'{chunk_id}:' + hashlib.sha256(quote.encode()).hexdigest()[:12]


def references(text):
    # Do not interpret isolated numbers, matrices or example labels as tasks.
    return list(dict.fromkeys(re.sub(r'\s+', '', m.group()).replace('．', '.')
                              for m in REFERENCE.finditer(text)))


def assess_visual(visual, cloud=()):
    """High-confidence refs at distinct times or matching completed cloud audio.

    Crop/full-frame passes at one instant count once, not as independent frames.
    Agreement does not mean the teacher assigned the displayed exercise.
    """
    if not visual:
        return visual
    observations = {}
    for frame in visual.get('frames', []):
        for ref in frame.get('references', []):
            confidence = ref.get('confidence')
            vision = ref.get('source') == 'deepseek_vision' and ref.get('legible') is True
            if not vision and (not isinstance(confidence, (float, int)) or not math.isfinite(confidence)
                               or not MIN_CONFIDENCE <= confidence <= 1):
                continue
            scope_page = ref.get('page') if vision and not ref['text'].endswith('页') else None
            key = (frame.get('candidate_id'), ref['text'], scope_page)
            observations.setdefault(key, set()).add(frame['seconds'])
    audio = {}
    for clip in cloud:
        if clip.get('status') == 'complete':
            refs = references(clip.get('cloud_text', ''))
            items = set(refs)
            for ref in refs:
                if ref.startswith('第') and ref.endswith('题') and not re.search(r'至|到|[-—]', ref):
                    items.update('第' + item.replace('（', '(').replace('）', ')') + '题'
                                 for item in re.split(r'[、,，及和]', ref[1:-1]))
            audio.setdefault(clip.get('candidate_id'), set()).update(items)
    evidence = []
    for (candidate, text, scope_page), times in sorted(observations.items(), key=lambda item: str(item[0])):
        multi_frame = len(times) >= 2 and max(times)-min(times) >= 5
        audio_match = text in audio.get(candidate, set())
        kind = 'page' if text.endswith('页') else 'exercise'
        audible = {r for r in audio.get(candidate, set())
                   if ('page' if r.endswith('页') else 'exercise') == kind}
        conflict = bool(audible) and not audio_match
        evidence.append({'candidate_id': candidate, 'text': text, 'seconds': sorted(times),
                         'page': scope_page,
                         'multi_frame_agreement': multi_frame, 'audio_agreement': audio_match,
                         'audio_conflict': conflict, 'supported': (multi_frame or audio_match) and not conflict,
                         'tentative': len(times) == 1 and not audio_match and not conflict})
    supported = [e for e in evidence if e['supported']]
    candidates = visual.get('candidate_ids', [])
    # Do not hide one missing board behind another reminder's successful OCR.
    complete = bool(candidates) and all(
        any(e['candidate_id'] == c for e in supported)
        and not any(e['candidate_id'] == c and e['audio_conflict'] for e in evidence)
        for c in candidates)
    return dict(visual, status='failed' if visual.get('status') == 'failed' else
                'references_supported' if complete else 'needs_verification',
                reference_status='supported' if complete else 'unverified',
                reference_evidence=evidence,
                notice='多帧或语音一致只佐证画面文字，不表示教师已布置该题。' if complete else
                       '部分题号或页码已有佐证，其余内容仍待核实；不表示已布置该题。' if supported else
                       '单帧清晰读出的题号或页码可作为存疑线索保留，尚无交叉佐证，不表示已布置该题。'
                       if any(e['tentative'] for e in evidence) else
                       '视觉核对未完成：尚无可交叉佐证的作业题号或页码。')
