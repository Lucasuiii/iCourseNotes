import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw
from src.data.database import Database
from src.pipeline import summary_figures as figures
from scripts.validate_db import validate_database


def image_row():
    image = Image.new('RGB', (600, 400), 'white')
    draw = ImageDraw.Draw(image); draw.ellipse((100, 80, 400, 350), outline='black', width=4)
    buf = io.BytesIO(); image.save(buf, 'JPEG')
    data = buf.getvalue()
    return {'id': hashlib.sha256(data).hexdigest(), 'data': base64.b64encode(data).decode(),
            'mime': 'image/jpeg', 'seconds': 125, 'source': 'video_frame', 'nearby_transcript': '图中事件A'}


class SummaryFiguresTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'db.sqlite'
        self.db = Database(str(self.path)); self.addCleanup(self.db.conn.close)
        self.db.upsert_course('1', '概率论', '')
        self.db.insert_lecture('2', '1', '2026-10-09第2-5节', '2026-10-09')
        self.summary = '## 事件\n\n正文公式 $P(A)$。\n\n## 结论\n\n后文。'
        self.image = image_row()
        self.selection = {'figures': [{'image_id': self.image['id'], 'section_id': 0,
                         'caption': '事件集合示意图', 'visible_evidence': '圆形集合边界',
                         'kind': 'diagram', 'legible': True}]}

    def test_actual_pixels_placed_in_whitelisted_section_and_exported(self):
        accepted = figures.validate_selection(self.selection, [self.image], figures.sections(self.summary))
        summary = figures.insert_figures(self.summary, accepted)
        self.assertLess(summary.index('#icourse-figure-'), summary.index('## 结论'))
        self.assertIn('$P(A)$', summary); self.assertIn('00:02:05', summary)
        state = {'schema': 1, 'sub_id': '2', 'course_id': '1', 'status': 'complete', 'figures': accepted}
        self.db.write_meta('summary_figures:2', json.dumps(state))
        self.db.update_summary('2', summary, 'test')
        validate_database(str(self.path))
        exported = figures.export_local(summary, state, self.tmp.name)
        self.assertIn(str(Path(self.tmp.name)/'figures'), exported)
        self.assertEqual((Path(self.tmp.name)/'figures'/(self.image['id']+'.jpg')).read_bytes(), base64.b64decode(self.image['data']))
        state['figures'][0]['data'] = base64.b64encode(b'not the same pixels').decode()
        with self.assertRaises(ValueError): figures.validate_assets(state, '2')

    def test_unknown_identity_unreadable_and_html_captions_rejected(self):
        for key, bad in [('image_id', 'a'*64), ('section_id', 99), ('legible', False),
                         ('caption', '<img src=x>'), ('caption', '猜测\n图注')]:
            selection = json.loads(json.dumps(self.selection)); selection['figures'][0][key] = bad
            with self.assertRaises(ValueError): figures.validate_selection(selection, [self.image], figures.sections(self.summary))
        self.assertEqual(figures.insert_figures(self.summary, []), self.summary)

    def test_reserved_or_failed_request_never_replays(self):
        for status in ['reserved', 'failed', 'collecting']:
            self.db.write_meta('summary_figures:2', json.dumps({'schema': 1, 'sub_id': '2', 'status': status,
                'summary_sha256': hashlib.sha256(self.summary.encode()).hexdigest(), 'figures': [], 'image_count': 1}))
            api = MagicMock(); summarizer = SimpleNamespace(summary_figure_client=lambda: (api, 'deepseek-flash'))
            with patch.object(figures, 'collect') as collect:
                result, state = figures.add_figures(self.db, MagicMock(), summarizer, '1', '2', self.summary, [], [], 3600)
            self.assertEqual(result, self.summary); collect.assert_not_called(); api.with_options.assert_not_called()

    def test_complete_call_reuses_results_without_api_and_storage_keeps_course_scope(self):
        api = MagicMock(); api.with_options.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(self.selection)))])
        summarizer = SimpleNamespace(summary_figure_client=lambda: (api, 'deepseek-flash'))
        with patch.object(figures, 'collect', return_value=([self.image], [])):
            result, state = figures.add_figures(self.db, MagicMock(), summarizer, '1', '2', self.summary, [], [], 3600)
            result2, _ = figures.add_figures(self.db, MagicMock(), summarizer, '1', '2', self.summary, [], [], 3600)
        self.assertEqual(result, result2); api.with_options.assert_called_once_with(max_retries=0)
        self.db.update_summary('2', result, 'test')
        from src.data.sharder import _build_meta_shard, _build_shard_db, shard_database, reassemble_database
        import sqlite3
        meta = Path(self.tmp.name)/'meta.db'; course = Path(self.tmp.name)/'course.db'
        _build_meta_shard(str(self.path), str(meta)); _build_shard_db(str(self.path), ['1'], str(course))
        with sqlite3.connect(meta) as conn: self.assertEqual(conn.execute("SELECT count(*) FROM meta WHERE key='summary_figures:2'").fetchone()[0], 0)
        with sqlite3.connect(course) as conn: self.assertEqual(conn.execute("SELECT count(*) FROM meta WHERE key='summary_figures:2'").fetchone()[0], 1)
        out = Path(self.tmp.name)/'encrypted'; restored = Path(self.tmp.name)/'restored.db'
        index = shard_database(str(self.path), str(out), 'test-encryption-password')
        self.assertFalse(any('事件集合'.encode() in p.read_bytes() for p in out.rglob('*.enc')))
        reassemble_database(index, str(out/'shards'), str(restored), 'test-encryption-password')
        validate_database(str(restored))
        with sqlite3.connect(restored) as conn:
            self.assertEqual(json.loads(conn.execute("SELECT value FROM meta WHERE key='summary_figures:2'").fetchone()[0])['figures'], state['figures'])

    def test_foreign_course_assets_or_missing_markdown_reference_fail_validation(self):
        state = {'schema': 1, 'sub_id': '2', 'course_id': '999', 'status': 'complete', 'figures': []}
        self.db.write_meta('summary_figures:2', json.dumps(state))
        with self.assertRaises(ValueError): validate_database(str(self.path))
        state['course_id'] = '1'; state['figures'] = figures.validate_selection(self.selection, [self.image], figures.sections(self.summary))
        self.db.write_meta('summary_figures:2', json.dumps(state)); self.db.update_summary('2', self.summary, 'test')
        with self.assertRaises(ValueError): validate_database(str(self.path))

    def test_incremental_board_retained_before_ocr_near_duplicate_drop(self):
        from src.pipeline.ppt_pipeline import PPTPipeline
        scheduler = MagicMock()
        pages = [{'page_num': i, 'created_sec': i*100, 'pptimgurl': 'private-never-exported'} for i in [1, 2]]
        raw = base64.b64decode(self.image['data'])
        scheduler.image_cache.wait.return_value = (pages, {1: raw, 2: raw})
        pipeline = PPTPipeline(self.db, scheduler)
        with patch('src.pipeline.ppt_pipeline.match_garbage', return_value=[]), patch('src.pipeline.ppt_pipeline.dedup_dhash', return_value=[1]):
            images, counts = pipeline._collect_survivors(MagicMock(), '1', '2')
        self.assertEqual(list(images), [1]); self.assertEqual(counts['dedupped'], 1)
        self.assertEqual([f['seconds'] for f in pipeline.figure_frames('2')], [100, 200])
        self.assertEqual(pipeline.figure_frames('2'), [])

    def test_uniform_or_tiny_images_dropped_and_sampling_handles_one(self):
        buf = io.BytesIO(); Image.new('RGB', (600, 400), 'white').save(buf, 'PNG')
        self.assertIsNone(figures._jpeg(buf.getvalue()))
        self.assertEqual(figures.spread([{'seconds': 1}, {'seconds': 2}], 1), [{'seconds': 2}])

    def test_failed_response_persisted_before_validation_without_replay(self):
        api = MagicMock()
        content = json.dumps(self.selection)
        api.with_options.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='length', message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=6000))
        summarizer = SimpleNamespace(summary_figure_client=lambda: (api, 'model'))
        with patch.object(figures, 'collect', return_value=([self.image], [])):
            _, state = figures.add_figures(self.db, MagicMock(), summarizer, '1', '2', self.summary, [], [], 3600)
            figures.add_figures(self.db, MagicMock(), summarizer, '1', '2', self.summary, [], [], 3600)
        self.assertEqual(state['error_code'], 'incomplete_response')
        self.assertEqual(state['response_content'], content)
        self.assertEqual(state['response_finish_reason'], 'length')
        self.assertEqual(state['tokens']['completion_tokens'], 6000)
        api.with_options.assert_called_once()

    def test_invalid_reply_records_row_and_caption_error(self):
        bad = json.loads(json.dumps(self.selection)); bad['figures'][0]['caption'] = '<script>'
        api = MagicMock(); api.with_options.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(bad)))])
        with patch.object(figures, 'collect', return_value=([self.image], [])):
            _, state = figures.add_figures(self.db, MagicMock(), SimpleNamespace(summary_figure_client=lambda: (api, 'm')),
                                          '1', '2', self.summary, [], [], 3600)
        self.assertEqual(state['error_row_index'], 0)
        self.assertEqual(state['error_code'], 'invalid_caption')
        self.assertEqual(state['response_finish_reason'], 'stop')
        self.assertEqual(state['figures'], [])

    def test_homework_retains_later_complete_frames_and_never_partial_list(self):
        frames = []
        def add(sec, labels, writing='stable'):
            figures.retain_homework_frame(frames, b'pixels', {'seconds':sec,'source':'video_frame',
                'candidate_id':'cue','reader':'deepseek_vision','vision_status':'complete','writing_state':writing,
                'references':[{'page':7,'text':x,'legible':True} for x in labels]})
        add(0,['7页','第2题'],'in_progress')
        for sec in range(1,20):add(sec,['7页','第2题','第3题'])
        self.assertEqual([f['seconds'] for f in frames], [17,18,19])
        complete=figures.completed_homework_frames(frames)
        self.assertEqual([f['seconds'] for f in complete], [19])
        self.assertEqual(complete[0]['completion_evidence_seconds'], [18,19])
        for sec in range(20,24):add(sec,['第3题'])  # erasure must not conceal earlier required items
        self.assertEqual(figures.completed_homework_frames(frames), [])

    def test_homework_single_stable_or_in_progress_frame_rejected(self):
        image=dict(self.image, candidate_id='cue')
        selection=json.loads(json.dumps(self.selection)); selection['figures'][0].update(kind='homework',caption='作业清单')
        with self.assertRaisesRegex(ValueError,'homework_completion_unverified'):
            figures.validate_selection(selection,[image],figures.sections(self.summary))
        image['homework_complete']=True
        self.assertEqual(len(figures.validate_selection(selection,[image],figures.sections(self.summary))),1)
        self.assertEqual(figures.completed_homework_frames([{'seconds':10,'candidate_id':'cue','writing_state':'stable',
                          'reader':'deepseek_vision','vision_status':'complete','references':[{'page':7,'text':'7页','legible':True}]}]),[])

    def test_complete_homework_has_room_when_ppt_budget_is_full(self):
        frames = []
        for sec in (90, 100):
            figures.retain_homework_frame(frames, b'complete', {'seconds': sec, 'source': 'video_frame',
                'candidate_id': 'cue', 'reader': 'deepseek_vision', 'vision_status': 'complete',
                'writing_state': 'stable', 'references': [{'page': 7, 'text': '2-6', 'legible': True}]})
        frames += [{'image': str(i).encode(), 'seconds': i, 'source': 'platform_screenshot'} for i in range(12)]
        with patch.object(figures, '_jpeg', side_effect=lambda data: data):
            candidates, errors = figures.collect(MagicMock(), '1', '2', [], [], 0, retained=frames)
        self.assertEqual(len(candidates), 12)
        homework = [c for c in candidates if c.get('homework_complete')]
        self.assertEqual([c['seconds'] for c in homework], [100])
        self.assertEqual(errors, [])

    def test_invalid_json_reply_is_retained_for_diagnosis(self):
        api = MagicMock(); api.with_options.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{broken'))])
        with patch.object(figures, 'collect', return_value=([self.image], [])):
            _, state = figures.add_figures(self.db, MagicMock(), SimpleNamespace(summary_figure_client=lambda: (api, 'm')),
                                          '1', '2', self.summary, [], [], 3600)
        self.assertEqual(state['error_code'], 'invalid_json')
        self.assertEqual(state['response_content'], '{broken')


if __name__ == '__main__': unittest.main()
