"""Generation limits, bounded sample coverage, queue terminal failures and gates."""
from contextlib import nullcontext
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import numpy as np

from src.ai.qwen_transcriber import QwenTranscriber, QwenTokenBudgetError, IncompleteQwenRecognitionError, RATE
from scripts.qwen_quality import generation_audit
from scripts.qwen_sharding import validate_result
from src.pipeline.asr_queue import SharedQueue, initial_queue, validate_queue
from src.pipeline.prepared_lecture import assemble_material
from test_shared_asr_queue import plan_for, decoded, MemoryStore
from test_qwen_production_pipeline import database
from scripts import production_qwen as pipeline, production_pool as pool

MODULES={'torch':SimpleNamespace(inference_mode=nullcontext),
         'transformers':SimpleNamespace(StoppingCriteriaList=list)}


def failed(block):
    return dict(decoded(block),text='',quality_state='missing_audio',
                missing_intervals=[{'start':block['start'],'end':block['end'],
                    'error_code':'qwen_token_budget','generation_phase':'normal',
                    'token_limit':2048,'observed_text_tokens':2040}])


def recognizer():
    t=QwenTranscriber();t._init=MagicMock();t._model=MagicMock();t._model.max_new_tokens=2048
    return t


class BlockRecoveryTests(unittest.TestCase):
    def test_normal_and_echo_retry_share_full_output_budget_and_restore_model(self):
        for echo,limit in [(False,2048),(True,2048)]:
            with self.subTest(echo=echo),patch.dict(sys.modules,MODULES):
                t=recognizer();t.set_terms(['矩阵','条件数','扰动','范数','逆矩阵'])
                texts=['术语：矩阵、条件数、扰动、范数、逆矩阵','截断文字'] if echo else ['截断文字']
                t._model.transcribe.side_effect=[[SimpleNamespace(text=x)] for x in texts]
                t._model.processor.tokenizer.encode.side_effect=[list(range(5)),list(range(2040))] if echo else [list(range(2040))]
                with self.assertRaises(QwenTokenBudgetError) as caught:t._recognize(np.zeros(10))
                self.assertEqual(caught.exception.diagnostic['token_limit'],limit)
                self.assertEqual(caught.exception.diagnostic['generation_phase'],'unhinted_echo_retry' if echo else 'normal')
                self.assertEqual(t._model.max_new_tokens,2048)

    def test_actual_generated_limit_cannot_hide_behind_short_parsed_text(self):
        with patch.dict(sys.modules,MODULES):
            t=recognizer()
            t._model.model.generate.side_effect=lambda **kw:SimpleNamespace(sequences=np.zeros((1,2048+3)))
            def transcribe(**kw):
                t._model.model.generate(input_ids=np.zeros((1,3)),max_new_tokens=2048)
                return [SimpleNamespace(text='解析后很短')]
            t._model.transcribe.side_effect=transcribe
            t._model.processor.tokenizer.encode.return_value=[1,2]
            with self.assertRaises(QwenTokenBudgetError) as caught:t._recognize(np.zeros(10))
            self.assertEqual(caught.exception.diagnostic['generated_tokens'],2048)
            self.assertEqual(caught.exception.diagnostic['observed_text_tokens'],2)

    def test_actual_echo_retry_generation_cap_and_nested_wrappers_restore(self):
        with patch.dict(sys.modules,MODULES):
            t=recognizer();t.set_terms(['矩阵','条件数','扰动','范数','逆矩阵'])
            native=t._model.model.generate
            native.side_effect=lambda **kw:SimpleNamespace(sequences=np.zeros((1,kw['max_new_tokens']+3 if native.call_count==2 else 13)))
            texts=iter(['术语：矩阵、条件数、扰动、范数、逆矩阵','短文字'])
            def transcribe(**kw):
                t._model.model.generate(input_ids=np.zeros((1,3)),max_new_tokens=t._model.max_new_tokens)
                return [SimpleNamespace(text=next(texts))]
            t._model.transcribe.side_effect=transcribe;t._model.processor.tokenizer.encode.return_value=[1,2]
            with self.assertRaises(QwenTokenBudgetError) as caught:t._recognize(np.zeros(10))
            self.assertEqual(caught.exception.diagnostic['token_limit'],2048)
            self.assertEqual(caught.exception.diagnostic['generated_tokens'],2048)
            self.assertIs(t._model.model.generate,native);self.assertEqual(t._model.max_new_tokens,2048)
            self.assertIn('stopping_criteria',native.call_args.kwargs)

    def test_short_leaf_does_not_repeat_identical_split_after_full_retry(self):
        t=recognizer();t._recognize=MagicMock(side_effect=QwenTokenBudgetError('normal',2048,2040))
        with patch.dict(sys.modules,MODULES):
            rows=t.recognize_blocks([{'chunk_id':0,'start':0,'end':10}],lambda b:np.zeros(10*RATE))
        self.assertEqual(t._recognize.call_count,2);self.assertEqual(rows[0]['missing_intervals'][0]['end'],10)

    def test_generation_wrapper_restored_on_exception_and_unknown_layout_fails_closed(self):
        native=SimpleNamespace(generate=lambda **kw:SimpleNamespace(sequences=np.zeros((1,4))))
        m=SimpleNamespace(model=native,max_new_tokens=2048);original=native.generate
        with self.assertRaises(RuntimeError),generation_audit(m):m.model.generate()
        self.assertIs(native.generate,original)
        with generation_audit(m) as calls:m.model.generate(input_ids=np.zeros((1,3)))
        self.assertEqual(calls,[{'generated_tokens':1,'token_limit':2048}])
        self.assertIs(native.generate,original)

    def test_full_retry_without_terms_can_recover(self):
        t=recognizer();t._recognize=MagicMock(side_effect=[QwenTokenBudgetError('normal',2048,2040),{'text':'完整内容'}])
        with patch.dict(sys.modules,MODULES):
            rows=t.recognize_blocks([{'chunk_id':3,'start':0,'end':1}],lambda b:np.zeros(RATE))
        self.assertEqual(rows[0]['text'],'完整内容');self.assertEqual(rows[0]['quality_state'],'bounded_retry')
        self.assertTrue(t._recognize.call_args.kwargs['unhinted'])
        self.assertIsInstance(t._recognize.call_args.kwargs['deadline'],float)

    def test_split_preserves_all_samples_and_discards_overlong_outputs(self):
        t=recognizer();calls=[];successful=[]
        samples=np.arange(120*RATE,dtype=np.float32)
        def decode(part,**kw):
            calls.append(len(part))
            if len(part)>30*RATE:raise QwenTokenBudgetError('normal',2048,2040)
            successful.append(part.copy());return {'text':str(int(part[0]))}
        t._recognize=decode
        with patch.dict(sys.modules,MODULES):
            rows=t.recognize_blocks([{'chunk_id':8,'start':7.00001,'end':127.00001}],lambda b:samples)
        np.testing.assert_array_equal(np.concatenate(successful),samples)
        self.assertEqual(calls,[120*RATE,120*RATE]+[30*RATE]*4)
        self.assertEqual(rows[0]['quality_state'],'split_retry');self.assertEqual(rows[0]['missing_intervals'],[])
        self.assertEqual(rows[0]['start'],7.00001);self.assertEqual(rows[0]['end'],127.00001)

    def test_exhausted_leaf_is_missing_but_later_block_and_good_subclips_complete(self):
        t=recognizer();checkpoints=[];calls=[]
        def decode(part,**kw):
            calls.append(len(part))
            # First 15s are irrecoverable; every other short interval is usable.
            if len(part)>15*RATE or part[0]==0:raise QwenTokenBudgetError('normal',2048,2040)
            return {'text':'可用片段'}
        t._recognize=decode
        blocks=[{'chunk_id':3,'start':10,'end':130},{'chunk_id':4,'start':130,'end':140}]
        with patch.dict(sys.modules,MODULES):
            rows=t.recognize_blocks(blocks,lambda b:np.arange(round((b['end']-b['start'])*RATE),dtype=np.float32)+(1 if b['chunk_id']==4 else 0),checkpoint=lambda rows:checkpoints.append(copy.deepcopy(rows)))
        self.assertEqual(len(rows),2);self.assertEqual(len(checkpoints),2)
        self.assertEqual(rows[0]['missing_intervals'][0]['start'],10)
        self.assertEqual(rows[0]['missing_intervals'][0]['end'],17.5)
        self.assertEqual(rows[0]['quality_state'],'missing_audio')
        self.assertEqual(rows[0]['text'].count('可用片段'),8)
        self.assertEqual(rows[1]['text'],'可用片段');self.assertEqual(len(calls),17)

    def test_permanent_limit_has_finite_attempts_and_no_text(self):
        from scripts.qwen_sharding import validate_block_row
        # Include the exact historical block 49 span (122s with overlap).
        for seconds,start,calls,gaps in [(120,0,30,16),(122,5508.555,31,17)]:
            with self.subTest(seconds=seconds):
                t=recognizer();t._recognize=MagicMock(side_effect=QwenTokenBudgetError('normal',2048,2040))
                block={'chunk_id':49,'start':start,'end':start+seconds}
                with patch.dict(sys.modules,MODULES):
                    rows=t.recognize_blocks([block],lambda b:np.zeros(seconds*RATE))
                self.assertEqual(t._recognize.call_count,calls);self.assertEqual(rows[0]['text'],'')
                self.assertEqual(len(rows[0]['missing_intervals']),gaps)
                self.assertAlmostEqual(sum(g['end']-g['start'] for g in rows[0]['missing_intervals']),seconds)
                validate_block_row(block,rows[0])

    def test_cooperative_retry_timeout_never_accepts_partial_text(self):
        t=recognizer();t._recognize=MagicMock(side_effect=[QwenTokenBudgetError('normal',2048,2040)]+[{'text':'截断'}]*2)
        from contextlib import contextmanager
        @contextmanager
        def timed_out(*args,**kwargs):yield {'timed_out':True}
        with patch.dict(sys.modules,MODULES),patch('src.ai.qwen_transcriber.bounded_retry',timed_out):
            rows=t.recognize_blocks([{'chunk_id':0,'start':0,'end':10}],lambda b:np.zeros(10*RATE))
        self.assertEqual(rows[0]['text'],'');self.assertEqual(rows[0]['missing_intervals'][0]['error_code'],'retry_timeout')

    def test_expired_budget_loads_no_rescue_audio_and_does_not_decode(self):
        t=recognizer();t._recognize=MagicMock()
        row=t._recognize_resilient(np.zeros(10*RATE),{'start':0,'end':10},time.monotonic()-1)
        t._recognize.assert_not_called();self.assertEqual(row['text'],'')
        self.assertEqual(row['missing_intervals'],[{'start':0,'end':10,'error_code':'worker_deadline'}])

    def test_native_error_is_not_disguised_as_token_limit(self):
        t=recognizer();t._recognize=MagicMock(side_effect=MemoryError('native allocation'))
        with self.assertRaises(MemoryError):t.recognize_blocks([{'chunk_id':0,'start':0,'end':1}],lambda b:np.zeros(RATE))
        self.assertEqual(t.last_chunks,[])

    def test_direct_pipeline_rejects_incomplete_transcription(self):
        t=recognizer();block={'chunk_id':0,'start':0,'end':1}
        t.prepare_pcm_stream=MagicMock(return_value=[(0,1)])
        def recognize(*args,**kwargs):t.last_chunks=[failed(block)]
        t.recognize_blocks=recognize
        with tempfile.NamedTemporaryFile() as audio,self.assertRaises(IncompleteQwenRecognitionError):
            t._consume_pcm_stream(lambda n:b'',lambda:True,lambda:b'',lambda:0,audio_path=audio.name)


