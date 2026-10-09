"""Bounded orchestration, durable dispatch and real sample/encryption checks."""
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import yaml
from scripts import production_pool as pool
from scripts import production_pool_stage as stage
from scripts import production_qwen as pipeline
from scripts import sharded_qwen_pilot as shards
from scripts.qwen_sharding import build_audio_plan, validate_plan
from test_shared_asr_queue import MemoryStore, decoded

ROOT = Path(__file__).resolve().parents[1]
FLAGS = {'AUTO_COURSE_TERMS': 'false', 'PUBLISH_RESULTS': 'false', 'SEND_EMAIL': 'false'}


def journal(count=5, run='99'):
    return pool.initial_state(run, 'a'*40, count, dict(FLAGS))


def plan_for(blocks=100):
    return build_audio_plan({'selection': {'course_id': '10', 'sub_id': '1'},
        'audio_seconds': blocks*120 or 1, 'full_chunks': [{'start': n*120, 'end': (n+1)*120} for n in range(blocks)],
        'recognition_terms': ['矩阵'], 'vad_windows': []}, reference={}, course_slot=0,
        run_id='99', audio_sha256='b'*64, production=True, mode='shared', worker_cap=6)


class Simulation:
    """Discrete-time stage runners; model-free real controller/CAS transitions."""
    def __init__(self, store, fail=None):
        self.store = store; store.key = b'k'*32
        self.tick = 0; self.started = {}; self.events = []; self.peak = 0
        self.remaining = [80, 300, 300, 300, 300, 40, 40, 40][:store.state['task_count']]
        self.fail = fail

    def source_ref(self): return 'frozen'

    def dispatch(self, t, ref):
        saved = next(x for x in self.store.read()[1]['tickets'] if x['nonce'] == t['nonce'])
        assert saved['status'] == 'reserved'  # persisted before network call
        self.started[t['nonce']] = self.tick
        self.events.append((self.tick, t['slot'], t['stage'], t['worker']))
        active = [x for x in self.store.read()[1]['tickets'] if x['status'] != 'completed']
        self.peak = max(self.peak, len(active))
        assert len(active) <= 14
        assert len({t['slot'] for t in active}) <= 5
        assert all(sum(t['slot'] == n for t in active) <= 6 for n in range(8))

    def poll(self, state):
        for t in state['tickets']:
            if t['status'] == 'completed' or t['nonce'] not in self.started: continue
            t['run'] = str(1000+list(self.started).index(t['nonce']))
            t['status'] = 'in_progress'
            elapsed = self.tick-self.started[t['nonce']]
            if t['stage'] == 'asr':
                if self.fail == t['slot'] and t['worker'] == 0 and elapsed >= 2:
                    t.update(status='completed', conclusion='failure'); continue
                if elapsed:
                    self.remaining[t['slot']] = max(0, self.remaining[t['slot']]-8)
                done = not self.remaining[t['slot']]
            else: done = elapsed >= (1+t['slot']%2 if t['stage'] == 'prepare' else 1)
            if done: t.update(status='completed', conclusion='success')

    def work(self, slot, course, attempt):
        n = self.remaining[slot]
        return {'complete': n == 0, 'remaining_seconds': n*120, 'remaining_blocks': n,
                'pending_blocks': n, 'worker_cap': 6, 'rtf': 2, 'attempt': attempt}

    def sleep(self, seconds): self.tick += 1

    def run(self):
        return pool.controller(self.store, self, attempt=1, clock=lambda: self.tick*30,
            sleep=self.sleep, timeout=3600, get_work=self.work, acquire=lambda *a: None)


