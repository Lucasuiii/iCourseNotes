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


# Spoken numerals after removing fillers; "百" must be followed by 十/零 to absorb more
# digits, so a restart such as "一百呃一百五十八" reads as 100 and 158, not 101.
_DIGIT = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_D = '一二两三四五六七八九'
_SPOKEN = re.compile(rf'[{_D}]百(?:零[{_D}]|[{_D}]十[{_D}]?|十[{_D}]?)?|[{_D}]?十[{_D}]?|\d{{1,3}}|[{_D}]')
_FILLERS = re.compile(r'[呃啊嗯额哦\s，,、。]')


def _spoken_value(token):
    if token.isdigit():
        return int(token)
    value = 0
    if '百' in token:
        head, token = token.split('百', 1)
        value += _DIGIT[head]*100
        token = token.lstrip('零〇')
    if '十' in token:
        head, tail = token.split('十', 1)
        value += (_DIGIT[head] if head else 1)*10 + (_DIGIT[tail] if tail else 0)
    elif token:
        value += _DIGIT[token]
    return value


def spoken_numbers(text):
    return [_spoken_value(m.group()) for m in _SPOKEN.finditer(_FILLERS.sub('', text or ''))]


def spoken_subitems(text):
    """'第二题的第二小题' / '第一题的第二题' -> (2, 2), (1, 2)."""
    clean = _FILLERS.sub('', text or '')
    num = rf'(?:[{_D}十百零]+|\d{{1,3}})'
    return {(_spoken_value(a), _spoken_value(b))
            for a, b in re.findall(rf'第({num})题的?第({num})小?题', clean)
            if _SPOKEN.fullmatch(a) and _SPOKEN.fullmatch(b)}


def apply_speech_conflicts(visual, chunks, candidates, *, before=300, after=120):
    """Compare board readings with the local transcript around each homework cue.

    Flags two OCR failure shapes that multi-frame agreement cannot catch: a page with
    a dropped leading digit (board P158 read as 58 while the teacher says 一百五十八)
    and a different sub-question (2(3) on the board reading, 第二题的第二小题 spoken).
    A flagged reference is no longer supported; the transcript never adds new items.
    """
    if not visual or not visual.get('reference_evidence') or not isinstance(chunks, list):
        return visual
    spoken = {}
    for c in candidates or []:
        if not isinstance(c, dict) or 'id' not in c or not isinstance(c.get('quote'), str):
            continue
        start, end = c.get('block_start', 0)-before, c.get('block_end', 0)+after
        text = '\n'.join(str(r.get('text') or '') for r in chunks
                         if r.get('end', 0) >= start and r.get('start', 0) <= end)
        spoken[candidate_key(c['id'], c['quote'])] = (
            {n for n in spoken_numbers(text) if 10 <= n <= 999}, spoken_subitems(text))
    evidence, flagged = [], {}
    for ref in visual['reference_evidence']:
        ref = dict(ref)
        numbers, items = spoken.get(ref.get('candidate_id'), (set(), set()))
        page = re.fullmatch(r'(?:第)?(\d{1,3})页', ref.get('text', ''))
        if page:
            shown = int(page[1])
            longer = sorted(n for n in numbers if n != shown and str(n).endswith(str(shown)))
            if shown not in numbers and longer:
                flagged[(ref.get('candidate_id'), shown)] = longer[0]
                ref.update(speech_conflict={'spoken': f'{longer[0]}页'})
        item = re.fullmatch(r'第(\d{1,3})\((\d{1,3})\)题', ref.get('text', ''))
        if item:
            x, z = int(item[1]), int(item[2])
            said = sorted(y for a, y in items if a == x and y != z)
            if said and (x, z) not in items:
                ref.update(speech_conflict={'spoken': f'第{x}({said[0]})题'})
        evidence.append(ref)
    for ref in evidence:  # Items scoped to a misread page are not corroborated either.
        if ref.get('page') is not None and (ref.get('candidate_id'), ref['page']) in flagged and 'speech_conflict' not in ref:
            ref['speech_conflict'] = {'spoken': f"{flagged[(ref.get('candidate_id'), ref['page'])]}页"}
    if not any('speech_conflict' in r for r in evidence):
        return visual
    for ref in evidence:
        if 'speech_conflict' in ref:
            ref.update(audio_conflict=True, supported=False, tentative=False)
    return dict(visual, reference_evidence=evidence, status='needs_verification', reference_status='unverified',
                notice='板书读取与课堂口述的页码或题号不一致，以口述为准提示核对；画面数字不确认为作业要求。')
