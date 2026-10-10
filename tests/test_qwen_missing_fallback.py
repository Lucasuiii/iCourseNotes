"""Synthetic transport + real encrypted gather; never calls campus or cloud."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from scripts.qwen_sharding import build_audio_plan, fingerprint, validate_result
from src.ai.qwen_missing_fallback import repair_missing
from src.ai.qwen_review_ledger import review_prepared, validate_ledger
from src.ai.qwen_transcriber import IncompleteQwenRecognitionError
from src.pipeline.prepared_lecture import assemble_material
from test_qwen_production_pipeline import database, snapshot, pipeline
from test_lecture_quality_gate import _load_runner_class


def fixture(spans=((0, 4),), *, partial=False, audio_sha256='a'*64):
    plan = build_audio_plan({'selection': {'course_id': '10', 'sub_id': '1'},
        'audio_seconds': max(b for a,b in spans), 'full_chunks': [dict(start=a,end=b) for a,b in spans],
        'recognition_terms': [], 'vad_windows': list(spans)}, reference={'pipeline':'production'},
        course_slot=0, run_id='99', audio_sha256=audio_sha256, production=True, mode='shared')
    results=[]
    for shard in plan['shards']:
        rows=[]
        for n in shard['chunk_ids']:
            b=plan['blocks'][n]
            gaps=[dict(start=b['start'],end=b['end'],error_code='retry_timeout')]
            parts=[]
            if partial:
                gaps=[dict(start=1,end=2,error_code='qwen_token_budget')]
                parts=[dict(start=0,end=1,text='前文'),dict(start=2,end=4,text='后文')]
            rows.append(dict(b,text='\n'.join(p['text'] for p in parts),
                quality_state='missing_audio',missing_intervals=gaps,recognized_segments=parts))
        results.append(dict(plan_hash=fingerprint(plan),shard_id=shard['shard_id'],complete=False,chunks=rows))
    return plan,results


def response(path, key, intervals, **kwargs):
    interval=intervals[0]
    return [(interval,[dict(start_ms=interval['start_ms'],end_ms=interval['end_ms'],text='豆包补全')])], \
           (interval['end_ms']-interval['start_ms'])/1000, False


class MissingFallbackTests(unittest.TestCase):
    def test_partial_qwen_is_spliced_in_order_original_results_unchanged(self):
        plan,results=fixture(partial=True);original=copy.deepcopy(results);state={};snapshots=[]
        def checkpoint(): snapshots.append(copy.deepcopy(state))
        def cloud(*args,**kwargs):
            self.assertEqual(snapshots[-1]['attempts'][0]['status'],'reserved')
            self.assertEqual(snapshots[-1]['seconds'],1)
            return response(*args,**kwargs)
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=cloud) as call:
            repaired=repair_missing(plan,results,'local.raw',state,checkpoint,api_key='fake')
        self.assertEqual(results,original);self.assertEqual(call.call_count,1)
        row=repaired[0]['chunks'][0];self.assertEqual(row['text'],'前文\n豆包补全\n后文')
        self.assertEqual(row['missing_intervals'],[]);self.assertEqual(row['quality_state'],'doubao_fallback')
        self.assertEqual(row['recognized_segments'][1]['source'],'doubao_fallback')
        validate_result(plan,repaired[0],0,require_complete=True)
        self.assertTrue(assemble_material(plan,repaired)['complete']);validate_ledger(state)

    def test_success_checkpoint_reused_without_resubmission(self):
        plan,results=fixture();state={}
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=response) as cloud:
            first=repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
            second=repair_missing(plan,results,'local.raw',json.loads(json.dumps(state)),lambda:None,api_key='fake')
        self.assertEqual(cloud.call_count,1);self.assertEqual(first,second);self.assertEqual(state['seconds'],4)

    def test_unknown_submission_keeps_reservation_and_never_replays(self):
        plan,results=fixture();state={};saved=[]
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                repair_missing(plan,results,'local.raw',state,lambda:saved.append(copy.deepcopy(state)),api_key='fake')
        state=saved[-1];self.assertEqual(state['seconds'],4);self.assertEqual(state['attempts'][0]['status'],'reserved')
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm') as cloud:
            repaired=repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
        cloud.assert_not_called();self.assertFalse(repaired[0]['complete']);validate_ledger(state)

    def test_reservation_checkpoint_failure_prevents_transport(self):
        plan,results=fixture()
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm') as cloud:
            with self.assertRaisesRegex(OSError,'checkpoint'):
                repair_missing(plan,results,'local.raw',{},MagicMock(side_effect=OSError('checkpoint')),api_key='fake')
        cloud.assert_not_called()

    def test_failed_empty_misaligned_or_partial_response_retains_gaps(self):
        plan,results=fixture()
        i={'start_ms':0,'end_ms':4000,'kind':'missing_asr'}
        bad=[([],4,True), ([(i,[])],4,False), ([(i,[dict(start_ms=0,end_ms=5000,text='bad')])],4,False),
             ([(i,[dict(start_ms=0,end_ms=4000,text='partial')])],0,False),
             ([(dict(i,end_ms=3000),[dict(start_ms=0,end_ms=3000,text='bad')])],4,False)]
        for result in bad:
            state={}
            with self.subTest(result=result),patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',return_value=result) as call:
                repaired=repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
                repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
            self.assertEqual(call.call_count,1);self.assertFalse(repaired[0]['complete'])
            self.assertEqual(state['attempts'][0]['status'],'failed');self.assertEqual(state['seconds'],4)
            self.assertEqual(repaired[0]['chunks'][0]['text'],'')

    def test_shared_seconds_and_clip_caps_are_never_reset(self):
        plan,results=fixture()
        for attempts in ([dict(interval=dict(start_ms=10000+i*60000,end_ms=70000+i*60000,kind='weak'),
                               seconds=60,status='reserved') for i in range(15)],
                         [dict(interval=dict(start_ms=10000+i*1000,end_ms=11000+i*1000,kind='weak'),
                               seconds=1,status='reserved') for i in range(40)]):
            state={'attempts':attempts,'seconds':sum(a['seconds'] for a in attempts)}
            with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm') as cloud:
                repaired=repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
            cloud.assert_not_called();self.assertFalse(repaired[0]['complete']);validate_ledger(state)

    def test_long_fractional_gap_uses_nonoverlapping_bounded_clips(self):
        plan,results=fixture(((122.107,244.107),));state={}
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=response) as cloud:
            repaired=repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
        self.assertTrue(repaired[0]['complete']);self.assertEqual(cloud.call_count,5)
        self.assertAlmostEqual(state['seconds'],122);self.assertTrue(all(a['seconds']<=30 for a in state['attempts']))
        for a,b in zip(state['attempts'],state['attempts'][1:]):
            self.assertEqual(a['interval']['end_ms'],b['interval']['start_ms'])
        validate_ledger(state)

    def test_quota_exhaustion_keeps_remaining_partial_timeline(self):
        plan,results=fixture(tuple((n*30,n*30+30) for n in range(31)));state={}
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=response) as cloud:
            repaired=repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
        self.assertEqual(cloud.call_count,30);self.assertEqual(state['seconds'],900)
        self.assertEqual(sum(len(r['missing_intervals']) for s in repaired for r in s['chunks']),1)
        with self.assertRaises(ValueError):assemble_material(plan,repaired)

    def test_missing_key_legacy_unaligned_or_overlapping_attempts_never_spend(self):
        plan,results=fixture(partial=True)
        for key,state,original in (('',{},results), ('fake',{},[dict(results[0],chunks=[dict(results[0]['chunks'][0],recognized_segments=[])])]),
                                   ('fake',{'seconds':1,'attempts':[dict(interval={'start_ms':1500,'end_ms':2500,'kind':'weak'},seconds=1,status='reserved')]},results)):
            with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm') as cloud:
                repaired=repair_missing(plan,original,'local.raw',state,lambda:None,api_key=key)
            cloud.assert_not_called();self.assertFalse(repaired[0]['complete'])

    def test_cache_is_bound_to_plan_and_original_results(self):
        plan,results=fixture();state={}
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=response):
            repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
        changed=copy.deepcopy(results);changed[0]['chunks'][0]['missing_intervals'][0]['error_code']='worker_deadline'
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm') as cloud:
            with self.assertRaisesRegex(ValueError,'immutable'):
                repair_missing(plan,changed,'local.raw',state,lambda:None,api_key='fake')
        cloud.assert_not_called()

    def test_cloud_review_does_not_recharge_fallback_or_treat_it_as_quote(self):
        plan,results=fixture();state={}
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=response):
            repaired=repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
        material=assemble_material(plan,repaired,audio_path='local.raw');state.update(homework={'candidates':[], 'intervals':[]},
            weak_intervals=[dict(start_ms=0,end_ms=4000,kind='weak')],intervals=[dict(start_ms=0,end_ms=2000,quote_start_ms=0,quote_end_ms=1000,text='quote')])
        with patch('src.runtime.config.DOUBAO_ASR_API_KEY','fake'),patch('src.ai.doubao_asr.rescue_intervals_pcm') as cloud:
            reviewed=review_prepared(material,[],MagicMock(),state,lambda:None)
        cloud.assert_not_called();self.assertEqual(state['seconds'],4);self.assertEqual(len(state['attempts']),1)
        self.assertEqual(reviewed['variants'],[]);self.assertEqual(reviewed['weak_rescues'],[])


class EncryptedFallbackTests(unittest.TestCase):
    def test_gather_accepts_small_gaps_without_key_but_rejects_fifteen_after_failed_fallback(self):
        import numpy as np
        import soundfile as sf
        import hashlib
        Runner=_load_runner_class()
        for key, gap_seconds in [('',7.5),('fake',15),('fake',16),('',15)]:
            with self.subTest(key=bool(key),seconds=gap_seconds),tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{
                'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,
                'GITHUB_ACTIONS':'false','QWEN_PRODUCTION_TASK':'true','AUTO_COURSE_TERMS':'false','SEND_EMAIL':'false','PUBLISH_RESULTS':'false'}):
                root=pipeline.root();(root/'inbox').mkdir()
                buf=io.BytesIO();sf.write(buf,np.zeros(24*16000),16000,format='FLAC');flac=buf.getvalue()
                plan,results=fixture(((0,24),),audio_sha256=hashlib.sha256(flac).hexdigest())
                row=results[0]['chunks'][0]
                row['recognized_segments']=[dict(start=0,end=1,text='前文课堂内容。'*40),dict(start=1+gap_seconds,end=24,text='后文课堂内容。'*40)]
                row['text']='\n'.join(p['text'] for p in row['recognized_segments'])
                row['missing_intervals']=[dict(start=1,end=1+gap_seconds,error_code='retry_timeout')]
                db=database(root/'fixture.db');payload=snapshot(db,root/'snapshot.db');db.conn.close()
                spec=dict(course_id='10',course_title='概率论',lecture={'sub_id':'1','_validation':{'date':'2026-09-18'}},mode='sharded',plan=plan,media_seconds=24)
                pipeline.encode({'specification.json':pipeline.shards.encoded(spec),'database.db':payload,'lecture.flac':flac},'prepared',root/'inbox/prepared.enc')
                llm=MagicMock();llm.summarize.return_value=('课堂摘要','test')
                with patch.object(pipeline,'artifact',return_value=False),patch.object(pipeline,'shared_results',return_value=results), \
                    patch('src.runtime.config.DOUBAO_ASR_API_KEY',key), \
                    patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',return_value=([],gap_seconds,True)) as cloud, \
                    patch.dict('sys.modules',{'src.pipeline.lecture_runner':SimpleNamespace(LectureRunner=Runner)}), \
                    patch('src.ai.summarizer.Summarizer',return_value=llm),patch('src.ai.qwen_review_ledger.review_prepared',return_value={}):
                    if gap_seconds<15:pipeline.gather()
                    else:
                        with self.assertRaises(IncompleteQwenRecognitionError):pipeline.gather()
                self.assertEqual(cloud.call_count,int(bool(key)))
                audit=json.loads((root/'out/validation-result.json').read_bytes())
                self.assertEqual(audit['asr_missing_seconds'],gap_seconds)
                self.assertEqual(audit['asr_integrity_passed'],gap_seconds<15)
                self.assertEqual(audit['processed'],gap_seconds<15);self.assertFalse(audit['asr_complete'])
                if gap_seconds>=15:llm.summarize.assert_not_called()

    def test_real_gather_checks_audio_then_fills_or_retains_gap_with_private_cache(self):
        import numpy as np
        import soundfile as sf
        import hashlib
        Runner=_load_runner_class()
        for success in (True,False):
            with self.subTest(success=success),tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{
                'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,
                'GITHUB_ACTIONS':'false','QWEN_PRODUCTION_TASK':'true','AUTO_COURSE_TERMS':'false','SEND_EMAIL':'false','PUBLISH_RESULTS':'false'}):
                root=pipeline.root();(root/'inbox').mkdir()
                buf=io.BytesIO();sf.write(buf,np.zeros(4*16000),16000,format='FLAC');flac=buf.getvalue()
                plan,results=fixture(partial=True,audio_sha256=hashlib.sha256(flac).hexdigest())
                row=results[0]['chunks'][0];row['recognized_segments'][0]['text']='前文课堂内容。'*40
                row['recognized_segments'][1]['text']='后文课堂内容。'*40
                row['text']='\n'.join(p['text'] for p in row['recognized_segments'])
                db=database(root/'fixture.db');payload=snapshot(db,root/'snapshot.db');db.conn.close()
                spec=dict(course_id='10',course_title='高代',lecture={'sub_id':'1','_validation':{'date':'2026-09-29'}},mode='sharded',plan=plan,media_seconds=4)
                pipeline.encode({'specification.json':pipeline.shards.encoded(spec),'database.db':payload,'lecture.flac':flac},'prepared',root/'inbox/prepared.enc')
                llm=MagicMock();llm.summarize.return_value=('完整摘要','test')
                def cloud(*args,**kw):
                    saved=pipeline.decode(root/'out/state.enc','state');ledger=json.loads(saved['review.json'])
                    self.assertEqual(ledger['attempts'][0]['status'],'reserved')
                    self.assertTrue(Path(args[0]).is_file())
                    return response(*args,**kw) if success else ([],1,True)
                with patch.object(pipeline,'artifact',return_value=False),patch.object(pipeline,'shared_results',return_value=results), \
                    patch('src.runtime.config.DOUBAO_ASR_API_KEY','fake'), \
                    patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=cloud) as call, \
                    patch.dict('sys.modules',{'src.pipeline.lecture_runner':SimpleNamespace(LectureRunner=Runner)}), \
                    patch('src.ai.summarizer.Summarizer',return_value=llm), \
                    patch('src.ai.qwen_review_ledger.review_prepared',return_value={}):
                    pipeline.gather()
                self.assertEqual(call.call_count,1);saved=pipeline.decode(root/'out/state.enc','state')
                self.assertEqual(json.loads(saved['review.json'])['seconds'],1)
                audit=json.loads((root/'out/validation-result.json').read_bytes())
                self.assertEqual(audit['fallback_clips'],1);self.assertEqual(audit['fallback_seconds'],1)
                self.assertEqual(audit['fallback_completed_clips'],int(success))
                self.assertEqual(audit['asr_complete'],success);self.assertTrue(audit['processed'])
                self.assertTrue(audit['asr_integrity_passed']);self.assertEqual(audit['asr_gap_tolerated'],not success)
                self.assertEqual(audit['asr_missing_seconds'],0 if success else 1)
                self.assertFalse(audit['emailed']);self.assertNotIn('豆包补全',json.dumps(audit,ensure_ascii=False))
                if success:
                    prompt=llm.summarize.call_args.args[1]
                    self.assertLess(prompt.index('前文课堂内容'),prompt.index('豆包补全'))
                    self.assertLess(prompt.index('豆包补全'),prompt.index('后文课堂内容'))
                else:
                    prompt=llm.summarize.call_args.args[1]
                    self.assertIn('有 1 秒语音未识别',prompt)
                    self.assertIn('不得推测或补写',prompt)
                    (root/'saved.db').write_bytes(saved['database.db'])
                    from src.data.database import Database
                    finished=Database(str(root/'saved.db'))
                    self.assertIn('有 1 秒语音未识别',finished.get_lecture('1')['summary'])
                    metadata=json.loads(finished.read_meta('qwen_pipeline:1'))
                    self.assertFalse(metadata['recognition_coverage']['complete'])
                    self.assertTrue(metadata['recognition_coverage']['accepted'])
                    finished.conn.close()

    def test_hash_and_decode_length_gates_precede_cloud_transport(self):
        import numpy as np
        import soundfile as sf
        import hashlib
        for fault in ('hash','decoded_length','media_shortfall'):
            with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0',
                'DB_ENCRYPTION_KEY':'k'*32,'GITHUB_ACTIONS':'false','QWEN_PRODUCTION_TASK':'true'}):
                root=pipeline.root();(root/'inbox').mkdir();buf=io.BytesIO()
                sf.write(buf,np.zeros((4 if fault=='media_shortfall' else 2)*16000),16000,format='FLAC');flac=buf.getvalue()
                plan,results=fixture(audio_sha256='a'*64 if fault=='hash' else hashlib.sha256(flac).hexdigest())
                db=database(root/'fixture.db');payload=snapshot(db,root/'snapshot.db');db.conn.close()
                spec=dict(course_id='10',course_title='高代',lecture={'sub_id':'1'},mode='sharded',plan=plan,
                          media_seconds=400 if fault=='media_shortfall' else 4)
                pipeline.encode({'specification.json':pipeline.shards.encoded(spec),'database.db':payload,'lecture.flac':flac},'prepared',root/'inbox/prepared.enc')
                with patch.object(pipeline,'artifact',return_value=False),patch.object(pipeline,'shared_results',return_value=results), \
                     patch('src.runtime.config.DOUBAO_ASR_API_KEY','fake'),patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm') as cloud:
                    with self.assertRaisesRegex(ValueError,'audio'):
                        pipeline.gather()
                cloud.assert_not_called()

    def test_fallback_material_is_identified_as_hybrid_at_quality_gate(self):
        Runner=_load_runner_class();plan,results=fixture();state={}
        with patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=response):
            repaired=repair_missing(plan,results,'local.raw',state,lambda:None,api_key='fake')
        runner=Runner(None,MagicMock(),MagicMock(),MagicMock(),MagicMock(),MagicMock())
        runner._prepared_asr=assemble_material(plan,repaired)
        runner._get_transcript(None,'10','1')
        self.assertEqual(runner._transcript_source,'hybrid_asr')


if __name__ == '__main__':unittest.main()