class SchedulerTests(unittest.TestCase):
    def test_compute_sizing_and_invalid_estimates(self):
        for minutes, workers in [(0, 0), (20, 1), (40, 2), (80, 3), (120, 4), (180, 5), (223, 6), (600, 6)]:
            self.assertEqual(pool.desired_workers(minutes*60, minutes), workers)
        self.assertEqual(pool.desired_workers(10000, 2), 2)
        self.assertEqual(pool.desired_workers(3600, 100, 4), 4)
        for values in [(math.nan, 1, 2), (1, 1, math.inf), (-1, 1, 2), (1, True, 2), (1, 1, .1)]:
            with self.assertRaises(ValueError): pool.desired_workers(*values)

    def test_observed_cost_needs_five_and_uses_median(self):
        rows = [{'start': 0, 'end': 10, 'decode_seconds': n} for n in [20, 21, 22, 23, 9999]]
        self.assertEqual(pool.estimate_rtf(rows[:4], 3.1), 3.1)
        self.assertEqual(pool.estimate_rtf(rows), 2.2)
        self.assertEqual(pool.estimate_rtf([dict(r, decode_seconds=10000) for r in rows]), 10)

    def test_real_controller_respects_all_limits_borrows_and_finalizes_independently(self):
        store = MemoryStore(journal(8)); simulation = Simulation(store)
        result = simulation.run()
        self.assertTrue(all(c['phase'] == 'done' for c in result['courses'].values()))
        self.assertEqual(simulation.peak, 14)
        first_gather = min(t for t, slot, phase, _ in simulation.events if phase == 'gather')
        last_asr = max(t for t, slot, phase, _ in simulation.events if phase == 'asr')
        self.assertLess(first_gather, last_asr)
        self.assertGreater(len({t for t, slot, phase, _ in simulation.events if slot == 3 and phase == 'asr'}), 1)
        self.assertTrue(all(t['status'] == 'completed' for t in result['tickets']))

    def test_one_course_failure_does_not_cancel_other_courses_or_finalize_failure(self):
        store = MemoryStore(journal(5)); simulation = Simulation(store, fail=2)
        result = simulation.run()
        self.assertEqual(result['courses']['2']['phase'], 'failed')
        self.assertTrue(all(c['phase'] == 'done' for k, c in result['courses'].items() if k != '2'))
        self.assertFalse(any(slot == 2 and phase in ('gather', 'publish') for _, slot, phase, _ in simulation.events))

    def test_unknown_dispatch_is_durable_and_not_repeated(self):
        store = MemoryStore(journal(1)); simulation = Simulation(store)
        simulation.dispatch = MagicMock(side_effect=ConnectionError('unknown outcome'))
        with self.assertRaises(ConnectionError): simulation.run()
        t = store.state['tickets'][0]
        self.assertEqual(t['status'], 'reserved'); self.assertIsNone(t['run'])
        with self.assertRaises(ValueError): pool.recover(store.state, 2)
        simulation.dispatch.assert_called_once()

    def test_accepted_third_dispatch_with_lost_reply_registers_all_and_finishes(self):
        store = MemoryStore(journal(1)); simulation = Simulation(store)
        dispatch = simulation.dispatch
        def accepted_then_error(ticket, ref):
            dispatch(ticket, ref)
            if len(simulation.events) == 3:
                raise pool.CoordinationError('github_dispatch', 'service_unavailable', 1)
        simulation.dispatch = accepted_then_error
        result = simulation.run()
        self.assertEqual(result['courses']['0']['phase'], 'done')
        self.assertEqual(len(simulation.events), len({tuple(e[1:]) for e in simulation.events}))
        self.assertTrue(all(t['run'] and t['status'] == 'completed' for t in result['tickets']))

    def test_compare_and_swap_conflict_stops_before_dispatch(self):
        store = MemoryStore(journal(1)); simulation = Simulation(store)
        store.compare_and_swap = MagicMock(return_value=False)
        with self.assertRaises(ValueError): simulation.run()
        self.assertEqual(simulation.events, [])

    def test_unknown_post_queries_are_bounded_and_never_replay_or_release(self):
        for code in pool.RETRYABLE:
            with self.subTest(code=code):
                store = MemoryStore(journal(1)); simulation = Simulation(store)
                error = pool.CoordinationError('github_dispatch', code, 1)
                simulation.dispatch = MagicMock(side_effect=error)
                with patch.object(pool, 'reconcile_dispatch', wraps=pool.reconcile_dispatch) as confirm:
                    with self.assertRaises(pool.CoordinationError) as caught:
                        simulation.run()
                self.assertIs(caught.exception, error)
                simulation.dispatch.assert_called_once(); confirm.assert_called_once()
                self.assertEqual(simulation.tick, 5)
                self.assertEqual(store.state['tickets'][0]['status'], 'reserved')
                self.assertIsNone(store.state['tickets'][0]['run'])
                with self.assertRaises(ValueError): pool.recover(store.state, 2)

    def test_reconcile_registers_earlier_children_while_current_run_is_delayed(self):
        state = journal(2)
        pool.reserve(state, 0, 'prepare', 1)
        later = pool.reserve(state, 1, 'prepare', 1)
        store = MemoryStore(state); polls = []
        def poll(current):
            polls.append(None)
            current['tickets'][0].update(run='101', status='queued')
            if len(polls) == 3: current['tickets'][1].update(run='102', status='queued')
        actions = MagicMock(); actions.poll.side_effect = poll
        sleep = MagicMock()
        self.assertTrue(pool.reconcile_dispatch(store, actions, later['nonce'], sleep=sleep))
        self.assertEqual(len(polls), 3); self.assertEqual(sleep.call_count, 2)
        self.assertEqual([t['run'] for t in store.state['tickets']], ['101', '102'])
        actions.dispatch.assert_not_called()

    def test_reconcile_temporary_reads_recover_but_identity_and_cas_fail_closed(self):
        state = journal(1); ticket = pool.reserve(state, 0, 'prepare', 1)
        def accepted(current): current['tickets'][0].update(run='101', status='queued')
        store = MemoryStore(state); actions = MagicMock(); sleep = MagicMock()
        def poll(current):
            if actions.poll.call_count == 1:
                raise pool.CoordinationError('github_read', 'service_unavailable', 3)
            accepted(current)
        actions.poll.side_effect = poll
        self.assertTrue(pool.reconcile_dispatch(store, actions, ticket['nonce'], sleep=sleep))
        sleep.assert_called_once_with(5)
        for error in (ValueError('Stage workflow source mismatch'),
                      ValueError('Duplicate stage dispatch; manual resolution required'),
                      pool.CoordinationError('github_read', 'authorization', 1),
                      pool.CoordinationError('github_read', 'tls', 1)):
            with self.subTest(error=str(error)):
                actions.poll.side_effect = error; sleep.reset_mock()
                with self.assertRaises(type(error)):
                    pool.reconcile_dispatch(MemoryStore(state), actions, ticket['nonce'], sleep=sleep)
                sleep.assert_not_called()
        store = MemoryStore(state); store.compare_and_swap = MagicMock(return_value=False)
        actions.poll.side_effect = accepted
        with self.assertRaisesRegex(ValueError, 'Another controller'):
            pool.reconcile_dispatch(store, actions, ticket['nonce'], sleep=sleep)

    def test_definite_dispatch_rejection_does_not_enter_unknown_reconciliation(self):
        store = MemoryStore(journal(1)); simulation = Simulation(store)
        simulation.dispatch = MagicMock(side_effect=pool.DispatchRejected(403))
        with patch.object(pool, 'reconcile_dispatch') as confirm:
            with self.assertRaises(pool.DispatchRejected): simulation.run()
        confirm.assert_not_called(); simulation.dispatch.assert_called_once()
        self.assertEqual(store.state['tickets'][0]['dispatch_rejected_http'], 403)
        self.assertEqual(store.state['tickets'][0]['status'], 'completed')

    def test_unused_capacity_is_not_a_wait_barrier(self):
        state = journal(1); t = pool.reserve(state, 0, 'prepare', 1)
        t.update(status='completed', conclusion='success')
        pool.refresh_phases(state, {}); self.assertEqual(state['courses']['0']['phase'], 'asr')
        pool.refresh_phases(state, {0: {'complete': True}})
        self.assertEqual(state['courses']['0']['phase'], 'gather')

    def test_six_logical_groups_preserve_every_original_block(self):
        plan = plan_for(100); validate_plan(plan)
        self.assertEqual(len(plan['shards']), 6)
        self.assertEqual(sorted(n for s in plan['shards'] for n in s['chunk_ids']), list(range(100)))
        for change in [{'target_seconds': 0}, {'cost_rtf': math.nan}, {'worker_cap': 20}]:
            altered = copy.deepcopy(plan); altered['runner_policy'].update(change)
            with self.assertRaises(ValueError): validate_plan(altered)
        bad = copy.deepcopy(plan); bad['execution'] = 'static'
        with self.assertRaises(ValueError): validate_plan(bad)

    def test_recovery_keeps_successful_stages_and_replaces_only_failed_generation(self):
        for phase, success, expected in [('prepare', True, 'asr'), ('prepare', False, 'new'),
                ('asr', False, 'asr'), ('gather', False, 'gather'), ('gather', True, 'publish'),
                ('publish', False, 'publish'), ('publish', True, 'done')]:
            with self.subTest(phase=phase, success=success):
                state = journal(1); t = pool.reserve(state, 0, phase, 1)
                t.update(status='completed', conclusion='success' if success else 'failure')
                state['courses']['0']['phase'] = 'failed'
                pool.recover(state, 2)
                self.assertEqual(state['courses']['0']['phase'], expected)
                pool.refresh_phases(state, {0: {'complete': False}})
                self.assertEqual(state['courses']['0']['phase'], expected)
                self.assertEqual(len(state['tickets']), 1)

    def test_stage_recovery_waits_for_every_old_worker(self):
        state = journal(1); pool.reserve(state, 0, 'asr', 1)
        with self.assertRaises(ValueError): pool.recover(state, 2)

    def test_poll_discovers_exact_runs_and_checks_source_before_authorization(self):
        state = journal(1); t = pool.reserve(state, 0, 'gather', 1)
        run = {'id': 100, 'head_sha': 'a'*40, 'path': '.github/workflows/'+pool.WORKFLOW,
               'display_title': 'icourse-stage-99-'+t['nonce'], 'status': 'completed', 'conclusion': 'failure'}
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'test/repo'}), \
             patch.object(pool.subprocess, 'check_output', return_value=json.dumps([{'workflow_runs': [run]}]).encode()), \
             patch.object(pool, 'api', return_value={'jobs': [{'steps': [{
                'name': 'Finalize through LectureRunner with saved quota', 'started_at': '2026-10-07', 'conclusion': 'failure'}]}]}):
            pool.Actions(state).poll(state)
        self.assertEqual(t['run'], '100'); self.assertTrue(t['entered'])
        for runs in [[run, run], [dict(run, head_sha='b'*40)], []]:
            with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'test/repo'}), \
                 patch.object(pool.subprocess, 'check_output', return_value=json.dumps([{'workflow_runs': runs}]).encode()):
                with self.assertRaises(ValueError): pool.Actions(state).poll(state)

    def test_public_failure_audit_contains_only_whitelisted_error_and_opaque_ids(self):
        state = journal(1); t = pool.reserve(state, 0, 'prepare', 1)
        audit = pool.public_audit(state, False, RuntimeError('password=private and classroom'))
        self.assertNotIn('private', json.dumps(audit)); self.assertNotIn('password', json.dumps(audit))
        self.assertNotIn('flags', audit); self.assertNotIn('sha', audit)
        self.assertEqual(audit['children'][0]['status'], 'reserved')



