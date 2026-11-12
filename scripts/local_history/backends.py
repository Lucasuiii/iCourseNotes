"""MLX Apple GPU adapter; immutable weights recorded independently of CPU revision."""
import gc
import hashlib
import time
from pathlib import Path
from src.ai.qwen_transcriber import QwenTranscriber, QwenTokenBudgetError, NORMAL_TOKENS
from src.ai.qwen_quality import context_echo
from src.ai.recognition_policy import decode_audio_chunks


class MLXTranscriber(QwenTranscriber):
    def __init__(self, model_path):
        super().__init__()
        self.model_path = Path(model_path)

    def _init(self):
        if self._model is None:
            import mlx.core as mx
            from mlx_qwen3_asr import load_model
            self._model, _ = load_model(str(self.model_path), dtype=mx.bfloat16)

    def _decode(self, samples, *, context, phase, deadline):
        return decode_audio_chunks(self._decode_chunk, samples, self._terms,
                                   context=context, phase=phase, deadline=deadline)

    def _decode_chunk(self, samples, *, context, phase, deadline):
        """One MLX decode; shared policy owns echo detection and recovery."""
        import mlx.core as mx
        from mlx_qwen3_asr import transcribe
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
            raise QwenTokenBudgetError(phase, NORMAL_TOKENS, NORMAL_TOKENS)
        return {'text': result.text, 'quality_state': 'recognized'}

    def _rescue_recognize(self, samples, budget):
        return self._recognize(samples, unhinted=True, deadline=time.monotonic()+budget)

    def release_model(self):
        self._model = None
        gc.collect()
        import mlx.core as mx
        mx.clear_cache()


