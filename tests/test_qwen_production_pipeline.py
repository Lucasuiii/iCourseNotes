"""Real SQLite/encryption/Git integration, with no course, model or SMTP calls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, MagicMock, patch
import yaml
from scripts.qwen_sharding import build_audio_plan, fingerprint, validate_plan
from scripts.production_db import snapshot, lecture_snapshot, merge_lecture
from scripts import production_qwen as pipeline
from src.data.database import Database
from src.pipeline.prepared_lecture import assemble_material, cached_material
from test_lecture_quality_gate import _load_runner_class

ROOT = Path(__file__).resolve().parents[1]


def fixture(slot=0, run='99'):
    plan = build_audio_plan({'selection': {'course_id':'10','sub_id':'1'}, 'audio_seconds':600,
        'full_chunks':[{'start':0,'end':120},{'start':119,'end':239}],
        'recognition_terms':['条件期望'], 'vad_windows':[[0,120],[119,239]]},
        reference={'pipeline':'production'}, course_slot=slot, run_id=run,
        audio_sha256='a'*64, production=True)
    results=[]
    for shard in plan['shards']:
        rows=[dict(plan['blocks'][i], text=('定义随机变量的分布与期望。'*70 if i==0 else '计算方差并使用条件概率公式。'*70))
              for i in shard['chunk_ids']]
        results.append({'plan_hash':fingerprint(plan),'shard_id':shard['shard_id'],
                        'complete':True,'chunks':rows,'attempts':[]})
    return plan, results


def database(path, *, summary=None):
    db=Database(str(path));db.upsert_course('10','概率论','教师')
    db.insert_lecture('1','10','课次','2026-10-04')
    if summary: db.update_summary('1',summary,'test');db.mark_processed('1')
    return db


class PreparedLectureTests(unittest.TestCase):
    def test_complete_original_blocks_pass_normal_runner_and_persist(self):
        Runner=_load_runner_class();plan,results=fixture()
        material=assemble_material(plan,results,media_seconds=600)
        with tempfile.TemporaryDirectory() as tmp:
            db=database(Path(tmp)/'course.db')
            summarizer=MagicMock();summarizer.summarize.return_value=('完整摘要','test')
            transcriber=MagicMock();scheduler=MagicMock()
            runner=Runner(None,db,scheduler,transcriber,summarizer,MagicMock())
            with patch('src.runtime.config.DOUBAO_ASR_API_KEY',''):
                result=runner.run('10','概率论',{'sub_id':'1'},prepared_asr=material,review_state={},checkpoint=lambda:None)
            row=db.get_lecture('1')
            self.assertEqual(result,'完整摘要');self.assertEqual(row['transcript'],material['transcript'])
            self.assertEqual(row['summary'],'完整摘要');self.assertIsNotNone(row['processed_at'])
            self.assertIsNone(row['emailed_at']);transcriber.transcribe_tail.assert_not_called()
            scheduler.prefetch_lecture.assert_not_called();db.conn.close()

    def test_missing_or_duplicate_shard_cannot_bypass_quality_boundary(self):
        plan,results=fixture()
        for invalid in (results[:1], [results[0],results[0]], [dict(results[0],complete=False),results[1]]):
            with self.assertRaises(ValueError): assemble_material(plan,invalid)

    def test_summary_failure_then_retry_reuses_original_timestamps_and_asr(self):
        Runner=Runner=_load_runner_class();plan,results=fixture();material=assemble_material(plan,results,media_seconds=600)
        with tempfile.TemporaryDirectory() as tmp:
            db=database(Path(tmp)/'course.db');summarizer=MagicMock()
            summarizer.summarize.side_effect=[RuntimeError('temporary'),('恢复摘要','test')]
            transcriber=MagicMock();runner=Runner(None,db,MagicMock(),transcriber,summarizer,MagicMock())
            with patch('src.runtime.config.DOUBAO_ASR_API_KEY',''):
                with self.assertRaises(RuntimeError):
                    runner.run('10','概率论',{'sub_id':'1'},prepared_asr=material,review_state={},checkpoint=lambda:None)
                row=db.get_lecture('1');self.assertIsNotNone(row['transcript']);self.assertIsNone(row['processed_at'])
                result=runner.run('10','概率论',{'sub_id':'1'},prepared_asr=material,review_state={},checkpoint=lambda:None)
            self.assertEqual(result,'恢复摘要');self.assertEqual(db.get_lecture('1')['error_count'],0)
            transcriber.transcribe_tail.assert_not_called();self.assertEqual(transcriber.last_chunks,material['full_chunks'])
            db.conn.close()

    def test_existing_summary_is_preserved_and_not_regenerated(self):
        Runner=_load_runner_class();plan,results=fixture();material=assemble_material(plan,results)
        with tempfile.TemporaryDirectory() as tmp:
            db=database(Path(tmp)/'course.db',summary='历史摘要');db.mark_emailed('1')
            llm=MagicMock();runner=Runner(None,db,MagicMock(),MagicMock(),llm,MagicMock())
            self.assertIsNone(runner.run('10','概率论',{'sub_id':'1'},prepared_asr=material))
            llm.summarize.assert_not_called();self.assertEqual(db.get_lecture('1')['summary'],'历史摘要');db.conn.close()

    def test_silent_complete_audio_uses_existing_no_content_gate(self):
        Runner=_load_runner_class();plan,results=fixture()
        plan['audio_seconds']=1800
        for result in results:
            result['plan_hash']=fingerprint(plan)
            for row in result['chunks']:row['text']=''
        material=assemble_material(plan,results,media_seconds=1800)
        with tempfile.TemporaryDirectory() as tmp:
            db=database(Path(tmp)/'course.db');llm=MagicMock()
            runner=Runner(None,db,MagicMock(),MagicMock(),llm,MagicMock())
            self.assertIsNone(runner.run('10','概率论',{'sub_id':'1'},prepared_asr=material))
            self.assertIsNotNone(db.get_lecture('1')['processed_at']);self.assertIsNone(db.get_lecture('1')['summary'])
            llm.summarize.assert_not_called();db.conn.close()

    def test_production_queue_accepts_more_than_five_total_tasks_and_uncapped_audio(self):
        plan,_=fixture(slot=255);validate_plan(plan)
        plan['course_slot']=256
        with self.assertRaises(ValueError): validate_plan(plan)
        plan,_=fixture();plan['audio_seconds']=10900
        validate_plan(plan)


class ClassroomSelectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('scripts.production_pool.verify_previous_pool'))

    def test_new_fixed_trial_reuses_exact_failed_selection_and_frozen_terms(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'100','GITHUB_REPOSITORY':'owner/repo',
                'GITHUB_RUN_ATTEMPT':'1',
                'COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,'QWEN_PRODUCTION_TASK':'true',
                'VALIDATION_COURSE_ID':'10','VALIDATION_LECTURE_RANK':'1',
                'VALIDATION_BEFORE_DATE':'2026-10-05','VALIDATION_SOURCE_RUN_ID':'99',
                'AUTO_COURSE_TERMS':'true','PUBLISH_RESULTS':'false','SEND_EMAIL':'false','COURSE_IDS':'10'}):
            root=pipeline.root();db=database(root/'fixture.db');db_bytes=snapshot(db,root/'snapshot.db');db.conn.close()
            lecture={'sub_id':'1','date':'2026-10-04','sub_title':'第1-2节',
                '_validation':{'date':'2026-10-04','playable_rank':1,'before_date':'2026-10-05'}}
            frozen={'schema':1,'course_id':'10','sub_id':'1','lecture_date':'2026-10-04',
                    'terms':['条件期望'],'terms_sha256':fingerprint(['条件期望'])}
            spec={'mode':'failed','course_id':'10','lecture':lecture,'glossary_snapshot':frozen}
            with pipeline.shards.environment({'GITHUB_RUN_ID':'99'}):
                pipeline.encode({'queue.json':pipeline.shards.encoded([['10','概率论',lecture]]),
                    'database.db':db_bytes,'history.db':b'preserved-encrypted-history'},'queue',root/'old-queue.enc')
                pipeline.encode({'specification.json':pipeline.shards.encoded(spec),'database.db':db_bytes},
                    'prepared',root/'old-prepared.enc')
            def download(name,target,**kwargs):
                if not kwargs.get('run'):return False
                target.mkdir(parents=True,exist_ok=True)
                file='queue.enc' if name=='qwen-production-queue' else 'prepared.enc'
                (target/file).write_bytes((root/('old-'+file)).read_bytes());return True
            info={'status':'completed','conclusion':'failure','path':'.github/workflows/parallel_pilot.yml'}
            with patch.object(pipeline,'artifact',side_effect=download), \
                 patch.object(pipeline.subprocess,'check_output',return_value=json.dumps(info).encode()), \
                 patch.object(pipeline,'write_outputs'), \
                 patch.object(pipeline,'latest_validation_task') as select:
                pipeline.plan()
            select.assert_not_called()
            saved=pipeline.decode(root/'out'/'queue.enc','queue');task=json.loads(saved['queue.json'])[0]
            self.assertEqual(task[2]['sub_id'],'1');self.assertEqual(task[2]['_frozen_glossary'],frozen)
            self.assertEqual(task[2]['_validation']['source_run_id'],'99')
            self.assertEqual(saved['history.db'],b'preserved-encrypted-history')
            with pipeline.shards.environment({'GITHUB_RUN_ID':'99'}):
                with self.assertRaises(Exception):pipeline.decode(root/'out'/'queue.enc','queue')
            with patch.dict(os.environ,{'GITHUB_RUN_ATTEMPT':'2'}), \
                 patch.object(pipeline,'artifact',return_value=False), \
                 patch.object(pipeline,'validation_source_queue') as source:
                with self.assertRaisesRegex(ValueError,'Rerun has lost its queue checkpoint'):
                    pipeline.plan()
                source.assert_not_called()

    def test_source_rejects_active_runs_success_or_existing_recognition_input(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RUNNER_TEMP':tmp,
                'GITHUB_REPOSITORY':'owner/repo','AUTO_COURSE_TERMS':'false'}):
            for status,conclusion in [('in_progress',None),('completed','success')]:
                with patch.object(pipeline.subprocess,'check_output',return_value=json.dumps({
                        'status':status,'conclusion':conclusion,'path':'.github/workflows/parallel_pilot.yml'}).encode()), \
                     patch.object(pipeline,'artifact') as fetch:
                    with self.assertRaises(ValueError):pipeline.validation_source_queue('99')
                    fetch.assert_not_called()
            lecture={'sub_id':'1'};queue={'queue.json':json.dumps([['10','概率论',lecture]]).encode()}
            for extra in ({'plan':{'blocks':[]}}, {'review':{'seconds':1}}, {'mode':'cached'}, {'audio':True}):
                spec=dict(mode='failed',course_id='10',lecture=lecture,**{k:v for k,v in extra.items() if k!='audio' and k!='mode'})
                spec['mode']=extra.get('mode','failed')
                prepared={'specification.json':json.dumps(spec).encode()}
                if extra.get('audio'):prepared['lecture.flac']=b'original'
                with patch.object(pipeline.subprocess,'check_output',return_value=json.dumps({
                        'status':'completed','conclusion':'failure','path':'.github/workflows/parallel_pilot.yml'}).encode()), \
                     patch.object(pipeline,'artifact'),patch.object(pipeline,'decode',side_effect=[queue,prepared]):
                    with self.assertRaises(ValueError):pipeline.validation_source_queue('99')

    def test_cutoff_selects_older_recording_and_rejects_invalid_dates(self):
        db=MagicMock();db.get_lecture.return_value=None
        client=MagicMock();client.get_course_detail.return_value={'title':'高代','lectures':[
            {'sub_id':'7','date':'2026-09-29'}, {'sub_id':'6','date':'2026-09-28'},
            {'sub_id':'5','date':'2026-09-22'}]}
        client.get_video_url.return_value='private-url'
        task,_=pipeline.latest_validation_task(client,db,'38404',before_date='2026-09-28')
        self.assertEqual(task[2]['sub_id'],'5')
        self.assertEqual(task[2]['_validation']['before_date'],'2026-09-28')
        client.get_video_url.assert_called_once_with('38404','5')
        for cutoff in ('2026-02-30','2026-9-28','bad'):
            with patch.dict(os.environ,{'VALIDATION_BEFORE_DATE':cutoff}):
                with self.assertRaises(ValueError):pipeline.validation_before_date()
        with patch.dict(os.environ,{'VALIDATION_BEFORE_DATE':'2026-09-28','VALIDATION_COURSE_ID':''}):
            with self.assertRaises(ValueError):pipeline.validation_course()

    def test_excluded_exercise_session_is_not_probed_or_counted_in_rank(self):
        from src.runtime.session_rules import parse_course_session_exclusions
        db=MagicMock();db.get_lecture.return_value=None
        client=MagicMock();client.get_course_detail.return_value={'title':'高代','lectures':[
            {'sub_id':'7','date':'2026-09-29','sub_title':'第1-2节'},
            {'sub_id':'6','date':'2026-09-28','sub_title':'第9-10节'},
            {'sub_id':'6','date':'2026-09-28','sub_title':'第9-10节'},
            {'sub_id':'5','date':'2026-09-22','sub_title':'第1-2节'},
            {'sub_id':'4','date':'2026-09-21','sub_title':'第9-10节'}]}
        client.get_video_url.return_value='private-url'
        with patch('src.runtime.config.COURSE_SESSION_EXCLUSIONS',parse_course_session_exclusions('38404=周一第6-10节')):
            task,_=pipeline.latest_validation_task(client,db,'38404',today='2026-10-05',rank=2)
        self.assertEqual(task[2]['sub_id'],'5')
        self.assertEqual(task[2]['_validation']['skipped_excluded'],2)
        self.assertEqual([c.args for c in client.get_video_url.call_args_list],[('38404','7'),('38404','5')])

    def test_penultimate_actual_recording_skips_empty_holidays_and_duplicates(self):
        db = MagicMock(); db.get_lecture.return_value = None
        client = MagicMock(); client.get_course_detail.return_value = {'title': '高代', 'lectures': [
            {'sub_id': '9', 'date': '2026-10-06'},
            {'sub_id': '8', 'date': '2026-10-05'},
            {'sub_id': '7', 'date': '2026-09-29'},
            {'sub_id': '7', 'date': '2026-09-29'},
            {'sub_id': '6', 'date': '2026-09-27'},
            {'sub_id': '5', 'date': '2026-09-22'}]}
        client.get_video_url.side_effect = [None, 'latest-private-url', None, 'penultimate-private-url']
        task, _ = pipeline.latest_validation_task(client, db, '38404', today='2026-10-05', rank=2)
        self.assertEqual(task[2]['sub_id'], '5')
        self.assertEqual(task[2]['_validation'], {'date': '2026-09-22', 'skipped_unavailable': 2, 'playable_rank': 2})
        self.assertEqual([c.args for c in client.get_video_url.call_args_list],
                         [('38404', '8'), ('38404', '7'), ('38404', '6'), ('38404', '5')])

    def test_rank_does_not_fall_back_to_latest_if_only_one_recording_exists(self):
        db = MagicMock(); db.get_lecture.return_value = None
        client = MagicMock(); client.get_course_detail.return_value = {'title': '高代',
            'lectures': [{'sub_id': '1', 'date': '2026-09-29'}]}
        client.get_video_url.return_value = 'private-url'
        with self.assertRaises(ValueError):
            pipeline.latest_validation_task(client, db, '38404', today='2026-10-05', rank=2)

    def test_rank_requires_isolated_validation_and_bounded_integer(self):
        for raw in ('0', '11', '2.0', '-1', 'oops'):
            with patch.dict(os.environ, {'VALIDATION_LECTURE_RANK': raw}):
                with self.assertRaises(ValueError): pipeline.validation_rank()
        with patch.dict(os.environ, {'VALIDATION_LECTURE_RANK': '2', 'VALIDATION_COURSE_ID': ''}):
            with self.assertRaises(ValueError): pipeline.validation_course()

    def test_multiple_ranks_are_bounded_unique_and_only_allowed_for_one_isolated_course(self):
        with patch.dict(os.environ, {'VALIDATION_COURSE_ID':'10', 'COURSE_IDS':'10,20',
                'VALIDATION_LECTURE_RANK':'1', 'VALIDATION_LECTURE_RANKS':'1,2',
                'VALIDATION_BEFORE_DATE':'', 'VALIDATION_SOURCE_RUN_ID':'',
                'VALIDATION_SELECTION_RUN_ID':'', 'PUBLISH_RESULTS':'false', 'SEND_EMAIL':'false'}):
            self.assertEqual(pipeline.validation_ranks(), [1,2])
            self.assertEqual(pipeline.validation_course(), '10')
            for raw in ('1,1', '01,1', '0,2', '1,11', '1,,2', '1.0,2', '١,2', '1,2,3,4,5,6'):
                with self.subTest(raw=raw), patch.dict(os.environ, {'VALIDATION_LECTURE_RANKS':raw}):
                    with self.assertRaises(ValueError): pipeline.validation_ranks()
            for change in ({'VALIDATION_COURSE_ID':''}, {'VALIDATION_COURSE_ID':'10,20'},
                           {'VALIDATION_SOURCE_RUN_ID':'99'}, {'VALIDATION_LECTURE_RANK':'2'},
                           {'PUBLISH_RESULTS':'true'}, {'SEND_EMAIL':'true'}):
                with self.subTest(change=change), patch.dict(os.environ,change):
                    with self.assertRaises(ValueError): pipeline.validation_course()

    def test_same_course_two_lectures_share_one_pool_and_keep_frozen_slots_on_rerun(self):
        from test_shared_asr_queue import MemoryStore
        store=MemoryStore(None)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','GITHUB_RUN_ATTEMPT':'1',
                'GITHUB_SHA':'a'*40,'GITHUB_REPOSITORY':'owner/repo',
                'COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,'QWEN_PRODUCTION_TASK':'true',
                'VALIDATION_COURSE_ID':'10','COURSE_IDS':'10','SHARD_MODE':'shared',
                'VALIDATION_LECTURE_RANK':'1','VALIDATION_LECTURE_RANKS':'1,2',
                'VALIDATION_BEFORE_DATE':'','VALIDATION_SOURCE_RUN_ID':'','VALIDATION_SELECTION_RUN_ID':'',
                'AUTO_COURSE_TERMS':'false','PUBLISH_RESULTS':'false','SEND_EMAIL':'false'}), \
             patch('scripts.production_pool.store_for',return_value=store):
            root=pipeline.root();db=database(root/'history-fixture.db',summary='历史摘要')
            db.insert_lecture('2','10','前一堂','2026-09-29')
            db.update_summary('2','前一堂历史摘要','test');db.mark_processed('2')
            original=snapshot(db,root/'original.db');db.conn.close()
            client=MagicMock();client.get_course_detail.return_value={'title':'概率论','lectures':[
                {'sub_id':'1','date':'2026-10-04'},{'sub_id':'2','date':'2026-09-29'}]}
            client.get_video_url.return_value='https://private.example/recording'
            fake_main=SimpleNamespace(login_with_retry=lambda:None,_enumerate_lectures=MagicMock(),
                _crawl_semester_catalog=MagicMock())
            with patch.dict('sys.modules',{'main':fake_main}), patch.object(pipeline,'artifact',return_value=False), \
                 patch.object(pipeline,'load_remote',side_effect=lambda path:path.write_bytes(original)), \
                 patch('src.api.icourse.ICourseClient',return_value=client), patch.object(pipeline,'write_outputs'):
                pipeline.plan()
            saved=pipeline.decode(root/'out'/'queue.enc','queue');tasks=json.loads(saved['queue.json'])
            self.assertEqual([(t[0],t[2]['sub_id'],t[2]['_validation']['playable_rank']) for t in tasks],
                [('10','1',1),('10','2',2)])
            self.assertEqual(store.state['task_count'],2)
            self.assertEqual(set(store.state['courses']),{'0','1'})
            (root/'fresh.db').write_bytes(saved['database.db']);db=Database(str(root/'fresh.db'))
            self.assertIsNone(db.get_lecture('1')['summary']);self.assertIsNone(db.get_lecture('2')['summary'])
            db.conn.close()
            (root/'preserved.db').write_bytes(saved['history.db']);db=Database(str(root/'preserved.db'))
            self.assertEqual(db.get_lecture('1')['summary'],'历史摘要')
            self.assertEqual(db.get_lecture('2')['summary'],'前一堂历史摘要');db.conn.close()
            audit=json.loads((root/'out'/'validation-selection.json').read_text())
            self.assertEqual([(r['task_slot'],r['course_id'],r['playable_rank']) for r in audit['courses']],
                [(0,'10',1),(1,'10',2)])
            self.assertNotIn('sub_id',json.dumps(audit));self.assertNotIn('https:',json.dumps(audit))
            with patch.dict(os.environ,{'GITHUB_RUN_ATTEMPT':'2'}), patch.object(pipeline,'artifact',return_value=True), \
                 patch.object(pipeline,'decode',return_value=saved), patch.object(pipeline,'latest_validation_task') as select, \
                 patch.object(pipeline,'write_outputs'):
                pipeline.plan();select.assert_not_called()
                for changes in ({'VALIDATION_LECTURE_RANKS':'2,1'},{'VALIDATION_LECTURE_RANKS':'1'}):
                    with patch.dict(os.environ,changes):
                        with self.assertRaises(ValueError): pipeline.plan()
                tampered=dict(saved,**{'queue.json':pipeline.shards.encoded(list(reversed(tasks)))})
                with patch.object(pipeline,'decode',return_value=tampered):
                    with self.assertRaises(ValueError): pipeline.plan()
            fake_main._enumerate_lectures.assert_not_called();fake_main._crawl_semester_catalog.assert_not_called()

    def test_rerun_cannot_change_the_selected_recording_rank(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'RUNNER_TEMP': tmp, 'VALIDATION_COURSE_ID': '38404', 'VALIDATION_LECTURE_RANK': '2',
            'PUBLISH_RESULTS': 'false', 'SEND_EMAIL': 'false', 'COURSE_IDS': '38404'}), \
             patch.object(pipeline, 'artifact', return_value=True), \
             patch.object(pipeline, 'decode', return_value={'queue.json': json.dumps([
                 ['38404', '高代', {'sub_id': '1', '_validation': {'date': '2026-09-29', 'playable_rank': 1}}]
             ]).encode()}):
            with self.assertRaises(ValueError): pipeline.plan()

    def test_latest_real_playback_skips_holidays_future_and_deleted_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = database(Path(tmp)/'history.db', summary='历史摘要')
            db.insert_lecture('9', '10', '删除课次', '2026-10-05')
            with db.conn: db.conn.execute("UPDATE lectures SET deleted_at='removed' WHERE sub_id='9'")
            client = MagicMock()
            client.get_course_detail.return_value = {'title':'概率论', 'teacher':'教师', 'lectures':[
                {'sub_id':'20','date':'2026-10-06'}, {'sub_id':'9','date':'2026-10-05'},
                {'sub_id':'8','date':'2026-10-05','sub_title':'第6节','has_playback':True},
                {'sub_id':'1','date':'2026-10-04','has_playback':False},
                {'sub_id':'4','date':'2026-02-30'}, {'sub_id':'bad','date':'2026-10-05'}]}
            client.get_video_url.side_effect = [None, 'https://private.example/recording']
            task, _ = pipeline.latest_validation_task(client, db, '10', today='2026-10-05')
            self.assertEqual(task[2]['sub_id'], '1')
            self.assertEqual(task[2]['_validation']['skipped_unavailable'], 1)
            self.assertEqual([c.args for c in client.get_video_url.call_args_list], [('10','8'),('10','1')])
            self.assertNotIn('https:', json.dumps(task))
            self.assertEqual(db.get_lecture('1')['summary'], '历史摘要')
            db.conn.close()

    def test_no_recording_is_not_an_authorization_to_process_other_courses(self):
        client = MagicMock(); client.get_course_detail.return_value = {'title':'高代', 'lectures':[
            {'sub_id':'1', 'date':'2026-10-01'}]}
        client.get_video_url.return_value = None
        db = MagicMock(); db.get_lecture.return_value = None
        with self.assertRaises(ValueError):
            pipeline.latest_validation_task(client, db, '38404', today='2026-10-05')
        client.get_course_detail.assert_called_once_with('38404')

    def test_validation_requires_both_side_effect_flags_disabled(self):
        for publish, email in [('true','false'), ('false','true'), ('','false')]:
            with patch.dict(os.environ, {'VALIDATION_COURSE_ID':'38404','COURSE_IDS':'38404,40329',
                                         'PUBLISH_RESULTS':publish,'SEND_EMAIL':email}):
                with self.assertRaises(ValueError): pipeline.validation_course()
        with patch.dict(os.environ, {'VALIDATION_COURSE_ID':'38404','COURSE_IDS':'38404,40329',
                                     'PUBLISH_RESULTS':'false','SEND_EMAIL':'false'}):
            self.assertEqual(pipeline.validation_course(), '38404')

    def test_validation_course_list_is_bounded_subscribed_and_isolated(self):
        with patch.dict(os.environ, {'COURSE_IDS':'10,20,30,40,50,60',
                'VALIDATION_LECTURE_RANK':'1', 'VALIDATION_BEFORE_DATE':'',
                'VALIDATION_SOURCE_RUN_ID':'', 'PUBLISH_RESULTS':'false', 'SEND_EMAIL':'false'}):
            for raw in ('10,10', '10,', '10,bad', '10,99', '10,20,30,40,50,60'):
                with patch.dict(os.environ, {'VALIDATION_COURSE_ID':raw}):
                    with self.assertRaises(ValueError): pipeline.validation_course()
            with patch.dict(os.environ, {'VALIDATION_COURSE_ID':'10, 20'}):
                self.assertEqual(pipeline.validation_course(), '10,20')
                with patch.dict(os.environ, {'VALIDATION_SOURCE_RUN_ID':'99'}):
                    with self.assertRaises(ValueError): pipeline.validation_course()
                for flags in ({'PUBLISH_RESULTS':'true'}, {'SEND_EMAIL':'true'}):
                    with patch.dict(os.environ, flags):
                        with self.assertRaises(ValueError): pipeline.validation_course()

    def test_two_course_trial_preserves_history_and_freezes_both_selections_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'RUNNER_TEMP':tmp, 'GITHUB_RUN_ID':'99', 'GITHUB_RUN_ATTEMPT':'1',
                'COURSE_SLOT':'0', 'DB_ENCRYPTION_KEY':'k'*32, 'QWEN_PRODUCTION_TASK':'true',
                'VALIDATION_COURSE_ID':'10,20', 'COURSE_IDS':'10,20,30', 'SHARD_MODE':'2',
                'VALIDATION_LECTURE_RANK':'1', 'VALIDATION_BEFORE_DATE':'', 'VALIDATION_SOURCE_RUN_ID':'',
                'PUBLISH_RESULTS':'false', 'SEND_EMAIL':'false'}):
            root=pipeline.root(); db=database(root/'history-fixture.db', summary='历史摘要')
            db.upsert_course('20','数值算法',''); db.insert_lecture('2','20','第1节','2026-10-04')
            db.update_summary('2','数值历史摘要','test'); db.mark_processed('2')
            original=snapshot(db,root/'original-snapshot.db'); db.conn.close()
            client=MagicMock()
            client.get_course_detail.side_effect=lambda course: {'title':{'10':'概率论','20':'数值算法'}[course],
                'lectures':[{'sub_id':{'10':'1','20':'2'}[course], 'date':'2026-10-04'}]}
            client.get_video_url.return_value='https://private.example/recording'
            fake_main=SimpleNamespace(login_with_retry=lambda:None, _enumerate_lectures=MagicMock(),
                                      _crawl_semester_catalog=MagicMock())
            with patch.dict('sys.modules', {'main':fake_main}), \
                 patch.object(pipeline,'artifact',return_value=False), \
                 patch.object(pipeline,'load_remote',side_effect=lambda path:path.write_bytes(original)), \
                 patch('src.api.icourse.ICourseClient',return_value=client), patch.object(pipeline,'write_outputs'):
                pipeline.plan()
            saved=pipeline.decode(root/'out'/'queue.enc','queue'); tasks=json.loads(saved['queue.json'])
            self.assertEqual([t[0] for t in tasks], ['10','20'])
            self.assertEqual([c.args[0] for c in client.get_course_detail.call_args_list], ['10','20'])
            (root/'fresh.db').write_bytes(saved['database.db']); db=Database(str(root/'fresh.db'))
            for sub in ('1','2'): self.assertIsNone(db.get_lecture(sub)['summary'])
            db.conn.close()
            (root/'preserved.db').write_bytes(saved['history.db']); db=Database(str(root/'preserved.db'))
            self.assertEqual(db.get_lecture('1')['summary'],'历史摘要')
            self.assertEqual(db.get_lecture('2')['summary'],'数值历史摘要'); db.conn.close()
            audit=json.loads((root/'out'/'validation-selection.json').read_text())
            self.assertEqual([x['course_id'] for x in audit['courses']], ['10','20'])
            self.assertNotIn('sub_id',json.dumps(audit)); self.assertNotIn('https:',json.dumps(audit))
            with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT':'2'}), \
                 patch.object(pipeline,'artifact',return_value=True), patch.object(pipeline,'decode',return_value=saved), \
                 patch.object(pipeline,'latest_validation_task') as select, patch.object(pipeline,'write_outputs'):
                pipeline.plan(); select.assert_not_called()
                tampered=dict(saved, **{'queue.json':pipeline.shards.encoded(list(reversed(tasks)))})
                with patch.object(pipeline,'decode',return_value=tampered):
                    with self.assertRaises(ValueError): pipeline.plan()
            fake_main._enumerate_lectures.assert_not_called()
            fake_main._crawl_semester_catalog.assert_not_called()

    def test_workflow_registration_never_processes_classrooms_on_push(self):
        workflow=yaml.safe_load((ROOT/'.github/workflows/parallel_pilot.yml').read_text())
        self.assertIn("github.event_name != 'push'",workflow['jobs']['plan']['if'])
        self.assertIn("needs.plan.result == 'success'",workflow['jobs']['lecture']['if'])
        self.assertEqual(workflow['jobs']['register']['permissions'],{})
        self.assertEqual(workflow['on']['push']['paths'],['.github/workflows/parallel_pilot.yml'])

    def test_validation_queue_forces_fresh_asr_and_retains_original_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,
            'QWEN_PRODUCTION_TASK':'true','GITHUB_RUN_ATTEMPT':'1','VALIDATION_COURSE_ID':'10',
            'COURSE_IDS':'10','PUBLISH_RESULTS':'false','SEND_EMAIL':'false'}):
            root=pipeline.root(); db=database(root/'original.db', summary='历史摘要')
            original=snapshot(db,root/'original-snapshot.db'); db.conn.close()
            def load(path): path.write_bytes(original)
            client=MagicMock();client.get_course_detail.return_value={'title':'概率论','teacher':'教师',
                'lectures':[{'sub_id':'1','date':'2026-10-04'}]}
            client.get_video_url.return_value='https://private.example/recording'
            fake_main=SimpleNamespace(login_with_retry=lambda:None, _enumerate_lectures=MagicMock(),
                                      _crawl_semester_catalog=MagicMock())
            with patch.dict('sys.modules', {'main':fake_main}), \
                 patch.object(pipeline,'artifact',return_value=False), patch.object(pipeline,'load_remote',side_effect=load), \
                 patch('src.api.icourse.ICourseClient',return_value=client), patch.object(pipeline,'write_outputs') as outputs:
                pipeline.plan()
            saved=pipeline.decode(root/'out'/'queue.enc','queue')
            tasks=json.loads(saved['queue.json']);self.assertEqual(len(tasks),1)
            self.assertEqual(tasks[0][2]['sub_id'],'1')
            (root/'fresh.db').write_bytes(saved['database.db']);db=Database(str(root/'fresh.db'))
            self.assertIsNone(db.get_lecture('1')['summary']);db.conn.close()
            (root/'preserved.db').write_bytes(saved['history.db']);db=Database(str(root/'preserved.db'))
            self.assertEqual(db.get_lecture('1')['summary'],'历史摘要');db.conn.close()
            fake_main._enumerate_lectures.assert_not_called();fake_main._crawl_semester_catalog.assert_not_called()
            outputs.assert_called_once_with(tasks={'include':[{'task_slot':0}]},count=1)
            audit=(root/'out'/'validation-selection.json').read_text()
            self.assertNotIn('sub_id',audit);self.assertNotIn('https:',audit)


class PrivateSummaryExportTests(unittest.TestCase):
    def test_raw_export_preserves_original_shard_text_and_timestamps(self):
        from scripts.production_result_export import raw_qwen_payload
        plan,results=fixture()
        exported=raw_qwen_payload({'mode':'sharded','plan':plan,'media_seconds':600},results)
        expected=assemble_material(plan,results,media_seconds=600)
        self.assertEqual(exported['transcript'],expected['transcript'])
        self.assertEqual(exported['segments'],expected['segments'])
        self.assertEqual(exported['chunks'],expected['full_chunks'])
        with self.assertRaises(ValueError):
            raw_qwen_payload({'mode':'sharded','plan':plan,'media_seconds':600},results[:1])

    def test_summary_export_is_bound_to_recipient_and_source(self):
        from scripts.production_result_export import encrypt,decrypt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        private=X25519PrivateKey.generate()
        raw=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        payload='私有摘要'.encode();blob=encrypt(payload,public,'99',0)
        self.assertNotIn(payload,blob);self.assertEqual(decrypt(blob,raw,'99',0),payload)
        for invalid,run,slot in [(blob,'100',0),(blob,'99',1),(blob[:-1]+bytes([blob[-1]^1]),'99',0)]:
            with self.assertRaises(Exception):decrypt(invalid,raw,run,slot)

    def test_export_reads_exact_completed_summary_without_transcript(self):
        from scripts.production_result_export import summary_payload
        with tempfile.TemporaryDirectory() as tmp:
            db=database(Path(tmp)/'source.db',summary='历史摘要')
            db.update_transcript('1','原课堂全文')
            files={'database.db':snapshot(db,Path(tmp)/'snapshot.db'),
                   'specification.json':pipeline.shards.encoded({'course_id':'10','course_title':'概率论',
                    'lecture':{'sub_id':'1','date':'2026-10-04'},'plan':{'recognition_terms':['条件期望']}}),
                   'review.json':b'{}'}
            db.conn.close();payload=summary_payload(files)
            self.assertEqual(payload['summary'],'历史摘要')
            self.assertEqual(payload['recognition_terms'],['条件期望'])
            self.assertNotIn('原课堂全文',json.dumps(payload,ensure_ascii=False))
            spec=json.loads(files['specification.json']);spec['lecture']['sub_id']='2'
            files['specification.json']=pipeline.shards.encoded(spec)
            with self.assertRaises(ValueError):summary_payload(files)

    def test_export_workflow_has_no_credentials_for_models_or_mail(self):
        workflow=yaml.safe_load((ROOT/'.github/workflows/qwen_production_validation.yml').read_text())
        job=workflow['jobs']['export']
        self.assertEqual(job['permissions'],{'contents':'read','actions':'read'})
        self.assertEqual([k for k in job['env'] if 'secrets.' in job['env'][k]],['DB_ENCRYPTION_KEY'])
        self.assertIn("github.event_name != 'workflow_dispatch'",workflow['jobs']['validate']['if'])


class CloudLedgerTests(unittest.TestCase):
    def test_reserved_unknown_call_is_not_repeated_on_resume(self):
        from src.ai.qwen_review_ledger import review_prepared,validate_ledger
        intervals=[dict(start_ms=i*10000,end_ms=i*10000+1000,quote_start_ms=i*10000,
                        quote_end_ms=i*10000+1000,text='疑点',chunk_id=i) for i in range(2)]
        material={'full_chunks':[],'vad_windows':[],'audio_path':'unused','recognition_terms':[]}
        ledger={'intervals':intervals};checkpoints=[]
        save=lambda:checkpoints.append(copy.deepcopy(ledger))
        with patch('src.ai.qwen_review_ledger.config.DOUBAO_ASR_API_KEY','fake'), \
             patch('src.ai.doubao_asr.rescue_intervals_pcm',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): review_prepared(material,[],MagicMock(),ledger,save)
        self.assertEqual(checkpoints[-1]['attempts'][0]['status'],'reserved')
        self.assertEqual(ledger['seconds'],1)
        def rescue(path,key,windows,**kwargs):
            self.assertEqual(windows,[intervals[1]])
            return [(windows[0],[{'start_ms':10000,'end_ms':11000,'text':'复核版本'}])],1,False
        with patch('src.ai.qwen_review_ledger.config.DOUBAO_ASR_API_KEY','fake'), \
             patch('src.ai.doubao_asr.rescue_intervals_pcm',side_effect=rescue) as transport:
            result=review_prepared(material,[],MagicMock(),ledger,save)
            review_prepared(material,[],MagicMock(),ledger,save)
        self.assertEqual(transport.call_count,1);self.assertEqual(ledger['seconds'],2)
        self.assertEqual(result['uncertain_calls'],1);validate_ledger(ledger)

    def test_tampered_or_excessive_quota_is_rejected(self):
        from src.ai.qwen_review_ledger import validate_ledger
        for ledger in ({'seconds':1},{'attempts':[{'seconds':-1,'interval':{'start_ms':0,'end_ms':1000},'status':'reserved'}], 'seconds':-1}):
            with self.assertRaises(ValueError):validate_ledger(ledger)


class ScopedPublicationTests(unittest.TestCase):
    def test_snapshot_includes_wal_and_only_one_lecture(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);db=database(tmp/'source.db');db.insert_lecture('2','10','另一节','2026-10-04')
            db.write_meta('qwen_pipeline:1','{}');db.write_meta('qwen_pipeline:2','{}')
            data=lecture_snapshot(db,tmp/'delta.db','10','1')
            self.assertTrue(data.startswith(b'SQLite format 3'))
            with sqlite3.connect(tmp/'delta.db') as conn:
                self.assertEqual(conn.execute('SELECT sub_id FROM lectures').fetchall(),[('1',)])
                self.assertEqual(conn.execute('SELECT key FROM meta').fetchall(),[('qwen_pipeline:1',)])
            db.conn.close()

    def test_newer_history_and_tombstones_win_but_matching_mail_receipt_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);local=database(tmp/'local.db',summary='过时摘要');remote=database(tmp/'remote.db',summary='新的摘要')
            local.conn.close();remote.conn.close()
            merge_lecture(tmp/'local.db',tmp/'remote.db','10','1')
            db=Database(str(tmp/'remote.db'));self.assertEqual(db.get_lecture('1')['summary'],'新的摘要');db.conn.close()
            db=Database(str(tmp/'local.db'));db.mark_emailed('1');db.conn.close()
            merge_lecture(tmp/'local.db',tmp/'remote.db','10','1')
            db=Database(str(tmp/'remote.db'));self.assertIsNotNone(db.get_lecture('1')['emailed_at'])
            db.suppress_lectures('10',['1']);db.conn.close()
            merge_lecture(tmp/'local.db',tmp/'remote.db','10','1')
            db=Database(str(tmp/'remote.db'));self.assertIsNone(db.get_lecture('1')['summary']);self.assertIsNotNone(db.get_lecture('1')['deleted_at']);db.conn.close()

    def test_cross_scope_delta_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);local=database(tmp/'local.db');remote=database(tmp/'remote.db')
            local.insert_lecture('2','10','另一个课次','2026-10-04');local.conn.close();remote.conn.close()
            with self.assertRaises(ValueError):merge_lecture(tmp/'local.db',tmp/'remote.db','10','1')

    def test_selected_pending_ppt_and_recovery_metadata_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);local=database(tmp/'local.db');remote=database(tmp/'remote.db')
            for db in (local,remote):db.insert_ppt_pages_pending('1',[{'page_num':1,'created_sec':1}])
            local.update_ppt_page('1',1,'有效OCR','done');local.write_meta('qwen_pipeline:1','{"review":{"seconds":0}}')
            local.conn.close();remote.conn.close();merge_lecture(tmp/'local.db',tmp/'remote.db','10','1')
            db=Database(str(tmp/'remote.db'));self.assertEqual(db.get_done_ppt_pages('1')[0]['text'],'有效OCR')
            self.assertIsNotNone(db.read_meta('qwen_pipeline:1'));db.conn.close()

    def test_real_encrypted_git_publish_conflict_refresh_preserves_both_lessons(self):
        from scripts import production_db as storage
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);bare=tmp/'remote.git'
            subprocess.run(['git','init','--bare','-q',str(bare)],check=True)
            a=database(tmp/'a.db');a.update_summary('1','第一堂摘要','test');a.mark_processed('1');a.conn.close()
            b=Database(str(tmp/'b.db'));b.upsert_course('20','高代','教师');b.insert_lecture('2','20','课次','2026-10-04')
            b.update_summary('2','第二堂摘要','test');b.mark_processed('2');b.conn.close()
            actual=storage.command;injected=False
            def git(args,**kwargs):
                nonlocal injected
                args=[str(bare) if x=='https://github.com/test/repo.git' else x for x in args]
                if args[:2]==['git','push'] and not injected:
                    injected=True;storage.publish(tmp/'b.db','20','2')
                return actual(args,**kwargs)
            with patch.dict(os.environ,{'GITHUB_REPOSITORY':'test/repo','DB_ENCRYPTION_KEY':'k'*32,'COURSE_IDS':'10,20'}), \
                 patch.object(storage,'command',side_effect=git):
                storage.publish(tmp/'a.db','10','1');storage.load_remote(tmp/'verified.db')
            db=Database(str(tmp/'verified.db'))
            self.assertEqual(db.get_lecture('1')['summary'],'第一堂摘要');self.assertEqual(db.get_lecture('2')['summary'],'第二堂摘要')
            db.conn.close();self.assertTrue(injected)


class FormalWorkflowTests(unittest.TestCase):
    def test_daily_primary_and_manual_entry_use_same_bounded_pipeline(self):
        caller=yaml.load((ROOT/'.github/workflows/check.yml').read_text(),Loader=yaml.BaseLoader)
        formal=yaml.load((ROOT/'.github/workflows/parallel_pilot.yml').read_text(),Loader=yaml.BaseLoader)
        child=yaml.load((ROOT/'.github/workflows/qwen_production_lecture.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual([v['cron'] for v in caller['on']['schedule']],['7 9 * * *'])
        self.assertIn('workflow_dispatch',caller['on'])
        self.assertEqual(set(caller['jobs']),{'check'})
        self.assertNotIn('needs',caller['jobs']['check'])
        self.assertNotIn('if',caller['jobs']['check'])
        self.assertEqual(caller['jobs']['check']['with']['publish_results'],'true')
        self.assertEqual(caller['jobs']['check']['with']['send_email'],'true')
        self.assertEqual(caller['jobs']['check']['uses'],'./.github/workflows/parallel_pilot.yml')
        self.assertIn('concurrency',caller);self.assertIn('concurrency',formal);self.assertNotIn('concurrency',child)
        self.assertEqual(caller['jobs']['check']['with']['caller_holds_lock'],'true')
        self.assertIn('icourse-subrun-',formal['concurrency']['group'])
        self.assertEqual(int(formal['jobs']['lecture']['strategy']['max-parallel'])*int(child['jobs']['asr']['strategy']['max-parallel']),15)
        self.assertEqual(child['jobs']['gather']['needs'],['prepare','asr'])
        self.assertEqual(child['jobs']['publish']['needs'],'gather')
        self.assertEqual(formal['on']['workflow_dispatch']['inputs']['send_email']['default'],'false')
        self.assertEqual(formal['on']['workflow_dispatch']['inputs']['publish_results']['default'],'false')
        self.assertNotIn('asr',str(child['jobs']['publish']['steps']))
        self.assertNotIn('toJSON(secrets)',str(formal))
        self.assertNotIn('SECRETS_CONTEXT',str(child))
        self.assertNotIn('SMTP_PASSWORD',child['jobs']['gather']['env'])
        self.assertEqual(child['jobs']['gather']['env']['DEEPSEEK_API_KEY'],'${{ secrets.DEEPSEEK_API_KEY }}')



class EncryptedStageTests(unittest.TestCase):
    def test_source_diagnostic_recovers_cold_session_with_one_fresh_session(self):
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from src.api.webvpn import AuthenticationError
        from scripts import production_media_inspection as inspection
        from scripts.production_result_export import decrypt
        key=X25519PrivateKey.generate()
        private=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RUNNER_TEMP':tmp,'SOURCE_RUN_ID':'99',
                'SOURCE_SLOT':'0','GITHUB_REPOSITORY':'owner/repo','RECIPIENT_PUBLIC_KEY':base64.b64encode(public).decode()}):
            info={'status':'completed','path':'.github/workflows/parallel_pilot.yml','head_sha':'abc'}
            spec={'course_id':'10','lecture':{'sub_id':'1'},'audio_diagnostics':{'audio_seconds':4000}}
            first=MagicMock();first.login.side_effect=AuthenticationError('cold_session');first.auth_diagnostics=[]
            second=MagicMock();second.auth_diagnostics=[{'stage':'api_verification','verified':True}]
            client=MagicMock();client.get_video_url.return_value='private-url';client.get_stream_params.return_value=('url','header')
            with patch.object(inspection.subprocess,'check_output',return_value=json.dumps(info).encode()), \
                 patch.object(pipeline,'artifact',return_value=True), \
                 patch.object(pipeline,'decode',return_value={'specification.json':json.dumps(spec).encode()}), \
                 patch('src.api.webvpn.WebVPNSession',side_effect=[first,second]) as factory, \
                 patch('src.api.icourse.ICourseClient',return_value=client), \
                 patch.object(inspection.time,'sleep') as sleep, \
                 patch.object(inspection,'probe_headers',return_value={'status':'complete','streams':[]}):
                inspection.inspect()
            self.assertEqual(factory.call_count,2);sleep.assert_called_once_with(2)
            first.session.close.assert_called_once();second.session.close.assert_called_once()
            second.authenticate_icourse.assert_called_once_with(strict=True)
            audit=json.loads(decrypt((pipeline.root()/'out/media-inspection.enc').read_bytes(),private,'99',0))
            self.assertEqual(audit['inspection_status'],'complete')
            self.assertEqual(audit['authentication_attempts'][0]['reason'],'cold_session')
            self.assertTrue(audit['authentication_attempts'][1]['verified'])
            self.assertNotIn('private-url',json.dumps(audit))

    def test_source_probe_reason_codes_never_export_private_stderr(self):
        from scripts.production_media_inspection import safe_probe_errors
        error=safe_probe_errors(b'https://private/token HTTP error 403 Forbidden\n'
                               b'Could not seek to position: private-cookie\n'
                               b'Input/output error\n')
        self.assertEqual(error,{'error_counts':{'input_read_error':1,'seek_failed':1},
                               'http_error_statuses':[403]})
        self.assertNotIn('private',json.dumps(error))

    def test_source_inspection_auth_failure_keeps_encrypted_stage_without_private_message(self):
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from scripts import production_media_inspection as inspection
        from scripts.production_result_export import decrypt
        private=X25519PrivateKey.generate()
        private_bytes=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,
                                           serialization.NoEncryption())
        public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{
                'RUNNER_TEMP':tmp,'SOURCE_RUN_ID':'99','SOURCE_SLOT':'0','GITHUB_REPOSITORY':'owner/repo',
                'RECIPIENT_PUBLIC_KEY':base64.b64encode(public).decode()}):
            info={'status':'completed','path':'.github/workflows/parallel_pilot.yml','head_sha':'abc'}
            spec={'course_id':'10','lecture':{'sub_id':'1','date':'2026-09-24'},'audio_diagnostics':{'audio_seconds':4000}}
            vpn=MagicMock();vpn.login.side_effect=RuntimeError('private-password-and-signed-url')
            with patch.object(inspection.subprocess,'check_output',return_value=json.dumps(info).encode()), \
                 patch.object(pipeline,'artifact',return_value=True), \
                 patch.object(pipeline,'decode',return_value={'specification.json':json.dumps(spec).encode()}), \
                 patch('src.api.webvpn.WebVPNSession',return_value=vpn), \
                 patch.object(inspection,'probe_headers') as probe:
                with self.assertRaises(RuntimeError):inspection.inspect()
            payload=json.loads(decrypt((pipeline.root()/'out'/'media-inspection.enc').read_bytes(),private_bytes,'99',0))
            self.assertEqual(payload['failure_stage'],'webvpn_login')
            self.assertEqual(payload['failure_type'],'RuntimeError')
            self.assertEqual(payload['inspection_status'],'failed')
            self.assertNotIn('private-password',json.dumps(payload));probe.assert_not_called()
            vpn.session.close.assert_called_once()

    def test_zero_exit_with_read_errors_cannot_create_asr_plan(self):
        spec={'audio_seconds':600,'media_seconds':600,'audio_diagnostics':{
            'decode_return_code':0,'stderr_complete':True,'decode_error_counts':{'premature_eof':1}}}
        with self.assertRaisesRegex(ValueError,'read or decode errors'):
            pipeline.validate_prepared_audio(spec)
        self.assertEqual(pipeline.failure_code(ValueError('Production audio has read or decode errors')),
                         'audio_decode_errors')
        spec['audio_diagnostics']['decode_error_counts']={}
        spec['audio_diagnostics']['stderr_complete']=False
        with self.assertRaisesRegex(ValueError,'diagnostics are incomplete'):
            pipeline.validate_prepared_audio(spec)
        spec['audio_diagnostics']['stderr_complete']=True
        pipeline.validate_prepared_audio(spec)
        spec['audio_seconds']=400
        with self.assertRaisesRegex(ValueError,'audio is incomplete'):pipeline.validate_prepared_audio(spec)

    def test_retention_waits_for_final_stderr_without_persisting_private_text(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RUNNER_TEMP':tmp}):
            raw=pipeline.root()/'partial.raw';raw.write_bytes(np.zeros(16000,dtype=np.float32).tobytes())
            process=MagicMock();process.poll.return_value=0;process.returncode=0
            counts={};done=MagicMock()
            def drained(timeout):
                counts.update(premature_eof=1);return True
            done.wait.side_effect=drained
            handle=SimpleNamespace(path=raw,process=process,stderr_chunks=[b'private-cookie'],
                                   decode_error_counts=counts,stderr_done=done)
            spec={};files={};pipeline.retain_prepared_audio(handle,spec,files)
            done.wait.assert_called_once_with(timeout=5)
            self.assertEqual(spec['audio_diagnostics']['decode_error_counts'],{'premature_eof':1})
            self.assertNotIn('private',json.dumps(spec));self.assertIn('lecture.flac',files)
            with self.assertRaisesRegex(ValueError,'read or decode errors'):pipeline.validate_prepared_audio(spec)

    def test_source_metadata_header_probe_never_decodes_or_exports_private_fields(self):
        from scripts.production_media_inspection import probe_headers, safe_metadata
        raw={'format':{'duration':'6565.43','filename':'private-url','tags':{'password':'secret'}},
             'streams':[{'index':1,'codec_type':'audio','codec_name':'aac','start_time':'0',
                'duration':'4078.527','time_base':'1/16000','tags':{'comment':'classroom'}},
                {'index':0,'codec_type':'video','duration':'6565.43','start_time':'0'},
                {'duration':'nan','start_time':'inf','codec_name':'private://secret','time_base':'invalid'}]}
        result=safe_metadata(raw)
        self.assertEqual(result['streams'][0]['end_time'],4078.527)
        self.assertEqual(result['streams'][1]['end_time'],6565.43)
        self.assertEqual(result['streams'][2],{})
        self.assertNotIn('private',json.dumps(result));self.assertNotIn('classroom',json.dumps(result))
        with patch('scripts.production_media_inspection.subprocess.run',return_value=SimpleNamespace(
                returncode=0,stdout=json.dumps(raw).encode(),stderr=b'')) as run:
            result=probe_headers('private-url','private-header')
        command=run.call_args.args[0]
        self.assertEqual(command[0],'ffprobe');self.assertIn('-nofind_stream_info',command)
        self.assertNotIn('-show_packets',command);self.assertNotIn('-show_frames',command)
        self.assertTrue(result['header_only']);self.assertEqual(result['status'],'complete')
        with patch('scripts.production_media_inspection.subprocess.run',return_value=SimpleNamespace(
                returncode=1,stdout=b'',stderr=b'private-url/secret')):
            failed=probe_headers('url','headers')
        self.assertEqual(failed['status'],'failed');self.assertNotIn('secret',json.dumps(failed))

    def test_source_metadata_workflow_has_no_models_publishers_or_mail(self):
        workflow=yaml.safe_load((ROOT/'.github/workflows/qwen_production_validation.yml').read_text())
        job=workflow['jobs']['inspect-source-metadata']
        self.assertEqual(job['permissions'],{'contents':'read','actions':'read'})
        self.assertEqual({k for k,v in job['env'].items() if 'secrets.' in v},
                         {'DB_ENCRYPTION_KEY','StuId','UISPsw'})
        text=json.dumps(job)
        self.assertNotIn('production_qwen prepare',text);self.assertNotIn('qwen_transcriber',text)
        self.assertNotIn('sharded_qwen_pilot worker',text)
        uploads=[s['with']['name'] for s in job['steps'] if s.get('uses')=='actions/upload-artifact@v4']
        self.assertEqual(uploads,['qwen-media-private-inspection-${{ github.run_attempt }}'])

    def test_late_audio_packet_diagnostic_is_bounded_and_never_decodes_payload(self):
        from scripts.production_media_inspection import probe_late_packets
        packets={'packets':[{'stream_index':1,'pts_time':'6500.1','duration_time':'0.021',
                            'data':'private audio','tags':{'secret':'not exported'}}]}
        with patch('scripts.production_media_inspection.subprocess.run',return_value=SimpleNamespace(
                returncode=0,stdout=json.dumps(packets).encode(),stderr=b'')) as run:
            result=probe_late_packets('private-url','header',4078,{'end_time':6565})
        command=run.call_args.args[0]
        self.assertEqual(command[0],'ffprobe');self.assertIn('-nofind_stream_info',command)
        self.assertEqual(command[command.index('-read_intervals')+1],'4088.000%+#64,6555.000%+#64')
        self.assertNotIn('-show_data',command);self.assertNotIn('-show_frames',command)
        self.assertEqual(result['packets'][0]['pts_time'],6500.1)
        self.assertNotIn('private',json.dumps(result));self.assertFalse(result['decoding'])
        with patch('scripts.production_media_inspection.subprocess.run') as run:
            self.assertEqual(probe_late_packets('url','head',4078,{'end_time':4080})['status'],'not_needed')
            run.assert_not_called()
        with patch('scripts.production_media_inspection.subprocess.run',return_value=SimpleNamespace(
                returncode=0,stdout=json.dumps({'packets':[{}]*129}).encode(),stderr=b'')):
            with self.assertRaises(ValueError):probe_late_packets('url','header',4078,{'end_time':6565})

    def test_incomplete_preparation_retains_audio_durations_terms_and_refuses_refetch(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,
                'QWEN_PRODUCTION_TASK':'true','AUTO_COURSE_TERMS':'true','PUBLISH_RESULTS':'false',
                'SHARD_MODE':'shared','GITHUB_ACTIONS':'false'}):
            root=pipeline.root(); db=database(root/'fixture.db')
            lecture={'sub_id':'1','date':'2026-10-04','_validation':{'date':'2026-10-04'}}
            frozen={'schema':1,'course_id':'10','sub_id':'1','lecture_date':'2026-10-04',
                    'terms':['条件期望'],'terms_sha256':fingerprint(['条件期望'])}
            lecture['_frozen_glossary']=frozen
            raw=root/'source.raw';raw.write_bytes(np.full(4*16000,.1,dtype=np.float32).tobytes())
            process=MagicMock();process.poll.return_value=0;process.returncode=0
            handle=SimpleNamespace(path=str(raw),process=process,timeline_preserved=True,
                stderr_chunks=[b'private-url:token\n Duration: 00:06:40.00, start: 0.000000'])
            scheduler=MagicMock();scheduler.audio_downloader.get.return_value=handle
            transcriber=MagicMock();transcriber.prepare_pcm_stream.return_value=[(1,3)]
            transcriber.last_audio_duration=4;transcriber.last_media_duration=400;transcriber.last_vad_windows=[(1,3)]
            with patch.object(pipeline,'artifact',return_value=False), \
                 patch.object(pipeline,'task_files',return_value=(db,'10','概率论',lecture)), \
                 patch.dict('sys.modules',{'main':SimpleNamespace(login_with_retry=lambda:None),
                    'src.pipeline.ppt_pipeline':SimpleNamespace(PPTPipeline=MagicMock()),
                    'src.api.icourse':SimpleNamespace(ICourseClient=MagicMock())}), \
                 patch('src.runtime.scheduler.Scheduler',return_value=scheduler), \
                 patch('src.ai.qwen_transcriber.QwenTranscriber',return_value=transcriber), \
                 patch.object(pipeline,'freeze_course_terms') as freeze:
                with self.assertRaisesRegex(ValueError,'Production audio is incomplete'):pipeline.prepare()
            freeze.assert_not_called();transcriber.recognize_blocks.assert_not_called()
            scheduler.audio_downloader.schedule.assert_called_once_with(ANY,'10','1',preserve_timestamps=True)
            saved=pipeline.decode(root/'out'/'prepared.enc','prepared');spec=json.loads(saved['specification.json'])
            self.assertEqual(spec['mode'],'failed');self.assertEqual(spec['error_code'],'incomplete_audio')
            self.assertEqual(spec['glossary_snapshot'],frozen);self.assertEqual(spec['audio_seconds'],4)
            self.assertEqual(spec['audio_diagnostics']['duration_gap_seconds'],396)
            self.assertTrue(spec['audio_diagnostics']['audio_retained'])
            self.assertEqual(hashlib.sha256(saved['lecture.flac']).hexdigest(),spec['audio_diagnostics']['audio_sha256'])
            self.assertNotIn('private-url',json.dumps(spec))
            (root/'inbox').mkdir();pipeline.encode(saved,'prepared',root/'inbox'/'prepared.enc')
            with patch.object(pipeline,'artifact',return_value=False):
                with self.assertRaisesRegex(ValueError,'Preparation failed'):pipeline.gather()
            audit=json.loads((root/'out'/'validation-result.json').read_bytes())
            self.assertEqual(audit['audio_seconds'],4);self.assertEqual(audit['media_seconds'],400)
            self.assertEqual(audit['frozen_terms_count'],1);self.assertEqual(audit['asr_execution'],'not_started')
            self.assertFalse(audit['asr_complete']);self.assertFalse(audit['processed'])
            with patch.object(pipeline,'artifact',return_value=True),patch.object(pipeline,'decode',return_value=saved), \
                 patch.object(pipeline,'task_files') as task:
                with self.assertRaisesRegex(ValueError,'Production audio is incomplete'):pipeline.prepare()
            task.assert_not_called()

    def test_failed_decode_retains_actual_samples_without_claiming_success(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RUNNER_TEMP':tmp}):
            raw=pipeline.root()/'partial.raw';raw.write_bytes(np.zeros(16000,dtype=np.float32).tobytes())
            process=MagicMock();process.poll.return_value=1;process.returncode=1
            handle=SimpleNamespace(path=raw,process=process,stderr_chunks=[b'Duration: 00:10:00.00'])
            spec={};files={};pipeline.retain_prepared_audio(handle,spec,files)
            self.assertEqual(spec['audio_diagnostics']['decode_return_code'],1)
            self.assertEqual(spec['audio_seconds'],1);self.assertEqual(spec['media_seconds'],600)
            self.assertIn('lecture.flac',files);self.assertNotIn('plan',spec)

    def test_existing_audio_inspection_exports_private_clips_without_asr_or_retrieval(self):
        import base64
        import numpy as np
        import soundfile as sf
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from scripts import production_audio_inspection as inspection
        from scripts.production_result_export import decrypt
        private = X25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'RUNNER_TEMP': tmp, 'GITHUB_RUN_ID': '100', 'GITHUB_REPOSITORY': 'owner/repo',
                'SOURCE_RUN_ID': '99', 'SOURCE_SLOT': '0', 'QWEN_PRODUCTION_TASK': 'true',
                'RECIPIENT_PUBLIC_KEY': base64.b64encode(public).decode()}):
            root = pipeline.root(); flac = root/'fixture.flac'
            sf.write(flac, np.zeros(60*16000, dtype='float32'), 16000, subtype='PCM_24')
            stats, levels = inspection.audio_stats(flac)
            self.assertEqual(stats['peak'], 0); self.assertEqual(stats['rms'], 0)
            self.assertEqual(stats['seconds_below_rms_1e_5'], 60)
            offsets = inspection.listening_offsets(levels, 60)
            self.assertTrue(all(0 <= n <= 50 for n in offsets)); self.assertLessEqual(len(offsets), 8)
            plan = build_audio_plan({'selection': {'course_id':'10','sub_id':'1'}, 'audio_seconds':60,
                'full_chunks':[], 'recognition_terms':[], 'vad_windows':[]}, reference={'pipeline':'production'},
                course_slot=0, run_id='99', audio_sha256=hashlib.sha256(flac.read_bytes()).hexdigest(),
                production=True, mode='shared')
            spec = {'plan':plan, 'course_id':'10','course_title':'高等代数',
                    'lecture':{'sub_id':'1','date':'2026-09-28','sub_title':'2026-09-28第1-2节'}}
            files = {'specification.json':pipeline.shards.encoded(spec),'lecture.flac':flac.read_bytes()}
            transcriber = MagicMock(); transcriber._model = None
            transcriber.prepare_pcm_stream.return_value = []; transcriber.last_vad_windows = []
            with patch.object(inspection.subprocess,'check_output',return_value=json.dumps({
                    'status':'completed','path':'.github/workflows/parallel_pilot.yml','head_sha':'a'*40}).encode()), \
                 patch.object(pipeline,'artifact') as artifact, patch.object(pipeline,'decode',return_value=files), \
                 patch('src.ai.qwen_transcriber.QwenTranscriber',return_value=transcriber):
                inspection.inspect()
            artifact.assert_called_once_with('qwen-production-prepare-0',root/'audio-inspection-source',run='99',required=True)
            transcriber._init.assert_not_called(); transcriber.recognize_blocks.assert_not_called()
            raw_private = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
            payload = json.loads(decrypt((root/'out'/'audio-inspection.enc').read_bytes(),raw_private,'99',0))
            self.assertEqual(payload['sub_title'],spec['lecture']['sub_title'])
            self.assertEqual(payload['original_blocks'],0); self.assertTrue(payload['clips'])
            for clip in payload['clips']:
                data = base64.b64decode(clip['mp3_base64'])
                self.assertEqual(hashlib.sha256(data).hexdigest(),clip['sha256'])
            spec.pop('plan');spec.update(mode='failed',audio_diagnostics={'audio_retained':True,
                'audio_seconds':60,'audio_sha256':plan['audio_sha256']})
            files['specification.json']=pipeline.shards.encoded(spec)
            with patch.object(inspection.subprocess,'check_output',return_value=json.dumps({
                    'status':'completed','path':'.github/workflows/parallel_pilot.yml','head_sha':'a'*40}).encode()), \
                 patch.object(pipeline,'artifact'),patch.object(pipeline,'decode',return_value=files), \
                 patch('src.ai.qwen_transcriber.QwenTranscriber',return_value=transcriber):
                inspection.inspect()
            failed=json.loads(decrypt((root/'out'/'audio-inspection.enc').read_bytes(),raw_private,'99',0))
            self.assertEqual(failed['preparation_mode'],'failed');self.assertEqual(failed['metrics']['audio_seconds'],60)
            self.assertEqual(failed['original_blocks'],0);transcriber.recognize_blocks.assert_not_called()

    def test_isolated_no_content_trial_fails_and_retains_checkpoint(self):
        Runner = _load_runner_class(); plan, results = fixture()
        plan['audio_seconds'] = 1800
        for result in results:
            result['plan_hash'] = fingerprint(plan)
            for row in result['chunks']: row['text'] = ''
        material = assemble_material(plan, results, media_seconds=1800)
        for isolated in (True, False):
            with self.subTest(isolated=isolated), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                    'RUNNER_TEMP': tmp, 'GITHUB_RUN_ID': '99', 'COURSE_SLOT': '0',
                    'DB_ENCRYPTION_KEY': 'k'*32, 'GITHUB_ACTIONS': 'false',
                    'QWEN_PRODUCTION_TASK': 'true', 'AUTO_COURSE_TERMS': 'false'}):
                root = pipeline.root(); (root/'inbox').mkdir()
                db = database(root/'fixture.db'); payload = snapshot(db, root/'snapshot.db'); db.conn.close()
                lecture = {'sub_id': '1'}
                if isolated: lecture['_validation'] = {'date': '2026-10-04', 'playable_rank': 2}
                spec = {'course_id': '10', 'course_title': '概率论', 'lecture': lecture,
                        'mode': 'cached', 'material': material, 'review': {'complete': True}}
                pipeline.encode({'specification.json': pipeline.shards.encoded(spec), 'database.db': payload},
                                'prepared', root/'inbox'/'prepared.enc')
                llm = MagicMock()
                with patch.dict('sys.modules', {'src.pipeline.lecture_runner': SimpleNamespace(LectureRunner=Runner)}), \
                     patch.object(pipeline, 'artifact', return_value=False), \
                     patch('src.ai.summarizer.Summarizer', return_value=llm), \
                     patch('src.runtime.config.DOUBAO_ASR_API_KEY', ''), \
                     patch.object(pipeline.shards, 'command') as commands:
                    if isolated:
                        with self.assertRaisesRegex(ValueError, 'Isolated validation produced no transcript or summary'):
                            pipeline.gather()
                    else:
                        pipeline.gather()
                commands.assert_not_called(); llm.summarize.assert_not_called()
                saved = pipeline.decode(root/'out'/'state.enc', 'state')
                self.assertEqual(json.loads(saved['review.json']), {'complete': True})
                (root/'saved.db').write_bytes(saved['database.db']); db = Database(str(root/'saved.db'))
                row = db.get_lecture('1')
                self.assertTrue(row['processed_at']); self.assertFalse(row['summary']); self.assertFalse(row['emailed_at'])
                self.assertEqual(row['error_count'], int(isolated))
                if isolated:
                    self.assertEqual(row['error_stage'], 'sharded_finalize')
                    audit = json.loads((root/'out'/'validation-result.json').read_bytes())
                    self.assertEqual(audit['transcript_chars'], 0); self.assertEqual(audit['summary_chars'], 0)
                    self.assertEqual(audit['error_count'], 1)
                db.conn.close()

    def test_encrypted_finalization_retries_summary_without_decoding_or_resetting_quota(self):
        Runner=_load_runner_class();plan,results=fixture();material=assemble_material(plan,results,media_seconds=600)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
            'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,
            'QWEN_PRODUCTION_TASK':'true','GITHUB_ACTIONS':'false','AUTO_COURSE_TERMS':'false'}):
            root=pipeline.root();(root/'inbox').mkdir()
            db=database(root/'fixture.db');payload=snapshot(db,root/'snapshot.db');db.conn.close()
            interval={'start_ms':0,'end_ms':1000,'quote_start_ms':0,'quote_end_ms':1000,'text':'疑点'}
            review={'complete':True,'seconds':1,'attempts':[{'interval':interval,'seconds':1,'status':'reserved'}],
                    'material':{'variants':[],'uncertain_calls':1}}
            spec={'course_id':'10','course_title':'概率论','lecture':{'sub_id':'1',
                  '_validation':{'date':'2026-10-04','skipped_unavailable':2}},'mode':'cached',
                  'material':material,'review':review}
            pipeline.encode({'specification.json':pipeline.shards.encoded(spec),'database.db':payload},
                            'prepared',root/'inbox'/'prepared.enc')
            llm=MagicMock();llm.summarize.side_effect=[RuntimeError('temporary'),('恢复后的摘要','test')]
            with patch.dict('sys.modules', {'src.pipeline.lecture_runner':SimpleNamespace(LectureRunner=Runner)}), \
                 patch.object(pipeline,'artifact',return_value=False), \
                 patch('src.ai.summarizer.Summarizer',return_value=llm), \
                 patch('src.runtime.config.DOUBAO_ASR_API_KEY',''), \
                 patch.object(pipeline.shards,'command') as commands:
                with self.assertRaises(RuntimeError):pipeline.gather()
            saved=pipeline.decode(root/'out'/'state.enc','state')
            self.assertEqual(json.loads(saved['review.json'])['seconds'],1)
            recovery=root/'after-failure.db';recovery.write_bytes(saved['database.db'])
            db=Database(str(recovery));self.assertIsNotNone(db.get_lecture('1')['transcript'])
            self.assertIsNone(db.get_lecture('1')['summary']);db.conn.close();commands.assert_not_called()
            def restore(name,target,**kwargs):
                target.mkdir(exist_ok=True)
                (target/'state.enc').write_bytes((root/'out'/'state.enc').read_bytes())
                return True
            with patch.dict('sys.modules', {'src.pipeline.lecture_runner':SimpleNamespace(LectureRunner=Runner)}), \
                 patch.object(pipeline,'artifact',side_effect=restore), \
                 patch('src.ai.summarizer.Summarizer',return_value=llm), \
                 patch('src.runtime.config.DOUBAO_ASR_API_KEY',''), \
                 patch.object(pipeline.shards,'command') as commands:
                pipeline.gather()
            saved=pipeline.decode(root/'out'/'state.enc','state');commands.assert_not_called()
            self.assertEqual(json.loads(saved['review.json'])['seconds'],1)
            recovery.write_bytes(saved['database.db']);db=Database(str(recovery))
            self.assertEqual(db.get_lecture('1')['summary'],'恢复后的摘要')
            self.assertIsNotNone(db.get_lecture('1')['processed_at'])
            self.assertIsNone(db.get_lecture('1')['emailed_at'])
            audit=json.loads(db.read_meta('qwen_pipeline:1'));self.assertTrue(audit['complete'])
            self.assertNotIn('material',audit);db.conn.close()
            audit=json.loads((root/'out'/'validation-result.json').read_bytes())
            self.assertTrue(audit['processed']);self.assertTrue(audit['asr_complete'])
            self.assertFalse(audit['emailed']);self.assertEqual(audit['review_seconds'],1)
            self.assertEqual(audit['summary_chars'],len('恢复后的摘要'))
            self.assertNotIn('恢复后的摘要',json.dumps(audit,ensure_ascii=False))

    def test_missing_asr_artifact_saves_retry_state_without_constructing_summarizer(self):
        _load_runner_class();plan,_=fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
            'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,
            'QWEN_PRODUCTION_TASK':'true','GITHUB_ACTIONS':'false'}):
            root=pipeline.root();(root/'inbox').mkdir()
            db=database(root/'fixture.db');payload=snapshot(db,root/'snapshot.db');db.conn.close()
            spec={'course_id':'10','course_title':'概率论','lecture':{'sub_id':'1'},'mode':'sharded',
                  'plan':plan,'media_seconds':600}
            pipeline.encode({'specification.json':pipeline.shards.encoded(spec),'database.db':payload},
                            'prepared',root/'inbox'/'prepared.enc')
            with patch.object(pipeline,'artifact',return_value=False),patch('src.ai.summarizer.Summarizer') as llm:
                with self.assertRaises(FileNotFoundError):pipeline.gather()
            llm.assert_not_called();saved=pipeline.decode(root/'out'/'state.enc','state')
            path=root/'verified.db';path.write_bytes(saved['database.db']);db=Database(str(path))
            self.assertIsNone(db.get_lecture('1')['transcript']);self.assertIsNone(db.get_lecture('1')['summary'])
            self.assertEqual(db.get_lecture('1')['error_stage'],'sharded_finalize')
            self.assertEqual(json.loads(db.read_meta('qwen_pipeline:1'))['recovery']['plan_hash'],fingerprint(plan))
            db.conn.close()

    def test_cross_run_recovery_rebinds_validated_completed_blocks(self):
        plan,results=fixture(slot=4,run='88');results[0]['complete']=False
        results[0]['chunks']=[]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
            'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'3','DB_ENCRYPTION_KEY':'k'*32,
            'QWEN_PRODUCTION_TASK':'true','GITHUB_ACTIONS':'false'}):
            root=pipeline.root();db=database(root/'fixture.db')
            db.write_meta('qwen_pipeline:1',json.dumps({'review':{'seconds':0},
              'recovery':{'run_id':'88','task_slot':4,'plan_hash':fingerprint(plan)}}))
            spec={'course_id':'10','course_title':'概率论','lecture':{'sub_id':'1'},'mode':'sharded','plan':plan}
            def artifacts(name,target,**kwargs):
                if '-state-' in name:return False
                target.mkdir(parents=True,exist_ok=True)
                with pipeline.shards.environment({'GITHUB_RUN_ID':'88','COURSE_SLOT':'4'}):
                    if 'prepare' in name:
                        pipeline.encode({'specification.json':pipeline.shards.encoded(spec)},'prepared',target/'prepared.enc')
                    else:
                        sid=int(name.rsplit('-',1)[1])
                        pipeline.encode({'result.json':pipeline.shards.encoded(results[sid])},f'result-{sid}',target/'worker-result.enc')
                return True
            with patch.object(pipeline,'artifact',side_effect=artifacts):
                files=pipeline.recover_preparation(db,'10','1')
            updated=json.loads(files['specification.json'])['plan']
            self.assertEqual(updated['run_id'],'99');self.assertEqual(updated['course_slot'],3)
            self.assertEqual(updated['blocks'],plan['blocks']);self.assertEqual(updated['audio_sha256'],plan['audio_sha256'])
            for shard in updated['shards']:
                saved=json.loads(files[f'completed-{shard["shard_id"]}.json'])
                self.assertEqual(saved['plan_hash'],fingerprint(updated))
                self.assertEqual(saved['chunks'],results[shard['shard_id']]['chunks'])
            db.conn.close()

    def test_queue_encryption_uses_fixed_queue_slot_but_task_results_are_bound(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
            'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,
            'QWEN_PRODUCTION_TASK':'true','GITHUB_ACTIONS':'false'}):
            root=pipeline.root();pipeline.encode({'queue.json':b'[]'},'queue',root/'queue.enc')
            pipeline.encode({'private':b'classroom'},'prepared',root/'prepared.enc')
            os.environ['COURSE_SLOT']='23'
            self.assertEqual(pipeline.decode(root/'queue.enc','queue')['queue.json'],b'[]')
            with self.assertRaises(Exception):pipeline.decode(root/'prepared.enc','prepared')


class RecoveryBoundaryTests(unittest.TestCase):
    def test_later_lost_finalization_cannot_refund_an_older_artifact(self):
        with patch.object(pipeline,'last_finalization_attempt',return_value=2):
            with self.assertRaises(ValueError):pipeline.validate_checkpoint_age({'attempt.json':b'1'},'99',0)
            pipeline.validate_checkpoint_age({'attempt.json':b'2'},'99',0)

    def test_weak_speech_rescue_keeps_whole_lecture_quota(self):
        from src.ai.qwen_review_ledger import review_prepared
        material={'full_chunks':[],'vad_windows':[],'audio_path':'unused','recognition_terms':[],
                  'audio_seconds':60,'transcript':'','weak_windows':[{'start_ms':0,'end_ms':20000,'text':''}]}
        state={'review_scope':'full'};saved=[]
        def recognize(path,key,windows,**kw):
            self.assertEqual(saved[-1]['seconds'],20)
            self.assertEqual(saved[-1]['attempts'][0]['status'],'reserved')
            return [(windows[0],[{'start_ms':0,'end_ms':20000,'text':'恢复实际讲话'}])],20,False
        with patch('src.ai.qwen_review_ledger.config.DOUBAO_ASR_API_KEY','fake'), \
             patch('src.ai.doubao_asr.rescue_intervals_pcm',side_effect=recognize):
            result=review_prepared(material,[],MagicMock(),state,lambda:saved.append(copy.deepcopy(state)))
        self.assertEqual(state['seconds'],20);self.assertEqual(len(result['weak_rescues']),1)
        self.assertEqual(result['variants'],[]);self.assertTrue(state['complete'])

    def test_cloud_caps_apply_across_all_candidates(self):
        from src.ai.qwen_review_ledger import review_prepared
        intervals=[dict(start_ms=i*60000,end_ms=(i+1)*60000,quote_start_ms=i*60000,
                        quote_end_ms=(i+1)*60000,text='疑点') for i in range(18)]
        state={'intervals':intervals};material={'full_chunks':[],'vad_windows':[],
                'audio_path':'unused','recognition_terms':[]}
        with patch('src.ai.qwen_review_ledger.config.DOUBAO_ASR_API_KEY','fake'), \
             patch('src.ai.doubao_asr.rescue_intervals_pcm',return_value=([],60,False)) as recognize:
            review_prepared(material,[],MagicMock(),state,lambda:None)
        self.assertEqual(recognize.call_count,15);self.assertEqual(state['seconds'],900)
        self.assertLessEqual(len(state['attempts']),40)

    def test_saved_review_variants_survive_missing_api_key(self):
        Runner=_load_runner_class();plan,results=fixture();material=assemble_material(plan,results)
        with tempfile.TemporaryDirectory() as tmp:
            db=database(Path(tmp)/'course.db');llm=MagicMock();llm.summarize.return_value=('摘要','test')
            runner=Runner(None,db,MagicMock(),MagicMock(),llm,MagicMock())
            review={'seconds':0,'complete':True,'material':{'variants':[{'original_quote':'疑点','cloud_text':'已有复核证据'}]}}
            with patch('src.runtime.config.DOUBAO_ASR_API_KEY',''):
                runner.run('10','概率论',{'sub_id':'1'},prepared_asr=material,review_state=review,checkpoint=lambda:None)
            self.assertIn('已有复核证据',llm.summarize.call_args.args[1]);db.conn.close()


class CompletedSummaryRecoveryTests(unittest.TestCase):
    def test_saved_summary_before_processed_marker_recovers_without_llm(self):
        Runner=_load_runner_class()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
            'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'99','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,
            'QWEN_PRODUCTION_TASK':'true','GITHUB_ACTIONS':'false','AUTO_COURSE_TERMS':'false'}):
            root=pipeline.root();(root/'inbox').mkdir()
            db=database(root/'fixture.db');db.update_summary('1','已保存的摘要','test')
            self.assertIsNone(db.get_lecture('1')['processed_at'])
            payload=snapshot(db,root/'snapshot.db');db.conn.close()
            spec={'course_id':'10','course_title':'概率论','lecture':{'sub_id':'1'},'mode':'finished'}
            pipeline.encode({'specification.json':pipeline.shards.encoded(spec),'database.db':payload},
                            'prepared',root/'inbox'/'prepared.enc')
            with patch.dict('sys.modules', {'src.pipeline.lecture_runner':SimpleNamespace(LectureRunner=Runner)}), \
                 patch.object(pipeline,'artifact',return_value=False),patch('src.ai.summarizer.Summarizer') as llm:
                pipeline.gather()
            llm.assert_not_called();saved=pipeline.decode(root/'out'/'state.enc','state')
            path=root/'verified.db';path.write_bytes(saved['database.db']);db=Database(str(path))
            self.assertIsNotNone(db.get_lecture('1')['processed_at']);self.assertEqual(db.get_lecture('1')['summary'],'已保存的摘要')
            self.assertIsNone(db.get_lecture('1')['emailed_at']);db.conn.close()

if __name__=='__main__':unittest.main()