class OwnerTests(unittest.TestCase):
    def check(self, state, owner, old, *, poll=lambda s: None):
        return pool.claim_owner(state, owner, open_pool=lambda run: old,
            inspect_parent=lambda run: {'status': 'completed'}, poll=poll)

    def test_cancelled_parent_does_not_free_live_or_unknown_children(self):
        for status in ['reserved', 'in_progress']:
            owner = MemoryStore({'schema': 1, 'run_id': '98'}); old = MemoryStore(journal(1, '98'))
            pool.reserve(old.state, 0, 'prepare', 1)['status'] = status
            with self.assertRaises(ValueError): self.check(journal(1), owner, old)
            self.assertEqual(owner.state['run_id'], '98')

    def test_only_confirmed_ended_children_allow_another_batch(self):
        owner = MemoryStore({'schema': 1, 'run_id': '98'}); old = MemoryStore(journal(1, '98'))
        pool.reserve(old.state, 0, 'prepare', 1)
        def poll(s):
            s['tickets'][0].update(status='completed', conclusion='failure', run='100')
        self.check(journal(1), owner, old, poll=poll)
        self.assertEqual(owner.state['run_id'], '99')
        self.assertEqual(old.state['tickets'][0]['status'], 'completed')

    def test_active_parent_or_missing_journal_cannot_be_overwritten(self):
        owner = MemoryStore({'schema': 1, 'run_id': '98'})
        with self.assertRaises(ValueError):
            pool.claim_owner(journal(1), owner, inspect_parent=lambda run: {'status': 'in_progress'})
        with self.assertRaises(ValueError): self.check(journal(1), owner, MemoryStore(None))
        self.assertEqual(owner.state['run_id'], '98')

    def test_plan_owner_gate_runs_before_authentication_or_input_selection(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), \
             patch.object(pool, 'verify_previous_pool', side_effect=ValueError('old children active')) as gate, \
             patch.object(pipeline, 'artifact') as select:
            with self.assertRaises(ValueError): pipeline.plan()
            gate.assert_called_once(); select.assert_not_called()

    def test_legacy_preflight_does_not_transfer_the_owner(self):
        owner = MemoryStore({'schema': 1, 'run_id': '98'}); old = MemoryStore(journal(1, '98'))
        pool.claim_owner({'run_id': '99'}, owner, open_pool=lambda r: old,
                        inspect_parent=lambda r: {'status': 'completed'}, poll=lambda s: None, transfer=False)
        self.assertEqual(owner.state['run_id'], '98')


