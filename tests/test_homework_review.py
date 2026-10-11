"""Assignment reminders, conservative timestamps, private OCR and retry quotas."""
import copy
import io
import unittest
from functools import partial
from unittest.mock import MagicMock, patch

from src.ai.homework_review import (assignment_candidates, prioritize_candidates, focus_intervals,
                                    nearby_pages, ensure_homework_notice, homework_prompt)
from src.ai.qwen_review_ledger import review_prepared, validate_ledger


def material():
    return {'full_chunks': [{'start': 0, 'end': 120, 'text': '现在把主要精力放在做矩阵这一章的作业里，具体题号再看一下。'}],
            'vad_windows': [[0, 120]], 'audio_seconds': 180, 'audio_path': 'unused',
            'recognition_terms': ['矩阵'], 'transcript': 'x'*220, 'weak_windows': []}


def aligned(report, selected, *_args, **_kwargs):
    item = selected[0]
    return [dict(start_ms=102000, end_ms=118000, quote_start_ms=105000, quote_end_ms=115000,
                 chunk_id=item['id'], text=item['quote'])], [], [], {}


class AssignmentEvidenceTests(unittest.TestCase):
    def test_fresh_theory_mode_keeps_images_without_alignment_or_speech_review(self):
        state = {}; saved = []; image_reader = MagicMock(return_value={'status': 'unavailable', 'frames': []})
        with patch('src.ai.qwen_quality.review_quality') as select, \
             patch('src.ai.qwen_audio_alignment.align_suspects') as align, \
             patch('src.ai.doubao_asr.rescue_intervals_pcm') as speech:
            result = review_prepared(material(), [], MagicMock(), state,
                                     lambda: saved.append(copy.deepcopy(state)), homework_ocr=image_reader)
            self.assertEqual(review_prepared(material(), [], MagicMock(), state, lambda: None,
                                             homework_ocr=image_reader), result)
        select.assert_not_called(); align.assert_not_called(); speech.assert_not_called()
        image_reader.assert_called_once()
        self.assertEqual(image_reader.call_args.args[1], [])
        self.assertEqual(state['review_scope'], 'theory')
        self.assertEqual(state['speech_review_status'], 'not_requested')
        self.assertEqual(state['theory_review_status'], 'pending_summary')
        self.assertEqual(result['variants'], [])
        self.assertTrue(saved)

    def test_old_failed_ledger_cannot_be_relabelled_as_new_theory_review(self):
        state = {'error_type': 'JSONDecodeError', 'material': {'unresolved': []}}
        with self.assertRaisesRegex(ValueError, 'Frozen review scope changed'):
            review_prepared(dict(material(), review_scope='theory'), [], MagicMock(), state, lambda: None)
        self.assertEqual(state['error_type'], 'JSONDecodeError')
        self.assertNotIn('complete', state)

    def test_late_instruction_survives_many_early_mentions(self):
        chunks = [{'start': i*120, 'end': (i+1)*120, 'text': f'这是第{i}次讨论，上次作业只是举个例子，继续研究矩阵。'} for i in range(8)]
        chunks.append({'start': 960, 'end': 1080, 'text': '今天布置课后作业，完成第七页第二题，下周提交。'})
        selected = prioritize_candidates(assignment_candidates(chunks))
        self.assertEqual(selected[0]['id'], 8)
        self.assertLessEqual(len(selected), 4)
        for candidate in selected:
            self.assertIn(candidate['quote'], chunks[candidate['id']]['text'])

    def test_negation_short_and_repeated_cues_are_not_assignment_facts(self):
        candidates = assignment_candidates([{'start': 0, 'end': 10, 'text': '今天没有作业，下次课再布置。'},
                                            {'start': 10, 'end': 20, 'text': '作业'}])
        self.assertEqual(len(candidates), 2)
        self.assertFalse(candidates[1]['alignable'])
        prompt = homework_prompt({'candidates': candidates})
        self.assertIn('取消要求必须保留', prompt)
        self.assertIn('今天没有作业', prompt)
        summary = ensure_homework_notice('矩阵知识摘要', {'candidates': candidates})
        self.assertIn('### 课程事项提醒', summary)
        self.assertNotIn('不清楚', summary)
        self.assertNotIn('原始转写块', summary)
        self.assertNotIn('今天没有作业', summary)  # Unverified raw quotes are not reader-facing facts.
        self.assertEqual(ensure_homework_notice(summary, {'candidates': candidates}), summary)
        self.assertEqual(ensure_homework_notice('摘要', {'candidates': []}), '摘要')

    def test_generated_course_heading_does_not_duplicate_homework_notice(self):
        summary = ('### 课程事项提醒\n\n#### 作业与课务\n\n'
                   '作业题号和提交方式尚未确认。\n\n### 矩阵的定义\n\n定义内容。')
        evidence = {'candidates': [{}], 'visual': {'reference_status': 'unverified'}}
        self.assertEqual(ensure_homework_notice(summary, evidence), summary)
        self.assertEqual(ensure_homework_notice(summary, {'candidates': [{}]}), summary)

    def test_global_image_status_does_not_override_existing_speech_requirements(self):
        later = '### 矩阵乘法\n\n公式条件待确认。'
        summary = '### **课程事项提醒**\n\n#### 作业安排\n\n完成第3题和第4题。\n\n'+later
        for visual in ({}, {'status': 'ok'}, {'reference_status': 'unverified'}, {'status': 'failed'}):
            with self.subTest(visual=visual):
                evidence = {'candidates': [{}], 'visual': visual,
                            'cloud': [{'status': 'complete', 'cloud_text': '完成第3题和第4题。'}]}
                result = ensure_homework_notice(summary, evidence)
                self.assertEqual(result, summary)
                self.assertNotIn('题号和页码尚未确认', result)
                self.assertEqual(ensure_homework_notice(result, evidence), result)

    def test_body_mentions_and_fenced_headings_cannot_hide_missing_reminder(self):
        for source in ['正文中提到作业与课务提醒。',
                       '```markdown\n### 作业与课务提醒\n```',
                       '~~~\n### 课程事项提醒\n~~~']:
            with self.subTest(source=source):
                result = ensure_homework_notice(source, {'candidates': [{}]})
                self.assertTrue(result.endswith('作业与课务安排请参阅课程通知。'))
                self.assertEqual(ensure_homework_notice(result, {'candidates': [{}]}), result)

    def test_technical_audit_and_raw_numbers_never_leak_from_fallback(self):
        import json
        evidence = {'candidates': [{'block_start': 2610.5, 'block_end': 2716.7,
                                   'quote': '乱码和候选题号P69 1 3 7，以及私密课堂内容'}],
                    'visual': {'reference_status': 'unverified', 'status': 'needs_verification'},
                    'vision_calls': [{'status': 'complete', 'model': 'internal-model'}]}
        before = json.dumps(evidence, ensure_ascii=False)
        result = ensure_homework_notice('### 矩阵运算\n\n知识内容。', evidence)
        for value in ['2610.5', '2716.7', 'P69', '乱码', '私密课堂内容', 'needs_verification',
                      'internal-model', '原始转写块', 'ASR', 'OCR']:
            self.assertNotIn(value, result)
        self.assertEqual(json.dumps(evidence, ensure_ascii=False), before)

    def test_verified_homework_and_cancellation_are_not_rewritten(self):
        summary = '### 课程事项提醒\n\n今天没有新作业，完成上次未完成的练习即可。'
        for status in ('supported', 'unverified'):
            evidence = {'candidates': [{}], 'visual': {'reference_status': status}}
            self.assertEqual(ensure_homework_notice(summary, evidence), summary)
        unverified = '### 作业与课务\n\n题号待核实，提交方式尚未明确。'
        self.assertEqual(ensure_homework_notice(unverified, {'candidates': [{}]}), unverified)

    def test_unfinished_exercises_do_not_mean_uncertain_identification(self):
        summary = '### 课程事项提醒\n\n完成上次未完成的作业。'
        result = ensure_homework_notice(summary, {'candidates': [{}]})
        self.assertEqual(result, summary)

    def test_partial_requirements_keep_known_items_and_local_uncertainty(self):
        summary = '### 作业安排\n\n完成矩阵乘法的第2题，其余题号待确认。'
        evidence = {'candidates': [{}], 'visual': {'reference_status': 'unverified'}}
        self.assertEqual(ensure_homework_notice(summary, evidence), summary)
        prompt = homework_prompt(evidence)
        self.assertIn('先写已有证据支持的具体要求', prompt)
        self.assertIn('可靠语音可独立支持作业要求', prompt)
        self.assertIn('仅对有缺失或冲突的具体项', prompt)

    def test_context_crosses_block_edge_without_changing_original_clock(self):
        raw, *_ = aligned({}, assignment_candidates(material()['full_chunks']))
        intervals = focus_intervals(raw, 180)
        self.assertEqual((intervals[0]['start_ms'], intervals[0]['end_ms']), (90000, 130000))
        self.assertEqual(intervals[0]['quote_start_ms'], 105000)
        self.assertLessEqual(intervals[0]['end_ms']-intervals[0]['start_ms'], 60000)
        self.assertEqual(focus_intervals(raw, 119)[0]['end_ms'], 119000)

    def test_stale_screenshots_and_guessed_keyword_times_are_excluded(self):
        candidate = {'block_start': 100, 'block_end': 120}
        pages = [{'created_sec': 0}, {'created_sec': 105}, {'created_sec': 120}, {'created_sec': 999}]
        self.assertEqual([p['created_sec'] for p in nearby_pages(pages, candidate)], [105, 120])


