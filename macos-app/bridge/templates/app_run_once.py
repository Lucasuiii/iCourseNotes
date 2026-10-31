"""One local lecture launched by the macOS app: checkpointed ASR, safe pause and resume.

Derived from local-runtime/templates/run_once.py.  Differences: a paused backend
run is recorded as `paused` (not `failed`), and `--resume` continues the same
frozen plan with the identical command line the backend requires.
"""
import os,sys,json,subprocess,time,threading,sqlite3,contextlib,shutil
from pathlib import Path
os.umask(0o077);ROOT=Path(__file__).resolve().parent;REPO=ROOT/'repo';BASE=ROOT.parents[1]
PY=ROOT.parent/'qwen-local-asr-test-env/bin/python';sys.path.insert(0,str(REPO))
from scripts.local_history.storage import Store,atomic
cfg=json.loads((REPO/'.local-history-refresh/settings.json').read_text());key=Path(cfg['key_file']).read_text().strip()
STORE=Store(ROOT/'lecture-@@SUB_ID@@',key);OUT=BASE/'outputs/@@RUN_NAME@@';OUT.mkdir(parents=True,exist_ok=True,mode=0o700)
TAG='@@COURSE_ID@@-@@SUB_ID@@'
RESUME='--resume' in sys.argv;RETRY='--retry-failed' in sys.argv
previous=json.loads((ROOT/'execution-status.json').read_text()) if (ROOT/'execution-status.json').exists() else {}
state={'status':'starting','phase':'acquisition','course_id':'@@COURSE_ID@@','sub_id':'@@SUB_ID@@','date':'@@DAY@@','pid':os.getpid(),'publication_authorized':False,'email_authorized':False,'preserve_transcripts':True,'launched_by':'local-app'}
if RESUME:
 assert previous.get('status') in ('paused','failed'),'Only paused or failed lectures resume'
 assert previous.get('status')=='paused' or RETRY,'Failed lectures need explicit retry'
 attempt=int(previous.get('resume_attempt',0))+1
 state.update(authorized_resume=True,resume_attempt=attempt,previous_status=previous.get('status'),previous_error_type=previous.get('error_type'))
 marker=ROOT/f'app-resume-{attempt}-started.json'
else:
 marker=ROOT/'execution-started.json'
def save(**v):state.update(v,updated=time.time());atomic(ROOT/'execution-status.json',json.dumps(state,ensure_ascii=False,indent=2).encode())
with marker.open('x') as f:json.dump({'pid':os.getpid(),'started':time.time(),'single_attempt':True,'resume':RESUME,'retry_failed':RETRY},f)
env=dict(os.environ,DYLD_FALLBACK_LIBRARY_PATH='/opt/homebrew/lib',PYTHONUNBUFFERED='1',OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='1',VECLIB_MAXIMUM_THREADS='4')
cmd=[str(PY),'-m','scripts.local_history_refresh','--env-file',str(ROOT/'queue.env'),'--run-dir',str(STORE.root),'--campus-mode','direct','--automatic-terms','--mlx-batch-size','2','--review-scope','theory']
stop=threading.Event()
def monitor():
 with (ROOT/'performance.jsonl').open('a') as log:
  while not stop.is_set():
   try:
    saved=STORE.read(TAG+'.enc') if STORE.exists(TAG+'.enc') else {}
    raw=json.loads(subprocess.check_output(['/opt/homebrew/bin/macmon','pipe','-s','1','-i','500'],timeout=5))
    point={'time':time.time(),'stage':saved.get('stage',state['phase']),'saved_blocks':len(saved.get('rows',[])),'planned_blocks':len(saved.get('spec',{}).get('plan',{}).get('blocks',[])),'gpu_active_percent':raw['gpu_active_ratio']*100,'system_watts':raw['sys_power'],'gpu_watts':raw['gpu_power'],'ram_gib':raw['memory']['ram_usage']/2**30,'swap_gib':raw['memory']['swap_usage']/2**30}
    atomic(ROOT/'performance-latest.json',json.dumps(point).encode());log.write(json.dumps(point)+'\n');log.flush()
   except Exception as e:atomic(ROOT/'performance-error.json',json.dumps({'error_type':type(e).__name__}).encode())
   stop.wait(20)
def invoke(args,log):
 with (ROOT/log).open('ab') as f:
  child=subprocess.Popen(args,cwd=REPO,env=env,stdout=f,stderr=subprocess.STDOUT);save(worker_pid=child.pid)
  return child.wait()
def export_transcripts():
 checkpoint=STORE.read(TAG+'.enc') if STORE.exists(TAG+'.enc') else {}
 rows=checkpoint.get('final_rows',checkpoint.get('rows',[]))
 if rows:atomic(OUT/'asr-transcript.md',('# 本地 ASR 完整转录\n\n'+''.join(f"## 块 {r['chunk_id']+1} · {r['start']:.3f}–{r['end']:.3f} 秒\n\n{r['text']}\n\n" for r in rows)).encode())
 path=STORE.root/TAG/'candidate.db'
 if path.exists():
  with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
   row=c.execute('select transcript from lectures where sub_id=?',('@@SUB_ID@@',)).fetchone()
   if row and row[0]:atomic(OUT/'transcript.md',('# 课堂整理转录\n\n'+row[0]+'\n').encode())
 return checkpoint
save();t=threading.Thread(target=monitor,daemon=True);t.start()
inhibitor=subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
 save(status='running')
 code=invoke(cmd+['run','--hours','8','--allow-active-actions']+(['--retry-failed'] if RETRY else []),'run-private.log');checkpoint=export_transcripts()
 progress=STORE.read('progress.enc');lesson=progress.get('lectures',{}).get(TAG,{})
 if lesson.get('status')=='paused':
  save(status='paused',phase=checkpoint.get('stage','acquisition'),error_type=None,saved_blocks=len(checkpoint.get('rows',[])),planned_blocks=len(checkpoint.get('spec',{}).get('plan',{}).get('blocks',[])))
  sys.exit(3)
 if code or lesson.get('status')!='complete':
  save(status='failed',phase=checkpoint.get('stage','acquisition'),error_type=checkpoint.get('last_error_type','LocalRunFailed'),saved_blocks=len(checkpoint.get('rows',[])))
  for name in ['summary-draft.md','summary-review.md']:
   p=STORE.root/TAG/name
   if p.exists():shutil.copyfile(p,OUT/name)
  raise RuntimeError('LocalRunFailed')
 save(phase='content_validation')
 if invoke(cmd+['review'],'review-private.log'):raise RuntimeError('LocalReviewExportFailed')
 save(status='awaiting_content_review',phase='independent_review',error_type=None,saved_blocks=len(checkpoint.get('rows',[])),planned_blocks=len(checkpoint['spec']['plan']['blocks']))
except SystemExit:raise
except BaseException as e:
 if state['status'] not in ('failed','paused'):save(status='paused' if isinstance(e,KeyboardInterrupt) else 'failed',error_type=None if isinstance(e,KeyboardInterrupt) else type(e).__name__)
 try:export_transcripts()
 except Exception:pass
 if state['status']=='failed':atomic(OUT/'failure.md',('# 本地新课处理未完成\n\n课程：@@COURSE_TITLE@@，@@DAY@@（@@COURSE_ID@@/@@SUB_ID@@）。\n\n阶段：'+str(state.get('phase'))+'；错误类型：'+str(state.get('error_type'))+'。\n\n保留音频、转录和加密检查点，可在应用中“重试失败”续跑。未盲目重放模型、推送或邮件。\n').encode())
finally:stop.set();t.join(timeout=6);inhibitor.terminate()