class ChildAuthorizationTests(unittest.TestCase):
    def setup_context(self):
        state = journal(1); t = pool.reserve(state, 0, 'asr', 1)
        t.update(run='100', status='in_progress')
        infos = {'99': {'head_sha': 'a'*40, 'path': '.github/workflows/parallel_pilot.yml', 'run_attempt': 1},
            '100': {'head_sha': 'a'*40, 'path': '.github/workflows/'+pool.WORKFLOW, 'run_attempt': 1,
                    'display_title': 'icourse-stage-99-'+t['nonce']}}
        return state, t, infos

    def test_exact_nonce_source_and_parent_attempt_are_required(self):
        state, t, infos = self.setup_context()
        authorized = stage.authorize('99', t['nonce'], '100', read=lambda: state, inspect=infos.__getitem__)
        self.assertEqual(authorized, (state, t))
        for which, key, value in [('100', 'head_sha', 'b'*40), ('100', 'run_attempt', 2),
                ('99', 'run_attempt', 2), ('99', 'path', '.github/workflows/ci.yml'),
                ('100', 'display_title', 'another')]:
            altered = copy.deepcopy(infos); altered[which][key] = value
            with self.assertRaises(ValueError):
                stage.authorize('99', t['nonce'], '100', read=lambda: state, inspect=altered.__getitem__)
        with self.assertRaises(ValueError):
            stage.authorize('99', 'f'*32, '100', read=lambda: state, inspect=infos.__getitem__)

    def test_unregistered_child_waits_without_models_and_times_out(self):
        state, t, infos = self.setup_context(); t['run'] = None; sleep = MagicMock()
        with self.assertRaises(TimeoutError):
            stage.authorize('99', t['nonce'], '100', read=lambda: state, inspect=infos.__getitem__, sleep=sleep)
        self.assertEqual(sleep.call_count, 12)

    def test_parent_identity_only_overrides_pipeline_subprocess(self):
        state, t, infos = self.setup_context(); store = MemoryStore(state)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '100', 'POOL_PARENT_RUN_ID': '99',
                'POOL_TICKET': t['nonce'], 'GITHUB_REPOSITORY': 'test/repo'}), \
             patch.object(pool, 'store_for', return_value=store), \
             patch.object(pool, 'api', side_effect=lambda path: infos[path.rsplit('/', 1)[-1]]):
            ticket, env = stage.context()
            self.assertEqual(os.environ['GITHUB_RUN_ID'], '100')
            self.assertEqual(env['GITHUB_RUN_ID'], '99'); self.assertEqual(env['SHARD_MODE'], 'shared')
            self.assertEqual(env['PUBLISH_RESULTS'], 'false')

    def test_artifact_routes_are_stage_worker_slot_specific(self):
        state = journal(2)
        for slot, phase, worker, run in [(0, 'prepare', 0, '100'), (0, 'asr', 1, '101'), (1, 'asr', 1, '102'),
                (0, 'gather', 0, '103'), (0, 'asr', 1, '104')]:
            t = pool.reserve(state, slot, phase, 1, worker); t.update(run=run, status='completed', conclusion='success')
        self.assertEqual(pool.artifact_sources(state, 'qwen-production-shared-0-1'), ['104', '101'])
        self.assertEqual(pool.artifact_sources(state, 'qwen-production-worker-input-0'), ['100'])
        self.assertEqual(pool.artifact_sources(state, 'qwen-validation-result-0'), ['103'])
        self.assertEqual(pool.artifact_sources(state, 'unknown'), [])

    def test_later_gather_without_checkpoint_cannot_refund_quota(self):
        state = journal(1); t = pool.reserve(state, 0, 'gather', 1)
        t.update(run='100', status='completed', conclusion='failure', entered=True)
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'POOL_ARTIFACTS': 'true', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_RUN_ID': '99'}), \
             patch.object(pool, 'store_for', side_effect=lambda *a: MemoryStore(state)):
            self.assertEqual(pipeline.last_finalization_attempt('99', 0, prior_only=True), 1)
            with self.assertRaises(ValueError):
                pipeline.validate_checkpoint_age({'attempt.json': b'0'}, '99', 0, prior_only=True)

    def test_old_native_source_keeps_its_quota_history_in_new_pool(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'POOL_ARTIFACTS': 'true',
                                    'GITHUB_RUN_ID': '100', 'GITHUB_REPOSITORY': 'test/repo'}), \
             patch.object(pool, 'store_for', return_value=MemoryStore(None)), \
             patch.object(pipeline.subprocess, 'check_output', side_effect=[
                 json.dumps({'run_attempt': 1}).encode(), json.dumps([{'jobs': [{
                     'name': 'lecture / finalize-0', 'steps': [{'name': 'Finalize through LectureRunner with saved quota',
                     'started_at': '2026-10-07', 'conclusion': 'failure'}]}]}]).encode()]):
            self.assertEqual(pipeline.last_finalization_attempt('98', 0), 1)

    def test_pool_input_artifact_routes_to_child_without_changing_parent_binding(self):
        state = journal(1); t = pool.reserve(state, 0, 'prepare', 1)
        t.update(run='100', status='completed', conclusion='success')
        with patch.dict(os.environ, {'POOL_ARTIFACTS': 'true', 'GITHUB_RUN_ID': '99', 'GITHUB_REPOSITORY': 'test/repo'}), \
             patch.object(pool, 'store_for', return_value=MemoryStore(state)), \
             patch.object(pipeline.subprocess, 'check_output', side_effect=[
                 json.dumps([{'artifacts': []}]).encode(),
                 json.dumps([{'artifacts': [{'name': 'qwen-production-worker-input-0', 'expired': False}]}]).encode()]), \
             patch.object(shards, 'command') as download:
            self.assertTrue(pipeline.artifact('qwen-production-worker-input-0', Path('/unused'), required=True))
            self.assertEqual(download.call_args.args[0][3], '100')
            self.assertEqual(os.environ['GITHUB_RUN_ID'], '99')



