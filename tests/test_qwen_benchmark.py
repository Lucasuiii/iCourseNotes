import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from scripts.benchmark_qwen_asr import parse_request, auth_phase, configure_auth_session,sample_seconds,latest_request,summary_material,recognition_context,record_summary_response,selected_test_request,review_hotwords,infer_runtime_sample
from scripts.qwen_segmentation import plan_chunks, plan_long_chunks, join_chunk_text
from src.ai.course_glossary import course_terms


class QwenBenchmarkTests(unittest.TestCase):
    def test_failed_media_probe_is_bounded_and_closes_response(self):
        import base64
        from unittest.mock import MagicMock
        from scripts.benchmark_qwen_asr import failed_media_probe
        client = MagicMock()
        response = client.vpn.session.get.return_value.__enter__.return_value
        response.status_code = 200
        response.headers = {'Content-Type': 'text/html'}
        response.url = 'private url'
        response.iter_content.return_value = iter([b'x' * 9000, b'unread'])
        result = failed_media_probe(client, 'private url')
        self.assertEqual(len(base64.b64decode(result['private_prefix_b64'])), 4096)
        client.vpn.session.get.return_value.__exit__.assert_called_once()
        client.vpn.session.get.assert_called_once_with('private url',
            headers={'Range': 'bytes=0-4095'}, stream=True, timeout=30)

    def test_latest_recording_skips_empty_schedule_and_keeps_review_gate_fallback(self):
        from scripts.benchmark_qwen_asr import resolve_latest_playback
        client = Mock()
        client.get_course_detail.return_value = {'title': '高等代数Ⅰ', 'lectures': [
            {'date': '2026-09-30', 'sub_id': '1', 'sub_title': '第1-2节', 'has_playback': False},
            {'date': '2026-10-01', 'sub_id': '2', 'sub_title': '第1-2节', 'has_playback': False},
            {'date': '2026-10-06', 'sub_id': '3', 'sub_title': '第1-2节', 'has_playback': True}]}
        client.get_video_url.side_effect = [None, 'private playback']
        with patch('scripts.benchmark_qwen_asr.save_encrypted'):
            request, url = resolve_latest_playback(client, {'course_id': '10001'}, '2026-10-04')
        self.assertEqual(request['sub_id'], '1')
        self.assertEqual(url, 'private playback')
        self.assertEqual(request['selection']['skipped_unavailable'][0]['sub_id'], '2')
        self.assertEqual(client.get_video_url.call_args_list, [
            unittest.mock.call('10001', '2'), unittest.mock.call('10001', '1')])
        client.get_video_url.side_effect = [None, None]
        with patch('scripts.benchmark_qwen_asr.save_encrypted'), self.assertRaises(RuntimeError):
            resolve_latest_playback(client, {'course_id': '10001'}, '2026-10-04')

    def test_final_failure_is_encrypted_and_preserves_prior_checkpoint(self):
        import base64
        import tempfile
        from pathlib import Path
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from scripts.benchmark_qwen_asr import save_encrypted, record_failure
        key = b'x' * 32
        with tempfile.TemporaryDirectory() as tmp, \
             patch('scripts.benchmark_qwen_asr.workspace', return_value=Path(tmp)), \
             patch.dict('os.environ', {'QWEN_ASR_TEST_KEY': base64.b64encode(key).decode()}):
            for checkpoint in (None, {'attempts': [1]}):
                (Path(tmp) / 'result.enc').unlink(missing_ok=True)
                if checkpoint:
                    save_encrypted(checkpoint)
                record_failure('fetch', RuntimeError('private playback reason'))
                data = (Path(tmp) / 'result.enc').read_bytes()
                self.assertNotIn(b'private playback reason', data)
                result = json.loads(AESGCM(key).decrypt(data[5:17], data[17:], b'qwen-asr-benchmark-v1'))
                self.assertEqual(result['test_failure']['private_error'], 'private playback reason')
                self.assertEqual(result.get('attempts'), checkpoint.get('attempts') if checkpoint else None)

    def test_parallel_requests_remain_isolated_and_single_mode_is_unchanged(self):
        requests = [{'course_id': '10', 'sub_id': '0', 'offset': 0, 'duration': 600},
                    {'course_id': '20', 'sub_id': '0', 'offset': 0, 'duration': 600}]
        for slot, course in [('0', '10'), ('1', '20')]:
            with patch.dict('os.environ', {'TEST_COURSE_SLOT': slot,
                            'QWEN_ASR_TEST_REQUESTS': json.dumps(requests)}):
                self.assertEqual(selected_test_request()['course_id'], course)
        with patch.dict('os.environ', {'TEST_COURSE_SLOT': '-1',
                        'QWEN_ASR_TEST_REQUEST': json.dumps(requests[0])}):
            self.assertEqual(selected_test_request()['course_id'], '10')
        for slot, values in [('2', requests), ('0', requests[:1]),
                             ('0', [requests[0], requests[0]])]:
            with patch.dict('os.environ', {'TEST_COURSE_SLOT': slot,
                            'QWEN_ASR_TEST_REQUESTS': json.dumps(values)}), self.assertRaises(ValueError):
                selected_test_request()

    @patch('src.ai.course_glossary._entries', return_value=[{'courses':['概率论'],'terms':['条件概率']}])
    def test_probability_test_does_not_use_numerical_course_hints(self, entries):
        terms = course_terms('概率论')
        self.assertIn('条件概率', terms)
        self.assertEqual(recognition_context('概率论'), '术语：' + '、'.join(terms))
        self.assertEqual(review_hotwords({'selection': {'course_title': '概率论'}}, {}), terms)
        self.assertNotIn('希尔伯特矩阵', recognition_context('概率论'))
        self.assertEqual(recognition_context('其他课程'), '')
        self.assertEqual(review_hotwords({'selection': {'course_title': '其他课程'}}, {}), [])
        with patch.dict('os.environ', {'LATEST_LECTURE': 'true'}):
            self.assertEqual(recognition_context(), '')
            self.assertEqual(review_hotwords({}, {}), [])

    def run_private_runtime(self, error=None, duration=160):
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        t = MagicMock(last_audio_duration=160, last_chunks=[{'start': 0, 'end': 160, 'text': '概率课堂'}],
                      last_vad_windows=[(0, 160)])
        if error:
            t.transcribe_tail.side_effect = error
        else:
            t.transcribe_tail.return_value = ('概率课堂', [{'start_ms': 0, 'end_ms': 160000, 'text': '概率课堂'}])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'evidence.json').write_text(json.dumps({'selection': {'course_title': '概率论'}}))
            reports=[]
            with patch.dict('os.environ', {'PUBLIC_SAMPLE': 'false', 'FULL_LECTURE': 'true'}), \
                 patch('scripts.benchmark_qwen_asr.workspace', return_value=root), \
                 patch.dict(sys.modules, {'soundfile': SimpleNamespace(info=lambda _: SimpleNamespace(samplerate=16000, channels=1, duration=duration))}), \
                 patch('scripts.benchmark_qwen_asr.subprocess.run'), \
                 patch('src.ai.transcriber.Transcriber', return_value=t), \
                 patch('scripts.benchmark_qwen_asr.save_encrypted', side_effect=lambda r: reports.append(json.loads(json.dumps(r)))):
                if error:
                    with self.assertRaises(type(error)):
                        infer_runtime_sample()
                else:
                    infer_runtime_sample()
        return t, reports

    def test_full_runtime_uses_production_transcriber_and_retains_selected_course(self):
        t, reports=self.run_private_runtime()
        t.set_terms.assert_called_once_with(course_terms('概率论'))
        t.transcribe_tail.assert_called_once()
        self.assertEqual(reports[-1]['source'], 'authorized_full_runtime')
        self.assertEqual(reports[-1]['selection']['course_title'], '概率论')
        self.assertTrue(reports[-1]['complete'])
        self.assertEqual(reports[-1]['vad_windows'], [[0, 160]])
        t.release_model.assert_called_once()

    def test_full_runtime_preserves_encrypted_partial_diagnostic_on_failure(self):
        t, reports=self.run_private_runtime(TimeoutError('private diagnostic'))
        self.assertFalse(reports[-1]['complete'])
        self.assertEqual(reports[-1]['error_type'], 'TimeoutError')
        self.assertEqual(reports[-1]['full_chunks'][0]['text'], '概率课堂')
        self.assertNotIn('private diagnostic', json.dumps(reports))
        t.release_model.assert_called_once()

    def test_rounded_acquisition_cutoff_is_rejected_before_model_inference(self):
        for duration in (10799.999, 10800, 10800.05):
            t, reports = self.run_private_runtime(ValueError('acquisition cap'), duration=duration)
            t.transcribe_tail.assert_not_called()
            self.assertFalse(reports[-1]['complete'])
            self.assertTrue(reports[-1]['acquisition_limit_reached'])
            self.assertEqual(reports[-1]['acquired_audio_seconds'], duration)

    def test_incomplete_summary_preserves_diagnostic_not_success(self):
        report={}
        choice=SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='未完成正文',reasoning_content='思考'))
        response=SimpleNamespace(choices=[choice],usage=None)
        self.assertFalse(record_summary_response(report,response,3))
        self.assertEqual(report['summary_attempts'][0]['markdown'],'未完成正文')
        self.assertEqual(report['summary_attempts'][0]['finish_reason'],'length')
        self.assertNotIn('test_summary',report)
        choice.finish_reason='stop'
        self.assertTrue(record_summary_response(report,response,4))
        self.assertFalse(report['test_summary']['email_sent'])
        choice.message.content=' '
        self.assertFalse(record_summary_response({},response,4))
        self.assertFalse(record_summary_response({},SimpleNamespace(choices=[],usage=None),1))
    def test_latest_selection_includes_reviewing_but_not_future(self):
        detail={'title':'数值算法','lectures':[
            {'date':'2026-09-22','sub_id':'1','sub_title':'2026-09-22第1-2节','has_playback':True},
            {'date':'2026-09-29','sub_id':'2','sub_title':'2026-09-29第1-2节','has_playback':False},
            {'date':'2026-10-06','sub_id':'3','sub_title':'2026-10-06第1-2节'}]}
        result=latest_request(detail,{'course_id':'10002'},today='2026-10-04')
        self.assertEqual(result['sub_id'],'2')
        self.assertEqual(result['offset'],0)
        with self.assertRaises(ValueError):
            latest_request({'lectures':[]},{},today='2026-10-04')
        with patch.dict('os.environ',{'LATEST_LECTURE':'true'}):
            self.assertNotIn('希尔伯特',recognition_context())

    def test_summary_keeps_cloud_as_variant_and_rejects_partial(self):
        report={'complete':True,'transcript':'原始课堂正文','full_chunks':[],
                'rescue_comparisons':[{'original_quote':'原句','cloud_text':'另一版本'}]}
        material=summary_material(report,{})
        self.assertIn('原始课堂正文',material)
        self.assertIn('另一识别版本',material)
        with self.assertRaises(ValueError):
            summary_material({**report,'complete':False},{})
        bounded = summary_material({**report, 'full_lecture_complete': False}, {})
        self.assertIn('完整课堂范围未经验证', bounded)
        self.assertIn('不得声称覆盖整堂课', bounded)
    def test_bounded_sample_duration(self):
        with patch.dict('os.environ',{'SAMPLE_MINUTES':'30','LONG_CHUNK_SAMPLE':'true'}):
            self.assertEqual(sample_seconds(),1800)
        for extra in ({'SAMPLE_MINUTES':'30','LONG_CHUNK_SAMPLE':'false'},
                      {'SAMPLE_MINUTES':'60','LONG_CHUNK_SAMPLE':'true'}):
            with patch.dict('os.environ',extra),self.assertRaises(ValueError):
                sample_seconds()
    def request(self, **extra):
        return json.dumps({"course_id": "1", "sub_id": "2", "offset": 5220, "duration": 600, **extra})

    def test_accepts_bounded_private_slice(self):
        self.assertEqual(parse_request(self.request())["duration"], 600)

    def test_rejects_long_or_invalid_selection(self):
        for extra in ({"duration": 601}, {"duration": 0}, {"offset": -1},
                      {"duration": float("nan")}, {"course_id": "1; echo bad"}):
            with self.assertRaises(ValueError):
                parse_request(self.request(**extra))

    def test_silence_skipped_and_pause_preserved(self):
        self.assertEqual(plan_chunks([], 120), [])
        self.assertEqual(plan_chunks([(10, 20), (40, 50)], 120), [(9, 21), (39, 51)])

    def test_short_pause_merges_and_long_speech_is_bounded(self):
        self.assertEqual(plan_chunks([(0, 10), (10.5, 20)], 60), [(0, 21)])
        chunks = plan_chunks([(0, 95)], 95)
        self.assertTrue(all(end - start <= 30 for start, end in chunks))
        self.assertEqual(chunks[0][0], 0)
        self.assertEqual(chunks[-1][1], 95)
        self.assertTrue(all(a[1] >= b[0] for a, b in zip(chunks, chunks[1:])))

    def test_invalid_window_rejected(self):
        for window in ((-1, 2), (2, 1), (0, 61), (0, float('nan'))):
            with self.assertRaises(ValueError):
                plan_chunks([window], 60)

    def test_boundary_dedupe_does_not_delete_nonoverlapping_repetition(self):
        rows = [{"start": 0, "end": 30, "text": "我们讨论希尔伯特矩阵"},
                {"start": 28, "end": 60, "text": "希尔伯特矩阵的条件数"}]
        self.assertEqual(join_chunk_text(rows), "我们讨论希尔伯特矩阵\n的条件数")
        rows[1]['start'] = 30
        self.assertIn("\n希尔伯特矩阵", join_chunk_text(rows))

    def test_short_repetition_preserved(self):
        rows = [{"start": 0, "end": 30, "text": "矩阵"},
                {"start": 28, "end": 60, "text": "矩阵很重要"}]
        self.assertEqual(join_chunk_text(rows), "矩阵\n矩阵很重要")

    def test_long_chunks_keep_short_pauses(self):
        windows = [(i, i+3) for i in range(0, 600, 5)]
        chunks = plan_long_chunks(windows, 600)
        self.assertLessEqual(len(chunks), 7)
        self.assertTrue(all(b-a <= 122 for a,b in chunks))
        self.assertEqual(chunks[0][0], 0)
        self.assertTrue(all(a[1] >= b[0] for a,b in zip(chunks,chunks[1:])))

    def test_long_chunks_skip_long_silence_not_quiet_speech(self):
        self.assertEqual(plan_long_chunks([], 600), [])
        self.assertEqual(plan_long_chunks([(10,20),(50,55)], 600), [(9,21),(49,56)])
        self.assertEqual(plan_long_chunks([(10,20),(25,35)], 600), [(9,36)])

    def test_long_chunks_continuous_speech_and_invalid_input(self):
        chunks = plan_long_chunks([(0,600)], 600)
        self.assertEqual(len(chunks),5)
        self.assertEqual(chunks[-1][1],600)
        with self.assertRaises(ValueError):
            plan_long_chunks([(0,601)],600)

    def test_auth_phase_never_exposes_query_or_unknown_path(self):
        self.assertEqual(auth_phase('https://example.org/idp/authn/authExecute?token=private'), 'credential_exchange')
        self.assertEqual(auth_phase('https://example.org/private-account?ticket=private'), 'portal_or_redirect')

    def test_timeout_policy_and_safe_diagnostics(self):
        session = Mock()
        response = Mock(status_code=200)
        original = Mock(return_value=response)
        session.request = original
        events = []
        configure_auth_session(session, events)
        session.request('GET', 'https://example.org/', timeout=5)
        self.assertEqual(original.call_args.kwargs['timeout'], (15, 20))
        original.side_effect = TimeoutError('private-token')
        with self.assertRaises(TimeoutError):
            session.request('POST', 'https://example.org/authExecute', timeout=60)
        self.assertEqual(original.call_count, 2)  # No credential POST replay.
        self.assertNotIn('private-token', json.dumps(events))
        self.assertEqual(events[-1]['error_type'], 'TimeoutError')


if __name__ == "__main__":
    unittest.main()
