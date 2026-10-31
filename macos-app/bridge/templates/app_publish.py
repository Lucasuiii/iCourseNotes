"""Publish one reviewed local lecture to data from the macOS app; mail only on request.

Reuses finalize.py of this control directory (scope check, durable encrypted
backups, single forward-only push with remote readback, one-attempt mailer).
`--request app-dry-run-request.json` rebuilds and validates the candidate
against current data without any journal, push or mail.  After a verified
publication, unnecessary intermediates are removed; summary, transcripts,
selected figures and run state are kept.
"""
import os,sys,json,sqlite3,contextlib,shutil,hashlib,base64,io,time,importlib.util,argparse
from pathlib import Path
os.umask(0o077);R=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('guarded_finalize',R/'finalize.py');f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
CID,SID,DAY=f.CID,f.SID,f.DAY
def status(**v):
 p=R/'app-publication-status.json';old=json.loads(p.read_text()) if p.exists() else {}
 old.update(v,updated=time.time());f.record(p.name,old)
def apply_figure_recovery(fresh,meta,audit,figures,summary):
 """Use the recovered figures only if the reviewed prose is unchanged and figures are merely inserted."""
 if not f.RECOVERY.exists('app-figure-recovery.enc'):return fresh,meta,figures,summary
 from src.pipeline.summary_figures import insert_figures,validate_assets
 artifact=f.RECOVERY.read('app-figure-recovery.enc');state=artifact['figure_state']
 assert artifact['kind']=='figure_recovery' and artifact['base_summary']==summary==audit['reviewed_summary'],'Recovery base differs from reviewed summary'
 assert figures.get('status')=='failed' and not figures.get('figures'),'Original selection was not a failed empty selection'
 validate_assets(state,SID);assert state['status']=='complete' and state['summary_sha256']==figures['summary_sha256']
 assert state['recovery']['model_calls']==0 and artifact['summary']==insert_figures(summary,state['figures']),'Recovered summary is not figure insertion only'
 final=artifact['summary'];fresh['lecture']['summary']=final
 fresh['lecture']['summary_model']=(fresh['lecture'].get('summary_model') or '')+' / figure recovery'
 fresh['meta']=[x for x in fresh['meta'] if x['key']!='summary_review:'+SID]
 for x in fresh['meta']:
  if x['key']=='summary_figures:'+SID:x['value']=json.dumps(state,ensure_ascii=False)
  if x['key']=='qwen_pipeline:'+SID:
   q=json.loads(x['value']);q['summary_editorial_update']={'schema':1,'kind':'figure_recovery_after_model_review','original_summary_sha256':f.sha(summary.encode()),'summary_sha256':f.sha(final.encode()),'figures_inserted_only':True,'original_model_review':audit,'final_text_reviewed_by_model':False};x['value']=json.dumps(q,ensure_ascii=False)
 return fresh,{x['key']:x['value'] for x in fresh['meta']},state,final
def load_candidate(request):
 from scripts.local_history.runtime import candidate_files
 from src.pipeline.summary_review import validate_published
 from src.pipeline.qwen_plan import validate_block_row
 from src.pipeline.history_refresh import digest
 from PIL import Image
 manifest=f.LOCAL.read('manifest.enc');assert manifest['targets']==[{'slot':0,'course_id':CID,'sub_id':SID,'date':DAY,'before_hash':None,'preview_only':True}]
 path,fresh=candidate_files(f.LOCAL,manifest['targets'][0]);state=f.LOCAL.read(CID+'-'+SID+'.enc')
 coverage=state['recognition_coverage'];assert coverage['complete'] is True and coverage['missing_seconds']==0,'Incomplete recognition'
 plan=state['spec']['plan'];rows=state.get('final_rows',state['rows']);assert len(rows)==len(plan['blocks'])
 for block,row in zip(plan['blocks'],rows):validate_block_row(block,row)
 assert [r['chunk_id'] for r in rows]==list(range(len(plan['blocks'])))
 meta={r['key']:r['value'] for r in fresh['meta']};audit=json.loads(meta['summary_review:'+SID])
 validate_published(audit,CID,SID,fresh['lecture']['summary'],DAY);assert audit['status'] in ('passed','revised')
 figures=json.loads(meta.get('summary_figures:'+SID,'{}'))
 for fig in figures.get('figures',[]):
  raw=base64.b64decode(fig['data'],validate=True);assert f.sha(raw)==fig['id'];Image.open(io.BytesIO(raw)).verify()
 summary=fresh['lecture']['summary']
 fresh,meta,figures,summary=apply_figure_recovery(fresh,meta,audit,figures,summary)
 # Exactly the text the user read in the app; anything else is refused.
 assert f.sha(summary.encode())==request['summary_sha256'],'Reviewed summary differs from candidate'
 assert not f.RECOVERY.exists('final-reviewed-editorial-artifact.enc'),'Manual editorial artifact exists; use its delivery flow'
 return path,fresh,state,rows,meta,audit,figures,summary,digest(fresh)