class AudioAndStorageTests(unittest.TestCase):
    def test_sample_seek_matches_old_ffmpeg_trim_including_overlaps_and_rounding(self):
        import numpy as np
        import soundfile as sf
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'lecture.flac'
            data = (np.sin(np.arange(64000)*.123)*.9).astype('float32')
            sf.write(source, data, 16000, subtype='PCM_24')
            audio = {'selection': {'course_id': '10', 'sub_id': '1'}, 'audio_seconds': 4,
                     'full_chunks': [{'start': 0, 'end': 1.234567}, {'start': 1.1, 'end': 2.7}, {'start': 3, 'end': 4}]}
            plan = build_audio_plan(audio, reference={}, course_slot=0, run_id='99', audio_sha256='b'*64, production=True)
            files = {}; pipeline.encode_audio_chunks(source, plan, files, root)
            for block in plan['blocks']:
                old = root/'old.flac'
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source), '-af',
                    f"atrim=start_sample={round(block['start']*16000)}:end_sample={round(block['end']*16000)}",
                    '-c:a', 'flac', '-y', str(old)], check=True, capture_output=True)
                got, rate = sf.read(io.BytesIO(files[f"chunk-{block['chunk_id']}.flac"]), dtype='int32')
                expected, _ = sf.read(old, dtype='int32')
                np.testing.assert_array_equal(got, expected)
                self.assertEqual(len(got), block['samples']); self.assertEqual(rate, 16000)
                self.assertEqual(block['flac_sha256'], hashlib.sha256(files[f"chunk-{block['chunk_id']}.flac"]).hexdigest())

    def test_worker_payload_omits_whole_audio_and_database_but_keeps_hashes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'POOL_ARTIFACTS': 'true',
                'QWEN_PRODUCTION_TASK': 'true', 'RUNNER_TEMP': tmp, 'GITHUB_RUN_ID': '99',
                'COURSE_SLOT': '0', 'DB_ENCRYPTION_KEY': 'k'*32}):
            plan = plan_for(1); spec = shards.encoded({'mode': 'sharded', 'plan': plan})
            files = {'specification.json': spec, 'lecture.flac': b'whole', 'database.db': b'private DB', 'chunk-0.flac': b'original bytes'}
            pipeline.encode(files, 'prepared', pipeline.out('prepared.enc'))
            thin = pipeline.decode(pipeline.out('worker-plan.enc'), 'worker-plan')
            self.assertEqual(json.loads(thin['specification.json'])['plan'], plan)
            self.assertEqual(pipeline.decode(pipeline.out('worker-input')/'prepared.enc', 'prepared'),
                             {'specification.json': spec, 'chunk-0.flac': b'original bytes'})
            self.assertEqual(pipeline.decode(pipeline.out('prepared.enc'), 'prepared'), files)
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '100'}):
                with self.assertRaises(Exception): pipeline.decode(pipeline.out('worker-input')/'prepared.enc', 'prepared')

    def test_actual_git_encrypted_journal_cas_and_owner_have_distinct_namespaces(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GITHUB_REPOSITORY': 'test/repo'}):
            remote = Path(tmp)/'remote.git'
            subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True, capture_output=True)
            a = pool.PoolStore('99', b'k'*32, remote_url=str(remote))
            b = pool.PoolStore('99', b'k'*32, remote_url=str(remote))
            owner = pool.OwnerStore(b'k'*32, remote_url=str(remote))
            try:
                self.assertTrue(a.compare_and_swap(None, journal(1)))
                version, state = a.read(); old, copy_state = b.read()
                pool.reserve(state, 0, 'prepare', 1)
                self.assertTrue(a.compare_and_swap(version, state))
                self.assertFalse(b.compare_and_swap(old, copy_state))
                self.assertIsNone(owner.read()[1])
                self.assertTrue(owner.compare_and_swap(None, {'schema': 1, 'run_id': '99'}))
                raw = a.command(['git', 'show', a.head()+':queue.enc'])
                self.assertNotIn(b'AUTO_COURSE_TERMS', raw)
                with self.assertRaises(Exception): owner.unseal(raw)
                self.assertEqual(a.read()[1], state)
            finally: a.close(); b.close(); owner.close()

    def test_cost_history_ignores_newer_unfinished_or_other_model_lessons(self):
        from src.data.database import Database
        from src.ai.qwen_transcriber import MODEL, REVISION
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp)/'db'))
            try:
                db.upsert_course('10', '课程', '姓名')
                for n, date, ratio, complete, model in [
                    ('1', '2026-09-01', 3, True, MODEL), ('2', '2026-09-02', 4, True, MODEL),
                    ('3', '2026-09-03', 9, False, MODEL), ('4', '2026-09-04', 9, True, 'different'),
                    ('5', '2026-10-01', 9, True, MODEL)]:
                    db.insert_lecture(n, '10', '课程', date)
                    db.mark_processed(n)
                    db.write_meta('qwen_pipeline:'+n, json.dumps({'complete': complete, 'asr_cost': {
                        'model': model, 'revision': REVISION, 'audio_seconds': 100,
                        'decode_seconds': ratio*100, 'blocks': 5}}))
                self.assertEqual(pipeline.historical_asr_cost(db, '10', '2026-09-05'), 3.5)
                self.assertEqual(pipeline.historical_asr_cost(db, '10', '2026-08-01'), 2)
            finally: db.conn.close()

    def test_failure_progress_persists_without_gather_or_new_quota_checkpoint(self):
        from src.data.database import Database
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'RUNNER_TEMP': tmp, 'GITHUB_RUN_ID': '99', 'COURSE_SLOT': '0',
                'COURSE_IDS': '10', 'PUBLISH_RESULTS': 'true'}):
            db = Database(str(Path(tmp)/'old.db'))
            db.upsert_course('10', '课程', '姓名'); db.insert_lecture('1', '10', '课程', '2026-09-01')
            db.update_transcript('1', '已经识别的内容')
            source = pipeline.lecture_snapshot(db, Path(tmp)/'snapshot.db', '10', '1'); db.conn.close()
            spec = shards.encoded({'course_id': '10', 'lecture': {'sub_id': '1'}})
            state = journal(1); t = pool.reserve(state, 0, 'gather', 1)
            t.update(status='completed', conclusion='failure', entered=True)
            state['courses']['0']['phase'] = 'failed'
            def present(name, *args, **kw): return '-prepare-' in name
            def publish(delta, *a):
                stored = Database(str(delta))
                try:
                    row = stored.get_lecture('1')
                    self.assertEqual(row['transcript'], '已经识别的内容')
                    self.assertEqual(row['error_stage'], 'pool_gather')
                    self.assertFalse(row['processed_at'])
                finally: stored.conn.close()
            with patch.object(pool, 'store_for', return_value=MemoryStore(state)), \
                 patch.object(pipeline, 'artifact', side_effect=present), \
                 patch.object(pipeline, 'decode', return_value={'database.db': source, 'specification.json': spec}), \
                 patch.object(pipeline, 'publish', side_effect=publish) as publisher, \
                 patch.object(pipeline, 'encode') as encoder:
                pipeline.persist_pool_failures()
            publisher.assert_called_once()
            self.assertEqual(encoder.call_args.args[1], 'pool-failures')
            self.assertNotIn('review.json', encoder.call_args.args[0])
            self.assertNotIn('state.enc', encoder.call_args.args[0])
            self.assertEqual(state['tickets'][0]['entered'], True)

    def test_failure_progress_waits_until_all_children_are_ended(self):
        state = journal(1); pool.reserve(state, 0, 'asr', 1)
        with patch.object(pool, 'store_for', return_value=MemoryStore(state)), patch.object(pipeline, 'publish') as publisher:
            with self.assertRaises(ValueError): pipeline.persist_pool_failures()
            publisher.assert_not_called()