class TerminalFailureTests(unittest.TestCase):
    def queue(self,n=2):
        plan=plan_for(n);store=MemoryStore(initial_queue(plan));return plan,store,SharedQueue(plan,store)

    def test_failed_block_is_terminal_across_workers_attempts_and_restore(self):
        plan,store,q=self.queue();block,token=q.claim('first',1);row=failed(block)
        q.finish(block['chunk_id'],token,row);q.release(block['chunk_id'],token)
        other,token=q.claim('peer',1);self.assertNotEqual(other['chunk_id'],block['chunk_id'])
        q.finish(other['chunk_id'],token,decoded(other))
        self.assertIsNone(q.claim('retry',2));q.restore_rows([row],3)
        self.assertEqual(q.snapshot()['blocks']['0']['status'],'failed')
        reports=q.results(require_complete=False);self.assertFalse(reports[0]['complete'])
        with self.assertRaises(ValueError):q.results()
        with self.assertRaises(ValueError):assemble_material(plan,reports)
        reports[0]['complete']=True
        with self.assertRaises(ValueError):validate_result(plan,reports[0],0)
        with self.assertRaises(ValueError):q.restore_rows([decoded(block)],4)
        # Cross-run seeding also preserves the terminal failure.
        reports[0]['complete']=False
        seeded=initial_queue(plan,reports);self.assertEqual(seeded['blocks']['0']['status'],'failed')

    def test_concurrent_workers_do_not_reclaim_exhausted_block(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        plan,store,q=self.queue(40);seen=[];lock=threading.Lock()
        def worker(n):
            queue=SharedQueue(plan,store)
            while (claim:=queue.claim(str(n),1)) is not None:
                block,token=claim
                queue.finish(block['chunk_id'],token,failed(block) if block['chunk_id']==0 else decoded(block))
                with lock:seen.append(block['chunk_id'])
        with ThreadPoolExecutor(max_workers=3) as workers:list(workers.map(worker,range(3)))
        self.assertEqual(sorted(seen),list(range(40)));self.assertIsNone(q.claim('retry',2))
        self.assertFalse(q.results(require_complete=False)[0]['complete'])

    def test_invalid_or_hidden_missing_interval_is_rejected(self):
        plan,store,q=self.queue(1);block=plan['blocks'][0]
        for bad in [dict(failed(block),missing_intervals=[]),dict(failed(block),missing_intervals=[{'start':-1,'end':60,'error_code':'qwen_token_budget'}])]:
            state=initial_queue(plan);state['blocks']['0']={'status':'failed','result':bad}
            with self.assertRaises(ValueError):validate_queue(plan,state)
        state=initial_queue(plan);state['blocks']['0']={'status':'complete','result':failed(block)}
        with self.assertRaises(ValueError):validate_queue(plan,state)

    def test_worker_continues_and_writes_safe_failure_audit(self):
        import soundfile as sf
        from scripts.shared_asr_worker import run_worker
        plan,store,q=self.queue();buf=io.BytesIO();sf.write(buf,np.zeros(60*RATE),RATE,format='FLAC');blob=buf.getvalue()
        import hashlib
        files={}
        for b in plan['blocks']:b['flac_sha256']=hashlib.sha256(blob).hexdigest();files[f'chunk-{b["chunk_id"]}.flac']=blob
        store=MemoryStore(initial_queue(plan));model=MagicMock()
        def decode(blocks,load,checkpoint,**kw):
            b=blocks[0];checkpoint([failed(b) if b['chunk_id']==0 else decoded(b)])
        model.recognize_blocks.side_effect=decode
        with tempfile.TemporaryDirectory() as tmp,patch('scripts.shared_asr_worker.shards.seal'),patch('scripts.shared_asr_worker.shards.root',return_value=Path(tmp)):
            report=run_worker(plan,files,store,0,1,transcriber=model)
            audit=json.loads((Path(tmp)/'out/worker-audit.json').read_text())
        self.assertEqual(report['failed_chunk_ids'],[0]);self.assertEqual(report['decoded_chunk_ids'],[0,1])
        self.assertEqual(audit['phase'],'complete');self.assertIsNone(audit['error_code'])
        self.assertEqual(audit['local_completed_blocks'],1);self.assertEqual(audit['local_terminal_blocks'],2)
        self.assertEqual(audit['block_diagnostics'][0]['chunk_id'],0)
        self.assertNotIn('矩阵课堂内容',json.dumps(audit,ensure_ascii=False))
        self.assertIsNone(SharedQueue(plan,store).claim('peer',2))

    def test_settled_incomplete_queue_reaches_gather_without_redispaching(self):
        plan,store,q=self.queue(1);b,t=q.claim('one',1);q.finish(0,t,failed(b))
        with patch('scripts.shared_asr_worker.store_for',return_value=store):
            work=pool.workload(0,{'mode':'sharded','plan':plan},1)
        self.assertTrue(work['settled']);self.assertFalse(work['complete']);self.assertEqual(work['pending_blocks'],0)
        from test_production_pool import journal
        state=journal(1);state['courses']['0']['phase']='asr';ticket=pool.reserve(state,0,'asr',1);ticket.update(status='completed',conclusion='success')
        pool.refresh_phases(state,{0:work});self.assertEqual(state['courses']['0']['phase'],'gather')

    def test_gather_records_gaps_without_summary_processed_mail_or_raw_text(self):
        plan,store,q=self.queue(1);b,t=q.claim('one',1);q.finish(0,t,failed(b))
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,'QWEN_PRODUCTION_TASK':'true','GITHUB_ACTIONS':'false','AUTO_COURSE_TERMS':'false'}):
            root=pipeline.root();(root/'inbox').mkdir();db=database(root/'fixture.db')
            from scripts.production_db import snapshot
            payload=snapshot(db,root/'snapshot.db');db.conn.close()
            spec={'course_id':'10','course_title':'高代','lecture':{'sub_id':'1','_validation':{'date':'2026-09-29'}},'mode':'sharded','plan':plan}
            pipeline.encode({'specification.json':pipeline.shards.encoded(spec),'database.db':payload},'prepared',root/'inbox'/'prepared.enc')
            with patch.object(pipeline,'artifact',return_value=False),patch.object(pipeline,'shared_results',return_value=q.results(require_complete=False)),patch.object(pipeline.shards,'command') as commands,patch('src.ai.summarizer.Summarizer') as summarize:
                with self.assertRaises(IncompleteQwenRecognitionError):pipeline.gather()
            summarize.assert_not_called();commands.assert_not_called()
            audit=json.loads((root/'out/validation-result.json').read_text())
            self.assertFalse(audit['asr_complete']);self.assertFalse(audit['processed']);self.assertFalse(audit['emailed'])
            self.assertEqual(audit['missing_intervals'][0]['chunk_id'],0);self.assertEqual(audit['summary_chars'],0)
            self.assertEqual(audit['error_count'],1)
            saved=pipeline.decode(root/'out/state.enc','state');(root/'after.db').write_bytes(saved['database.db'])
            from src.data.database import Database
            db=Database(str(root/'after.db'));lecture=db.get_lecture('1')
            self.assertIsNone(lecture['summary']);self.assertIsNone(lecture['processed_at']);self.assertEqual(lecture['error_stage'],'sharded_finalize');db.conn.close()