def build(before,fresh,meta,overwrite=None):
 """Candidate DB for this one lecture. A cloud note is replaced only when the user chose to
 overwrite exactly that note (its summary hash); otherwise it is preserved."""
 replaced=None
 with contextlib.closing(sqlite3.connect(before)) as db:
  db.row_factory=sqlite3.Row;old=db.execute('select * from lectures where sub_id=?',(SID,)).fetchone()
  assert db.execute('select 1 from courses where course_id=?',(CID,)).fetchone(),'Course removed; do not recreate'
  if old:
   assert old['course_id']==CID and old['date']==DAY and not old['deleted_at'],'Lecture removed or changed'
   if old['summary'] or old['emailed_at']:
    assert old['summary'] and overwrite,'Remote lesson already processed; preserve it'
    assert f.sha(old['summary'].encode())==overwrite,'Cloud note changed since comparison; compare again'
    replaced={'summary':old['summary'],'summary_model':old['summary_model'],'processed_at':old['processed_at'],'emailed_at':old['emailed_at']}
  else:
   sel=json.loads((R/'selection.json').read_text());assert sel['historical_exists'] is False and sel['available'] and sel['session_selected']
 candidate=R/'publish-candidate.db';shutil.copyfile(before,candidate)
 with contextlib.closing(sqlite3.connect(candidate)) as db,db:
  incoming=fresh['lecture'];cols=[r[1] for r in db.execute('pragma table_info(lectures)')]
  allowed=['transcript','summary','summary_model','processed_at','error_count','error_stage','error_msg']
  if old:db.execute('update lectures set '+','.join('"'+c+'"=?' for c in allowed)+' where sub_id=?',[incoming[c] for c in allowed]+[SID])
  else:
   assert incoming['sub_id']==SID and not incoming.get('deleted_at')
   names=[k for k in incoming if k in cols and k!='id'];db.execute('insert into lectures('+','.join('"'+k+'"' for k in names)+') values('+','.join('?' for k in names)+')',[incoming[k] for k in names])
  db.execute('delete from ppt_pages where sub_id=?',(SID,))
  for row in fresh['ppt']:
   names=list(row);db.execute('insert into ppt_pages('+','.join('"'+n+'"' for n in names)+') values('+','.join('?' for _ in names)+')',[row[n] for n in names])
  for k in f.scope_keys(CID,SID):db.execute('delete from meta where key=?',(k,))
  for k,v in meta.items():assert k in f.scope_keys(CID,SID);db.execute('insert into meta(key,value) values (?,?)',(k,v))
 f.validate_database(str(candidate));f.scope(before,candidate)
 return candidate,replaced
def dry_run(request):
 path,fresh,state,rows,meta,audit,figures,summary,fingerprint=load_candidate(request)
 before=R/'app-dry-run-before.db'
 try:
  base=f.load_remote(before);assert base
  candidate,replaced=build(before,fresh,meta,request.get('overwrite_remote_sha256'))
  result={'status':'passed','base':base,'overwrites_remote':bool(replaced),'blocks':len(rows),'figures':len(figures.get('figures',[])),'summary_review_status':audit['status'],'summary_sha256':f.sha(summary.encode()),'candidate_fingerprint':fingerprint,'scope_verified':True,'pushed':False,'mail':False,'checked':time.time()}
 finally:
  for p in [before,R/'publish-candidate.db']:
   for suffix in ['','-wal','-shm']:Path(str(p)+suffix).unlink(missing_ok=True)
 f.record('app-dry-run-result.json',result)
def export_outputs(summary,figures,audit):
 from src.pipeline.summary_figures import export_local
 from src.pipeline.summary_review import report
 f.OUT.mkdir(parents=True,exist_ok=True,mode=0o700)
 f.atomic(f.OUT/'summary.md',(export_local(summary,figures,f.OUT) if figures else summary).encode())
 f.atomic(f.OUT/'summary-review.md',report(audit).encode())
