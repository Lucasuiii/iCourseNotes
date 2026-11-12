"""Backend-independent prompt, output and recovery rules for course ASR."""
import time

from src.ai.qwen_quality import context_echo, low_information

MAX_NEW_TOKENS = 2048
MAX_TERMS = 30


def normalize_terms(terms):
    return list(dict.fromkeys(term.strip() for term in terms
                             if isinstance(term, str) and term.strip()))[:MAX_TERMS]


def recognize_with_context(decode, samples, terms, *, unhinted=False, deadline=None):
    """Apply the same echo recovery and filler policy to every decoder.

    The backend implements one bounded decode. A detected echo gets one
    full-input unhinted decode within the same remaining deadline. Persistent
    echoes remain explicit failures for the existing bounded split recovery.
    """
    if deadline is None:
        deadline = time.monotonic() + 600
    reference = '术语：' + '、'.join(terms) if terms else ''

    def invoke(context, phase):
        if deadline is not None and time.monotonic() >= deadline:
            return {'text': '', 'quality_state': 'retry_timeout'}
        row = decode(samples, context=context, phase=phase, deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return dict(row, text='', quality_state='retry_timeout')
        return row

    row = invoke('' if unhinted else reference, 'unhinted' if unhinted else 'normal')
    if row.get('quality_state') == 'retry_timeout':
        return dict(row, text='')
    echo = bool(row.get('context_echo') or reference and context_echo(row['text'], reference))
    if echo and not unhinted:
        original = row
        try:
            row = invoke('', 'unhinted_echo_retry')
        except RuntimeError as error:
            if hasattr(error, 'diagnostic'):
                error.diagnostic['generation_diagnostics'] = (original.get('generation_diagnostics', [])
                    + error.diagnostic.get('generation_diagnostics', []))
            raise
        diagnostics = original.get('generation_diagnostics', []) + row.get('generation_diagnostics', [])
        if diagnostics:
            row['generation_diagnostics'] = diagnostics
        row['context_echo_recovery'] = {'detected': True, 'unhinted': True}
        if row.get('quality_state') == 'retry_timeout':
            return dict(row, text='')
        echo = bool(row.get('context_echo') or reference and context_echo(row['text'], reference))
        if not echo:
            row['quality_state'] = 'unhinted_retry'
    if echo:
        return dict(row, text='', quality_state='unresolved_context_echo')
    text = row['text'].strip()
    filler = low_information(text)
    return dict(row, text='' if filler else text,
                quality_state='low_information' if filler else row.get('quality_state', 'recognized'))


def decode_audio_chunks(decode, samples, terms, *, context, phase, deadline):
    """Check native-sized clips before joining either backend's output."""
    import hashlib
    import numpy as np
    from src.ai.qwen_segmentation import split_inference_audio
    reference = '术语：'+'、'.join(terms) if terms else ''
    parts, children, diagnostics = [], [], []
    for clip, start in split_inference_audio(samples):
        if deadline is not None and time.monotonic() >= deadline:
            return {'text':'', 'quality_state':'retry_timeout', 'generation_diagnostics':diagnostics}
        try:
            row = decode(clip, context=context, phase=phase, deadline=deadline)
        except RuntimeError as error:
            if hasattr(error, 'diagnostic'):
                error.diagnostic['generation_diagnostics'] = (diagnostics
                    + error.diagnostic.get('generation_diagnostics', []))
            raise
        diagnostics.extend(row.get('generation_diagnostics', []))
        if row.get('quality_state') == 'retry_timeout':
            return dict(row, text='', generation_diagnostics=diagnostics)
        children.append({'start_sample':start, 'end_sample':start+len(clip),
                         'sample_sha256':hashlib.sha256(np.asarray(clip,dtype='<f4').tobytes()).hexdigest(),
                         'context_echo':bool(reference and context_echo(row['text'], reference)),
                         'truncated':False})
        parts.append(row['text'].strip())
    return {'text': ''.join(parts), 'quality_state':'recognized', 'child_chunks':children,
            'context_echo':any(child['context_echo'] for child in children),
            'generation_diagnostics':diagnostics}
