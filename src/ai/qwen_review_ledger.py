"""Whole-lecture rescue reservations survive summary failures and job retries."""
from pathlib import Path
import subprocess
import tempfile
from src.runtime import config
from src.ai.segment_rescue import MAX_CLOUD_SECONDS, MAX_CLOUD_CLIPS


def validate_ledger(state):
    import math
    attempts = state.get('attempts', [])
    seconds = sum(item['seconds'] for item in attempts)
    identities = [(i['interval']['start_ms'], i['interval']['end_ms']) for i in attempts]
    if (len(attempts) > MAX_CLOUD_CLIPS or not math.isfinite(seconds)
            or seconds < 0 or seconds > MAX_CLOUD_SECONDS + 1e-6
            or len(set(identities)) != len(identities)
            or any(not 0 < i['seconds'] <= 60
                   or abs(i['seconds']-(i['interval']['end_ms']-i['interval']['start_ms'])/1000) > 1e-6
                   or i['status'] not in ('reserved', 'complete', 'failed') for i in attempts)
            or abs(state.get('seconds', 0)-seconds) > 1e-6):
        raise ValueError('Invalid whole-lecture cloud quota checkpoint')
    calls = state.get('homework', {}).get('vision_calls', [])
    if (not isinstance(calls, list) or len(calls) > 4
            or any(not isinstance(c, dict) or c.get('status') not in ('reserved', 'complete', 'failed')
                   or not isinstance(c.get('candidate_id'), str) for c in calls)
            or len({c['candidate_id'] for c in calls}) != len(calls)):
        raise ValueError('Invalid homework vision checkpoint')