def cleanup(keep_remote_backup=False):
 """Only after published + remote readback verified. Keeps outputs, state and checkpoints;
 after an overwrite also keeps the encrypted pre-publication data with the replaced cloud note."""
 journal=json.loads((R/'notes-publication.json').read_text());assert journal['status']=='published' and journal['remote_readback_verified']
 protected=[p for p in f.OUT.rglob('*') if p.is_file()];hashes={str(p):f.sha(p.read_bytes()) for p in protected}
 assert (f.OUT/'summary.md').exists() and (f.OUT/'transcript.md').exists() and (f.OUT/'asr-transcript.md').exists(),'Transcripts not exported'
 remove=[];dirs=[]
 for p in R.rglob('*'):
  rel=p.relative_to(R)
  if rel.parts[0]=='repo' or p.is_symlink():continue
  if p.is_dir() and p.name.endswith('-checkout'):dirs.append(p);continue
  if not p.is_file():continue
  name=p.name
  if name.endswith(('.db','.db-wal','.db-shm','.raw','.wav','.m4a','.mp4','.aac','private.log')):remove.append(p)
  elif rel.parts[0]=='recovery' and name.endswith('.db.enc'):
   if not (keep_remote_backup and name.startswith('remote-before-')):remove.append(p)
  elif name=='baseline.db.enc':remove.append(p)
 removed=[]
 for p in remove:
  if any(p.is_relative_to(d) for d in dirs):continue
  assert p.resolve().is_relative_to(R.resolve()) and str(p) not in hashes
  removed.append({'path':str(p.relative_to(R)),'bytes':p.stat().st_size});p.unlink()
 for d in dirs:
  size=sum(x.stat().st_size for x in d.rglob('*') if x.is_file() and not x.is_symlink());shutil.rmtree(d);removed.append({'path':str(d.relative_to(R))+'/','bytes':size})
 assert all(Path(k).exists() and f.sha(Path(k).read_bytes())==v for k,v in hashes.items()),'Protected output changed'
 f.record('cleanup-result.json',{'status':'complete','files_removed':len(removed),'bytes_freed':sum(x['bytes'] for x in removed),'removed':removed,'preserved_outputs':len(hashes),'policy':'after verified publication: drop audio, plaintext databases, checkouts, private logs and redundant encrypted DB backups; keep outputs, run state and recognition checkpoints'})
 return sum(x['bytes'] for x in removed)
