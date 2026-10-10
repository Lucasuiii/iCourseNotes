"""Bounded B2 decoder for the pinned mlx-qwen3-asr 0.4.4 runtime.

One model and KV cache, independent left-padding masks, positions and EOS per lane.
Audio encoding remains sequential. Callers retain the existing single-lane rescue.
"""
import gc
import time

def decode_pair(model, tokenizer, audios, *, context='', max_tokens=2048, deadline):
    import mlx.core as mx
    from mlx_qwen3_asr.audio import compute_features
    from mlx_qwen3_asr.generate import GenerationConfig, _detect_repetition, _finalize_generation_result, _periodic_eval
    from mlx_qwen3_asr.tokenizer import parse_asr_output
    if time.monotonic() >= deadline:
        raise TimeoutError('MLX pair deadline')
    assert 1<=len(audios)<=2 and all(0<len(a)<=30*16000 for a in audios)
    config=GenerationConfig(max_new_tokens=max_tokens,temperature=0.0)
    enc=[];prompts=[]
    for audio in audios:
        mel,lens=compute_features(audio)
        features,_=model.audio_tower(mel.astype(mx.bfloat16),lens)
        mx.eval(features)
        if time.monotonic() >= deadline:
            raise TimeoutError('MLX pair audio deadline')
        enc.append(features)
        prompts.append(tokenizer.build_prompt_tokens(n_audio_tokens=features.shape[1],language='Chinese',context=context))
    lengths=[len(p) for p in prompts];length=max(lengths);pads=[length-n for n in lengths]
    ids=mx.array([[0]*pad+p for pad,p in zip(pads,prompts)])
    audio_tokens=max(f.shape[1] for f in enc)
    features=mx.concatenate([mx.pad(f,[(0,0),(0,audio_tokens-f.shape[1]),(0,0)]) for f in enc],axis=0)
    pos=mx.array([[0]*pad+list(range(n)) for pad,n in zip(pads,lengths)])
    pos3=mx.stack([pos,pos,pos],axis=1)
    embeds=model._embed_tokens(ids,validate_input_ids=True)
    embeds=model._inject_audio_features(embeds,features,ids==model.audio_token_id)
    # Mask left padding for both prefill and every decode step. Padding is never
    # added to real prompt positions or interpreted as an audio placeholder.
    keys=mx.arange(length)[None,None,None,:]
    queries=mx.arange(length)[None,None,:,None]
    valid=keys>=mx.array(pads)[:,None,None,None]
    causal=keys<=queries
    mask=mx.where(valid & causal,mx.array(0,mx.bfloat16),mx.array(-1e9,mx.bfloat16))
    cache=model.create_cache(max_seq_len=length+max_tokens)
    hidden=model.model(inputs_embeds=embeds,position_ids=pos3,attention_mask=mask,cache=cache)
    logits=model.lm_head(hidden[:,-1:,:])
    token=mx.argmax(logits[:,0,:],axis=-1).tolist()
    generated=[[int(t)] for t in token];finished=[False]*len(audios)
    for step in range(1,max_tokens):
        for i,values in enumerate(generated):
            finished[i]=finished[i] or values[-1] in config.eos_token_ids or _detect_repetition(values)
        if all(finished):break
        if time.monotonic() >= deadline: raise TimeoutError('MLX pair decode deadline')
        next_ids=mx.array([[int(t)] for t in token])
        positions=mx.array([[n+step-1] for n in lengths]);positions=mx.stack([positions]*3,axis=1)
        size=length+step
        mask=mx.where(mx.arange(size)[None,None,None,:]>=mx.array(pads)[:,None,None,None],
                      mx.array(0,mx.bfloat16),mx.array(-1e9,mx.bfloat16))
        hidden=model.model(input_ids=next_ids,position_ids=positions,attention_mask=mask,cache=cache)
        logits=model.lm_head(hidden)
        token=mx.argmax(logits[:,0,:],axis=-1).tolist()
        for i,t in enumerate(token):
            if not finished[i]:generated[i].append(int(t))
        _periodic_eval(cache=cache,step=step,eval_interval=config.eval_interval)
    if time.monotonic() >= deadline:
        raise TimeoutError('MLX pair deadline')
    results=[]
    for values in generated:
        gen=_finalize_generation_result(values,config)
        _,text=parse_asr_output(tokenizer.decode(gen.tokens),user_language='Chinese')
        results.append({'text':text,'finish_reason':gen.finish_reason,'truncated':gen.truncated,
                        'generated_tokens':gen.generated_tokens})
    del cache,hidden,logits,enc,features,embeds,ids,mask,pos3
    mx.clear_cache();gc.collect()
    return results

