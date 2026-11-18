"""Newest-first queue started by the macOS app, with cooperative pause and resume.

Up to two lectures overlap: the next one starts once the running one is past
acquisition (see PAST_ACQUISITION). A pause request file stops new launches;
running lectures save their checkpoints (the app sends SIGINT to those in local
stages) and the queue ends paused once they have stopped.  `--resume`
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
# Pipelining: the next lecture may start once every running one is past acquisition, so its
# login and audio fetch overlap the previous recognition, and its recognition overlaps the
# previous summary. The GPU lock in the backend still keeps a single model loaded.
MAX_WORKERS=2;POLL=float(os.environ.get('ICOURSE_QUEUE_POLL','5'));PAST_ACQUISITION={'vad','ppt','gpu_wait','asr','asr_fallback','summary','candidate_validation','complete'}
def stage_of(item,since):
 try:
  v=json.loads((Path(item['control'])/('lecture-'+item['sub_id'])/'stage.json').read_text())
  return v.get('stage') if v.get('time',0)>=since else None
 except (OSError,ValueError):return None
def plan(item):
 current=lecture_state(Path(item['control']))
 if not current:return []
 if current.get('status')=='paused':return ['--resume']
 if current.get('status')=='failed' and RETRY:return ['--resume','--retry-failed']
 return None
running={};pause_hit=False
def reap():
 global pause_hit
 for sid,(child,item,_,log) in list(running.items()):
  code=child.poll()
  if code is None:continue
  log.close();del running[sid]
  result=lecture_state(Path(item['control'])) or {'status':'coordinator_failed','error_type':'MissingExecutionStatus'}
  state['lectures'][sid]={'status':result.get('status'),'phase':result.get('phase'),'error_type':result.get('error_type'),'returncode':code,'output':item['output']}
  if result.get('status')=='paused':pause_hit=True
def can_start():
 if len(running)>=MAX_WORKERS:return False
 return all(stage_of(item,since) in PAST_ACQUISITION for _,item,since,_ in running.values())
def publish():
 latest=max(running.items(),key=lambda kv:kv[1][2]) if running else None
 if latest:state.update(current_sub_id=latest[0],current_course=latest[1][1]['course_title'],current_date=latest[1][1]['date'])
 state.update(worker_pid=latest[1][0].pid if latest else None,active_sub_ids=sorted(running,key=lambda s:running[s][2]),
              active_worker_pids=[running[s][0].pid for s in sorted(running,key=lambda s:running[s][2])]);save(state)
try:
 pending=[(item,extra) for item in targets for extra in [plan(item)] if extra is not None]
 while True:
  reap()
  if (R/'app-pause-request.json').exists():pause_hit=True
  while pending and not pause_hit and can_start():
   item,_=pending.pop(0);extra=plan(item)
   if extra is None:continue
   control=Path(item['control']);log=(control/'coordinator-private.log').open('ab')
   child=subprocess.Popen([str(PY),str(control/'app_run_once.py')]+extra,env=dict(os.environ,DYLD_FALLBACK_LIBRARY_PATH='/opt/homebrew/lib'),stdout=log,stderr=subprocess.STDOUT)
   running[item['sub_id']]=(child,item,time.time(),log)
  publish()
  if not running and (not pending or pause_hit):break
  time.sleep(POLL)
 if pause_hit and (pending or any(v.get('status')=='paused' for v in state['lectures'].values())):state['status']='paused'
 else:
  finals=[lecture_state(Path(x['control'])).get('status') for x in targets]
  state['status']='awaiting_content_review' if all(s in ('awaiting_content_review','completed','delivered') for s in finals) else 'finished_with_failures'
 state.update(finished=time.time(),worker_pid=None,active_sub_ids=[],active_worker_pids=[]);save(state)
except BaseException as e:state.update(status='paused' if isinstance(e,KeyboardInterrupt) else 'coordinator_failed',error_type=type(e).__name__,finished=time.time());save(state)
finally:
 try:(R/'app-pause-request.json').unlink()
 except FileNotFoundError:pass
print(json.dumps({'status':state['status']},ensure_ascii=False))
