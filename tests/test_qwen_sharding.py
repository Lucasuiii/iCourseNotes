import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
import wave

from scripts.qwen_sharding import (build_plan, validate_plan, validate_result, assemble,
                                  fingerprint, parse_baselines, MAX_RUNNERS, build_audio_plan)
from scripts import sharded_qwen_pilot as pilot, benchmark_qwen_asr as benchmark
from src.ai.qwen_transcriber import MODEL, REVISION


def baseline(duration=5, rows=None):
    rows = rows or [{'start': i, 'end': i+2, 'text': text, 'quality_state': 'recognized'}
        for i, text in enumerate(['前面内容边界重复句子', '边界重复句子接着讲矩阵', '第三个原始块', '第四个原始块'])]
    return {'source': 'authorized_full_runtime', 'model': MODEL, 'revision': REVISION,
            'complete': True, 'audio_seconds': duration, 'seconds': 10,
            'recognition_terms': ['矩阵'], 'full_chunks': rows,
            'selection': {'course_id': '10001', 'sub_id': '123', 'course_title': '高等代数Ⅰ'},
            'vad_windows': [[0, duration]], 'transcript': '基线',
            'reference_evidence': {'selection': {'course_title': '高等代数Ⅰ'}, 'ppt': []}}


def plan_for(data=None, mode='2'):
    return build_plan(data or baseline(), reference={'run_id': '11', 'artifact': 'qwen-asr-encrypted-result-1'},
                      course_slot=0, run_id='100', audio_sha256='a'*64, mode=mode)


def results_for(plan, data):
    return [{'plan_hash': fingerprint(plan), 'shard_id': s['shard_id'], 'complete': True, 'seconds': 1,
             'chunks': [{**data['full_chunks'][n], 'chunk_id': n} for n in s['chunk_ids']]}
            for s in plan['shards']]


