"""Concurrent claims, real local Git CAS, failed-worker recovery and graphs."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
import yaml

from scripts.qwen_sharding import build_audio_plan, fingerprint
from scripts.asr_queue_store import GitHubQueueStore
from src.pipeline.asr_queue import SharedQueue, initial_queue, validate_queue
from src.pipeline.prepared_lecture import assemble_material

ROOT = Path(__file__).resolve().parents[1]


def plan_for(minutes=40):
    return build_audio_plan({'selection': {'course_id': '10', 'sub_id': '1'},
        'audio_seconds': minutes*60, 'full_chunks': [{'start': i*60, 'end': (i+1)*60} for i in range(minutes)],
        'recognition_terms': ['矩阵'], 'vad_windows': [[0, minutes*60]]},
        reference={'pipeline': 'production'}, course_slot=0, run_id='99', audio_sha256='a'*64,
        mode='shared', production=True)


def decoded(block):
    return {'chunk_id': block['chunk_id'], 'start': block['start'], 'end': block['end'],
            'text': '矩阵课堂内容'+str(block['chunk_id']), 'decode_seconds': 1}


class MemoryStore:
    def __init__(self, state):
        self.lock = threading.Lock(); self.version = 0; self.state = state
    def read(self):
        with self.lock:
            return self.version, copy.deepcopy(self.state)
    def compare_and_swap(self, previous, state):
        with self.lock:
            if previous != self.version:
                return False
            self.state = copy.deepcopy(state); self.version += 1
            return True
    def close(self):
        pass


class SharedQueueTests(unittest.TestCase):
    def test_lost_swaps_back_off_before_retrying_and_eventually_give_up(self):
        # 2026-11-08: four Runners exhausted 16 immediate retries and one died with its claim.
        from src.pipeline import asr_queue
        plan = plan_for(1)
        class Contended(MemoryStore):
            def __init__(self, state, losses):
                super().__init__(state); self.losses = losses
            def compare_and_swap(self, previous, state):
                if self.losses:
                    self.losses -= 1; return False
                return super().compare_and_swap(previous, state)
        pauses = []
        queue = SharedQueue(plan, Contended(initial_queue(plan), 9), sleep=pauses.append, jitter=lambda a, b: b)
        self.assertIsNotNone(queue.claim('one', 1))
        self.assertEqual(pauses, [1.0, 2.0, 4.0, 8.0, 16.0, 20.0, 20.0, 20.0, 20.0])
        pauses.clear()
        queue = SharedQueue(plan, Contended(initial_queue(plan), 10**6), sleep=pauses.append, jitter=lambda a, b: a)
        with self.assertRaisesRegex(RuntimeError, 'retry budget'):
            queue.claim('one', 1)
        self.assertEqual(len(pauses), asr_queue.CONFLICT_ATTEMPTS-1)
        self.assertLess(sum(pauses), 600)

    def test_capacity_depends_on_pending_audio_and_global_reservation(self):
        for minutes, count in [(0, 0), (10, 1), (40, 2), (60, 2), (80, 3), (180, 3)]:
            plan = plan_for(minutes) if minutes else build_audio_plan({
                'selection': {'course_id': '10', 'sub_id': '1'}, 'audio_seconds': 3600,
                'full_chunks': [], 'recognition_terms': [], 'vad_windows': []},
                reference={}, course_slot=0, run_id='99', audio_sha256='a'*64, mode='shared', production=True)
            self.assertEqual(len(plan['shards']), count)
            self.assertLessEqual(5*count, 15)

    def test_concurrent_fast_worker_claims_more_blocks_without_duplicates(self):
        plan = plan_for(); store = MemoryStore(initial_queue(plan)); seen = []; lock = threading.Lock()
        def worker(n):
            # Conflict pauses scaled down so decode time, not backoff, decides who claims more.
            queue = SharedQueue(plan, store, sleep=lambda s: time.sleep(s/1000))
            while True:
                claim = queue.claim(f'worker{n}', 1)
                if claim is None: return
                block, token = claim
                time.sleep(.02 if n == 0 else .001)
                queue.finish(block['chunk_id'], token, decoded(block))
                with lock: seen.append((n, block['chunk_id']))
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(worker, [0, 1]))
        self.assertEqual(sorted(i for _, i in seen), list(range(40)))
        self.assertGreater(sum(n == 1 for n, _ in seen), sum(n == 0 for n, _ in seen))
        material = assemble_material(plan, SharedQueue(plan, store).results())
        self.assertEqual([r['chunk_id'] for r in material['full_chunks']], list(range(40)))
        self.assertEqual(material['recognition_terms'], ['矩阵'])

    def test_slow_claim_not_stolen_but_new_attempt_fences_old_result(self):
        plan = plan_for(1); queue = SharedQueue(plan, MemoryStore(initial_queue(plan)))
        old, token = queue.claim('slow', 1)
        self.assertIsNone(queue.claim('fast', 1))
        new, new_token = queue.claim('retry', 2)
        self.assertEqual(old, new)
        with self.assertRaises(ValueError): queue.finish(old['chunk_id'], token, decoded(old))
        queue.finish(new['chunk_id'], new_token, decoded(new))
        self.assertIsNone(queue.claim('retry', 3))
        queue.finish(new['chunk_id'], new_token, decoded(new))  # idempotent saved result
        with self.assertRaises(ValueError): queue.finish(new['chunk_id'], new_token, dict(decoded(new), text='changed'))

    def test_partial_assembly_forbidden_and_release_does_not_drop_success(self):
        plan = plan_for(2); queue = SharedQueue(plan, MemoryStore(initial_queue(plan)))
        block, token = queue.claim('one', 1)
        queue.finish(block['chunk_id'], token, decoded(block)); queue.release(block['chunk_id'], token)
        with self.assertRaises(ValueError): queue.results()
        partial = queue.results(require_complete=False)
        self.assertEqual(len(partial[0]['chunks']), 1)
        other, token = queue.claim('two', 1); queue.release(other['chunk_id'], token)
        self.assertEqual(queue.snapshot()['blocks'][str(other['chunk_id'])]['status'], 'pending')

    def test_plan_timestamps_and_completed_row_cannot_change(self):
        plan = plan_for(1); state = initial_queue(plan)
        for bad in [dict(state, plan_hash='other'), dict(state, blocks={}), dict(state, revision=-1)]:
            with self.assertRaises(ValueError): validate_queue(plan, bad)
        queue = SharedQueue(plan, MemoryStore(state)); block, token = queue.claim('one', 1)
        with self.assertRaises(ValueError): queue.finish(0, token, dict(decoded(block), start=2))
        self.assertEqual(queue.snapshot()['blocks']['0']['status'], 'claimed')

    def test_real_git_compare_and_swap_and_encryption_identity(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GITHUB_REPOSITORY': 'test/repo'}):
            remote = str(Path(tmp)/'remote.git')
            subprocess.run(['git', 'init', '-q', '--bare', remote], check=True)
            a = GitHubQueueStore('99', 0, b'k'*32, remote_url=remote)
            b = GitHubQueueStore('99', 0, b'k'*32, remote_url=remote)
            other = GitHubQueueStore('100', 0, b'k'*32, remote_url=remote)
            try:
                plan = plan_for(2); state = initial_queue(plan)
                self.assertTrue(a.compare_and_swap(None, state))
                va, sa = a.read(); vb, sb = b.read(); self.assertEqual(va, vb)
                sa['revision'] += 1; sa['blocks']['0'] = {'status': 'complete', 'result': decoded(plan['blocks'][0])}
                self.assertTrue(a.compare_and_swap(va, sa))
                sb['revision'] += 1
                self.assertFalse(b.compare_and_swap(vb, sb))  # genuine non-fast-forward race
                self.assertEqual(b.read()[1], sa)
                raw = a.command(['git', 'show', a.head()+':queue.enc'])
                self.assertNotIn('矩阵'.encode(), raw)
                with self.assertRaises(Exception): other.unseal(raw)
                self.assertEqual(a.command(['git', 'ls-tree', '-r', '--name-only', a.head()]).strip(), b'queue.enc')
            finally:
                a.close(); b.close(); other.close()

    def test_missing_queue_or_transport_failure_never_means_start_fresh(self):
        plan = plan_for(1); store = MagicMock(); store.read.return_value = (None, None)
        with self.assertRaises(ValueError): SharedQueue(plan, store).claim('one', 1)
        store.compare_and_swap.assert_not_called()
        from scripts.shared_asr_worker import initialize
        with patch('scripts.shared_asr_worker.store_for', return_value=store), patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
            with self.assertRaises(ValueError): initialize(plan, {})
        store.compare_and_swap.assert_not_called()

    def test_worker_decodes_only_claimed_remaining_block_and_saves_before_next_claim(self):
        import numpy as np
        import soundfile as sf
        from scripts.shared_asr_worker import run_worker
        plan = plan_for(2); buf = io.BytesIO()
        sf.write(buf, np.zeros(960000, dtype=np.float32), 16000, format='FLAC')
        blob = buf.getvalue(); files = {}
        for block in plan['blocks']:
            block['flac_sha256'] = hashlib.sha256(blob).hexdigest(); files[f'chunk-{block["chunk_id"]}.flac'] = blob
        store = MemoryStore(initial_queue(plan)); queue = SharedQueue(plan, store)
        done, token = queue.claim('old', 1); queue.finish(done['chunk_id'], token, decoded(done))
        model = MagicMock()
        def recognize(blocks, load, *, checkpoint, **kwargs):
            self.assertEqual(len(blocks), 1); self.assertEqual(len(load(blocks[0])), 960000)
            checkpoint([decoded(blocks[0])])
        model.recognize_blocks.side_effect = recognize
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.shared_asr_worker.shards.seal') as seal, patch('scripts.shared_asr_worker.shards.root', return_value=Path(tmp)):
            report = run_worker(plan, files, store, 0, 2, transcriber=model)
            model.recognize_blocks.assert_called_once(); self.assertEqual(seal.call_count, 3)
            self.assertEqual(report['decoded_chunk_ids'], [1])
            again = run_worker(plan, files, store, 0, 3, transcriber=model)
            self.assertEqual(again['decoded_chunk_ids'], [])
            model.recognize_blocks.assert_called_once()

    def test_decoded_local_result_survives_failed_remote_save_and_is_not_redecoded(self):
        import numpy as np
        import soundfile as sf
        from scripts.shared_asr_worker import run_worker
        plan = plan_for(1); buf = io.BytesIO()
        sf.write(buf, np.zeros(960000, dtype=np.float32), 16000, format='FLAC')
        blob = buf.getvalue(); plan['blocks'][0]['flac_sha256'] = hashlib.sha256(blob).hexdigest()
        store = MemoryStore(initial_queue(plan)); saved = []
        def seal(files, role, path):
            if role.startswith('shared-local'):
                saved.append(json.loads(files['local.json']))
        model = MagicMock()
        model.recognize_blocks.side_effect = lambda blocks, load, checkpoint, **kw: checkpoint([decoded(blocks[0])])
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.shared_asr_worker.shards.seal', side_effect=seal), patch('scripts.shared_asr_worker.shards.root', return_value=Path(tmp)):
            with patch.object(SharedQueue, 'finish', side_effect=ConnectionError('transport failed')):
                with self.assertRaises(ConnectionError):
                    run_worker(plan, {'chunk-0.flac': blob}, store, 0, 1, transcriber=model)
            self.assertEqual(saved[-1]['chunks'], [decoded(plan['blocks'][0])])
            # Retain its claim until retry: peers must not re-decode the saved output.
            self.assertIsNone(SharedQueue(plan, store).claim('peer', 1))
            result = run_worker(plan, {'chunk-0.flac': blob}, store, 0, 2,
                                transcriber=model, previous_rows=saved[-1]['chunks'])
            self.assertEqual(result['decoded_chunk_ids'], [])
            model.recognize_blocks.assert_called_once()
            self.assertTrue(SharedQueue(plan, store).results()[0]['complete'])

    def test_recovery_restores_all_workers_before_any_new_claim(self):
        from scripts import production_qwen as pipeline
        plan = plan_for(); restored = []
        checkpoints = {0: [decoded(plan['blocks'][0])], 1: [decoded(plan['blocks'][1])]}
        files = {'specification.json': json.dumps({'mode': 'sharded', 'plan': plan}).encode()}
        store = MagicMock()
        with patch.dict(os.environ, {'SHARD_ID': '1', 'GITHUB_RUN_ATTEMPT': '2'}), \
             patch.object(pipeline, 'read_preparation', return_value=files), \
             patch.object(pipeline, 'shared_local_checkpoints', return_value=checkpoints), \
             patch('scripts.shared_asr_worker.store_for', return_value=store), \
             patch('src.pipeline.asr_queue.SharedQueue.restore_rows', side_effect=lambda rows, attempt: restored.extend(rows)), \
             patch('scripts.shared_asr_worker.run_worker', side_effect=lambda *a, **kw: self.assertEqual(restored, checkpoints[0]+checkpoints[1]) or {}) as worker, \
             patch.object(pipeline, 'out'):
            pipeline.worker()
            self.assertEqual(worker.call_args.kwargs['previous_rows'], checkpoints[1])
            store.close.assert_called_once()

    def test_qwen_keeps_loaded_model_between_shared_blocks_but_default_releases(self):
        import numpy as np
        from src.ai.qwen_transcriber import QwenTranscriber
        qwen = QwenTranscriber.__new__(QwenTranscriber)
        qwen.last_vad_windows = []; qwen._init = MagicMock()
        qwen._recognize = MagicMock(side_effect=lambda samples, **kwargs: {'text': '矩阵'})
        qwen.release_model = MagicMock()
        block = {'chunk_id': 0, 'start': 0, 'end': 1}
        for _ in range(2):
            qwen.recognize_blocks([block], lambda b: np.zeros(16000), keep_model=True)
        qwen.release_model.assert_not_called()
        qwen.recognize_blocks([block], lambda b: np.zeros(16000))
        qwen.release_model.assert_called_once()

    def test_local_restore_cannot_replace_current_claim_or_change_timestamps(self):
        plan = plan_for(1); queue = SharedQueue(plan, MemoryStore(initial_queue(plan)))
        block, token = queue.claim('old', 1)
        with self.assertRaises(ValueError): queue.restore_rows([decoded(block)], 1)
        with self.assertRaises(ValueError): queue.restore_rows([dict(decoded(block), end=2)], 2)
        queue.restore_rows([decoded(block)], 2)
        self.assertTrue(queue.results()[0]['complete'])
        with self.assertRaises(ValueError): queue.restore_rows([dict(decoded(block), text='different')], 3)

    def test_completed_dynamic_results_pass_lecture_runner_sqlite_save_without_asr(self):
        from test_lecture_quality_gate import _load_runner_class
        from src.data.database import Database
        plan = plan_for(2); queue = SharedQueue(plan, MemoryStore(initial_queue(plan)))
        while (claim := queue.claim('worker', 1)) is not None:
            block, token = claim; row = decoded(block); row['text'] *= 100
            queue.finish(block['chunk_id'], token, row)
        material = assemble_material(plan, queue.results(), media_seconds=120)
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp)/'course.db'))
            db.upsert_course('10', '高等代数', '教师'); db.insert_lecture('1', '10', '课次', '2026-10-04')
            model = MagicMock(); model.summarize.return_value = ('完整摘要', 'isolated')
            transcriber = MagicMock(); scheduler = MagicMock()
            runner = _load_runner_class()(None, db, scheduler, transcriber, model, MagicMock())
            with patch('src.runtime.config.DOUBAO_ASR_API_KEY', ''), patch.dict(os.environ, {'AUTO_COURSE_TERMS': 'false'}):
                runner.run('10', '高等代数', {'sub_id': '1'}, prepared_asr=material, review_state={}, checkpoint=lambda: None)
            row = db.get_lecture('1')
            self.assertEqual(row['transcript'], material['transcript']); self.assertEqual(row['summary'], '完整摘要')
            self.assertIsNotNone(row['processed_at']); self.assertIsNone(row['emailed_at'])
            transcriber.transcribe_tail.assert_not_called(); scheduler.prefetch_lecture.assert_not_called()
            db.conn.close()

    def test_cross_run_resume_rebinds_local_and_committed_rows_only_after_source_ends(self):
        from scripts import production_qwen as pipeline
        from src.data.database import Database
        plan = plan_for(2); store = MemoryStore(initial_queue(plan))
        block, _ = SharedQueue(plan, store).claim('old', 1)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'RUNNER_TEMP': tmp, 'GITHUB_RUN_ID': '100', 'COURSE_SLOT': '1',
            'GITHUB_REPOSITORY': 'test/repo', 'GITHUB_ACTIONS': 'false'}):
            db = Database(str(Path(tmp)/'course.db'))
            db.upsert_course('10', '高代', '教师'); db.insert_lecture('1', '10', '课次', '2026-10-04')
            db.write_meta('qwen_pipeline:1', json.dumps({'recovery': {'run_id': '99', 'task_slot': 0, 'plan_hash': fingerprint(plan)}}))
            spec = {'mode': 'sharded', 'plan': plan}
            def present(name, *args, **kwargs): return '-prepare-' in name
            with patch.object(pipeline, 'artifact', side_effect=present), \
                 patch.object(pipeline, 'decode', return_value={'specification.json': json.dumps(spec).encode()}), \
                 patch.object(pipeline, 'shared_local_checkpoints', return_value={0: [decoded(block)]}), \
                 patch('scripts.shared_asr_worker.store_for', return_value=store), \
                 patch.object(pipeline.subprocess, 'check_output', return_value=json.dumps({'status': 'in_progress', 'run_attempt': 1}).encode()) as info:
                with self.assertRaises(ValueError): pipeline.recover_preparation(db, '10', '1')
                self.assertEqual(store.state['blocks']['0']['status'], 'claimed')
                info.return_value = json.dumps({'status': 'completed', 'run_attempt': 1}).encode()
                files = pipeline.recover_preparation(db, '10', '1')
            updated = json.loads(files['specification.json'])['plan']
            result = json.loads(files['completed-0.json'])
            self.assertEqual(updated['run_id'], '100'); self.assertEqual(updated['course_slot'], 1)
            self.assertEqual(updated['blocks'], plan['blocks']); self.assertEqual(result['chunks'], [decoded(block)])
            self.assertEqual(result['plan_hash'], fingerprint(updated)); self.assertFalse(result['complete'])
            db.conn.close()

    def test_private_export_reads_complete_shared_queue_without_fixed_worker_artifacts(self):
        import base64
        from scripts import production_qwen as pipeline
        from scripts import production_result_export as exporter
        plan = plan_for(1); queue = SharedQueue(plan, MemoryStore(initial_queue(plan)))
        block, token = queue.claim('worker', 1); queue.finish(0, token, decoded(block))
        files = {'specification.json': json.dumps({'mode': 'sharded', 'plan': plan}).encode()}
        with patch.dict(os.environ, {'SOURCE_RUN_ID': '99', 'SOURCE_SLOT': '0', 'GITHUB_REPOSITORY': 'test/repo',
                'RECIPIENT_PUBLIC_KEY': base64.b64encode(b'p'*32).decode(), 'INCLUDE_TRANSCRIPT': 'true'}), \
             patch.object(exporter.subprocess, 'check_output', return_value=json.dumps({'status': 'completed',
                'conclusion': 'success', 'path': '.github/workflows/parallel_pilot.yml'}).encode()), \
             patch.object(pipeline, 'root', return_value=Path('/unused')), patch.object(pipeline, 'artifact') as download, \
             patch.object(pipeline, 'decode', return_value=files), patch.object(exporter, 'summary_payload', return_value={}), \
             patch.object(pipeline, 'shared_results', return_value=queue.results()), patch.object(pipeline, 'out') as output, \
             patch.object(exporter, 'encrypt', side_effect=lambda payload, *a: payload):
            exporter.export()
            download.assert_called_once()
            payload = json.loads(output.return_value.write_bytes.call_args.args[0])
            self.assertEqual(payload['raw_qwen']['chunks'], [decoded(block)])

    def test_real_actions_graph_keeps_independent_gather_and_schedule_unchanged(self):
        caller = yaml.safe_load((ROOT/'.github/workflows/parallel_pilot.yml').read_text())
        child = yaml.safe_load((ROOT/'.github/workflows/qwen_production_lecture.yml').read_text())
        scheduled = yaml.safe_load((ROOT/'.github/workflows/check.yml').read_text())
        self.assertIn('shared', caller['on']['workflow_dispatch']['inputs']['shard_mode']['options'])
        self.assertEqual(caller['on']['workflow_dispatch']['inputs']['shard_mode']['default'], 'shared')
        self.assertFalse(caller['on']['workflow_dispatch']['inputs']['automatic_terms']['default'])
        self.assertEqual(scheduled['jobs']['check']['with']['shard_mode'], 'shared')
        self.assertFalse(scheduled['jobs']['check']['with']['automatic_terms'])
        self.assertEqual(child['jobs']['gather']['needs'], ['prepare', 'asr'])
        self.assertEqual(caller['jobs']['lecture']['strategy']['max-parallel']*child['jobs']['asr']['strategy']['max-parallel'], 15)
        self.assertEqual(child['jobs']['asr']['permissions']['contents'], 'write')
        self.assertNotIn('SMTP_PASSWORD', child['jobs']['asr']['env'])


if __name__ == '__main__':
    unittest.main()