class AssignmentLedgerTests(unittest.TestCase):
    def test_assignment_priority_ocr_and_shared_quota_resume(self):
        data = material(); data['weak_windows'] = [{'start_ms': 95000, 'end_ms': 105000, 'text': ''}]
        state = {'intervals': [dict(start_ms=100000, end_ms=110000, quote_start_ms=100000,
                                    quote_end_ms=110000, text='普通疑点')]}  # overlaps focused context
        saved = []; ocr = MagicMock(return_value={'status': 'ok', 'frames': [{'seconds': 110, 'text': '第二题'}]})
        def recognize(path, key, windows, **kwargs):
            self.assertEqual(saved[-1]['attempts'][0]['status'], 'reserved')
            self.assertEqual(saved[-1]['seconds'], 40)
            self.assertEqual(windows[0]['kind'], 'homework')
            self.assertEqual(kwargs['hotwords'], ['矩阵'])
            return [(windows[0], [{'start_ms': 105000, 'end_ms': 115000, 'text': '矩阵章节第二题，下周提交。'}])], 40, False
        with patch('src.runtime.config.DOUBAO_ASR_API_KEY', 'fake'), \
             patch('src.ai.qwen_review_ledger.subprocess.run'), \
             patch('scripts.qwen_audio_alignment.align_suspects', side_effect=aligned), \
             patch('src.ai.doubao_asr.rescue_intervals_pcm', side_effect=recognize) as cloud:
            result = review_prepared(data, [], MagicMock(), state, lambda: saved.append(copy.deepcopy(state)), homework_ocr=ocr)
            resumed = review_prepared(data, [], MagicMock(), state, lambda: None, homework_ocr=ocr)
        self.assertEqual(cloud.call_count, 1); self.assertEqual(ocr.call_count, 1)
        self.assertEqual(state['seconds'], 40); self.assertEqual(result, resumed)
        self.assertIn('第二题', result['homework']['cloud'][0]['cloud_text'])
        validate_ledger(state)

    def test_interrupted_assignment_transport_is_not_repeated_or_refunded(self):
        state = {'intervals': []}; saved = []
        with patch('src.runtime.config.DOUBAO_ASR_API_KEY', 'fake'), \
             patch('src.ai.qwen_review_ledger.subprocess.run'), \
             patch('scripts.qwen_audio_alignment.align_suspects', side_effect=aligned), \
             patch('src.ai.doubao_asr.rescue_intervals_pcm', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                review_prepared(material(), [], MagicMock(), state, lambda: saved.append(copy.deepcopy(state)))
        self.assertEqual(saved[-1]['attempts'][0]['status'], 'reserved')
        with patch('src.runtime.config.DOUBAO_ASR_API_KEY', 'fake'), \
             patch('src.ai.doubao_asr.rescue_intervals_pcm') as cloud:
            result = review_prepared(material(), [], MagicMock(), state, lambda: None)
        cloud.assert_not_called(); self.assertEqual(state['seconds'], 40)
        self.assertEqual(result['homework']['cloud'][0]['status'], 'reserved')

    def test_no_key_or_failed_alignment_still_preserves_reminder(self):
        for key, failure in [('', None), ('fake', RuntimeError('do not expose private data'))]:
            state = {'intervals': []}
            with patch('src.runtime.config.DOUBAO_ASR_API_KEY', key), \
                 patch('src.ai.qwen_review_ledger.subprocess.run'), \
                 patch('scripts.qwen_audio_alignment.align_suspects', side_effect=failure), \
                 patch('src.ai.doubao_asr.rescue_intervals_pcm') as cloud:
                result = review_prepared(material(), [], MagicMock(), state, lambda: None)
            self.assertTrue(result['homework']['candidates']); cloud.assert_not_called()
            self.assertNotIn('private data', str(state))

    def test_forty_short_clips_allowed_but_forty_first_and_over_time_rejected(self):
        for duration, expected in [(1, 40), (60, 15)]:
            state = {'intervals': [dict(start_ms=i*60000, end_ms=i*60000+duration*1000,
                                        quote_start_ms=i*60000, quote_end_ms=i*60000+duration*1000,
                                        text='疑点') for i in range(45)]}
            data = dict(material(), full_chunks=[], transcript='')
            with patch('src.runtime.config.DOUBAO_ASR_API_KEY', 'fake'), \
                 patch('src.ai.doubao_asr.rescue_intervals_pcm', return_value=([], duration, False)) as cloud:
                review_prepared(data, [], MagicMock(), state, lambda: None)
            self.assertEqual(cloud.call_count, expected)
            self.assertLessEqual(state['seconds'], 900); validate_ledger(state)
            if duration == 1:
                too_many = copy.deepcopy(state)
                too_many['attempts'].append({'interval': {'start_ms': 9999999, 'end_ms': 10000999},
                                            'seconds': 1, 'status': 'reserved'})
                too_many['seconds'] += 1
                with self.assertRaises(ValueError): validate_ledger(too_many)
        extra = copy.deepcopy(state)
        extra['attempts'].append({'interval': {'start_ms': 9999999, 'end_ms': 10000999}, 'seconds': 1, 'status': 'reserved'})
        extra['seconds'] += 1
        with self.assertRaises(ValueError): validate_ledger(extra)

    def test_existing_reservations_consume_new_shared_budget_on_resume(self):
        # Old weak/homework requests, including unknown outcomes, remain charged.
        for duration, prior_count, expected in [(1, 20, 20), (60, 10, 5)]:
            intervals = [dict(start_ms=i*60000, end_ms=i*60000+duration*1000,
                              quote_start_ms=i*60000, quote_end_ms=i*60000+duration*1000,
                              text='疑点') for i in range(50)]
            attempts = [dict(interval=dict(intervals[i], chunk_id=i, kind='homework' if i%2 else 'weak'),
                             seconds=duration, status='reserved' if i%2 else 'complete')
                        for i in range(prior_count)]
            state = {'attempts': attempts, 'seconds': prior_count*duration,
                     'intervals': intervals, 'weak_intervals': [],
                     'homework': {'candidates': [], 'intervals': [], 'visual': {'status': 'unavailable'}}}
            data = dict(material(), full_chunks=[], transcript='')
            with patch('src.runtime.config.DOUBAO_ASR_API_KEY', 'fake'), \
                 patch('src.ai.doubao_asr.rescue_intervals_pcm', return_value=([], duration, False)) as cloud:
                review_prepared(data, [], MagicMock(), state, lambda: None)
            self.assertEqual(cloud.call_count, expected)
            self.assertEqual(len(state['attempts']), prior_count+expected)
            self.assertEqual(state['seconds'], (prior_count+expected)*duration)
            self.assertEqual(state['attempts'][1]['status'], 'reserved')
            validate_ledger(state)

    def test_completed_old_twenty_clip_checkpoint_is_not_reopened(self):
        attempts = [dict(interval=dict(start_ms=i*30000, end_ms=i*30000+10000),
                         seconds=10, status='complete') for i in range(20)]
        result = {'transcript': '已有结果'}
        state = {'attempts': attempts, 'seconds': 200, 'complete': True, 'material': result}
        saved = copy.deepcopy(state)
        with patch('src.runtime.config.DOUBAO_ASR_API_KEY', 'fake'), \
             patch('src.ai.doubao_asr.rescue_intervals_pcm') as cloud:
            self.assertEqual(review_prepared(material(), [], MagicMock(), state, lambda: None), result)
        cloud.assert_not_called()
        self.assertEqual(state, saved)

    def test_failed_ocr_preserves_audio_review_and_does_not_expose_transport_details(self):
        state = {'intervals': []}
        with patch('src.runtime.config.DOUBAO_ASR_API_KEY', 'fake'), \
             patch('src.ai.qwen_review_ledger.subprocess.run'), \
             patch('scripts.qwen_audio_alignment.align_suspects', side_effect=aligned), \
             patch('src.ai.doubao_asr.rescue_intervals_pcm', return_value=([], 40, False)) as cloud:
            result = review_prepared(material(), [], MagicMock(), state, lambda: None,
                                     homework_ocr=MagicMock(side_effect=RuntimeError('private-cookie-and-url')))
        cloud.assert_called_once()
        self.assertEqual(result['homework']['visual']['status'], 'failed')
        self.assertNotIn('private-cookie', str(state)); self.assertEqual(state['seconds'], 40)


def board_png(size=(640, 360)):
    from PIL import Image
    buf = io.BytesIO(); Image.new('RGB', size, 'black').save(buf, format='PNG')
    return buf.getvalue()


class AssignmentVisualTests(unittest.TestCase):
    def test_short_snapshot_does_not_skip_delayed_board_capture(self):
        from src.pipeline.homework_visual import collect_visual_evidence as collect
        collect_visual_evidence = partial(collect, frames_per_cue=6, delay_seconds=90)
        client = MagicMock(); client.get_ppt_list.return_value = [{'id': 1, 'created_sec': 110, 'pptimgurl': 'private'}]
        candidates = assignment_candidates(material()['full_chunks']); intervals = focus_intervals(aligned({}, candidates)[0], 180)
        client.get_video_url.return_value = None
        result = collect_visual_evidence(client, '10', '1', candidates, intervals, audio_seconds=180,
                                        screenshot_fetcher=MagicMock(return_value=board_png()), ocr=lambda _: '第2题')
        self.assertEqual(result['frames'][0]['text'], '第2题')
        self.assertEqual(result['reference_status'], 'unverified')  # no confidence or matching audio
        self.assertEqual(result['capture_status'], 'partial')
        self.assertNotIn('private', str(result)); self.assertEqual(client.get_video_url.call_count, 6)

    def test_delayed_board_is_captured_after_original_audio_focus(self):
        from src.pipeline.homework_visual import collect_visual_evidence as collect
        collect_visual_evidence = partial(collect, frames_per_cue=6, delay_seconds=90)
        client = MagicMock(); client.get_ppt_list.return_value = []
        client.get_video_url.return_value = 'signed-private'
        client.get_stream_params.return_value = ('vpn-private', 'cookies-private')
        candidates = assignment_candidates(material()['full_chunks']); intervals = focus_intervals(aligned({}, candidates)[0], 180)
        with patch('src.pipeline.homework_visual.video_frame', return_value=board_png()) as frame:
            result = collect_visual_evidence(client, '10', '1', candidates, intervals,
                                            audio_seconds=180, screenshot_fetcher=MagicMock(),
                                            ocr=lambda _: [{'text': '作业第2题', 'confidence': .95}])
        self.assertEqual(frame.call_count, 6)
        self.assertEqual([row['seconds'] for row in result['frames']], [95, 115, 135, 155, 175, 179.9])
        self.assertEqual(result['reference_status'], 'supported')
        self.assertEqual(result['capture_status'], 'complete')
        self.assertNotIn('private', str(result)); self.assertEqual(client.get_video_url.call_count, 6)

    def test_unaligned_quote_uses_block_range_without_claiming_word_alignment(self):
        from src.pipeline.homework_visual import collect_visual_evidence as collect
        collect_visual_evidence = partial(collect, frames_per_cue=6, delay_seconds=90)
        client = MagicMock(); client.get_ppt_list.return_value = []
        client.get_video_url.return_value = 'private'; client.get_stream_params.return_value = ('private', '')
        with patch('src.pipeline.homework_visual.video_frame', return_value=None) as capture:
            result = collect_visual_evidence(client, '10', '1', assignment_candidates(material()['full_chunks']), [],
                                            screenshot_fetcher=MagicMock(), ocr=MagicMock())
        self.assertEqual(result['reference_status'], 'unverified'); self.assertEqual(result['capture_status'], 'unavailable')
        self.assertEqual(capture.call_count, 6)
        self.assertFalse(result['windows'][0]['aligned'])
        self.assertEqual(result['windows'][0]['anchor'], 'asr_block')


class AssignmentRunnerTests(unittest.TestCase):
    def test_corroborated_vision_subquestions_reach_saved_summary(self):
        from test_lecture_quality_gate import _load_runner_class
        from src.ai.homework_vision import validated_frames
        from src.ai.homework_visual_evidence import assess_visual
        Runner = _load_runner_class(); db = MagicMock(); db.get_done_ppt_pages.return_value = []
        llm = MagicMock(); llm.summarize.return_value = ('### 作业安排\n\n矩阵运算要多练习。', 'test')
        runner = Runner(None, db, MagicMock(), MagicMock(), llm, MagicMock())
        data = material(); runner._prepared_asr = data
        raw = copy.deepcopy(data)
        frames = validated_frames({'frames': [
            {'frame_index': i, 'text': 'P69 1(1)(3)', 'references': [
                {'raw': 'P69 1(1)(3)', 'page': 69, 'exercises': ['1(1)(3)'], 'legible': True}]}
            for i in range(2)]}, 2)
        for frame, seconds in zip(frames, [100, 120]):
            frame.update(seconds=seconds, candidate_id='cue')
        evidence = {'candidates': [{}], 'visual': assess_visual({
            'candidate_ids': ['cue', 'missing'], 'frames': frames})}
        runner._qwen_review_material = {'homework': evidence}
        summary = runner._summarize('1', '高等代数', data['transcript'], [])
        self.assertIn('板书列出的练习：第69页：1(1)(3)', summary)
        self.assertEqual(summary.count('作业安排'), 1)
        self.assertNotIn('必须完成', summary)
        self.assertEqual(db.update_summary.call_args.args[1], summary)
        self.assertEqual(data, raw)

    def test_saved_summary_contains_notice_without_mutating_raw_transcript(self):
        from test_lecture_quality_gate import _load_runner_class
        Runner = _load_runner_class(); db = MagicMock(); db.get_done_ppt_pages.return_value = []
        llm = MagicMock(); llm.summarize.return_value = ('仅包含数学知识的摘要', 'test')
        runner = Runner(None, db, MagicMock(), MagicMock(), llm, MagicMock())
        data = material(); runner._prepared_asr = data
        raw = copy.deepcopy(data)
        summary = runner._summarize('1', '高等代数', data['transcript'], [])
        self.assertIn('课程事项提醒', summary)
        self.assertIn('题号', llm.summarize.call_args.args[1])
        self.assertEqual(data, raw); self.assertEqual(db.update_summary.call_args.args[1], summary)

    def test_gather_has_scoped_read_credentials_but_no_mail_credentials(self):
        import yaml
        from pathlib import Path
        workflow = yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/qwen_production_lecture.yml').read_text())
        env = workflow['jobs']['gather']['env']
        self.assertEqual(env['StuId'], '${{ secrets.STUID }}')
        self.assertEqual(env['UISPsw'], '${{ secrets.UISPSW }}')
        self.assertNotIn('SMTP_PASSWORD', env)


class BoardEvidenceTests(unittest.TestCase):
    def test_summary_retains_supported_items_despite_global_unverified_status(self):
        evidence = {'candidates': [{}], 'visual': {'reference_status': 'unverified',
            'reference_evidence': [
                {'text': '第1(1)(3)题', 'page': 69, 'supported': True},
                {'text': '第2题', 'page': 69, 'supported': False},
                {'text': '第3题', 'page': 69, 'supported': True, 'audio_conflict': True}]}}
        source = '### 课程事项提醒\n\n完成上次未完成的练习。\n\n### 矩阵\n\n知识。'
        result = ensure_homework_notice(source, evidence)
        self.assertIn('- 板书列出的练习：第69页：1(1)(3)。', result)
        self.assertLess(result.index('板书列出的练习'), result.index('### 矩阵'))
        self.assertNotIn('第2题', result); self.assertNotIn('第3题', result)
        self.assertEqual(result.count('课程事项提醒'), 1)
        self.assertEqual(ensure_homework_notice(result, evidence), result)
        missing_section = ensure_homework_notice('矩阵知识。', evidence)
        self.assertIn('1(1)(3)', missing_section)
        self.assertNotIn('请参阅', missing_section)
        self.assertEqual(ensure_homework_notice(missing_section, evidence), missing_section)

    def test_existing_numbers_require_correct_page_and_full_subquestion(self):
        evidence = {'candidates': [{}], 'visual': {'reference_evidence': [
            {'text': '第1(1)(3)题', 'page': 69, 'supported': True},
            {'text': '第11题', 'page': 69, 'supported': True}]}}
        for text in ['第69页：1（1）（3）、11。', 'P69 1(1)(3)、11。']:
            existing = '### 作业与课务\n\n' + text
            self.assertEqual(ensure_homework_notice(existing, evidence), existing)
        for source in ['第70页：1(1)(3)、11。', '第69页：1(1)、111。']:
            result = ensure_homework_notice('### 作业安排\n\n' + source, evidence)
            self.assertIn('板书列出的练习：第69页：1(1)(3)、11。', result)
            self.assertEqual(ensure_homework_notice(result, evidence), result)

    def visual(self, texts, *, confidence=.95, candidate='cue'):
        from src.ai.homework_visual_evidence import references
        return {'candidate_ids': [candidate], 'frames': [
            {'seconds': time, 'candidate_id': candidate, 'text': text,
             'references': [{'text': ref, 'confidence': confidence} for ref in references(text)]}
            for time, text in texts]}

    def test_formula_and_example_numbers_never_count_as_exercises(self):
        from src.ai.homework_visual_evidence import assess_visual
        result = assess_visual(self.visual([(100, '例2.5.1 A2=4 第2章 习题二'), (120, '例2.5.1 A2=4 第2章 习题二')]))
        self.assertEqual(result['status'], 'needs_verification')
        self.assertEqual(result['reference_evidence'], [])

    def test_single_instant_crops_and_low_confidence_are_not_corroboration(self):
        from src.ai.homework_visual_evidence import assess_visual
        for visual in [self.visual([(100, '第2题')]*3),
                       self.visual([(100, '第2题'), (120, '第2题')], confidence=.4)]:
            self.assertEqual(assess_visual(visual)['reference_status'], 'unverified')

    def test_audio_must_be_completed_matching_and_from_same_reminder(self):
        from src.ai.homework_visual_evidence import assess_visual
        visual = self.visual([(100, '第7、9题')])
        matching = {'candidate_id': 'cue', 'status': 'complete', 'cloud_text': '请完成第7、9题'}
        self.assertEqual(assess_visual(visual, [matching])['reference_status'], 'supported')
        for clip in [dict(matching, status='reserved'), dict(matching, candidate_id='other'),
                     dict(matching, cloud_text='做第8题')]:
            self.assertEqual(assess_visual(visual, [clip])['reference_status'], 'unverified')

    def test_one_reminder_cannot_hide_another_missing_board(self):
        from src.ai.homework_visual_evidence import assess_visual
        visual = self.visual([(100, '习题二：1、3、5'), (120, '习题二：1、3、5')])
        visual['candidate_ids'].append('other')
        self.assertEqual(assess_visual(visual)['reference_status'], 'unverified')
        self.assertTrue(assess_visual(visual)['reference_evidence'][0]['supported'])

    def test_audio_conflict_overrides_repeated_visual_reference(self):
        from src.ai.homework_visual_evidence import assess_visual
        result = assess_visual(self.visual([(100, '第7题'), (120, '第7题')]),
                               [{'candidate_id': 'cue', 'status': 'complete', 'cloud_text': '完成第8题'}])
        self.assertEqual(result['reference_status'], 'unverified')
        self.assertTrue(result['reference_evidence'][0]['audio_conflict'])
        mixed = assess_visual(self.visual([(100, '第7题，第8题'), (120, '第7题，第8题')]),
                               [{'candidate_id': 'cue', 'status': 'complete', 'cloud_text': '完成第8题'}])
        self.assertEqual(mixed['reference_status'], 'unverified')

    def test_strict_runtime_does_not_turn_engine_failure_into_no_text(self):
        import importlib.util
        import sys
        from pathlib import Path
        from types import ModuleType
        from src.pipeline.homework_visual import read_frame
        fake = ModuleType('rapidocr_onnxruntime'); fake.RapidOCR = MagicMock()
        spec = importlib.util.spec_from_file_location('_board_ocr_test',
            Path(__file__).resolve().parents[1]/'src/ai/ocr.py')
        runtime = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'rapidocr_onnxruntime': fake, '_board_ocr_test': runtime, 'numpy': MagicMock()}):
            spec.loader.exec_module(runtime)
            runtime._engine = MagicMock(side_effect=RuntimeError('private'))
            result = read_frame(board_png(), runtime.ocr_image_strict)
        self.assertEqual(result['status'], 'ocr_failed')
        self.assertNotIn('private', str(result))

    def test_capture_decode_no_text_and_ocr_failures_are_distinct(self):
        from src.pipeline.homework_visual import read_frame
        self.assertEqual(read_frame(None, MagicMock())['status'], 'capture_failed')
        self.assertEqual(read_frame(b'not-image', MagicMock())['status'], 'image_decode_failed')
        self.assertEqual(read_frame(board_png(), lambda _: [])['status'], 'no_text')
        failed = read_frame(board_png(), MagicMock(side_effect=RuntimeError('private-cookie')))
        self.assertEqual(failed['status'], 'ocr_failed')
        self.assertNotIn('private-cookie', str(failed))

    def test_original_resolution_and_overlapping_enlarged_regions(self):
        from src.pipeline.homework_visual import image_views
        views = image_views(board_png((1920, 1080)))
        self.assertEqual(views[0][2], (1920, 1080))
        self.assertEqual(len(views), 3)
        self.assertGreater(views[1][2][0], 1280)
        self.assertGreater(views[1][2][1], 1080)

    def test_transport_finishes_before_video_ocr_and_new_cue_refreshes_source(self):
        from src.pipeline.homework_visual import collect_visual_evidence as collect
        collect_visual_evidence = partial(collect, frames_per_cue=6, delay_seconds=90)
        client = MagicMock(); client.get_ppt_list.return_value = []; client.get_video_url.return_value = 'private'
        client.get_stream_params.return_value = ('private', '')
        candidates = assignment_candidates([dict(start=0, end=120, text='作业完成第二题。'),
                                            dict(start=120, end=240, text='下次作业完成第三题。')])
        intervals = [dict(chunk_id=c['id'], text=c['quote'], quote_start_ms=(i*120+10)*1000,
                          quote_end_ms=(i*120+20)*1000) for i, c in enumerate(candidates)]
        events = []
        def fetch(*args, **kwargs): events.append('capture'); return board_png()
        def ocr(data): events.append('ocr'); return []
        with patch('src.pipeline.homework_visual.video_frame', side_effect=fetch):
            collect_visual_evidence(client, '10', '1', candidates, intervals, audio_seconds=300, ocr=ocr)
        self.assertEqual(events[:6], ['capture']*6)
        self.assertEqual(events[24:30], ['capture']*6)  # 6 captures + 18 OCR passes
        self.assertEqual(client.get_video_url.call_count, 12)

    def test_independent_seeks_never_reuse_signed_transport_identity(self):
        from src.pipeline.homework_visual import collect_visual_evidence as collect
        collect_visual_evidence = partial(collect, frames_per_cue=6, delay_seconds=90)
        client = MagicMock(); client.get_ppt_list.return_value = []
        client.get_video_url.side_effect = [f'signed-{i}' for i in range(6)]
        client.get_stream_params.side_effect = lambda url: (url, 'private')
        candidates = assignment_candidates(material()['full_chunks']); intervals = focus_intervals(aligned({}, candidates)[0], 180)
        seen = set()
        def fetch(params, seconds, **kwargs):
            if params[0] in seen: return {'image': None, 'error_code': 'http_403'}
            seen.add(params[0]); return {'image': board_png(), 'error_code': None}
        with patch('src.pipeline.homework_visual.video_frame', side_effect=fetch):
            result = collect_visual_evidence(client, '10', '1', candidates, intervals, audio_seconds=180, ocr=lambda _: [])
        self.assertEqual(len(seen), 6)
        self.assertEqual(result['capture_status'], 'complete')

    def test_frame_transport_diagnostics_never_expose_private_error_bodies(self):
        import subprocess
        from src.pipeline.homework_visual import video_frame
        error = subprocess.CalledProcessError(1, 'ffmpeg', stderr=b'HTTP error 403: private-url?cookie=secret')
        with patch('src.pipeline.homework_visual.subprocess.run', side_effect=error):
            result = video_frame(('private-url', 'private-cookie'), 100, diagnostic=True)
        self.assertEqual(result, {'image': None, 'error_code': 'http_403'})
        self.assertNotIn('secret', str(result))

    def test_only_later_frames_with_homework_references_support_evidence(self):
        from src.pipeline.homework_visual import collect_visual_evidence as collect
        collect_visual_evidence = partial(collect, frames_per_cue=6, delay_seconds=90)
        client = MagicMock(); client.get_ppt_list.return_value = [{'id': 1, 'created_sec': 110}]
        client.get_video_url.return_value = 'private'; client.get_stream_params.return_value = ('private', 'private')
        candidates = assignment_candidates(material()['full_chunks']); intervals = focus_intervals(aligned({}, candidates)[0], 180)
        ordinary, board = board_png(), board_png((650, 360))
        def ocr(data):
            from PIL import Image
            w, _ = Image.open(io.BytesIO(data)).size
            return [{'text': '第7题' if w == 650 else 'A2=4', 'confidence': .96}]
        with patch('src.pipeline.homework_visual.video_frame', side_effect=lambda params, sec, **kw: board if sec >= 155 else ordinary) as frame:
            result = collect_visual_evidence(client, '10', '1', candidates, intervals, audio_seconds=180,
                                            screenshot_fetcher=lambda *a, **k: ordinary, ocr=ocr)
        self.assertEqual(result['reference_status'], 'supported')
        self.assertEqual(result['reference_evidence'][0]['seconds'], [155, 175, 179.9])
        self.assertEqual(frame.call_count, 6)

    def test_no_frame_seeks_past_end_and_four_cues_remain_bounded(self):
        from src.pipeline.homework_visual import collect_visual_evidence as collect
        collect_visual_evidence = partial(collect, frames_per_cue=6, delay_seconds=90)
        client = MagicMock(); client.get_ppt_list.return_value = []; client.get_video_url.return_value = 'private'
        client.get_stream_params.return_value = ('private', '')
        chunks = [dict(start=i*120, end=(i+1)*120, text='今天布置作业，完成第二题。') for i in range(4)]
        candidates = assignment_candidates(chunks)
        intervals = [dict(chunk_id=c['id'], text=c['quote'], quote_start_ms=(i*120+10)*1000,
                          quote_end_ms=(i*120+20)*1000) for i, c in enumerate(candidates)]
        with patch('src.pipeline.homework_visual.video_frame', return_value=None) as frame:
            result = collect_visual_evidence(client, '10', '1', candidates, intervals, audio_seconds=410, ocr=MagicMock())
        self.assertLessEqual(frame.call_count, 24)
        self.assertTrue(all(0 <= call.args[1] < 410 for call in frame.call_args_list))
        self.assertEqual(result['capture_status'], 'unavailable')

    def test_old_success_marker_does_not_satisfy_visual_acceptance(self):
        from scripts.production_homework_validation import acceptance
        state = {'material': {'homework': {'visual': {'status': 'ok', 'frames': [{'text': 'A=I'}]}}}}
        self.assertFalse(acceptance(state, {'start': 0, 'end': 10}, 0, '作业与课务提醒', True)['visual_completed'])
        summary = ensure_homework_notice('## 作业与课务提醒\n矩阵作业待核实。',
                                        {'candidates': [{}], 'visual': {'status': 'ok'}})
        self.assertIn('待核实', summary)
        self.assertNotIn('视觉核对未完成', summary)
        self.assertEqual(ensure_homework_notice(summary, {'candidates': [{}]}), summary)


