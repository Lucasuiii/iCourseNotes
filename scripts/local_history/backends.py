"""MLX Apple GPU adapter; immutable weights recorded independently of CPU revision."""
import gc
import time
from pathlib import Path
from src.ai.qwen_transcriber import QwenTranscriber, QwenTokenBudgetError, NORMAL_TOKENS
from src.ai.qwen_quality import context_echo, low_information


class MLXTranscriber(QwenTranscriber):
    def __init__(self, model_path):
        super().__init__()
        self.model_path = Path(model_path)

    def _init(self):
        if self._model is None:
            import mlx.core as mx
            from mlx_qwen3_asr import load_model
            self._model, _ = load_model(str(self.model_path), dtype=mx.bfloat16)

    def _recognize(self, samples, *, unhinted=False, deadline=None):
        if deadline is not None and time.monotonic() >= deadline:
            return {'text': '', 'quality_state': 'retry_timeout'}
        import mlx.core as mx
        from mlx_qwen3_asr import transcribe
        context = '术语：'+'、'.join(self._terms) if self._terms and not unhinted else ''
        def progress(*args, **kwargs):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('MLX generation deadline')
        try:
            result = transcribe(samples, model=self._model, dtype=mx.bfloat16,
                context=context, language='Chinese', max_new_tokens=NORMAL_TOKENS,
                verbose=False, on_progress=progress)
        except TimeoutError:
            return {'text': '', 'quality_state': 'retry_timeout'}
        if deadline is not None and time.monotonic() >= deadline:
            return {'text': '', 'quality_state': 'retry_timeout'}
        if result.truncated:
            raise QwenTokenBudgetError('mlx', NORMAL_TOKENS, NORMAL_TOKENS)
        text = result.text.strip()
        if context and context_echo(text, context):
            if not unhinted:
                row = self._recognize(samples, unhinted=True, deadline=deadline)
                if context_echo(row['text'], context):
                    row.update(text='', quality_state='unresolved_context_echo')
                return row
            return {'text': '', 'quality_state': 'unresolved_context_echo'}
        return {'text': '' if low_information(text) else text,
                'quality_state': 'low_information' if low_information(text) else 'recognized'}

    def _rescue_recognize(self, samples, budget):
        return self._recognize(samples, unhinted=True, deadline=time.monotonic()+budget)

    def release_model(self):
        self._model = None
        gc.collect()
        import mlx.core as mx
        mx.clear_cache()
