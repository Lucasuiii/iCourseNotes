"""Live counts, private-data exclusion and best-effort display isolation."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from scripts.pool_progress import PoolProgress, show_progress
from scripts import production_pool as pool
from test_production_pool import journal, Simulation
from test_shared_asr_queue import MemoryStore


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)/'progress.json'
        self.summary = Path(self.temp.name)/'summary.md'
        self.clock = 0; self.emit = MagicMock()
        self.reporter = PoolProgress(self.output, repository='Lucasuiii/Fudan_iCourse_Subscriber',
            summary=self.summary, emit=self.emit, clock=lambda: self.clock)

    def test_live_counts_heartbeat_and_meaningful_changes(self):
        state = journal(1); state['courses']['0']['phase'] = 'asr'
        counts = {'total_blocks': 59, 'completed_blocks': 35, 'failed_blocks': 1,
                  'pending_blocks': 20, 'claimed_blocks': 3, 'remaining_blocks': 23}
        self.reporter.update(state, {0: counts})
        self.assertIn('成功 35/59，缺失 1，待领取 20，识别中 3', self.emit.call_args.args[0])
        self.clock = 30; self.reporter.update(state, {0: counts}); self.assertEqual(self.emit.call_count, 1)
        self.clock = 120; self.reporter.update(state, {0: counts}); self.assertEqual(self.emit.call_count, 2)
        counts['completed_blocks'] += 1; self.reporter.update(state, {0: counts})
        self.assertEqual(self.emit.call_count, 3)
        # Live logs are immediate; summary is written only when finalized.
        self.assertFalse(self.summary.exists())

    def test_failed_gather_retains_gaps_and_counts_survive_plan_removal(self):
        state = journal(1); state['courses']['0']['phase'] = 'asr'
        self.reporter.update(state, {0: {'total_blocks': 59, 'completed_blocks': 58,
            'failed_blocks': 1, 'pending_blocks': 0, 'claimed_blocks': 0, 'remaining_blocks': 0}})
        state['courses']['0']['phase'] = 'failed'
        self.reporter.update(state, {}, final=True, error=True)
        saved = json.loads(self.output.read_text()); self.assertEqual(saved['status'], 'incomplete')
        self.assertIn('不完整（存在缺失块）', self.summary.read_text())
        self.assertIn('成功 58/59', self.summary.read_text())

    def test_successful_gather_with_retained_gaps_is_distinct_from_complete_asr(self):
        state=journal(1);state['courses']['0']['phase']='gather'
        counts={0:dict(total_blocks=131,completed_blocks=130,failed_blocks=1,pending_blocks=0,claimed_blocks=0,remaining_blocks=0)}
        self.assertEqual(self.reporter.snapshot(state,counts)['status'],'running_incomplete')
        state['courses']['0']['phase']='done'
        saved=self.reporter.snapshot(state,{},final=True)
        self.assertEqual(saved['status'],'success_with_gaps')
        self.assertEqual(saved['courses'][0]['completed_blocks'],130)
        self.assertIn('完成（保留未识别片段）',self.reporter.markdown(saved))
        self.assertEqual(self.reporter.snapshot(state,{},final=True,error=True)['status'],'controller_stopped')

    def test_child_links_unknown_reservations_and_worker_statuses(self):
        state = journal(1)
        t = pool.reserve(state, 0, 'asr', 1); t.update(status='in_progress', run='123')
        pool.reserve(state, 0, 'asr', 1, worker=1)
        snapshot = self.reporter.snapshot(state, {})
        course = snapshot['courses'][0]
        self.assertEqual((course['workers_running'], course['workers_waiting']), (1, 1))
        markdown = self.reporter.markdown(snapshot)
        self.assertIn('/actions/runs/123', markdown); self.assertIn('等待登记', markdown)
        self.assertIn('块数待音频准备完成后确定', markdown)
        self.assertNotIn('成功 0/0', markdown)

    def test_public_output_whitelist_and_markdown_injection(self):
        secret = 'PRIVATE-transcript-secret-url'
        state = journal(1); state['courses']['0'].update(plan={'text': secret}, title=secret)
        t = pool.reserve(state, 0, 'prepare', 1); t.update(run='123', conclusion=secret, private=secret)
        state['private'] = secret
        self.reporter.update(state, {0: {'text': secret, 'total_blocks': 3,
            'completed_blocks': secret, 'failed_blocks': -1}}, final=True)
        combined = self.output.read_text()+self.summary.read_text()+str(self.emit.call_args)
        self.assertNotIn(secret, combined); self.assertNotIn(t['nonce'], combined)
        bad = PoolProgress(self.output, repository='owner/repo](secret)')
        self.assertNotIn('url', bad.snapshot(state, {})['courses'][0]['children'][0])

    def test_controller_failure_and_uninitialized_state_do_not_claim_success(self):
        snapshot = self.reporter.snapshot({'run_id': '99', 'tickets': []}, {}, final=True, error=True)
        self.assertEqual(snapshot['status'], 'controller_stopped')
        self.assertIn('子任务状态需核验', self.reporter.markdown(snapshot))
        state = journal(1); state['courses']['0']['phase'] = 'done'
        self.assertEqual(self.reporter.snapshot(state, {}, final=True)['status'], 'success')
        self.assertEqual(self.reporter.snapshot(state, {}, final=True, error=True)['status'], 'controller_stopped')

    def test_unavailable_display_does_not_change_real_controller_dispatch(self):
        store = MemoryStore(journal(2)); simulation = Simulation(store)
        reporter = MagicMock(); reporter.update.side_effect = OSError('private filesystem details')
        reporter.warned = False
        with patch('sys.stdout', io.StringIO()) as output:
            result = pool.controller(store, simulation, attempt=1, clock=lambda: simulation.tick*30,
                sleep=simulation.sleep, timeout=3600, get_work=simulation.work,
                acquire=lambda *a: None, progress=reporter)
        self.assertTrue(all(c['phase'] == 'done' for c in result['courses'].values()))
        self.assertEqual(output.getvalue().count('进度显示暂不可用'), 1)
        self.assertNotIn('private filesystem details', output.getvalue())
        self.assertEqual(sum(stage == 'prepare' for _, _, stage, _ in simulation.events), 2)

    def test_reporter_does_not_hide_unknown_dispatch_or_repeat_it(self):
        store = MemoryStore(journal(1)); simulation = Simulation(store)
        simulation.dispatch = MagicMock(side_effect=ConnectionError('unknown outcome'))
        with self.assertRaises(ConnectionError):
            pool.controller(store, simulation, attempt=1, clock=lambda: simulation.tick*30,
                sleep=simulation.sleep, get_work=simulation.work, acquire=lambda *a: None,
                progress=self.reporter)
        simulation.dispatch.assert_called_once()
        self.assertEqual(store.state['tickets'][0]['status'], 'reserved')

    def test_isolated_validation_label_and_failure_label(self):
        state = journal(1); state['courses']['0']['phase'] = 'publish'
        self.assertEqual(self.reporter.snapshot(state, {})['courses'][0]['label'], '隔离结果校验')
        state['flags']['PUBLISH_RESULTS'] = 'true'
        self.assertEqual(self.reporter.snapshot(state, {})['courses'][0]['label'], '发布结果')
        state['courses']['0']['phase'] = 'failed'
        self.assertEqual(self.reporter.snapshot(state, {}, final=True)['status'], 'failed')

    def test_counts_use_real_queue_terminals_and_current_attempt_claims(self):
        from test_shared_asr_queue import plan_for, decoded
        from test_qwen_block_recovery import failed
        from src.pipeline.asr_queue import SharedQueue, initial_queue
        plan = plan_for(5); store = MemoryStore(initial_queue(plan)); queue = SharedQueue(plan, store)
        for index in range(3):
            block, token = queue.claim('peer', 1)
            queue.finish(block['chunk_id'], token, failed(block) if index == 2 else decoded(block))
        queue.claim('running', 1)
        with patch('scripts.shared_asr_worker.store_for', return_value=store):
            work = pool.workload(0, {'mode': 'sharded', 'plan': plan}, 1)
            recovered = pool.workload(0, {'mode': 'sharded', 'plan': plan}, 2)
        self.assertEqual([work[k] for k in ('total_blocks', 'completed_blocks', 'failed_blocks',
            'claimed_blocks', 'pending_blocks', 'remaining_blocks')], [5, 2, 1, 1, 1, 2])
        self.assertEqual((recovered['claimed_blocks'], recovered['pending_blocks']), (0, 2))

    def test_display_initialization_failure_still_runs_controller_and_writes_audit(self):
        state = journal(1); state['courses']['0']['phase'] = 'done'
        store = MemoryStore(state)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '99', 'GITHUB_REPOSITORY': 'owner/repo'}), \
                patch.object(pool, 'store_for', return_value=store), \
                patch.object(pool, 'PoolProgress', side_effect=OSError('private path')), \
                patch.object(pool, 'controller', return_value=state) as controller, \
                patch('scripts.production_qwen.out', side_effect=lambda name: Path(self.temp.name)/name), \
                patch('scripts.production_qwen.write_outputs') as outputs, \
                patch('sys.stdout', io.StringIO()) as output:
            pool.main()
        controller.assert_called_once(); self.assertIsNone(controller.call_args.kwargs['progress'])
        outputs.assert_called_once_with(all_ended=True)
        self.assertIsNone(json.loads((Path(self.temp.name)/'pool-audit.json').read_text())['error_code'])
        self.assertNotIn('private path', output.getvalue())

    def test_partial_failure_keeps_other_course_running_and_prepare_failure_stops_waiting(self):
        state = journal(2)
        ticket = pool.reserve(state, 0, 'prepare', 1); ticket.update(status='completed', conclusion='failure')
        state['courses']['0']['phase'] = 'failed'; state['courses']['1']['phase'] = 'asr'
        snapshot = self.reporter.snapshot(state, {})
        self.assertEqual(snapshot['status'], 'running_with_failures')
        self.assertIn('进行中（部分课次失败）', self.reporter.markdown(snapshot))
        self.assertEqual(self.reporter.block_text(snapshot['courses'][0]), '音频准备失败；未进入分块识别')
        with_gaps = self.reporter.snapshot(state, {1: {'failed_blocks': 1}})
        self.assertEqual(with_gaps['status'], 'running_incomplete')
        self.assertEqual(self.reporter.snapshot(state, {}, final=True, error=True)['status'], 'controller_stopped')