class SummarySourceTests(unittest.TestCase):
    def completed_course(self):
        state = journal(2)
        state['courses']['0']['phase'] = 'failed'
        state['courses']['1']['phase'] = 'done'
        children = {}
        for index, stage in enumerate(('gather', 'publish')):
            ticket = pool.reserve(state, 1, stage, 1)
            ticket.update(status='completed', conclusion='success', run=str(101+index))
            children[ticket['run']] = {'path': '.github/workflows/'+pool.WORKFLOW,
                'status': 'completed', 'conclusion': 'success', 'head_sha': state['sha'],
                'display_title': 'icourse-stage-99-'+ticket['nonce'], 'run_attempt': 1}
        state['courses']['1']['phase'] = 'done'
        info = {'path': '.github/workflows/parallel_pilot.yml', 'status': 'completed',
                'conclusion': 'failure', 'head_sha': state['sha']}
        return info, state, children

    def test_failed_batch_exports_only_independently_successful_course(self):
        from scripts.production_result_export import validate_summary_source
        info, state, children = self.completed_course()
        validate_summary_source(info, '99', 1, read_pool=lambda: state, inspect_run=children.__getitem__)
        for run, slot in [('99', 0), ('99', 2), ('100', 1)]:
            with self.assertRaises(ValueError):
                validate_summary_source(info, run, slot, read_pool=lambda: state, inspect_run=children.__getitem__)
        for changes in ({'status': 'in_progress'}, {'conclusion': 'cancelled'},
                        {'path': '.github/workflows/check.yml'}, {'head_sha': 'b'*40}):
            with self.assertRaises(ValueError):
                validate_summary_source(dict(info, **changes), '99', 1,
                    read_pool=lambda: state, inspect_run=children.__getitem__)

    def test_child_identity_and_latest_stage_completion_cannot_be_bypassed(self):
        from scripts.production_result_export import validate_summary_source
        info, state, children = self.completed_course()
        for stage_index in range(2):
            for changes in ({'status': 'in_progress'}, {'conclusion': 'failure'},
                            {'head_sha': 'b'*40}, {'run_attempt': 2}, {'display_title': 'wrong'},
                            {'path': '.github/workflows/check.yml'}):
                altered = copy.deepcopy(children)
                altered[str(101+stage_index)].update(changes)
                with self.assertRaises(ValueError):
                    validate_summary_source(info, '99', 1, read_pool=lambda: state,
                                            inspect_run=altered.__getitem__)
        for changes in ({'status': 'reserved'}, {'run': None}, {'conclusion': 'failure'}):
            altered = copy.deepcopy(state); altered['tickets'][-1].update(changes)
            with self.assertRaises(ValueError):
                validate_summary_source(info, '99', 1, read_pool=lambda: altered,
                                        inspect_run=children.__getitem__)
        altered = copy.deepcopy(state)
        pool.reserve(altered, 1, 'gather', 2)
        with self.assertRaises(ValueError):
            validate_summary_source(info, '99', 1, read_pool=lambda: altered,
                                    inspect_run=children.__getitem__)

    def test_successful_legacy_summary_needs_no_pool(self):
        from scripts.production_result_export import validate_summary_source
        info = {'path': '.github/workflows/parallel_pilot.yml', 'status': 'completed', 'conclusion': 'success'}
        read = MagicMock()
        validate_summary_source(info, '99', 0, read_pool=read)
        read.assert_not_called()


