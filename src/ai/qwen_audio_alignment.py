"""Separate CPU-only alignment stage; no ASR model co-resident."""
import gc
import resource
import sys
import time
from src.ai.qwen_quality import aligned_quote_span,aligned_rescue_intervals

MODEL='Qwen/Qwen3-ForcedAligner-0.6B'
REVISION='c7cbfc2048c462b0d63a45797104fc9db3ad62b7'


def align_suspects(report,selected,path,checkpoint, *, budget=120, model_path=None):
    if not selected:
        return [],[],[],{'model':MODEL,'seconds':0}
    import torch
    import soundfile as sf
    from huggingface_hub import snapshot_download
    from qwen_asr import Qwen3ForcedAligner
    torch.set_num_threads(4)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass  # Qwen runtime may already have initialized the shared CPU pool.
    began=time.perf_counter()
    local_model = model_path is not None
    if model_path is None:
        model_path=snapshot_download(MODEL,revision=REVISION,
                                     allow_patterns=['*.json','*.safetensors','*.txt'])
    aligner=Qwen3ForcedAligner.from_pretrained(model_path,dtype=torch.float32,
                   device_map='cpu',attn_implementation='eager')
    located,unresolved=[],[]
    with sf.SoundFile(path) as audio:
        for item in selected:
            chunk=report['full_chunks'][item['id']]
            audio.seek(round(chunk['start']*16000))
            samples=audio.read(round(chunk['end']*16000)-round(chunk['start']*16000),dtype='float32')
            try:
                result=aligner.align(audio=(samples,16000),text=chunk['text'],language='Chinese')[0]
                items=[{'text':x.text,'start':x.start_time,'end':x.end_time} for x in result]
                start,end=aligned_quote_span(chunk,item['quote'],items,report['vad_windows'])
                located.append({**item,'start':start,'end':end,'state':'audio_forced_alignment'})
                report.setdefault('alignment_items',{})[str(item['id'])]=items
                del result
            except Exception as error:
                # Error body may contain data; keep only whitelisted QA codes.
                state=str(error) if isinstance(error,ValueError) else 'alignment_runtime_failure'
                if len(state)>80 or ' ' in state:
                    state='alignment_runtime_failure'
                unresolved.append({**item,'state':state})
            report['localization']={'located':located,'unresolved':unresolved}
            checkpoint(report)
            del samples
            gc.collect()
    intervals,accepted,rejected=aligned_rescue_intervals(report['full_chunks'],located,budget=budget)
    metrics={'model':MODEL,'revision':None if local_model else REVISION,
             'local_model':local_model,'seconds_including_load':time.perf_counter()-began,
             'peak_rss_gib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**3 if sys.platform=='darwin' else 1024**2),
             'note':'Structural QA and VAD checks, not calibrated alignment confidence or transcript accuracy.'}
    del aligner
    gc.collect()
    return intervals,accepted,unresolved+rejected,metrics