class TailValidationTests(unittest.TestCase):
    def test_visual_replay_reuses_pinned_quote_and_original_alignment(self):
        import hashlib
        from scripts.production_homework_visual_validation import replay_selection, AUDIO_HASH, PLAN_HASH
        quote = '今天布置作业，请完成具体题目，题号再看一下。'
        original = {'audio_seconds': 6532.096, 'audio_sha256': AUDIO_HASH, 'plan_hash': PLAN_HASH,
                    'full_chunks': [dict(start=5985+i*120, end=6105+i*120, text=quote if i == 3 else '') for i in range(4)]}
        with patch('scripts.production_homework_visual_validation.QUOTE_HASH', hashlib.sha256(quote.encode()).hexdigest()):
            data, scope, candidates, intervals = replay_selection(original)
            self.assertEqual(intervals[0]['quote_end_ms'], 6346475)
            self.assertEqual(intervals[0]['text'], quote)
            with self.assertRaises(ValueError): replay_selection(dict(original, audio_sha256='changed'))
            original['full_chunks'][3]['text'] += '改变'
            with self.assertRaises(ValueError): replay_selection(original)

    def test_visual_replay_credentials_cannot_call_cloud_or_send_email(self):
        import yaml
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        flow = yaml.safe_load((root/'.github/workflows/qwen_homework_visual_validation.yml').read_text())
        self.assertEqual(flow['permissions'], {'contents': 'read', 'actions': 'read'})
        env = flow['jobs']['replay']['env']
        for name in ['SMTP_PASSWORD', 'DOUBAO_ASR_API_KEY', 'DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY', 'GEMINI_API_KEY']:
            self.assertNotIn(name, env)
        self.assertEqual(flow['jobs']['replay']['if'], "github.event_name == 'workflow_dispatch'")
        self.assertFalse(any(step.get('with', {}).get('inference') == 'true' for step in flow['jobs']['replay']['steps']))

    def test_tail_preserves_original_clock_and_excludes_partial_boundary_block(self):
        from scripts.production_homework_validation import scoped_material
        original = {'audio_seconds': 1000, 'full_chunks': [
            {'chunk_id': 0, 'start': 350, 'end': 472, 'text': '边界前内容'},
            {'chunk_id': 1, 'start': 470, 'end': 592, 'text': '具体作业要求'}]}
        data, scope = scoped_material(original)
        self.assertEqual(scope['start'], 400)
        self.assertEqual(data['full_chunks'][0]['chunk_id'], 1)
        self.assertEqual(data['segments'][0]['start_ms'], 470000)
        self.assertEqual(data['transcript'], '具体作业要求')
        self.assertEqual(data['weak_windows'], [])
        self.assertEqual(len(original['full_chunks']), 2)

    def test_tail_review_keeps_every_original_quota_reservation(self):
        from scripts.production_homework_validation import isolated_ledger
        original = {'complete': True, 'seconds': 30, 'attempts': [
            {'interval': {'start_ms': 0, 'end_ms': 30000, 'kind': 'weak'},
             'seconds': 30, 'status': 'complete', 'segments': []}], 'intervals': [{'old': True}]}
        state = isolated_ledger(original)
        self.assertEqual(state['attempts'], original['attempts'])
        self.assertEqual(state['seconds'], 30)
        self.assertNotIn('complete', state)
        self.assertEqual(state['intervals'], [])
        self.assertTrue(original['complete']); validate_ledger(state)
        for unsafe in [dict(original, complete=False), dict(original, failed=True)]:
            with self.assertRaises(ValueError): isolated_ledger(unsafe)

    def test_tail_entry_is_read_only_and_cannot_run_on_push(self):
        import yaml
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        flow = yaml.safe_load((root/'.github/workflows/qwen_homework_validation.yml').read_text())
        self.assertEqual(flow['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertIn("github.event_name == 'workflow_dispatch'", flow['jobs']['review']['if'])
        env = flow['jobs']['review']['env']
        self.assertNotIn('SMTP_PASSWORD', env)
        self.assertEqual(env['AUTO_COURSE_TERMS'], 'false')
        self.assertEqual(flow['jobs']['register']['permissions'], {})