class InspectionSourceTests(unittest.TestCase):
    def test_live_parent_allows_only_confirmed_ended_preparation_child(self):
        from scripts.production_result_export import validate_inspection_source
        state=journal(2); ticket=pool.reserve(state,1,'prepare',1)
        ticket.update(status='completed',conclusion='failure',run='101')
        info={'path':'.github/workflows/parallel_pilot.yml','status':'in_progress','head_sha':state['sha']}
        child={'path':'.github/workflows/'+pool.WORKFLOW,'status':'completed','head_sha':state['sha'],
               'display_title':'icourse-stage-99-'+ticket['nonce'],'run_attempt':1}
        validate_inspection_source(info,'99',1,read_pool=lambda:state,inspect_run=lambda run:child)
        for changes in ({'status':'in_progress'}, {'head_sha':'b'*40}, {'run_attempt':2}, {'display_title':'wrong'}):
            with self.assertRaises(ValueError):
                validate_inspection_source(info,'99',1,read_pool=lambda:state,inspect_run=lambda run:dict(child,**changes))
        for changes in ({'status':'reserved'}, {'run':None}):
            altered=copy.deepcopy(state);altered['tickets'][0].update(changes)
            with self.assertRaises(ValueError):
                validate_inspection_source(info,'99',1,read_pool=lambda:altered,inspect_run=lambda run:child)
        for run,slot in [('100',1),('99',0)]:
            with self.assertRaises(ValueError):
                validate_inspection_source(info,run,slot,read_pool=lambda:state,inspect_run=lambda run:child)

    def test_completed_legacy_inspection_remains_read_only_and_wrong_workflow_rejected(self):
        from scripts.production_result_export import validate_inspection_source
        info={'path':'.github/workflows/parallel_pilot.yml','status':'completed'}
        read=MagicMock()
        validate_inspection_source(info,'99',0,read_pool=read);read.assert_not_called()
        with self.assertRaises(ValueError):
            validate_inspection_source(dict(info,path='.github/workflows/check.yml'),'99',0,read_pool=read)


