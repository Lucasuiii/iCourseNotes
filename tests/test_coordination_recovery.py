"""Fault-injected transport, ASR cleanup and login diagnostics; no live services."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import requests
import yaml

from scripts.asr_queue_store import GitHubQueueStore
from scripts import production_pool as pool, production_qwen as pipeline, production_pool_stage as stage
from scripts.coordination_transport import CoordinationError, read_json, read_with_retry
from scripts.shared_asr_worker import run_worker
from src.api.auth_recovery import authenticated_session
from src.pipeline.asr_queue import initial_queue
from test_shared_asr_queue import MemoryStore, plan_for

ROOT = Path(__file__).resolve().parents[1]


def unavailable():
    return subprocess.CalledProcessError(128, ['private-command'], stderr=b'HTTP 503 private-url-and-token')


class CoordinationRecoveryTests(unittest.TestCase):
    def test_read_outage_recovers_and_exhaustion_has_only_fixed_diagnostics(self):
        with patch('scripts.coordination_transport.time.sleep') as sleep:
            read = MagicMock(side_effect=[unavailable(), b'ok'])
            self.assertEqual(read_with_retry('git_fetch', read), b'ok')
            sleep.assert_called_once_with(2)
            read = MagicMock(side_effect=unavailable())
            with self.assertRaises(CoordinationError) as caught:
                read_with_retry('git_fetch', read)
            self.assertEqual(read.call_count, 3)
            self.assertEqual(caught.exception.code, 'service_unavailable')
            self.assertNotIn('private-url-and-token', str(caught.exception))

    def test_permissions_and_tls_are_not_retried(self):
        for message, code in [(b'HTTP 403 private', 'authorization'),
                              (b'SSL certificate problem private', 'tls')]:
            read = MagicMock(side_effect=subprocess.CalledProcessError(128, [], stderr=message))
            with patch('scripts.coordination_transport.time.sleep') as sleep:
                with self.assertRaises(CoordinationError) as caught:
                    read_with_retry('git_ls_remote', read)
                self.assertEqual(caught.exception.code, code)
                read.assert_called_once(); sleep.assert_not_called()

    def test_html_response_is_retried_only_for_reads_and_never_exported(self):
        with patch('scripts.coordination_transport.time.sleep'):
            read = MagicMock(side_effect=[b'<html>private-body</html>', b'{"ok":true}'])
            self.assertEqual(read_json(read), {'ok': True})
            self.assertEqual(read.call_count, 2)
            read = MagicMock(return_value=b'<html>private-body</html>')
            with self.assertRaises(CoordinationError) as caught: read_json(read)
            self.assertEqual(caught.exception.code, 'invalid_response')
            self.assertEqual(read.call_count, 3)
        result = SimpleNamespace(returncode=0, stdout='<html>private-body</html>', stderr='')
        with patch.object(pool.subprocess, 'run', return_value=result) as request:
            with self.assertRaises(CoordinationError): pool.api('dispatch', payload={})
            request.assert_called_once()

    def test_early_worker_failure_writes_safe_stage_diagnostics_before_queue_exists(self):
        error = CoordinationError('github_read', 'service_unavailable', 3)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'RUNNER_TEMP': tmp}), \
             patch('sys.argv', ['production_qwen', 'worker']), patch.object(pipeline, 'worker', side_effect=error):
            with self.assertRaises(CoordinationError): pipeline.main()
            audit = json.loads(pipeline.out('pipeline-failure.json').read_text())
        self.assertEqual(audit['error_code'], 'coordination_failure')
        self.assertEqual(audit['coordination']['operation'], 'github_read')

    def test_stage_boundary_keeps_authorization_and_download_failure_diagnostics(self):
        for command in ('bootstrap', 'execute'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp, \
                 patch.dict(os.environ, {'RUNNER_TEMP': tmp}):
                error = CoordinationError('github_read', 'service_unavailable', 3)
                with patch.object(stage, command, side_effect=error):
                    with self.assertRaises(CoordinationError) as caught: stage.main(command)
                self.assertIs(caught.exception, error)
                audit = json.loads(pipeline.out('stage-failure.json').read_text())
            self.assertEqual(audit['entry'], command)
            self.assertEqual(audit['coordination'], {'operation': 'github_read',
                'failure': 'service_unavailable', 'attempts': 3})

    def test_failed_stage_authorization_still_uploads_fixed_diagnostics(self):
        workflow = yaml.load((ROOT/'.github/workflows/qwen_production_stage.yml').read_text(), Loader=yaml.BaseLoader)
        upload = next(s for s in workflow['jobs']['execute']['steps']
                      if s.get('with', {}).get('name') == 'qwen-production-stage-audit')
        self.assertEqual(upload['if'], '${{ always() }}')
        self.assertIn('/out/stage-failure.json', upload['with']['path'])

    def test_parent_plan_authentication_failure_retains_fixed_phases_and_is_uploaded(self):
        error = requests.exceptions.ReadTimeout('private-password-cookie-ticket')
        error.auth_failure_diagnostics = {'failure_phase':'webvpn_context','error_type':'ReadTimeout',
            'failure':'auth_read_timeout','auth_attempts':3}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'RUNNER_TEMP':tmp}), \
             patch('sys.argv',['production_qwen','plan']), patch.object(pipeline,'plan',side_effect=error):
            with self.assertRaises(requests.exceptions.ReadTimeout): pipeline.main()
            audit=json.loads(pipeline.out('pipeline-failure.json').read_text())
        self.assertEqual(audit['authentication']['failure_phase'],'webvpn_context')
        self.assertEqual(audit['authentication']['auth_attempts'],3)
        self.assertEqual(audit['error_code'],'auth_read_timeout')
        self.assertNotIn('private',json.dumps(audit))
        workflow=yaml.load((ROOT/'.github/workflows/parallel_pilot.yml').read_text(),Loader=yaml.BaseLoader)
        upload=next(s for s in workflow['jobs']['plan']['steps']
            if s.get('with',{}).get('name')=='qwen-production-plan-audit')
        self.assertEqual(upload['if'],'${{ always() }}')
        self.assertIn('/out/pipeline-failure.json',upload['with']['path'])
        self.assertEqual(workflow['jobs']['plan']['env']['VALIDATION_LECTURE_RANKS'],
            '${{ inputs.validation_lecture_ranks }}')

    def test_real_git_lost_push_response_and_transient_rejection_keep_same_commit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GITHUB_REPOSITORY': 'test/repo'}):
            remote = str(Path(tmp)/'remote.git')
            subprocess.run(['git', 'init', '-q', '--bare', remote], check=True)
            store = GitHubQueueStore('99', 0, b'k'*32, remote_url=remote)
            try:
                original = store.command
                pushes = []
                def lost(args, data=None):
                    result = original(args, data)
                    if args[1] == 'push':
                        pushes.append(args[-1])
                        raise subprocess.TimeoutExpired(args, 120)
                    return result
                with patch.object(store, 'command', side_effect=lost):
                    self.assertTrue(store.compare_and_swap(None, {'revision': 1}))
                self.assertEqual(len(pushes), 1)
                before, _ = store.read()
                pushes.clear()
                def transient(args, data=None):
                    if args[1] == 'push':
                        pushes.append(args[-1])
                        if len(pushes) == 1: raise unavailable()
                    return original(args, data)
                with patch.object(store, 'command', side_effect=transient), patch('scripts.asr_queue_store.time.sleep'):
                    self.assertTrue(store.compare_and_swap(before, {'revision': 2}))
                self.assertEqual(pushes[0], pushes[1])
                self.assertEqual(store.read()[1], {'revision': 2})
            finally: store.close()

    def test_unknown_push_outcome_never_replays_or_means_conflict(self):
        store = GitHubQueueStore.__new__(GitHubQueueStore)
        store.remote = 'private'; store.ref = 'refs/heads/codex/asr-queue-99-0'
        store.seal = lambda state: b'encrypted'
        store.command = MagicMock(side_effect=[b'b'*40, b't'*40, b'c'*40, unavailable()])
        store.head = MagicMock(side_effect=CoordinationError('git_ls_remote', 'timeout', 3))
        with self.assertRaises(CoordinationError): store.compare_and_swap('a'*40, {})
        self.assertEqual(store.command.call_count, 4)

    def test_api_get_retries_but_post_never_duplicates_unknown_dispatch(self):
        failed = SimpleNamespace(returncode=1, stderr='HTTP 503 private-response', stdout='')
        ok = SimpleNamespace(returncode=0, stderr='', stdout='{"status":"completed"}')
        with patch.object(pool.subprocess, 'run', side_effect=[failed, ok]) as request, \
             patch('scripts.coordination_transport.time.sleep'):
            self.assertEqual(pool.api('private-read')['status'], 'completed')
            self.assertEqual(request.call_count, 2)
        with patch.object(pool.subprocess, 'run', return_value=failed) as request:
            with self.assertRaises(CoordinationError): pool.api('private-dispatch', payload={})
            request.assert_called_once()

    def test_controller_poll_survives_one_outage_without_reserving_again(self):
        from test_production_pool import journal
        state = journal(1); ticket = pool.reserve(state, 0, 'prepare', 1)
        run = {'id': 100, 'display_title': 'icourse-stage-99-'+ticket['nonce'],
               'head_sha': state['sha'], 'path': '.github/workflows/'+pool.WORKFLOW,
               'status': 'completed', 'conclusion': 'success'}
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'test/repo'}), \
             patch.object(pool.subprocess, 'check_output', side_effect=[unavailable(), json.dumps([{'workflow_runs': [run]}]).encode()]), \
             patch('scripts.coordination_transport.time.sleep'):
            pool.Actions(state).poll(state)
        self.assertEqual(len(state['tickets']), 1)
        self.assertEqual(ticket['status'], 'completed')

    def test_worker_original_error_survives_snapshot_failure_and_local_rows_remain(self):
        import numpy as np
        import soundfile as sf
        plan = plan_for(1); buf = io.BytesIO()
        sf.write(buf, np.zeros(960000, dtype=np.float32), 16000, format='FLAC')
        blob = buf.getvalue(); plan['blocks'][0]['flac_sha256'] = hashlib.sha256(blob).hexdigest()
        store = MemoryStore(initial_queue(plan)); model = MagicMock()
        model.recognize_blocks.side_effect = RuntimeError('Qwen chunk reached its token budget')
        with tempfile.TemporaryDirectory() as tmp, \
             patch('scripts.shared_asr_worker.shards.root', return_value=Path(tmp)), \
             patch('scripts.shared_asr_worker.shards.seal'), \
             patch('src.pipeline.asr_queue.SharedQueue.snapshot', side_effect=CoordinationError('git_fetch', 'timeout', 3)):
            with self.assertRaisesRegex(RuntimeError, 'token budget'):
                run_worker(plan, {'chunk-0.flac': blob}, store, 0, 1, transcriber=model)
            audit = json.loads((Path(tmp)/'out/worker-audit.json').read_text())
        self.assertEqual(audit['error_code'], 'qwen_token_budget')
        self.assertEqual(audit['secondary_error_codes'], ['coordination_failure'])
        self.assertFalse(audit['queue_snapshot_saved'])
        self.assertEqual(store.state['blocks']['0']['status'], 'pending')

    def test_worker_success_with_failed_snapshot_still_reports_failure(self):
        plan = plan_for(1); store = MemoryStore(initial_queue(plan))
        store.state['blocks']['0'] = {'status': 'complete', 'result': {
            'chunk_id': 0, 'start': 0, 'end': 60, 'text': 'synthetic'}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch('scripts.shared_asr_worker.shards.root', return_value=Path(tmp)), \
             patch('scripts.shared_asr_worker.shards.seal'), \
             patch('src.pipeline.asr_queue.SharedQueue.snapshot', side_effect=CoordinationError('git_fetch', 'timeout', 3)):
            with self.assertRaises(CoordinationError): run_worker(plan, {}, store, 0, 1)
            audit = json.loads((Path(tmp)/'out/worker-audit.json').read_text())
        self.assertEqual(audit['phase'], 'cleanup')
        self.assertEqual(audit['coordination']['operation'], 'git_fetch')

    def test_prepare_login_timeout_retains_all_fresh_session_failure_phases(self):
        sessions = [MagicMock() for _ in range(3)]
        for vpn, phase in zip(sessions, ('login_service_probe', 'webvpn_ticket_follow', 'icourse_api_verification')):
            vpn.auth_phase = phase
            vpn.login.side_effect = requests.exceptions.ReadTimeout('private-cookie-ticket')
        with self.assertRaises(requests.exceptions.ReadTimeout) as caught:
            authenticated_session(factory=MagicMock(side_effect=sessions), sleep=MagicMock())
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'RUNNER_TEMP': tmp}):
            pipeline.preparation_failure_audit({'prepare_phase': 'login'}, {}, caught.exception)
            audit = json.loads(pipeline.out('prepare-failure.json').read_text())
        self.assertEqual(audit['error_code'], 'auth_read_timeout')
        self.assertEqual(audit['authentication']['auth_attempts'], 3)
        self.assertEqual([r['failure_phase'] for r in audit['authentication']['attempt_failures']],
                         ['login_service_probe', 'webvpn_ticket_follow', 'icourse_api_verification'])
        self.assertNotIn('private', json.dumps(audit))
        for vpn in sessions: vpn.session.close.assert_called_once()

    def test_every_data_lock_entrant_keeps_pending_runs_and_daily_primary(self):
        entrants = []
        for path in (ROOT/'.github/workflows').glob('*.yml'):
            data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            concurrency = data.get('concurrency', {})
            if 'icourse-data' not in concurrency.get('group', ''): continue
            entrants.append(path.name)
            self.assertEqual(concurrency['queue'], 'max', path.name)
            self.assertEqual(concurrency['cancel-in-progress'], 'false', path.name)
        self.assertIn('check.yml', entrants); self.assertIn('parallel_pilot.yml', entrants)
        check = yaml.load((ROOT/'.github/workflows/check.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(check['on']['schedule'], [{'cron': '0 10 * * *'}])
        self.assertTrue(check['jobs']['check']['with']['caller_holds_lock'])


if __name__ == '__main__': unittest.main()