class ShardPlanTests(unittest.TestCase):
    def test_review_selection_respects_each_profile_clip_limit(self):
        from scripts.qwen_quality import review_quality
        client = Mock()
        client.chat.completions.create.return_value.choices = [Mock(message=Mock(content=json.dumps({
            'suspects': [{'id': i, 'quote': f'具体异常原文编号{i:02}', 'reason': '口述不完整'} for i in range(20)]})))]
        report = {'full_chunks': [{'start': i*120, 'end': (i+1)*120,
                  'text': f'具体异常原文编号{i:02}'} for i in range(20)]}
        for profile, expected in [('production', 20), ('pilot15', 18)]:
            selected = review_quality(client, 'fake', report, {}, max_suspects=100, budget_profile=profile)
            self.assertEqual(len(selected), expected)

    def test_40_60_80_minutes_use_post_vad_work_and_balance(self):
        self.assertEqual(MAX_RUNNERS, 15)
        for minutes, count in [(40, 2), (60, 2), (80, 3)]:
            duration = minutes*60
            rows = [{'start': i, 'end': min(i+120, duration), 'text': '原始块'} for i in range(0, duration, 120)]
            plan = plan_for(baseline(duration, rows), mode='auto')
            validate_plan(plan)
            self.assertEqual(len(plan['shards']), count)
            self.assertEqual(plan['pending_audio_seconds'], duration)
            loads = [s['audio_seconds'] for s in plan['shards']]
            self.assertLessEqual(max(loads)-min(loads), 120)
            self.assertEqual(len(plan_for(baseline(duration, rows))['shards']), 2)

    def test_preserves_noncontiguous_global_ids_and_original_time_dedupe(self):
        data = baseline()
        plan = plan_for(data)
        results = results_for(plan, data)
        report = assemble(plan, list(reversed(results)), data)
        self.assertEqual([r['chunk_id'] for r in report['full_chunks']], list(range(4)))
        self.assertEqual(report['segments'][1]['text'], '接着讲矩阵')
        self.assertEqual(report['segments'][1]['start_ms'], 1000)
        self.assertEqual(report['full_chunks'][1]['text'], '边界重复句子接着讲矩阵')
        self.assertFalse(report['baseline_comparison']['quality_accuracy_verified'])

    def test_missing_duplicate_stale_and_changed_timestamp_results_are_rejected(self):
        data = baseline()
        plan = plan_for(data)
        original = results_for(plan, data)
        cases = [original[:1], [original[0], original[0]]]
        for field, value in [('complete', False), ('plan_hash', 'stale')]:
            changed = deepcopy(original)
            changed[0][field] = value
            cases.append(changed)
        changed = deepcopy(original)
        changed[0]['chunks'][0]['start'] = 0.5
        cases.append(changed)
        changed = deepcopy(original)
        changed[0]['chunks'].pop()
        cases.append(changed)
        for values in cases:
            with self.assertRaises(ValueError):
                assemble(plan, values, data)
        bad_plan = deepcopy(plan)
        bad_plan['shards'][1]['chunk_ids'].append(bad_plan['shards'][0]['chunk_ids'][0])
        with self.assertRaises(ValueError):
            validate_plan(bad_plan)

    def test_partial_success_cannot_claim_complete(self):
        data = baseline()
        plan = plan_for(data)
        result = results_for(plan, data)[0]
        result['chunks'] = result['chunks'][:1]
        with self.assertRaises(ValueError):
            validate_result(plan, result, 0)
        result['complete'] = False
        self.assertEqual(validate_result(plan, result, 0), {result['chunks'][0]['chunk_id']})

    def test_changed_vad_timeline_is_not_reported_as_aligned_chunk_comparison(self):
        data = baseline()
        plan = plan_for(data)
        older = deepcopy(data)
        older['full_chunks'][0]['end'] = 1.9
        report = assemble(plan, results_for(plan, data), older)
        self.assertFalse(report['baseline_comparison']['same_block_timeline_verified'])
        self.assertIsNone(report['baseline_comparison']['changed_chunk_ids'])

    def test_independent_baseline_must_match_lecture_duration_and_terms(self):
        plan = plan_for()
        pilot.validate_baseline_match(plan, baseline())
        for change in ('lecture', 'duration', 'terms', 'incomplete'):
            data = baseline()
            if change == 'lecture':
                data['selection']['sub_id'] = '124'
            elif change == 'duration':
                data['audio_seconds'] = 6
            elif change == 'terms':
                data['recognition_terms'] = ['行列式']
            else:
                data['complete'] = False
            with self.assertRaises(ValueError):
                pilot.validate_baseline_match(plan, data)
        plan['allow_partial_comparison'] = True
        older = baseline(6)
        older['complete'] = False
        pilot.validate_baseline_match(plan, older)
        near_cap = baseline(10799.999)
        partial = build_audio_plan(near_cap, reference=plan['reference'], course_slot=0,
            run_id='100', audio_sha256='a'*64, allow_partial=True)
        self.assertTrue(partial['allow_partial_comparison'])

    def test_baseline_and_request_boundaries(self):
        reference = {'run_id': '1', 'artifact': 'qwen-asr-encrypted-result-0'}
        self.assertEqual(parse_baselines(json.dumps([reference])), [reference])
        self.assertEqual(parse_baselines(json.dumps([{**reference, 'reuse_source_slot': 1}]))[0]['reuse_source_slot'], 1)
        for value in (-1, 5, True, '1'):
            with self.assertRaises(ValueError):
                parse_baselines(json.dumps([{**reference, 'reuse_source_slot': value}]))
        for values in ([], [reference]*2, [reference]*6, [{'run_id': '1', 'artifact': '../../private'}]):
            with self.assertRaises(ValueError):
                parse_baselines(json.dumps(values))
        for field, value in [('complete', False), ('revision', 'unknown'), ('source', 'other'),
                             ('acquisition_limit_reached', True)]:
            data = baseline()
            data[field] = value
            with self.assertRaises(ValueError):
                plan_for(data)
        for duration in (10799.999, 10800):
            with self.assertRaises(ValueError):
                plan_for(baseline(duration))
        plan_for(baseline(10799))


class ShardRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'RUNNER_TEMP': str(self.base/'prepare'),
            'QWEN_ASR_TEST_KEY': base64.b64encode(b'k'*32).decode(), 'GITHUB_RUN_ID': '100',
            'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REPOSITORY': 'owner/repo', 'COURSE_SLOT': '0',
            'SHARD_MODE': '2', 'BASELINE_RUN_ID': '11', 'BASELINE_ARTIFACT': 'qwen-asr-encrypted-result-1',
            'QWEN_REVIEW_PROFILE': 'production'})
        self.env.start()
        Path(os.environ['RUNNER_TEMP']).mkdir()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def stage(self, name):
        os.environ['RUNNER_TEMP'] = str(self.base/name)
        Path(os.environ['RUNNER_TEMP']).mkdir(exist_ok=True)
        return pilot.root()

    def prepare_fixture(self, *, independent=False):
        if not shutil.which('ffmpeg'):
            self.skipTest('ffmpeg needed for lossless media roundtrip')
        import numpy as np
        data = baseline()
        fixture = self.base/'source.wav'
        samples = (np.sin(np.arange(80000)*0.1)*1000).astype('<i2')
        with wave.open(str(fixture), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(samples.tobytes())
        original_command = pilot.command
        def execute(args, **kwargs):
            if args[0] == 'gh':
                self.assertFalse(independent, 'Independent preparation must not download pending baseline')
                destination = pilot.root()/'baseline'
                destination.mkdir()
                benchmark.save_encrypted(data)
                shutil.copyfile(benchmark.workspace()/'result.enc', destination/'result.enc')
            else:
                original_command(args, **kwargs)
        def fetch():
            if independent:
                self.assertEqual(os.environ['TEST_COURSE_SLOT'], '1')
                self.assertEqual(os.environ['LATEST_LECTURE'], 'true')
            else:
                request = json.loads(os.environ['QWEN_ASR_TEST_REQUEST'])
                self.assertEqual(request['sub_id'], '123')
                self.assertEqual(os.environ['LATEST_LECTURE'], 'false')
            shutil.copyfile(fixture, benchmark.workspace()/'audio.wav')
            (benchmark.workspace()/'evidence.json').write_text(json.dumps(
                {**data['reference_evidence'], 'selection': data['selection']}))
        def vad(transcriber, *args, **kwargs):
            transcriber.last_vad_windows = [(0, 5)]
            return [(r['start'], r['end']) for r in data['full_chunks']]
        with patch.dict(os.environ, {'PREPARE_MODE': 'private_latest' if independent else 'baseline'}), \
             patch.object(pilot, 'command', side_effect=execute), patch.object(benchmark, 'fetch', side_effect=fetch), \
             patch('src.ai.qwen_transcriber.QwenTranscriber.prepare_pcm_stream', autospec=True, side_effect=vad), \
             patch('src.ai.course_glossary.course_terms', return_value=data['recognition_terms']):
            pilot.prepare()
        prepared = pilot.root()/'out'
        manifest = json.loads(pilot.unseal(prepared/'plan.enc', 'plan')['manifest.json'])
        return data, manifest, prepared, samples

    def worker_fixture(self, prepared, manifest, data, shard, *, fail=False, previous=None, stage=None):
        location = self.stage(stage or f'worker-{shard}')
        (location/'inbox').mkdir()
        shutil.copyfile(prepared/f'shard-{shard}.enc', location/'inbox'/f'shard-{shard}.enc')
        if previous:
            (location/'previous').mkdir()
            shutil.copyfile(previous, location/'previous'/'worker-result.enc')
        os.environ['SHARD_ID'] = str(shard)
        ids = manifest['shards'][shard]['chunk_ids']
        if previous:
            old = json.loads(pilot.unseal(previous, f'result-{shard}')['result.json'])
            done = {r['chunk_id'] for r in old['chunks']}
            ids = [i for i in ids if i not in done]
        values = [{'text': data['full_chunks'][n]['text'], 'quality_state': 'recognized'} for n in ids]
        if fail:
            values[-1] = RuntimeError('fake decoder failure')
        with patch('src.ai.qwen_transcriber.QwenTranscriber._init'), \
             patch('src.ai.qwen_transcriber.QwenTranscriber._recognize', side_effect=values) as decode:
            if fail:
                with self.assertRaises(RuntimeError):
                    pilot.worker()
            else:
                pilot.worker()
        return location/'out'/'worker-result.enc', decode.call_count

    def gather_fixture(self, prepared, results, stage='gather'):
        location = self.stage(stage)
        (location/'inbox').mkdir()
        shutil.copyfile(prepared/'input.enc', location/'inbox'/'input.enc')
        for i, source in enumerate(results):
            folder = location/'results'/f'qwen-shard-result-0-{i}'
            folder.mkdir(parents=True)
            shutil.copyfile(source, folder/'worker-result.enc')
        return location

    def test_lossless_pack_workers_partial_retry_and_strict_gather(self):
        import numpy as np
        import soundfile as sf
        data, manifest, prepared, original_samples = self.prepare_fixture()
        payload = pilot.unseal(prepared/'shard-0.enc', 'input-0')
        import io
        for i in manifest['shards'][0]['chunk_ids']:
            samples, rate = sf.read(io.BytesIO(payload[f'chunk-{i}.flac']), dtype='int16')
            self.assertEqual(rate, 16000)
            block = manifest['blocks'][i]
            np.testing.assert_array_equal(samples, original_samples[round(block['start']*16000):round(block['end']*16000)])
        failed, calls = self.worker_fixture(prepared, manifest, data, 0, fail=True)
        self.assertEqual(calls, 2)
        partial = json.loads(pilot.unseal(failed, 'result-0')['result.json'])
        self.assertFalse(partial['complete'])
        self.assertEqual(len(partial['chunks']), 1)
        retried, calls = self.worker_fixture(prepared, manifest, data, 0, previous=failed, stage='retry')
        self.assertEqual(calls, 1)
        other, _ = self.worker_fixture(prepared, manifest, data, 1)
        location = self.gather_fixture(prepared, [retried, other])
        def review():
            report = pilot.legacy_report(benchmark.workspace()/'result.enc')
            self.assertTrue((location/'out'/'result.enc').exists())
            report.update(quality_limits={'cloud_seconds': 600, 'max_clips': 12, 'max_suspects': 12},
                          cloud_review={'attempted_audio_seconds': 0, 'completed_clips': 0}, review_suspects=[])
            benchmark.save_encrypted(report)
        def summary():
            report = pilot.legacy_report(benchmark.workspace()/'result.enc')
            self.assertTrue(report['shard_review_complete'])
            report['test_summary'] = {'markdown': '测试摘要', 'email_sent': False, 'production_written': False}
            benchmark.save_encrypted(report)
        with patch.object(benchmark, 'quality_review', side_effect=review) as review_call, \
             patch.object(benchmark, 'generate_summary', side_effect=summary) as summary_call, \
             patch.object(pilot, 'timing_snapshot', return_value={'test': True}):
            pilot.gather()
        review_call.assert_called_once()
        summary_call.assert_called_once()
        result = pilot.legacy_report(location/'out'/'result.enc')
        self.assertEqual(result['segments'][1]['text'], '接着讲矩阵')
        self.assertEqual(result['review_diagnostics']['remaining_seconds'], 600)
        self.assertEqual(len(result['sharding']['worker_attempts'][0]), 2)
        self.assertFalse(result['test_summary']['email_sent'])

    def test_incomplete_shard_prevents_all_cloud_and_summary_calls(self):
        data, manifest, prepared, _ = self.prepare_fixture()
        failed, _ = self.worker_fixture(prepared, manifest, data, 0, fail=True)
        other, _ = self.worker_fixture(prepared, manifest, data, 1)
        self.gather_fixture(prepared, [failed, other])
        with patch.object(benchmark, 'quality_review') as review, patch.object(benchmark, 'generate_summary') as summary:
            with self.assertRaises(ValueError):
                pilot.gather()
        review.assert_not_called()
        summary.assert_not_called()

    def test_independent_asr_starts_before_baseline_then_attaches_it_before_review(self):
        with patch.dict(os.environ, {'ALLOW_PARTIAL_COMPARISON': 'true'}):
            data, manifest, prepared, _ = self.prepare_fixture(independent=True)
        data['audio_seconds'] = 6  # Same lecture, different acquired duration is explicitly allowed.
        self.assertEqual(manifest['preparation_mode'], 'independent_vad')
        self.assertEqual(json.loads(pilot.unseal(prepared/'input.enc', 'gather-input')['baseline.json'])['source'],
                         'pending_baseline')
        results = [self.worker_fixture(prepared, manifest, data, i)[0] for i in range(2)]
        location = self.gather_fixture(prepared, results)
        with patch.object(pilot, 'command', side_effect=subprocess.CalledProcessError(1, 'gh')), \
             patch.object(benchmark, 'quality_review') as review:
            with self.assertRaises(subprocess.CalledProcessError):
                pilot.gather()
        review.assert_not_called()
        self.assertFalse((location/'out'/'result.enc').exists())
        original_command = pilot.command
        def execute(args, **kwargs):
            if args[0] == 'gh':
                destination = pilot.root()/'baseline'
                destination.mkdir(exist_ok=True)
                benchmark.save_encrypted(data)
                shutil.copyfile(benchmark.workspace()/'result.enc', destination/'result.enc')
            else:
                original_command(args, **kwargs)
        def review():
            report = pilot.legacy_report(benchmark.workspace()/'result.enc')
            self.assertEqual(report['baseline_snapshot'], data)
            self.assertEqual(report['reference_evidence'], data['reference_evidence'])
            self.assertTrue(report['comparison_is_partial'])
            self.assertFalse(report['full_lecture_complete'])
            report.update(quality_limits={'cloud_seconds': 600, 'max_clips': 12, 'max_suspects': 12},
                          cloud_review={'attempted_audio_seconds': 0, 'completed_clips': 0})
            benchmark.save_encrypted(report)
        def summarize():
            report = pilot.legacy_report(benchmark.workspace()/'result.enc')
            report['test_summary'] = {'markdown': '试跑摘要', 'email_sent': False}
            benchmark.save_encrypted(report)
        with patch.object(pilot, 'command', side_effect=execute), \
             patch.object(benchmark, 'quality_review', side_effect=review) as review_call, \
             patch.object(benchmark, 'generate_summary', side_effect=summarize), \
             patch.object(pilot, 'timing_snapshot', return_value={}):
            pilot.gather()
        review_call.assert_called_once()
        self.assertTrue(pilot.legacy_report(location/'out'/'result.enc')['shard_review_complete'])

    def test_quota_experiment_reuses_verified_asr_without_fetch_or_model(self):
        self.exercise_asr_reuse()

    def test_partial_finalization_reuses_completed_asr_without_prior_final_or_model(self):
        self.exercise_asr_reuse(asr_only=True)

    def exercise_asr_reuse(self, *, asr_only=False):
        source_slot = 1 if asr_only else 0
        os.environ['COURSE_SLOT'] = str(source_slot)
        data, manifest, prepared, _ = self.prepare_fixture(independent=True)
        results = [self.worker_fixture(prepared, manifest, data, i)[0] for i in range(2)]
        raw = [json.loads(pilot.unseal(path, f'result-{i}')['result.json']) for i, path in enumerate(results)]
        report = assemble(manifest, raw, data)
        report.update(shard_review_complete=True, test_summary={'markdown': '原试验完整摘要'}, baseline_snapshot=data)
        benchmark.save_encrypted(report)
        source_final = self.base/'source-final.enc'
        shutil.copyfile(benchmark.workspace()/'result.enc', source_final)
        artifacts = {f'qwen-shard-input-{source_slot}': prepared/'input.enc', f'qwen-shard-final-{source_slot}': source_final}
        for i, path in enumerate(results):
            artifacts[f'qwen-shard-audio-{source_slot}-{i}'] = prepared/f'shard-{i}.enc'
            artifacts[f'qwen-shard-result-{source_slot}-{i}'] = path
        def download(args, **kwargs):
            if asr_only:
                self.assertNotEqual(args[args.index('--name')+1], f'qwen-shard-final-{source_slot}')
            source = artifacts[args[args.index('--name')+1]]
            target = Path(args[args.index('--dir')+1])
            target.mkdir(parents=True, exist_ok=True)
            filename = source.name if source != source_final else 'result.enc'
            shutil.copyfile(source, target/filename)
        self.stage('reuse-prepare')
        os.environ.update(GITHUB_RUN_ID='101', REUSE_SHARDED_RUN_ID='100', QWEN_REVIEW_PROFILE='pilot15',
                          COURSE_SLOT='0', REUSE_SOURCE_SLOT=str(source_slot),
                          ALLOW_PARTIAL_COMPARISON='true' if asr_only else 'false')
        listing = {'total_count': 0, 'artifacts': []}
        with patch.object(pilot.subprocess, 'check_output', return_value=json.dumps(listing).encode()), \
             patch.object(pilot, 'command', side_effect=download), patch.object(benchmark, 'fetch') as fetch:
            pilot.prepare()
        fetch.assert_not_called()
        reused = pilot.root()/'out'
        new_plan = json.loads(pilot.unseal(reused/'plan.enc', 'plan')['manifest.json'])
        self.assertEqual(new_plan['asr_reuse_run_id'], '100')
        self.assertEqual(new_plan['course_slot'], 0)
        self.assertEqual(new_plan['asr_reuse_source_slot'], source_slot)
        if asr_only:
            self.assertTrue(new_plan['allow_partial_comparison'])
        new_results = []
        for i in range(2):
            location = self.stage(f'reused-worker-{i}')
            (location/'inbox').mkdir()
            shutil.copyfile(reused/f'shard-{i}.enc', location/'inbox'/f'shard-{i}.enc')
            os.environ['SHARD_ID'] = str(i)
            with patch('src.ai.qwen_transcriber.QwenTranscriber._init') as init, \
                 patch('src.ai.qwen_transcriber.QwenTranscriber._recognize') as decode:
                pilot.worker()
            init.assert_not_called()
            decode.assert_not_called()
            result_path = location/'out'/'worker-result.enc'
            result = json.loads(pilot.unseal(result_path, f'result-{i}')['result.json'])
            self.assertEqual(result['chunks'], raw[i]['chunks'])
            new_results.append(result_path)
        self.assertEqual(assemble(new_plan, [json.loads(pilot.unseal(p, f'result-{i}')['result.json'])
                         for i, p in enumerate(new_results)], data)['full_chunks'], report['full_chunks'])
        self.stage('reuse-invalid')
        os.environ['SHARD_MODE'] = 'auto'
        with patch.object(pilot.subprocess, 'check_output', return_value=json.dumps(listing).encode()), \
             patch.object(pilot, 'command', side_effect=download), self.assertRaises(ValueError):
            pilot.prepare_reuse()
        if asr_only:
            os.environ['SHARD_MODE'] = '2'
            listing['artifacts'] = [{'name': f'qwen-shard-final-{source_slot}'}]
            listing['total_count'] = 1
            with patch.object(pilot.subprocess, 'check_output', return_value=json.dumps(listing).encode()), \
                 patch.object(pilot, 'command', side_effect=download), self.assertRaises(ValueError):
                pilot.prepare_reuse()

    def test_interrupted_review_persists_quota_marker_and_does_not_repeat(self):
        data, manifest, prepared, _ = self.prepare_fixture()
        results = [self.worker_fixture(prepared, manifest, data, i)[0] for i in range(2)]
        location = self.gather_fixture(prepared, results)
        with patch.object(benchmark, 'quality_review', side_effect=TimeoutError), \
             patch.object(benchmark, 'generate_summary') as summary:
            with self.assertRaises(TimeoutError):
                pilot.gather()
        summary.assert_not_called()
        prior = location/'out'/'result.enc'
        self.assertTrue(pilot.legacy_report(prior)['shard_review_started'])
        retry = self.gather_fixture(prepared, results, stage='gather-retry')
        (retry/'previous').mkdir()
        shutil.copyfile(prior, retry/'previous'/'result.enc')
        with patch.object(benchmark, 'quality_review') as review, patch.object(benchmark, 'generate_summary') as summary:
            with self.assertRaises(RuntimeError):
                pilot.gather()
        review.assert_not_called()
        summary.assert_not_called()

    def test_authenticated_bundle_rejects_cross_course_role_run_and_tampering(self):
        from cryptography.exceptions import InvalidTag
        path = pilot.root()/'test.enc'
        pilot.seal({'manifest.json': b'private content'}, 'plan', path)
        self.assertNotIn(b'private content', path.read_bytes())
        with self.assertRaises(InvalidTag):
            pilot.unseal(path, 'input-0')
        with self.assertRaises(InvalidTag):
            pilot.unseal(path, 'plan', 1)
        with pilot.environment({'GITHUB_RUN_ID': '101'}), self.assertRaises(InvalidTag):
            pilot.unseal(path, 'plan')
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        with self.assertRaises(InvalidTag):
            pilot.unseal(path, 'plan')
        with self.assertRaises(ValueError):
            pilot.seal({'../audio': b'x'}, 'plan', path)

    def test_five_long_courses_share_fifteen_workers_and_duplicate_lectures_are_rejected(self):
        refs = [{'run_id': str(11+i), 'artifact': 'qwen-asr-encrypted-result-0'} for i in range(5)]
        os.environ['BASELINES'] = json.dumps(refs)
        os.environ['GITHUB_OUTPUT'] = str(self.base/'outputs')
        rows = [{'start': i, 'end': i+120, 'text': '原始块'} for i in range(0, 4800, 120)]
        manifests = []
        for i, ref in enumerate(refs):
            data = baseline(4800, rows)
            data['selection']['sub_id'] = str(123+i)
            manifest = build_plan(data, reference=ref, course_slot=i, run_id='100', audio_sha256='a'*64, mode='auto')
            manifests.append(manifest)
            pilot.seal({'manifest.json': pilot.encoded(manifest)}, 'plan',
                       pilot.root()/'plans'/f'qwen-shard-plan-{i}'/'plan.enc', i)
        pilot.workers()
        output = dict(line.split('=', 1) for line in Path(os.environ['GITHUB_OUTPUT']).read_text().splitlines())
        matrix = json.loads(output['workers'])['include']
        course_matrix = json.loads(output['courses'])['include']
        self.assertEqual(len(matrix), 15)
        self.assertEqual({(r['course_slot'], r['shard_id']) for r in matrix},
                         {(i, j) for i in range(5) for j in range(3)})
        self.assertEqual([c['course_slot'] for c in course_matrix], list(range(5)))
        self.assertEqual([w for c in course_matrix for w in c['workers']['include']], matrix)
        for c in course_matrix:
            self.assertTrue(all(w['course_slot'] == c['course_slot'] for w in c['workers']['include']))
        for field, value in [('course_slot', 4), ('run_id', '999'),
                             ('reference', {'run_id': '999', 'artifact': refs[0]['artifact']})]:
            wrong = deepcopy(manifests[0])
            wrong[field] = value
            pilot.seal({'manifest.json': pilot.encoded(wrong)}, 'plan',
                       pilot.root()/'plans'/'qwen-shard-plan-0'/'plan.enc', 0)
            with self.assertRaises(ValueError):
                pilot.workers()
        pilot.seal({'manifest.json': pilot.encoded(manifests[0])}, 'plan',
                   pilot.root()/'plans'/'qwen-shard-plan-0'/'plan.enc', 0)
        manifests[1]['selection'] = manifests[0]['selection']
        pilot.seal({'manifest.json': pilot.encoded(manifests[1])}, 'plan',
                   pilot.root()/'plans'/'qwen-shard-plan-1'/'plan.enc', 1)
        with self.assertRaises(ValueError):
            pilot.workers()

    def test_restore_fails_closed_on_api_error(self):
        os.environ['RESTORE_ARTIFACT'] = 'qwen-shard-final-0'
        with patch.object(pilot.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(1, 'gh')), \
             patch.object(pilot, 'command') as download:
            with self.assertRaises(subprocess.CalledProcessError):
                pilot.restore()
        download.assert_not_called()

    def test_existing_artifact_without_checkpoint_cannot_reset_quota(self):
        os.environ['RESTORE_ARTIFACT'] = 'qwen-shard-final-0'
        listing = {'total_count': 1, 'artifacts': [{'name': 'qwen-shard-final-0', 'expired': False}]}
        with patch.object(pilot.subprocess, 'check_output', return_value=json.dumps(listing).encode()), \
             patch.object(pilot, 'command'), self.assertRaises(ValueError):
            pilot.restore()


if __name__ == '__main__':
    unittest.main()