class HistoryInspectionTests(unittest.TestCase):
    def test_latest_release_identifies_same_poison_block_across_workers(self):
        from scripts.production_block_inspection import released_claims
        plan=plan_for(1);store=MemoryStore(initial_queue(plan));q=SharedQueue(plan,store)
        history=[q.snapshot()]
        for n in range(3):
            block,token=q.claim(f'worker-{n}',1);history.append(q.snapshot())
            q.release(0,token);history.append(q.snapshot())
        releases=released_claims(plan,list(reversed(history)))
        self.assertEqual(len(releases),3)
        for n in range(3):self.assertEqual(releases[(f'worker-{n}',1)]['chunk_id'],0)
        self.assertNotIn('text',str(releases))

    def test_finished_block_is_not_misidentified_as_a_failure_release(self):
        from scripts.production_block_inspection import released_claims
        plan=plan_for(1);store=MemoryStore(initial_queue(plan));q=SharedQueue(plan,store)
        b,t=q.claim('worker-0',1);before=q.snapshot();q.finish(0,t,decoded(b))
        self.assertEqual(released_claims(plan,[q.snapshot(),before]),{})
        bad=copy.deepcopy(before);bad['revision']-=1
        with self.assertRaises(ValueError):released_claims(plan,[q.snapshot(),bad])

    def test_block_inspection_requires_ended_parent_before_reading_any_queue(self):
        from scripts import production_block_inspection as reader
        with patch.dict(os.environ,{'SOURCE_RUN_ID':'99','SOURCE_SLOT':'0','GITHUB_REPOSITORY':'test/repo'}),patch.object(pool,'api',return_value={'status':'in_progress','path':'.github/workflows/parallel_pilot.yml'}),patch.object(pool,'store_for') as store:
            with self.assertRaises(ValueError):reader.inspect()
        store.assert_not_called()

    def test_diagnostics_job_is_read_only_and_excludes_other_dispatch_modes(self):
        import yaml
        workflow=yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/qwen_production_validation.yml').read_text())
        job=workflow['jobs']['inspect-block-failures']
        self.assertEqual(job['permissions'],{'contents':'read','actions':'read'})
        self.assertNotIn('STUID',job['env']);self.assertNotIn('UISPSW',job['env']);self.assertNotIn('SMTP_PASSWORD',job['env'])
        for name in ('export','inspect-audio','inspect-authentication','inspect-source-metadata'):
            self.assertIn('!inputs.inspect_block_failures',workflow['jobs'][name]['if'])


if __name__=='__main__':unittest.main()
