"""Newest-first queue started by the macOS app, with cooperative pause and resume.

A pause request file stops the queue after the current lecture saves its
checkpoint (the app also sends SIGINT to the current worker).  `--resume`
continues paused lectures, and failed ones only with `--retry-failed`; finished
lectures are never run again.
"""
import os,sys,json,time,subprocess
from pathlib import Path
os.umask(0o077);R=Path(__file__).resolve().parent;PY=R.parent/'qwen-local-asr-test-env/bin/python'
RESUME='--resume' in sys.argv;RETRY='--retry-failed' in sys.argv
targets=json.loads((R/'targets.json').read_text())
if not RESUME:
 with (R/'execution-started.json').open('x') as f:json.dump({'pid':os.getpid(),'started':time.time(),'single_attempt':True,'launched_by':'local-app'},f)
def save(data):
 p=R/'queue-status.tmp';p.write_text(json.dumps(data,ensure_ascii=False,indent=2));p.replace(R/'queue-status.json')
old=json.loads((R/'queue-status.json').read_text()) if RESUME and (R/'queue-status.json').exists() else {}
state={'status':'running','pid':os.getpid(),'started':old.get('started',time.time()),'order':[x['sub_id'] for x in targets],'lectures':old.get('lectures',{}),'publication_authorized':False,'email_authorized':False,'preserve_transcripts':True,'launched_by':'local-app'}
if RESUME:state.update(resumed=time.time(),resume_attempt=int(old.get('resume_attempt',0))+1,retry_failed=RETRY)
save(state)
def lecture_state(control):
 p=control/'execution-status.json';return json.loads(p.read_text()) if p.exists() else {}
try:
 for item in targets:
  if (R/'app-pause-request.json').exists():state['status']='paused';break
  control=Path(item['control']);sid=item['sub_id'];current=lecture_state(control)
  extra=[]
  if current:
   if current.get('status')=='paused':extra=['--resume']
   elif current.get('status')=='failed' and RETRY:extra=['--resume','--retry-failed']
   else:continue
  state.update(current_sub_id=sid,current_course=item['course_title'],current_date=item['date']);save(state)
  with (control/'coordinator-private.log').open('ab') as log:
   child=subprocess.Popen([str(PY),str(control/'app_run_once.py')]+extra,env=dict(os.environ,DYLD_FALLBACK_LIBRARY_PATH='/opt/homebrew/lib'),stdout=log,stderr=subprocess.STDOUT)
   state['worker_pid']=child.pid;save(state);code=child.wait()
  result=lecture_state(control) or {'status':'coordinator_failed','error_type':'MissingExecutionStatus'}
  state['lectures'][sid]={'status':result.get('status'),'phase':result.get('phase'),'error_type':result.get('error_type'),'returncode':code,'output':item['output']};state['worker_pid']=None;save(state)
  if result.get('status')=='paused':state['status']='paused';break
 if state['status']!='paused':
  finals=[lecture_state(Path(x['control'])).get('status') for x in targets]
  state['status']='awaiting_content_review' if all(s in ('awaiting_content_review','completed','delivered') for s in finals) else 'finished_with_failures'
 state.update(finished=time.time(),worker_pid=None);save(state)
except BaseException as e:state.update(status='paused' if isinstance(e,KeyboardInterrupt) else 'coordinator_failed',error_type=type(e).__name__,finished=time.time());save(state)
finally:
 try:(R/'app-pause-request.json').unlink()
 except FileNotFoundError:pass
print(json.dumps({'status':state['status']},ensure_ascii=False))
