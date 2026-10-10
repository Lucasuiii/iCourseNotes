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
        from scripts.merge_db import merge
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