class SelectionReplayTests(unittest.TestCase):
    def test_fresh_trial_copies_only_ended_pre_asr_selection_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'RUNNER_TEMP':tmp,'GITHUB_RUN_ID':'100','COURSE_SLOT':'0','DB_ENCRYPTION_KEY':'k'*32,
                'GITHUB_REPOSITORY':'owner/repo','QWEN_PRODUCTION_TASK':'true',**FLAGS}):
            state=journal(2);ticket=pool.reserve(state,0,'prepare',1)
            ticket.update(status='completed',conclusion='cancelled',run='101')
            info={'path':'.github/workflows/parallel_pilot.yml','status':'completed','head_sha':state['sha']}
            child={'status':'completed','head_sha':state['sha'],'path':'.github/workflows/'+pool.WORKFLOW,
                   'display_title':'icourse-stage-99-'+ticket['nonce'],'run_attempt':1}
            files={'queue.json':shards.encoded([['10','概率论',{'sub_id':'1','_validation':{'date':'2026-10-04'}}],
                ['20','数值',{'sub_id':'2','_validation':{'date':'2026-10-03'}}]]),
                'database.db':b'empty scratch database','history.db':b'original encrypted history'}
            with shards.environment({'GITHUB_RUN_ID':'99'}):
                pipeline.encode(files,'queue',pipeline.root()/'fixture.enc')
            def download(name,target,**kwargs):
                self.assertEqual(kwargs['run'],'99');target.mkdir(parents=True,exist_ok=True)
                (target/'queue.enc').write_bytes((pipeline.root()/'fixture.enc').read_bytes());return True
            api=lambda path:info if path.endswith('/99') else child
            with patch.object(pool,'store_for',return_value=MemoryStore(state)), \
                 patch.object(pool,'api',side_effect=api),patch.object(pipeline,'artifact',side_effect=download):
                copied=pipeline.validation_selection_queue('99')
            self.assertEqual(copied['database.db'],files['database.db'])
            self.assertEqual(copied['history.db'],files['history.db'])
            tasks=json.loads(copied['queue.json'])
            self.assertEqual([t[2]['sub_id'] for t in tasks],['1','2'])
            self.assertTrue(all(t[2]['_validation']['selection_source_run_id']=='99' for t in tasks))
            for change in ('asr','gather','publish','unknown','active','source_sha'):
                altered=copy.deepcopy(state);changed_child=dict(child);changed_info=dict(info)
                if change in ('asr','gather','publish'):altered['tickets'][0]['stage']=change
                if change=='unknown':altered['tickets'][0]['run']=None
                if change=='active':changed_child['status']='in_progress'
                if change=='source_sha':changed_info['head_sha']='b'*40
                with patch.object(pool,'store_for',return_value=MemoryStore(altered)), \
                     patch.object(pool,'api',side_effect=lambda path:changed_info if path.endswith('/99') else changed_child), \
                     patch.object(pipeline,'artifact') as fetch:
                    with self.assertRaises(ValueError):pipeline.validation_selection_queue('99')
                    fetch.assert_not_called()

    def test_selection_source_cannot_enable_side_effects_or_conflict_with_old_source(self):
        with patch.dict(os.environ, {'VALIDATION_COURSE_ID':'10,20','COURSE_IDS':'10,20',
                'VALIDATION_LECTURE_RANK':'1','VALIDATION_BEFORE_DATE':'',
                'VALIDATION_SELECTION_RUN_ID':'99','VALIDATION_SOURCE_RUN_ID':'',**FLAGS}):
            self.assertEqual(pipeline.validation_course(),'10,20')
            for changes in ({'VALIDATION_SOURCE_RUN_ID':'98'}, {'PUBLISH_RESULTS':'true'},
                            {'VALIDATION_SELECTION_RUN_ID':'bad'}, {'VALIDATION_COURSE_ID':''}):
                with patch.dict(os.environ,changes):
                    with self.assertRaises(ValueError):pipeline.validation_course()


class WorkflowTests(unittest.TestCase):
    def test_graph_uses_one_controller_single_job_children_and_disjoint_legacy_path(self):
        parent = yaml.safe_load((ROOT/'.github/workflows/parallel_pilot.yml').read_text())
        child = yaml.safe_load((ROOT/'.github/workflows/qwen_production_stage.yml').read_text())
        routine = yaml.safe_load((ROOT/'.github/workflows/check.yml').read_text())
        self.assertEqual(parent['on']['workflow_call']['inputs']['shard_mode']['default'], 'shared')
        self.assertIn("inputs.shard_mode != 'shared'", parent['jobs']['lecture']['if'])
        self.assertIn("needs.pool.outputs.all_ended == 'true'", parent['jobs']['finalize']['if'])
        self.assertEqual(set(child['jobs']), {'register', 'execute'})
        self.assertEqual(child['jobs']['execute']['if'], "${{ github.event_name == 'workflow_dispatch' }}")
        self.assertEqual(child['jobs']['register']['if'], "${{ github.event_name == 'push' }}")
        self.assertEqual(child['jobs']['register']['permissions'], {})
        self.assertNotIn('env', child['jobs']['register'])
        self.assertEqual(parent['jobs']['pool']['permissions']['actions'], 'write')
        self.assertEqual(routine['jobs']['check']['permissions']['actions'], 'write')
        self.assertEqual(routine['jobs']['check']['with']['shard_mode'], 'shared')
        self.assertFalse(parent['on']['workflow_dispatch']['inputs']['publish_results']['default'])
        self.assertFalse(parent['on']['workflow_dispatch']['inputs']['send_email']['default'])
        self.assertFalse(routine['jobs']['check']['with']['automatic_terms'])
        self.assertEqual([i['cron'] for i in routine['on']['schedule']], ['7 9 * * *'])
        env = child['jobs']['execute']['env']; self.assertNotIn('SMTP_EMAIL', env)
        authorization = next(s for s in child['jobs']['execute']['steps'] if s.get('id') == 'authorize')
        self.assertNotIn('env', authorization)
        for s in child['jobs']['execute']['steps']:
            self.assertNotIn('secrets: inherit', json.dumps(s))
            for name, value in s.get('env', {}).items():
                self.assertNotIn(name, ['SMTP_EMAIL', 'RECEIVER_EMAIL', 'RECEIVER_EMAILS'])
                self.assertIn("stage != 'asr'", value)


if __name__ == '__main__': unittest.main()