def review_prepared(material, pages, summarizer, state, checkpoint, *, homework_ocr=None):
    from src.ai.qwen_quality import review_quality
    from src.ai.qwen_audio_alignment import align_suspects
    from src.ai.doubao_asr import rescue_intervals_pcm
    validate_ledger(state)
    if state.get('complete'):
        return state.get('material', {})
    if not callable(checkpoint):
        raise ValueError('Cloud review requires durable checkpoints')
    report = {'full_chunks': material['full_chunks'], 'vad_windows': material['vad_windows']}
    alignment_options = ({'model_path': material['alignment_model_path']}
                         if material.get('alignment_model_path') else {})
    attempts = state.setdefault('attempts', [])
    used = {(a['interval']['start_ms'], a['interval']['end_ms']) for a in attempts}

    def material_state():
        variants, weak, homework_cloud = [], [], []
        for item in attempts:
            interval, segments = item['interval'], item.get('segments', [])
            if interval.get('kind') == 'missing_asr':
                continue  # Already incorporated into the verified transcript.
            elif interval.get('kind') == 'weak':
                if segments: weak.append((interval, segments))
            elif interval.get('kind') == 'homework':
                from src.ai.homework_visual_evidence import candidate_key
                homework_cloud.append({'start_ms': interval['start_ms'], 'end_ms': interval['end_ms'],
                                       'candidate_id': candidate_key(interval['chunk_id'], interval['text']),
                                       'original_quote': interval['text'], 'status': item['status'],
                                       'cloud_text': ' '.join(s['text'] for s in segments)})
            elif any(s['start_ms'] < interval['quote_end_ms'] and interval['quote_start_ms'] < s['end_ms'] for s in segments):
                variants.append({'original_quote': interval['text'], 'cloud_text': ' '.join(s['text'] for s in segments)})
        homework = state.get('homework', {})
        if homework.get('visual'):
            from src.ai.homework_visual_evidence import assess_visual
            homework['visual'] = assess_visual(homework['visual'], homework_cloud)
        return {'variants': variants, 'weak_rescues': weak, 'unresolved': state.get('unresolved', []),
                # Transport ledger belongs in the private recovery state, not
                # duplicated into the summary's visual evidence/prompt.
                'homework': {**{k: v for k, v in homework.items() if k != 'vision_calls'}, 'cloud': homework_cloud},
                'uncertain_calls': sum(i['status'] == 'reserved' for i in attempts)}

    def rescue(intervals):
        for interval in intervals:
            identity = (interval['start_ms'], interval['end_ms'])
            seconds = (interval['end_ms']-interval['start_ms'])/1000
            if identity in used or not 0 < seconds <= 60:
                continue
            # Focused assignment context must not be charged again as a weak
            # or generic suspect clip. Existing checkpoints keep their quota.
            if any(interval['start_ms'] < a['interval']['end_ms']
                   and a['interval']['start_ms'] < interval['end_ms']
                   and (interval.get('kind') in ('homework','missing_asr')
                        or a['interval'].get('kind') in ('homework','missing_asr'))
                   for a in attempts):
                continue
            if len(attempts) >= MAX_CLOUD_CLIPS or state.get('seconds', 0)+seconds > MAX_CLOUD_SECONDS:
                continue
            item = {'interval': interval, 'seconds': seconds, 'status': 'reserved'}
            attempts.append(item); used.add(identity)
            state['seconds'] = state.get('seconds', 0)+seconds
            state['material'] = material_state()
            checkpoint()  # Reserve before transport; unknown outcomes never refund.
            rescues, _, failed = rescue_intervals_pcm(material['audio_path'], config.DOUBAO_ASR_API_KEY,
                [interval], max_seconds=seconds, max_clips=1, hotwords=material['recognition_terms'])
            item.update(status='failed' if failed else 'complete', segments=rescues[0][1] if rescues else [])
            state['failed'] = failed; state['material'] = material_state()
            checkpoint()
            if failed: break

    try:
        from src.ai.homework_review import assignment_candidates, prioritize_candidates, focus_intervals, nearby_pages, MAX_FOCUS
        if 'homework' not in state:
            candidates = assignment_candidates(material['full_chunks'])
            state['homework'] = {'candidates': prioritize_candidates(candidates), 'deferred_count': max(0, len(candidates)-MAX_FOCUS)}
            checkpoint()
        homework = state['homework']
        if 'intervals' not in homework:
            selected = [c for c in homework['candidates'] if c.get('alignable', True)]
            intervals, unresolved = [], [dict(c, state='invalid_or_repeated_quote') for c in homework['candidates']
                                        if not c.get('alignable', True)]
            budget = min(240, MAX_CLOUD_SECONDS-state.get('seconds', 0))
            if (selected and config.DOUBAO_ASR_API_KEY and material.get('audio_path') and budget > 0
                    and len(attempts) < MAX_CLOUD_CLIPS and not state.get('failed')):
                try:
                    with tempfile.TemporaryDirectory(prefix='icourse-homework-') as tmp:
                        wav = Path(tmp)/'audio.wav'
                        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'f32le', '-ar', '16000',
                                        '-ac', '1', '-i', material['audio_path'], '-y', str(wav)],
                                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
                        raw, _, rejected, _ = align_suspects(report, selected, wav, lambda _: None,
                                                            budget=budget, **alignment_options)
                        unresolved.extend(rejected)
                        intervals = focus_intervals(raw, material.get('audio_seconds', 0))
                except Exception as error:
                    unresolved.extend(dict(c, state='focus_alignment_failed', error_type=type(error).__name__) for c in selected)
            homework.update(intervals=intervals, unresolved=unresolved)
            state['material'] = material_state()
            checkpoint()
        if homework['candidates'] and 'visual' not in homework:
            try:
                if callable(homework_ocr):
                    homework['visual'] = homework_ocr(homework['candidates'], homework['intervals'])
                else:
                    frames = []
                    seen = set()
                    for candidate in homework['candidates']:
                        for page in nearby_pages(pages, candidate):
                            identity = (page.get('page_num'), page['created_sec'])
                            if identity not in seen and str(page.get('text') or '').strip():
                                seen.add(identity)
                                frames.append({'source': 'existing_ppt_ocr', 'seconds': page['created_sec'],
                                               'text': page['text'][:2000], 'status': 'ok'})
                    homework['visual'] = {'status': 'ok' if frames else 'unavailable', 'frames': frames}
            except Exception as error:
                homework['visual'] = {'status': 'failed', 'error_type': type(error).__name__, 'frames': []}
            state['material'] = material_state()
            checkpoint()
        if config.DOUBAO_ASR_API_KEY and not state.get('failed'):
            rescue(homework['intervals'])  # Assignment cues take priority, within the shared ledger.
        if not config.DOUBAO_ASR_API_KEY:
            homework['cloud_unavailable'] = True
            state['material'] = material_state()
            checkpoint()
            return state['material']
        if 'weak_intervals' not in state:
            from src.ai.segment_rescue import select_weak_windows
            weak = select_weak_windows(material.get('weak_windows', []), material.get('audio_seconds', 0),
                max_clips=MAX_CLOUD_CLIPS//2 if len(material.get('transcript', '')) >= 200 else MAX_CLOUD_CLIPS)
            state['weak_intervals'] = [dict(w, kind='weak') for w in weak]
            checkpoint()
        if not state.get('failed'): rescue(state['weak_intervals'])
        if 'intervals' not in state:
            intervals, unresolved = [], []
            remaining = MAX_CLOUD_SECONDS-state.get('seconds', 0)
            clips = MAX_CLOUD_CLIPS-len(attempts)
            if not state.get('failed') and remaining > 0 and clips > 0 and len(material.get('transcript', '')) >= 200:
                provider = summarizer.providers[0]
                suspects = review_quality(summarizer._clients[provider['name']], provider['models'][0],
                                         report, {'ppt': pages}, max_suspects=clips, input_budget=96000)
                if suspects:
                    with tempfile.TemporaryDirectory(prefix='icourse-review-') as tmp:
                        wav = Path(tmp)/'audio.wav'
                        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'f32le', '-ar', '16000',
                                        '-ac', '1', '-i', material['audio_path'], '-y', str(wav)],
                                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
                        intervals, _, unresolved, _ = align_suspects(report, suspects, wav, lambda _: None,
                                                                   budget=remaining, **alignment_options)
            state.update(intervals=intervals, unresolved=unresolved)
            checkpoint()
        if not state.get('failed'): rescue(state['intervals'])
        state['material'] = material_state(); state['complete'] = True
        checkpoint()
        return state['material']
    except Exception as error:
        state['error_type'] = type(error).__name__
        state['material'] = material_state()
        checkpoint()
        return state['material']
