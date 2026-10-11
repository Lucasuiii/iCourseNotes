"""Offline contract/gate tests. They do not measure the model's detection rate."""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src.data.database import Database
from src.pipeline import summary_review as audit
from scripts.validate_db import validate_database


def result(issues=None):
    issues = issues or []
    return {'verdict': 'needs_revision' if issues else 'pass',
            'checks': [{'category': c, 'detail': '已按材料逐项核对。'} for c in audit.CATEGORIES],
            'issues': issues}


class SummaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'candidate.db'
        self.db = Database(str(self.path)); self.addCleanup(self.db.conn.close)
        self.db.upsert_course('1', '概率论', '')
        self.db.insert_lecture('2', '1', '课次', '2026-10-09')
        self.material = 'I_A在A上为1，在补集上为0；图中作业P64题1、4、5、6。'
        self.text = '在0<=x<1时，{I_A<=x}=A。'
        self.api = MagicMock()
        self.summarizer = SimpleNamespace(summary_review_client=lambda: (self.api, 'deepseek-flash'))
        self.generate = MagicMock(return_value=(self.text, 'deepseek/test', []))

    def response(self, value, finish='stop'):
        self.api.with_options.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=value))])

    def run_review(self):
        text, _, _, state = audit.draft(self.db, '1', '2', self.material, self.generate)
        return audit.review(self.db, self.summarizer, '1', '2', '概率论', self.material, text, state=state)

    def test_reported_math_error_keeps_draft_and_blocks_completed_result(self):
        finding = {'category': 'math', 'severity': 'error', 'quote': self.text,
                   'reason': '示性函数小于1对应补集，集合写反。', 'evidence_quote': self.material,
                   'suggestion': '将A改为A的补集。'}
        self.response(json.dumps(result([finding])))
        state = self.run_review()
        self.assertEqual(state['status'], 'needs_revision')
        with self.assertRaises(audit.SummaryReviewBlocked): audit.require_accepted(state)
        self.assertEqual(audit.read_state(self.db, '2')['draft_summary'], self.text)
        self.assertIsNone(self.db.get_lecture('2')['summary'])
        self.run_review()
        self.generate.assert_called_once()
        self.api.with_options.return_value.chat.completions.create.assert_called_once()

    def test_theory_review_needs_no_six_categories_exact_quote_or_evidence(self):
        self.response(json.dumps({'verdict': 'needs_revision', 'issues': [
            {'quote': '第一段示性函数的集合等式', 'reason': '补集方向写反', 'suggestion': '改为A的补集'}]}))
        state = self.run_review()
        self.assertEqual(state['review_scope'], 'theory')
        self.assertEqual(state['status'], 'needs_revision')
        self.assertEqual(state['result']['checks'], [])
        options = self.api.with_options.return_value.chat.completions.create.call_args.kwargs
        self.assertNotIn('max_tokens', options)
        self.assertIn('不要求时间定位', options['messages'][0]['content'])

    def test_simple_theory_pass_validates_and_truncated_reply_is_preserved(self):
        self.response('{"verdict":"pass","issues":[]}')
        state = self.run_review()
        self.assertEqual(state['status'], 'passed')
        audit.validate_published(state, '1', '2', self.text, '2026-10-09')
        self.db.write_meta(audit.PREFIX+'2', '')
        self.response('{"verdict":"pass",', finish='length')
        state = self.run_review()
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['response_content'], '{"verdict":"pass",')

    def test_legacy_six_category_pass_remains_readable(self):
        state = {'schema': 1, 'course_id': '1', 'sub_id': '2', 'date': '2026-10-09',
                 'summary_sha256': audit.digest(self.text), 'reviewed_summary': self.text,
                 'status': 'passed', 'result': result()}
        audit.validate_published(state, '1', '2', self.text, '2026-10-09')
        state['result']['checks'] = []
        with self.assertRaises(ValueError):
            audit.validate_published(state, '1', '2', self.text, '2026-10-09')

    def test_exact_pass_is_cached_and_published_metadata_is_hash_bound(self):
        self.response(json.dumps(result()))
        state = self.run_review(); self.assertEqual(state['status'], 'passed')
        audit.require_accepted(state)
        self.run_review(); self.generate.assert_called_once()
        self.api.with_options.assert_called_once_with(max_retries=0)
        self.db.update_summary('2', self.text, 'test')
        validate_database(str(self.path))
        self.db.update_summary('2', self.text+'tampered', 'test')
        with self.assertRaises(ValueError): validate_database(str(self.path))

    def test_timeout_parse_failure_and_truncation_never_reissue(self):
        for mode in ('timeout', 'parse', 'truncated'):
            with self.subTest(mode=mode):
                self.db.write_meta(audit.PREFIX+'2', '')
                self.api.reset_mock(); call = self.api.with_options.return_value.chat.completions.create
                call.side_effect = TimeoutError() if mode == 'timeout' else None
                self.response('not json' if mode == 'parse' else json.dumps(result()),
                              finish='length' if mode == 'truncated' else 'stop')
                state = self.run_review(); self.assertEqual(state['status'], 'failed')
                if mode == 'truncated': self.assertEqual(state['response_finish_reason'], 'length')
                if mode == 'parse':
                    self.assertEqual(state['response_content'], 'not json')
                    self.assertIn('validation_error', state)
                self.run_review(); self.assertEqual(call.call_count, 1)
                with self.assertRaises(audit.SummaryReviewBlocked): audit.require_accepted(state)

    def test_reserved_crash_checkpoint_does_not_call_api(self):
        _, _, _, state = audit.draft(self.db, '1', '2', self.material, self.generate)
        state.update(status='reserved', summary_sha256=audit.digest(self.text), reviewed_summary=self.text)
        audit.save_state(self.db, state)
        self.assertEqual(self.run_review()['status'], 'reserved')
        self.api.with_options.assert_not_called()

    def test_blocked_draft_exports_report_without_masking_asset_failure(self):
        self.response('not json'); self.run_review()
        self.db.write_meta('summary_figures:2', '{invalid')
        def atomic(path, data):
            path.write_bytes(data)
        audit.export_draft(self.db, '2', self.tmp.name, atomic)
        report = (Path(self.tmp.name)/'summary-review.md').read_text()
        text = (Path(self.tmp.name)/'summary-draft.md').read_text()
        self.assertIn('failed', report); self.assertIn('JSONDecodeError', report)
        self.assertIn('图片导出失败', report)
        self.assertIn('未通过', text); self.assertIn(self.text, text)

    def test_modified_material_rejected_before_regeneration(self):
        self.response(json.dumps(result())); self.run_review()
        with self.assertRaises(audit.SummaryReviewBlocked):
            audit.draft(self.db, '1', '2', self.material+'changed', self.generate)
        self.generate.assert_called_once()
        state = audit.read_state(self.db, '2')
        with self.assertRaises(audit.SummaryReviewBlocked):
            audit.review(self.db, self.summarizer, '1', '2', '概率论', self.material,
                         self.text+'changed', state=state)

    def test_unknown_quotes_missing_checks_and_false_pass_are_rejected(self):
        issue = {'category': 'exercises', 'severity': 'uncertain', 'quote': self.text,
                 'reason': '疑点', 'suggestion': '人工核对', 'evidence_quote': ''}
        invalid = [dict(result([issue]), verdict='pass'), dict(result(), checks=[]),
                   result([dict(issue, quote='不存在的摘要')]),
                   result([dict(issue, evidence_quote='编造的证据')])]
        for value in invalid:
            with self.assertRaises(ValueError): audit.validate_result(value, self.text, self.material)

    def test_no_deepseek_is_explicitly_unavailable_and_never_a_pass(self):
        self.summarizer.summary_review_client = lambda: None
        state = self.run_review(); self.assertEqual(state['status'], 'unavailable')
        self.assertNotIn('result', state); self.api.with_options.assert_not_called()

    def test_oversized_input_fails_without_partial_material_or_api(self):
        self.material = 'A' * (audit.MAX_TEXT+1)
        state = self.run_review(); self.assertEqual(state['status'], 'failed')
        self.api.with_options.assert_not_called()

    def test_real_selected_pixels_and_course_date_reach_reviewer(self):
        from test_summary_figures import image_row
        from src.pipeline.summary_figures import validate_selection, sections
        image = image_row()
        selection = {'figures': [{'image_id': image['id'], 'section_id': 0, 'caption': '集合图',
                                 'visible_evidence': '圆', 'kind': 'diagram', 'legible': True}]}
        figures = {'schema': 1, 'course_id': '1', 'sub_id': '2', 'status': 'complete',
                   'figures': validate_selection(selection, [image], sections(self.text))}
        self.response(json.dumps(result()))
        _, _, _, state = audit.draft(self.db, '1', '2', self.material, self.generate)
        audit.review(self.db, self.summarizer, '1', '2', '概率论', self.material, self.text,
                     state=state, figures=figures)
        args = self.api.with_options.return_value.chat.completions.create.call_args.kwargs
        body = args['messages'][1]['content']
        self.assertEqual(json.loads(body[0]['text'])['date'], '2026-10-09')
        self.assertEqual(body[-1]['image_url']['url'], 'data:image/jpeg;base64,'+image['data'])
        self.assertIn('证明方向', args['messages'][0]['content'])

    def test_course_shard_encryption_merge_scope_and_suppression_preserve_gate(self):
        self.response(json.dumps(result())); self.run_review(); self.db.update_summary('2', self.text, 'test')
        self.db.mark_processed('2')
        from src.data.sharder import _build_meta_shard, _build_shard_db, shard_database, reassemble_database
        from src.pipeline.history_refresh import lesson_state
        from scripts.production_db import lecture_snapshot
        from scripts.merge_db import merge_delta as merge
        root = Path(self.tmp.name)
        _build_meta_shard(str(self.path), str(root/'meta.db'))
        _build_shard_db(str(self.path), ['1'], str(root/'course.db'))
        with sqlite3.connect(root/'meta.db') as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM meta WHERE key GLOB 'summary_review:*'").fetchone()[0], 0)
        with sqlite3.connect(root/'course.db') as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM meta WHERE key GLOB 'summary_review:*'").fetchone()[0], 1)
        index = shard_database(str(self.path), str(root/'encrypted'), 'offline-test-password')
        reassemble_database(index, str(root/'encrypted/shards'), str(root/'restored.db'), 'offline-test-password')
        validate_database(str(root/'restored.db'))
        self.assertIn(audit.PREFIX+'2', [r['key'] for r in lesson_state(self.db.conn, '1', '2')['meta']])
        lecture_snapshot(self.db, root/'delta.db', '1', '2')
        remote = Database(str(root/'remote.db')); remote.conn.close()
        merge(str(root/'delta.db'), str(root/'remote.db')); validate_database(str(root/'remote.db'))
        self.db.suppress_lectures('1', ['2'])
        self.assertIsNone(audit.read_state(self.db, '2'))

    def test_runner_does_not_save_or_mark_complete_after_blocking_review(self):
        from test_lecture_quality_gate import _load_runner_class
        Runner = _load_runner_class()
        llm = MagicMock(); llm.summarize.return_value = (self.text, 'test')
        llm.summary_review_client.return_value = (self.api, 'deepseek-flash')
        self.response(json.dumps(result([{'category': 'math', 'severity': 'error',
                      'quote': self.text, 'reason': '补集写反', 'suggestion': '更正集合', 'evidence_quote': ''}])))
        runner = Runner(None, self.db, MagicMock(), MagicMock(), llm, MagicMock())
        runner._homework_course_id = '1'
        with patch.dict('os.environ', {'SUMMARY_FIGURES': 'false'}):
            with self.assertRaises(audit.SummaryReviewBlocked):
                runner._summarize('2', '概率论', self.material, [])
        row = self.db.get_lecture('2')
        self.assertIsNone(row['summary']); self.assertIsNone(row['processed_at'])
        self.assertEqual(row['error_stage'], 'summary_review')
        with patch.dict('os.environ', {'SUMMARY_FIGURES': 'false'}):
            with self.assertRaises(audit.SummaryReviewBlocked):
                runner._summarize('2', '概率论', self.material, [])
        llm.summarize.assert_called_once()

    def needs_revision(self):
        self.response(json.dumps({'verdict': 'needs_revision', 'issues': [
            {'quote': self.text, 'reason': '补集方向写反', 'suggestion': '改为A的补集'}]}))
        return self.run_review()

    def correct(self, state, text=None):
        self.response(json.dumps({'summary': text or '在0<=x<1时，{I_A<=x}=A的补集。'}))
        return audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material, state=state)

    def test_one_correction_is_accepted_without_claiming_second_review_pass(self):
        state = self.correct(self.needs_revision())
        self.assertEqual(state['status'], 'revised')
        self.assertEqual(state['result']['verdict'], 'needs_revision')
        self.assertEqual(state['draft_summary'], self.text)
        self.assertEqual(state['revision']['source_summary'], self.text)
        self.assertFalse(state['revision']['second_review_performed'])
        self.assertEqual(state['schema'], 3)
        audit.require_accepted(state)
        self.db.update_summary('2', state['reviewed_summary'], 'corrected')
        validate_database(str(self.path))
        call = self.api.with_options.return_value.chat.completions.create
        self.assertEqual(call.call_count, 2)
        payload = json.loads(call.call_args.kwargs['messages'][1]['content'])
        self.assertEqual(payload['summary'], self.text)
        self.assertEqual(payload['issues'][0]['reason'], '补集方向写反')
        self.assertNotIn('max_tokens', call.call_args.kwargs)
        self.assertIn('未进行第二次自审', audit.report(state))
        # Resume starts from the same original draft and figure-selection input.
        resumed = self.run_review()
        resumed = audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material, state=resumed)
        self.assertEqual(resumed, state)
        self.assertEqual(call.call_count, 2)
        self.generate.assert_called_once()

    def test_correction_failure_or_unknown_outcome_never_reissues(self):
        for mode in ('timeout', 'parse', 'truncated', 'empty', 'unchanged', 'no_choices'):
            with self.subTest(mode=mode):
                self.db.write_meta(audit.PREFIX+'2', '')
                self.api.reset_mock()
                call = self.api.with_options.return_value.chat.completions.create
                call.side_effect = None
                state = self.needs_revision()
                self.response('not json' if mode == 'parse' else json.dumps({'summary':
                    '' if mode == 'empty' else self.text if mode == 'unchanged' else 'A的补集'}),
                    finish='length' if mode == 'truncated' else 'stop')
                if mode == 'timeout': call.side_effect = TimeoutError()
                if mode == 'no_choices': call.return_value = SimpleNamespace(choices=[])
                state = audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material, state=state)
                self.assertEqual(state['status'], 'needs_revision')
                self.assertEqual(state['revision']['status'], 'failed')
                with self.assertRaises(audit.SummaryReviewBlocked): audit.require_accepted(state)
                cached = audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material)
                self.assertEqual(cached, state)
                self.assertEqual(call.call_count, 2)
                if mode == 'parse': self.assertEqual(state['revision']['response_content'], 'not json')
                if mode == 'truncated': self.assertEqual(state['revision']['error_code'], 'incomplete_response')

    def test_correction_reservation_precedes_transport_and_crash_is_not_replayed(self):
        state = self.needs_revision()
        def crashed_call(**kwargs):
            saved = audit.read_state(self.db, '2')
            self.assertEqual(saved['revision']['status'], 'reserved')
            raise KeyboardInterrupt()
        call = self.api.with_options.return_value.chat.completions.create
        call.side_effect = crashed_call
        with self.assertRaises(KeyboardInterrupt):
            audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material, state=state)
        state = audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material)
        self.assertEqual(state['revision']['status'], 'reserved')
        self.assertEqual(call.call_count, 2)
        with self.assertRaises(audit.SummaryReviewBlocked): audit.require_accepted(state)

    def test_correction_preserves_images_captions_and_coverage_notices(self):
        figure = '!['+'集合图'+'](#icourse-figure-'+'a'*64+')'
        source = self.text+'\n'+figure+'\n*课堂原图 · 00:01:00：集合图*\n> 识别缺口待确认'
        good = source.replace(self.text, '在0<=x<1时，{I_A<=x}=A的补集。')
        self.assertEqual(audit.validate_revision({'summary': good}, source), good)
        for changed in (good.replace(figure, ''), good.replace('a'*64, 'b'*64),
                        good.replace('00:01:00', '00:02:00'), good.replace('> 识别缺口待确认', ''),
                        good+'\n![新增图](https://example.com/new.jpg)'):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError): audit.validate_revision({'summary': changed}, source)

    def test_corrected_publication_rejects_tampering_and_retains_original_findings(self):
        state = self.correct(self.needs_revision()); text = state['reviewed_summary']
        for mutate in (lambda s: s['revision'].update(source_summary='改过的原稿'),
                       lambda s: s['revision'].update(response_finish_reason='length'),
                       lambda s: s['revision'].update(second_review_performed=True),
                       lambda s: s['revision'].update(response_content='{"summary":"篡改"}'),
                       lambda s: s['result']['issues'][0].update(reason='其他问题'),
                       lambda s: s.update(schema=2)):
            copy = json.loads(json.dumps(state)); mutate(copy)
            with self.assertRaises(ValueError): audit.validate_published(copy, '1', '2', text, '2026-10-09')
        self.db.update_summary('2', text, 'corrected')
        from src.data.sharder import shard_database, reassemble_database
        root = Path(self.tmp.name)
        index = shard_database(str(self.path), str(root/'revised-shards'), 'offline-test-password')
        reassemble_database(index, str(root/'revised-shards/shards'), str(root/'revised-restored.db'), 'offline-test-password')
        validate_database(str(root/'revised-restored.db'))
        with sqlite3.connect(root/'revised-restored.db') as conn:
            restored = json.loads(conn.execute('select value from meta where key=?', (audit.PREFIX+'2',)).fetchone()[0])
        self.assertEqual(restored, state)

    def test_passed_unavailable_or_failed_review_never_requests_correction(self):
        for response in ('{"verdict":"pass","issues":[]}', 'not json'):
            self.db.write_meta(audit.PREFIX+'2', ''); self.api.reset_mock(); self.response(response)
            state = self.run_review()
            audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material, state=state)
            self.assertEqual(self.api.with_options.return_value.chat.completions.create.call_count, 1)
        self.db.write_meta(audit.PREFIX+'2', ''); self.api.reset_mock()
        self.summarizer.summary_review_client = lambda: None
        audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material, state=self.run_review())
        self.api.with_options.assert_not_called()

    def test_repair_identity_and_input_limits_block_before_request(self):
        state = self.needs_revision()
        with self.assertRaises(audit.SummaryReviewBlocked):
            audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material+'changed', state=state)
        with self.assertRaises(audit.SummaryReviewBlocked):
            audit.revise(self.db, self.summarizer, '1', 'different', '概率论', self.material, state=state)
        self.material = 'A' * (audit.MAX_TEXT+1); state['material_sha256'] = audit.digest(self.material)
        state = audit.revise(self.db, self.summarizer, '1', '2', '概率论', self.material, state=state)
        self.assertEqual(state['revision']['error_code'], 'input_too_large')
        self.assertEqual(self.api.with_options.return_value.chat.completions.create.call_count, 1)

    def test_runner_saves_corrected_summary_and_resumes_without_repeating_calls(self):
        from test_lecture_quality_gate import _load_runner_class
        Runner = _load_runner_class(); llm = MagicMock(); llm.summarize.return_value = (self.text, 'test')
        llm.summary_review_client.return_value = (self.api, 'deepseek-flash')
        corrected = '在0<=x<1时，{I_A<=x}=A的补集。'
        def response(raw):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(raw)))])
        self.api.with_options.return_value.chat.completions.create.side_effect = [
            response({'verdict': 'needs_revision', 'issues': [{'quote': self.text, 'reason': '补集写反', 'suggestion': '更正集合'}]}),
            response({'summary': corrected})]
        runner = Runner(None, self.db, MagicMock(), MagicMock(), llm, MagicMock()); runner._homework_course_id = '1'
        with patch.dict('os.environ', {'SUMMARY_FIGURES': 'false'}):
            self.assertEqual(runner._summarize('2', '概率论', self.material, []), corrected)
            self.assertEqual(runner._summarize('2', '概率论', self.material, []), corrected)
        self.assertEqual(self.db.get_lecture('2')['summary'], corrected)
        self.assertIn('deepseek/deepseek-flash correction', self.db.get_lecture('2')['summary_model'])
        llm.summarize.assert_called_once()
        self.assertEqual(self.api.with_options.return_value.chat.completions.create.call_count, 2)
        validate_database(str(self.path))
