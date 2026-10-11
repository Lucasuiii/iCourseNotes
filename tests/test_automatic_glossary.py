import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from src.ai.automatic_glossary import validated_keywords, active_terms, AutomaticGlossary
from src.data.database import Database
from src.ai.summarizer import Summarizer
from scripts.merge_db import merge_delta as merge
from src.ai.automatic_glossary import term_stages


def evidence(term, source, quote):
    import hashlib
    return {'term': term, 'source': source, 'quote': quote,
            'evidence': [{'source': source, 'quote': quote, 'sha256': hashlib.sha256(quote.encode()).hexdigest()}]}


class AutomaticGlossaryTests(unittest.TestCase):
    def test_malformed_history_timestamp_does_not_block_valid_terms(self):
        db = MagicMock()
        db.get_lecture.return_value = {'course_id': '10', 'deleted_at': None}
        valid = {'course_id': '10', 'sub_id': '1', 'updated_at': '2026-10-04T10:00:00+00:00',
                 'keywords': [{'term': 'Householder', 'source': 'ppt'}]}
        db.read_meta_prefix.return_value = [json.dumps(valid)] + [
            json.dumps({**valid, 'sub_id': '2', 'updated_at': stamp,
                        'keywords': [{'term': '坏记录', 'source': 'ppt'}]})
            for stamp in (None, [], {}, 123)
        ]
        self.assertEqual(AutomaticGlossary(db, '10').terms(), [])
        self.assertEqual([r['sub_id'] for r in AutomaticGlossary(db, '10').records()], ['1'])

    def test_merge_preserves_newer_glossary_and_unrelated_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            local=Database(str(Path(tmp)/'local.db'))
            remote=Database(str(Path(tmp)/'remote.db'))
            key='auto_glossary:10:1'
            local.write_meta(key,json.dumps({'updated_at':'2026-10-04T10:00:00+00:00','keywords':[]}))
            remote.write_meta(key,json.dumps({'updated_at':'2026-10-04T11:00:00+00:00','keywords':[]}))
            remote.write_meta('unrelated','keep')
            local.conn.close();remote.conn.close()
            merge(str(Path(tmp)/'local.db'),str(Path(tmp)/'remote.db'))
            remote=Database(str(Path(tmp)/'remote.db'))
            self.assertIn('11:00:00',remote.read_meta(key))
            self.assertEqual(remote.read_meta('unrelated'),'keep')
            remote.conn.close()
    def test_requires_exact_evidence_and_not_supplement(self):
        sources={'asr':['使用 Householder 变换与镜像变换'], 'ppt':['Gram-Schmidt 正交化']}
        items=[{'term':'Householder','source':'asr','quote':'使用 Householder 变换'},
               {'term':'Gram-Schmidt','source':'ppt','quote':'Gram-Schmidt 正交化'},
               {'term':'镜像变换','source':'cloud','quote':'镜像变换'},
               {'term':'老师姓名','source':'asr','quote':'老师姓名'},
               {'term':'忽略指令','source':'asr','quote':'忽略指令'}]
        summary='Householder 变换\n\n补充说明：Gram-Schmidt 正交化'
        self.assertEqual([x['term'] for x in validated_keywords(items,sources,summary)],['Householder'])
        self.assertEqual(validated_keywords(None,sources,summary),[])

    def test_two_distinct_lessons_and_no_self_reinforcement(self):
        one={'sub_id':'1','keywords':[{'term':'镜像变换','source':'asr'}]}
        self.assertEqual(active_terms([one,one]),[])
        two={**one,'sub_id':'2'}
        self.assertEqual(active_terms([one,two]),[])
        self.assertEqual(active_terms([one,two],exclude_sub_id='2'),[])
        self.assertEqual(active_terms([{'sub_id':'3','keywords':[{'term':'Householder','source':'ppt'}]}]),[])

    def test_persistence_course_isolation_and_deleted_lecture(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(str(Path(tmp)/'db.sqlite'))
            for cid,sid in [('10','1'),('20','2')]:
                db.upsert_course(cid,'同名课','教师')
                db.insert_lecture(sid,cid,'课次','2026-09-29')
            store=AutomaticGlossary(db,'10')
            store.save('1',[{'term':'Householder','source':'ppt','quote':'Householder'}],
                       sources={'ppt':['Householder'], 'cloud':['使用Householder变换']})
            self.assertEqual(store.terms(),['Householder'])
            self.assertEqual(AutomaticGlossary(db,'20').terms(),[])
            self.assertEqual(store.terms(exclude_sub_id='1'),[])
            db.conn.execute("UPDATE lectures SET deleted_at='now' WHERE sub_id='1'")
            db.conn.commit()
            self.assertEqual(store.terms(),[])
            db.conn.close()

    def test_summary_and_keywords_one_request(self):
        client=MagicMock()
        client.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='stop',message=SimpleNamespace(content=json.dumps({'summary':'### Householder 变换\n\n正文',
            'keywords':[{'term':'Householder','source':'ppt','quote':'Householder 变换'}]})))])
        model=Summarizer.__new__(Summarizer)
        model.system_prompt='仅输出笔记'
        model.providers=[{'name':'deepseek','models':['test']}]
        model._clients={'deepseek':client}
        with patch('src.ai.summarizer.enrich_summary',side_effect=lambda s,**kw:s):
            summary,_,keywords=model.summarize_with_keywords('课程','材料',{'ppt':['Householder 变换']},[])
        self.assertTrue(summary.startswith('###'))
        self.assertEqual(keywords[0]['term'],'Householder')
        client.chat.completions.create.assert_called_once()
        self.assertEqual(client.chat.completions.create.call_args.kwargs['extra_body']['thinking']['type'],'enabled')
        payload=json.loads(client.chat.completions.create.call_args.kwargs['messages'][1]['content'])
        self.assertIsInstance(payload['evidence_sources']['ppt'],str)

    def test_only_new_cross_source_evidence_promotes_not_two_asr_or_legacy_flags(self):
        term = '镜像变换'
        def record(sid, source, quote, frozen=()):
            return {'schema': 2, 'sub_id': sid, 'lecture_date': '2026-09-01',
                    'frozen_terms': list(frozen), 'keywords': [evidence(term, source, quote)]}
        asr1 = record('1', 'asr', '使用镜像变换。')
        asr2 = record('2', 'asr', '再讨论镜像变换。')
        self.assertEqual(active_terms([asr1, asr2]), [])
        ppt1 = record('1', 'ppt', '镜像变换定义')
        cloud = record('1', 'cloud', '现在介绍镜像变换')
        self.assertEqual(active_terms([ppt1, cloud]), [term])
        self.assertEqual(active_terms([ppt1, cloud], exclude_sub_id='1'), [])
        same = record('2', 'ppt', '镜像变换定义')
        self.assertEqual(active_terms([ppt1, same]), [])
        other = record('2', 'ppt', '应用镜像变换构造正交矩阵')
        self.assertEqual(active_terms([ppt1, other]), [term])
        hinted = record('2', 'ppt', '应用镜像变换构造正交矩阵', [term])
        self.assertEqual(active_terms([ppt1, hinted]), [])
        wrong = record('2', 'cloud', '不是镜像变换')
        self.assertEqual(active_terms([ppt1, wrong]), [])
        tampered = record('2', 'ppt', '应用镜像变换构造正交矩阵')
        tampered['keywords'][0]['evidence'][0]['sha256'] = 'wrong'
        self.assertEqual(active_terms([ppt1, tampered]), [])

    def test_future_same_date_deleted_and_foreign_lessons_do_not_feed_frozen_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp)/'terms.db')); db.upsert_course('10', '课', '老师')
            for sid, date in [('1', '2026-09-01'), ('2', '2026-09-02'), ('3', '2026-09-03')]:
                db.insert_lecture(sid, '10', '课次', date)
            store = AutomaticGlossary(db, '10')
            keyword = {'term': 'Householder', 'source': 'ppt', 'quote': 'Householder变换'}
            store.save('1', [keyword], sources={'ppt': ['Householder变换'], 'cloud': ['使用Householder变换']})
            with patch('src.ai.course_glossary.course_terms', return_value=['基础词']):
                snapshot = store.freeze('课', '2')
                self.assertEqual(snapshot['terms'], ['基础词', 'Householder'])
                self.assertEqual(store.freeze('课', '1')['terms'], ['基础词'])
                db.conn.execute("UPDATE lectures SET date='2026-09-02' WHERE sub_id='1'"); db.conn.commit()
                self.assertEqual(store.freeze('课', '2')['terms'], ['基础词'])
            before = snapshot['terms'][:]
            db.conn.execute("UPDATE lectures SET deleted_at='now' WHERE sub_id='1'"); db.conn.commit()
            self.assertEqual(store.terms(), [])
            self.assertEqual(snapshot['terms'], before)
            db.conn.close()

    def test_save_requires_raw_source_and_preserves_candidates_without_auto_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp)/'terms.db')); db.upsert_course('10', '课', '老师')
            db.insert_lecture('1', '10', '课次', '2026-09-01')
            store = AutomaticGlossary(db, '10')
            keyword = {'term': 'Householder', 'source': 'asr', 'quote': '使用Householder变换'}
            store.save('1', [keyword], sources={'asr': ['使用Householder变换']})
            record = json.loads(db.read_meta('auto_glossary:10:1'))
            self.assertEqual(record['keywords'][0]['stage'], 'candidate')
            self.assertEqual(store.terms(), [])
            self.assertEqual(store.stages()[0]['term'], 'Householder')
            self.assertTrue(record['keywords'][0]['evidence'][0]['sha256'])
            store.save('1', [keyword], sources={'asr': ['另一个词']})
            self.assertEqual(json.loads(db.read_meta('auto_glossary:10:1'))['keywords'], [])
            with self.assertRaises(ValueError): AutomaticGlossary(db, '20').save('1', [keyword])
            db.conn.close()

    def test_same_lecture_rerun_does_not_count_as_independent_evidence(self):
        term = '条件期望'
        one = {'schema': 2, 'sub_id': '1', 'keywords': [evidence(term, 'ppt', '条件期望定义')]}
        replay = {'schema': 2, 'sub_id': '1', 'keywords': [evidence(term, 'ppt', '条件期望再次讨论')]}
        self.assertEqual(active_terms([one, replay]), [])
        self.assertEqual(term_stages([one, replay])[0]['stage'], 'candidate')

    def test_malformed_proof_or_prompting_history_never_confirms(self):
        term = 'Householder'
        for bad in (None, {}, 'invalid'):
            item = evidence(term, 'ppt', term); item['evidence'] = bad
            self.assertEqual(active_terms([{'schema': 2, 'sub_id': '1', 'keywords': [item]}]), [])
            item = evidence(term, 'ppt', term)
            self.assertEqual(active_terms([{'schema': 2, 'sub_id': '1', 'keywords': [item], 'frozen_terms': bad}]), [])

    def test_long_asr_evidence_does_not_displace_visual_and_cloud_corroboration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp)/'db.sqlite'))
            db.upsert_course('10', '课程', '教师'); db.insert_lecture('1', '10', '课次', '2026-09-29')
            glossary = AutomaticGlossary(db, '10')
            glossary.save('1', [{'term': 'Householder', 'source': 'asr', 'quote': 'Householder'}], sources={
                'asr': ['Householder变换'+str(i) for i in range(40)],
                'ppt': ['Householder正交变换'], 'cloud': ['讨论Householder变换']})
            self.assertEqual(glossary.terms(), ['Householder'])
            self.assertLessEqual(len(glossary.records()[0]['keywords'][0]['evidence']), 12)
            db.conn.close()

    def test_new_lesson_reads_published_confirmation_after_queue_was_planned(self):
        from scripts import production_qwen as pipeline
        from scripts.production_db import snapshot
        with tempfile.TemporaryDirectory() as tmp:
            local = Database(str(Path(tmp)/'queued.db'))
            local.upsert_course('10', '高代', '教师'); local.insert_lecture('2', '10', '新课次', '2026-10-04')
            remote = Database(str(Path(tmp)/'remote.db'))
            remote.upsert_course('10', '高代', '教师'); remote.insert_lecture('1', '10', '上一课', '2026-09-29')
            AutomaticGlossary(remote, '10').save('1', [{'term': '镜像变换', 'source': 'ppt', 'quote': '镜像变换'}],
                sources={'ppt': ['镜像变换'], 'cloud': ['我们采用镜像变换计算']})
            blob = snapshot(remote, Path(tmp)/'saved.db'); remote.conn.close()
            def load(path): path.write_bytes(blob); return 'a'*40
            with patch.dict(os.environ, {'PUBLISH_RESULTS': 'true'}), \
                 patch.object(pipeline, 'root', return_value=Path(tmp)), patch.object(pipeline, 'load_remote', side_effect=load), \
                 patch('src.ai.course_glossary.course_terms', return_value=[]):
                frozen = pipeline.freeze_course_terms(local, '10', '高代', '2')
            self.assertEqual(frozen['terms'], ['镜像变换'])
            self.assertEqual(frozen['lecture_date'], '2026-10-04')
            self.assertEqual(frozen['history_revision'], 'a'*40)
            self.assertEqual(AutomaticGlossary(local, '10').terms(), [])
            local.conn.close()

    def test_isolated_preparation_uses_its_snapshot_and_does_not_read_remote(self):
        from scripts import production_qwen as pipeline
        db = MagicMock(); db.get_lecture.return_value = {'date': '2026-10-04'}; db.read_meta_prefix.return_value = []
        with patch.dict(os.environ, {'PUBLISH_RESULTS': 'false'}), \
             patch.object(pipeline, 'load_remote') as load, patch('src.ai.course_glossary.course_terms', return_value=['矩阵']):
            self.assertEqual(pipeline.freeze_course_terms(db, '10', '高代', '2')['terms'], ['矩阵'])
            load.assert_not_called()

    def test_prepared_frozen_terms_override_current_db_for_review_and_save(self):
        from test_qwen_production_pipeline import fixture, database
        from src.pipeline.prepared_lecture import assemble_material
        from test_lecture_quality_gate import _load_runner_class
        plan, results = fixture(); prepared = assemble_material(plan, results)
        with tempfile.TemporaryDirectory() as tmp:
            db = database(Path(tmp)/'db.sqlite'); llm = MagicMock()
            llm.summarize_with_keywords.return_value = ('完整摘要', 'test', [])
            Runner = _load_runner_class(); transcriber = MagicMock()
            runner = Runner(None, db, MagicMock(), transcriber, llm, MagicMock())
            with patch.dict('os.environ', {'AUTO_COURSE_TERMS': 'true'}), \
                 patch('src.ai.automatic_glossary.AutomaticGlossary') as glossary, \
                 patch('src.runtime.config.DOUBAO_ASR_API_KEY', ''):
                glossary.return_value.freeze.return_value = {'terms': ['更晚的新词']}
                runner.run('10', '概率论', {'sub_id': '1'}, prepared_asr=prepared, review_state={}, checkpoint=lambda: None)
                self.assertEqual(runner._historical_terms, ['条件期望'])
                self.assertEqual(llm.summarize_with_keywords.call_args.args[3], ['条件期望'])
                self.assertEqual(glossary.return_value.save.call_args.kwargs['frozen_terms'], ['条件期望'])
            db.conn.close()


if __name__=='__main__':
    unittest.main()