class MLXBatchTranscriber(MLXTranscriber):
    """Two original blocks share decoding; each keeps the serial rescue policy."""
    batch_size = 2

    def _init(self):
        import importlib.metadata
        if importlib.metadata.version('mlx-qwen3-asr') != '0.4.4':
            raise ValueError('B2 requires the validated mlx-qwen3-asr 0.4.4 runtime')
        import mlx.core as mx
        mx.set_memory_limit(7*1024**3)
        super()._init()

    def _decode(self, samples, *, context, phase, deadline):
        prefetched = getattr(self, '_prefetched', None)
        if prefetched is None or phase != 'normal':
            return super()._decode(samples, context=context, phase=phase, deadline=deadline)
        self._prefetched = None
        if prefetched.get('timed_out'):
            return {'text':'', 'quality_state':'retry_timeout'}
        if prefetched['truncated']:
            raise QwenTokenBudgetError(phase, NORMAL_TOKENS, NORMAL_TOKENS)
        return {'text':prefetched['text'], 'quality_state':'recognized',
                'context_echo':bool(prefetched.get('context_echo'))}

    def _initial_pair(self, samples, *, deadline):
        from mlx_qwen3_asr import transcribe
        from src.ai.qwen_segmentation import split_inference_audio
        from mlx_qwen3_asr.tokenizer import _TokenizerHolder, join_text_parts
        from scripts.local_history.mlx_batch import decode_pair
        import mlx.core as mx
        chunks = [split_inference_audio(s) for s in samples]
        parts = [[] for _ in samples]
        tokenizer = _TokenizerHolder.get(str(self.model_path))
        context = '术语：'+'、'.join(self._terms) if self._terms else ''
        def progress(*args, **kwargs):
            if time.monotonic() >= deadline:
                raise TimeoutError('MLX pair singleton deadline')
        for index in range(max(map(len,chunks))):
            owners = [i for i,c in enumerate(chunks) if index < len(c)]
            clips = [chunks[i][index][0] for i in owners]
            if len(clips)==2:
                outputs = decode_pair(self._model,tokenizer,clips,context=context,
                                      max_tokens=NORMAL_TOKENS,deadline=deadline)
            else:
                progress()
                value = transcribe(clips[0],model=self._model,dtype=mx.bfloat16,
                    language='Chinese',context=context,max_new_tokens=NORMAL_TOKENS,
                    verbose=False,on_progress=progress)
                outputs = [{'text':value.text,'truncated':value.truncated}]
            if len(outputs)!=len(owners):
                raise RuntimeError('MLX pair returned inconsistent lane identities')
            for owner, row, clip in zip(owners, outputs, clips):
                start = chunks[owner][index][1]
                child = {'start_sample': start, 'end_sample': start + len(clip),
                         'sample_sha256': hashlib.sha256(clip.tobytes()).hexdigest(),
                         'context_echo': bool(context and context_echo(row['text'], context)),
                         'truncated': bool(row['truncated'])}
                parts[owner].append(dict(row, child=child))
        return [{'text':join_text_parts([r['text'] for r in rows],'Chinese'),
                 'truncated':any(r['truncated'] for r in rows),
                 'context_echo':any(r['child']['context_echo'] for r in rows),
                 'child_chunks':[r['child'] for r in rows]} for rows in parts]

    def recognize_blocks(self, blocks, load_samples, *, checkpoint=None, timeout=18000, keep_model=False):
        if len(blocks)>2 or not blocks:
            raise ValueError('MLX B2 requires one or two original blocks')
        if len(blocks)==1:
            return super().recognize_blocks(blocks,load_samples,checkpoint=checkpoint,
                                           timeout=timeout,keep_model=keep_model)
        import gc
        import mlx.core as mx
        began=time.monotonic(); deadline=began+timeout
        self.last_chunks=[]; self._last_speech_windows=[]
        try:
            if timeout<=0: raise TimeoutError('Qwen shard timeout')
            self._init()
            samples=[load_samples(b) for b in blocks]
            if any(len(s)!=b['samples'] for s,b in zip(samples,blocks)):
                raise RuntimeError('Incomplete Qwen audio block')
            initial_started=time.monotonic()
            initial_deadline=min(deadline,initial_started+min(600,*(60+8*len(s)/16000 for s in samples)))
            try:
                outputs=self._initial_pair(samples,deadline=initial_deadline)
            except TimeoutError:
                outputs=[{'timed_out':True} for _ in blocks]
                mx.clear_cache(); gc.collect()
            # Native/shape/OOM errors propagate rather than silently changing policy.
            if len(outputs)!=len(blocks): raise RuntimeError('MLX pair lost a block')
            initial_elapsed=time.monotonic()-initial_started
            for block,wave,output in zip(blocks,samples,outputs):
                if time.monotonic()>=deadline: raise TimeoutError('Qwen shard timeout')
                self._prefetched=output
                started=time.monotonic()
                row=self._recognize_resilient(wave,block,deadline)
                row.update(start=block['start'],end=block['end'],chunk_id=block['chunk_id'],
                           decode_seconds=initial_elapsed/len(blocks)+time.monotonic()-started,
                           mlx_batch={'size':2,'shared_initial_seconds':initial_elapsed,
                                      'initial_budget_seconds':initial_deadline-initial_started,
                                      'timeout':bool(output.get('timed_out')),
                                      'truncated':bool(output.get('truncated')),
                                      'child_chunks':output.get('child_chunks',[]),
                                      'context_echo':bool(output.get('context_echo') or
                                          (output.get('text') and self._terms and
                                           context_echo(output['text'],'术语：'+'、'.join(self._terms))))})
                self.last_chunks.append(row)
                for a,b in self.last_vad_windows:
                    a,b=max(a,row['start']),min(b,row['end'])
                    if b>a:
                        self._last_speech_windows.append({'start_ms':round(a*1000),'end_ms':round(b*1000),
                                                         'text':row['text'],'chunk_id':row['chunk_id']})
                if checkpoint: checkpoint(self.last_chunks)
                print(f'[Qwen B2] Block {len(self.last_chunks)}/{len(blocks)} completed.',flush=True)
            return self.last_chunks
        finally:
            self._prefetched=None
            gc.collect(); mx.clear_cache()
            if not keep_model: self.release_model()
