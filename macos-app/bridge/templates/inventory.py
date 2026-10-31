"""Read current encrypted data and subscribed campus calendars; no inference or dispatch."""
import os,sys,json,sqlite3,contextlib
from pathlib import Path
from functools import partial
from datetime import datetime
from zoneinfo import ZoneInfo
os.umask(0o077);ROOT=Path(__file__).resolve().parent;REPO=ROOT/'repo'
sys.path.insert(0,str(REPO));os.chdir(REPO)
result={'status':'failed'}
try:
 with (ROOT/'resource-check-started.json').open('x') as marker:json.dump({'pid':os.getpid(),'read_only':True,'audio_downloads':0,'inference_calls':0},marker)
 with (ROOT/'inventory-private.log').open('x') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
  from scripts.local_history_refresh import load_env
  cfg=json.loads((REPO/'.local-history-refresh/settings.json').read_text())
  for path in cfg['env_files']:load_env(path)
  key=Path(cfg['key_file']).read_text().strip()
  os.environ.update(GITHUB_REPOSITORY='Lucasuiii/iCourseNotes',DB_ENCRYPTION_KEY=key,DATA_DIR=str(ROOT/'inventory-scratch'),PUBLISH_RESULTS='false',SEND_EMAIL='false')
  for old,new in [('STUID','StuId'),('UISPSW','UISPsw')]:
   if old in os.environ:os.environ[new]=os.environ[old]
  from scripts.production_db import load_remote
  from scripts.local_history.storage import Store
  revision=load_remote(ROOT/'baseline-inventory.db')
  conn=sqlite3.connect(ROOT/'baseline-inventory.db');conn.row_factory=sqlite3.Row
  meta={r[0]:r[1] for r in conn.execute('select key,value from meta where key in (?,?)',('subscribed_course_ids','course_ids'))}
  raw=meta.get('subscribed_course_ids',meta.get('course_ids',os.environ.get('COURSE_IDS','')))
  ids=[str(x).strip() for x in (json.loads(raw) if raw.startswith('[') else raw.split(',')) if str(x).strip()]
  assert ids and all(x.isdigit() for x in ids),'No subscribed course selection'
  os.environ['COURSE_IDS']=','.join(ids)
  from src.runtime import config
  from scripts.local_history.runtime import choose_access_mode
  from src.api.auth_recovery import initial_authenticated_session
  from src.api.webvpn import WebVPNSession
  from src.api.icourse import ICourseClient
  from src.runtime.session_rules import lecture_is_selected
  mode=choose_access_mode('auto');vpn=initial_authenticated_session(max_attempts=1,factory=partial(WebVPNSession,access_mode=mode));client=ICourseClient(vpn)
  local_registry=ROOT.parent/'local-lecture-decisions.json'
  local_decisions=json.loads(local_registry.read_text()).get('lectures',{}) if local_registry.exists() else {}
  courses=[];new=[];today=datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
  try:
   for cid in ids:
    detail=client.get_course_detail(cid);historical={str(r['sub_id']):dict(r) for r in conn.execute('select * from lectures where course_id=?',(cid,))};rows=[]
    for row in sorted(detail['lectures'],key=lambda r:(r['date'],str(r['sub_id'])),reverse=True):
     sid=str(row['sub_id']);old=historical.get(sid)
     available=bool(row['has_playback'] and row['date']<=today)
     selected=lecture_is_selected(cid,row,config.COURSE_SESSION_RULES,config.COURSE_SESSION_OVERRIDE_DATES,exclusions=config.COURSE_SESSION_EXCLUSIONS)
     r={'course_id':cid,'course_title':detail['title'],'sub_id':sid,'title':row['sub_title'],'date':row['date'],'available':available,'historical_exists':bool(old),'has_summary':bool(old and old.get('summary')),'deleted':bool(old and old.get('deleted_at')),'session_selected':selected}
     r['local_not_needed']=local_decisions.get(cid+'/'+sid,{}).get('status')=='not_needed'
     r['local_pending']=False
     for status_path in ROOT.parent.glob('local-latest-'+cid+'-'+sid+'-*/execution-status.json'):
      local=json.loads(status_path.read_text())
      if local.get('status') in ('running','awaiting_content_review','awaiting_review','awaiting_publication','publishing','published'):
       r['local_pending']=True
     rows.append(r)
     if available and selected and not r['deleted'] and not r['has_summary'] and not r['local_not_needed'] and not r['local_pending']:new.append(r)
    courses.append({'course_id':cid,'course_title':detail['title'],'lectures':rows})
   new.sort(key=lambda x:(x['date'],int(x['sub_id'])),reverse=True)
   deferred=[];ready=[]
   for item in new:
    try:
     available_url=client.get_video_url(item['course_id'],item['sub_id'])
     diagnostic=client.video_lookup_diagnostics(item['course_id'],item['sub_id'])
     item['resource_available']=bool(available_url)
     if available_url:ready.append(item)
     else:deferred.append(dict(item,resource_error='NoPlayableURL',resource_diagnostics=diagnostic))
    except Exception as e:deferred.append(dict(item,resource_available=False,resource_error=type(e).__name__))
   new=ready
   backup=Store(ROOT/'recovery',key);raw=(ROOT/'baseline-inventory.db').read_bytes();backup.save_bytes('inventory-baseline.db.enc',raw);assert backup.read_bytes('inventory-baseline.db.enc')==raw
   result={'status':'complete','today':today,'baseline_revision':revision,'mode':mode,'courses':courses,'unrecognized_available':new,'deferred_unavailable':deferred,'source_sha':json.loads((ROOT/'source.json').read_text())['sha'],'selection_rule':'available, within subscription/session rules, not deleted, no existing summary'}
   (ROOT/'inventory.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
  finally:client.close_media_session();vpn.session.close();conn.close()
except BaseException as e:
 import traceback
 frames=[{'file':Path(x.filename).name,'function':x.name,'line':x.lineno} for x in traceback.extract_tb(e.__traceback__)]
 result={'status':'failed','error_type':type(e).__name__,'missing_module':getattr(e,'name',None),'frames':frames};(ROOT/'inventory-status.json').write_text(json.dumps(result))
print(json.dumps({'status':result['status'],'error_type':result.get('error_type'),'control':str(ROOT),'ready_count':len(result.get('unrecognized_available',[])),'unavailable_count':len(result.get('deferred_unavailable',[]))},ensure_ascii=False))
sys.exit(0 if result['status']=='complete' else 2)