def publish(request):
 for name in ['finalization-started.json','corrected-delivery-started.json','notes-publication.json','app-publication-started.json']:
  assert not (R/name).exists(),'Publication already attempted: '+name
 status(status='validating',email_requested=request['email'],dry_run=False)
 path,fresh,state,rows,meta,audit,figures,summary,fingerprint=load_candidate(request)
 f.record('app-publication-started.json',{'started':time.time(),'single_attempt':True,'email_requested':request['email'],'summary_sha256':request['summary_sha256']})
 st=json.loads((R/'execution-status.json').read_text());st.update(status='publishing',phase='publication',updated=time.time());f.record('execution-status.json',st)
 approval={'approved':True,'blocking_issues':[],'checks':request['checks'],'notes':request.get('notes',''),'reviewer':'local-app user','summary_sha256':f.sha(summary.encode()),'candidate_fingerprint':fingerprint,'figure_ids':[x['id'] for x in figures.get('figures',[])],'approved_at':time.time()}
 f.record('editorial-approval.json',approval);f.RECOVERY.save('independent-editorial-approval.enc',approval)
 export_outputs(summary,figures,audit);f.durable_backup(path,'local-reviewed-candidate')
 status(status='publishing')
 before=R/'publish-before.db';base=f.load_remote(before);assert base;f.durable_backup(before,'remote-before-'+base)
 candidate,replaced=build(before,fresh,meta,request.get('overwrite_remote_sha256'))
 if replaced:
  # Keep a readable copy of the cloud note being replaced; the encrypted full backup stays too.
  f.atomic(f.OUT/'replaced-cloud-summary.md',('# 被本机版本替换的云端笔记\n\n模型：'+str(replaced['summary_model'])+'；处理时间：'+str(replaced['processed_at'])+'\n\n'+replaced['summary']+'\n').encode())
  status(overwrote_remote=True,replaced_summary_sha256=f.sha(replaced['summary'].encode()),remote_backup='recovery/remote-before-'+base+'.db.enc')
 commit=f.publish(candidate,before,base,'notes')
 status(status='published',notes_commit=commit)
 email_status='not_requested';final=commit
 if request['email']:
  status(status='mailing')
  mailbase=R/'mail-before.db';mailrev=f.load_remote(mailbase);f.durable_backup(mailbase,'remote-before-mail-'+mailrev)
  with contextlib.closing(sqlite3.connect(mailbase)) as db:
   row=db.execute('select summary,emailed_at,deleted_at from lectures where sub_id=?',(SID,)).fetchone()
   assert row and row[0]==summary and not row[2],'Published note changed before mail'
  if row[1]:email_status='already_emailed';final=mailrev
  else:
   mailer=f.OneAttemptEmailer();assert mailer.receivers and mailer.sender and mailer.password,'Email configuration unavailable'
   mailer.summary_hash=f.sha(summary.encode())
   item={'course_id':CID,'sub_id':SID,'course_title':state['spec']['course_title'],'sub_title':fresh['lecture']['sub_title'],'date':DAY,'summary':summary,'figure_state':figures or None}
   if not mailer.send([item]):raise RuntimeError('MailNotConfirmedNoAutomaticRetry')
   receipt=f.RECOVERY.read('mail-attempt.enc');assert receipt['status']=='sent';email_status='sent'
   rb=R/'receipt-before.db';rev=f.load_remote(rb);f.durable_backup(rb,'remote-before-receipt-'+rev)
   with contextlib.closing(sqlite3.connect(rb)) as db:
    row=db.execute('select summary,emailed_at,deleted_at from lectures where sub_id=?',(SID,)).fetchone()
    assert row and row[0]==summary and not row[2],'Email sent, but database changed: retain receipt without resending'
   if not row[1]:
    rc=R/'receipt-working.db';shutil.copyfile(rb,rc)
    with contextlib.closing(sqlite3.connect(rc)) as db,db:db.execute('update lectures set emailed_at=? where sub_id=?',(receipt['sent_at'],SID))
    final=f.publish(rc,rb,rev,'receipt',receipt=True)
   else:final=rev
 result={'notes_commit':commit,'data_commit':final,'email_status':email_status,'summary':str(f.OUT/'summary.md'),'coverage_blocks':len(rows),'summary_review_status':audit['status'],'figures':len(figures.get('figures',[]))}
 f.record('delivery-result.json',result)
 f.atomic(f.OUT/'completion-report.md',('# 本机新课已发布\n\n@@COURSE_TITLE@@，@@DAY@@。\n\n'+str(len(rows))+' 块覆盖核验通过；理论审核 '+audit['status']+'，配图 '+str(result['figures'])+' 张。在本地应用中人工审核后发布，远端回读一致。\n\n邮件：'+{'not_requested':'未选择发送','sent':'SMTP 已接受（不等于已验证收件箱送达）','already_emailed':'data 中已有回执，未重发'}[email_status]+'。\n\n笔记提交：https://github.com/Lucasuiii/iCourseNotes/commit/'+commit+'\n').encode())
 status(status='cleaning',data_commit=final,email_status=email_status)
 freed=cleanup(keep_remote_backup=bool(replaced))
 st=json.loads((R/'execution-status.json').read_text());st.update(status='completed',phase='complete',updated=time.time(),pid=None,worker_pid=None,data_commit=final,email_status=email_status,figures=result['figures'],cleanup_complete=True);f.record('execution-status.json',st)
 status(status='completed',cleanup_complete=True,bytes_freed=freed)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--request',required=True);a=p.parse_args()
 assert a.request in ('app-dry-run-request.json','app-publication-request.json')
 request=json.loads((R/a.request).read_text())
 try:
  with (R/'app-publish-private.log').open('a') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
   dry_run(request) if request['dry_run'] else publish(request)
 except Exception as e:
  if request['dry_run']:f.record('app-dry-run-result.json',{'status':'failed','error_type':type(e).__name__,'detail':str(e)[:200] if type(e) is AssertionError else None,'pushed':False,'mail':False,'checked':time.time()})
  else:
   status(status='failed',error_type=type(e).__name__,detail=str(e)[:200] if type(e) is AssertionError else None,notes_journal=(R/'notes-publication.json').exists(),mail_reserved=f.RECOVERY.exists('mail-attempt.enc'),automatic_retry=False)
   try:
    st=json.loads((R/'execution-status.json').read_text())
    if st.get('status')=='publishing':st.update(status='publication_failed',phase='publication',error_type=type(e).__name__,updated=time.time());f.record('execution-status.json',st)
   except Exception:pass
  sys.exit(1)
