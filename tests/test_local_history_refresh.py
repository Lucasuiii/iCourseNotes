"""Offline local-entry checks: synthetic history, fake ASR and temporary Git only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from scripts import local_history_refresh as entry
from scripts.local_history.storage import Store, atomic, file_hash
from scripts.local_history import runtime, publication
from src.pipeline import history_refresh as policy
from src.pipeline.qwen_plan import fingerprint
from src.ai.qwen_transcriber import QwenTranscriber, QwenTokenBudgetError
from scripts.local_history.backends import MLXTranscriber
from test_history_refresh import historical, final_candidate
from test_qwen_production_pipeline import fixture


class LocalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'private', 'k'*64)
        self.remote = self.root/'remote.db'
        db = historical(self.remote)
        self.targets = [policy.baseline_manifest(db.conn, {'course_id': '10', 'lecture_ids': [sid]}, 'a'*40)['targets'][0]
                        for sid in ('1', '2')]
        db.conn.close()
        self.manifest = {'schema': 1, 'targets': self.targets, 'backend': {'kind': 'mlx'},
                         'audio_acquisition': 'mp4',
                         'source': {'sha': 'b'*40}, 'run_id': '99', 'baseline_revision': 'a'*40,
                         'repository': 'owner/repository', 'model_path': '/unused'}
        self.store.save('manifest.enc', self.manifest)
        self.store.save_bytes('baseline.db.enc', self.remote.read_bytes())
        self.store.save('progress.enc', {'status': 'finished', 'lectures': {}})

    def candidate(self, target):
        tag = target['course_id']+'-'+target['sub_id']
        path = self.store.root/tag/'candidate.db'; path.parent.mkdir(mode=0o700)
        files = final_candidate(path, target)
        spec = json.loads(files['specification.json'])
        spec['requested_audio_acquisition'] = self.manifest['audio_acquisition']
        spec['audio_seconds'] = 600
        spec['audio_diagnostics'] = {'decode_return_code': 0, 'stderr_complete': True,
                                     'pcm_sample_aligned': True, 'decode_error_counts': {}}
        spec['plan']['local_backend'] = self.manifest['backend']
        plan, results = fixture(); rows = [r for v in results for r in v['chunks']]
        with sqlite3.connect(path) as conn:
            raw = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', ('qwen_pipeline:'+target['sub_id'],)).fetchone()[0])
            raw['plan_hash'] = fingerprint(spec['plan'])
            conn.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(raw), 'qwen_pipeline:'+target['sub_id']))
        self.store.save(tag+'.enc', {'spec': spec, 'review': json.loads(files['review.json']), 'rows': rows})
        progress = self.store.read('progress.enc'); progress['lectures'][tag] = {'status': 'complete'}
        self.store.save('progress.enc', progress)
        return path

    def test_encrypted_atomic_checkpoints_and_wrong_key(self):
        self.store.save('test.enc', {'secret': 'lecture text'})
        self.assertNotIn(b'lecture text', (self.store.root/'test.enc').read_bytes())
        self.assertEqual(self.store.read('test.enc')['secret'], 'lecture text')
        with self.assertRaises(Exception):
            Store(self.store.root, 'x'*64).read('test.enc')
        self.assertEqual((self.store.root/'test.enc').stat().st_mode & 0o777, 0o600)

    def test_process_lock_rejects_second_writer(self):
        with self.store.lock():
            with self.assertRaises(RuntimeError):
                with Store(self.store.root, 'k'*64).lock():
                    self.fail('second lock succeeded')

    def test_status_can_read_while_run_holds_the_writer_lock(self):
        with self.store.lock(), patch.object(entry,'REPO',self.root), \
             patch.dict(os.environ, DB_ENCRYPTION_KEY='k'*64, GITHUB_REPOSITORY=self.manifest['repository']):
            self.assertEqual(entry.main(['--run-dir',str(self.store.root),'status']),0)

    def test_literal_env_never_executes_shell(self):
        path = self.root/'secrets.env'
        atomic(path, b'LOCAL_TEST_VALUE=$(touch /tmp/do-not-run-local-test)\n')
        with patch.dict(os.environ):
            entry.load_env(path)
            self.assertEqual(os.environ['LOCAL_TEST_VALUE'], '$(touch /tmp/do-not-run-local-test)')
        path.chmod(0o644)
        with self.assertRaises(ValueError): entry.load_env(path)

    def test_local_access_auto_uses_direct_only_after_precredential_probe(self):
        for error,expected in ((None,'direct'),(TimeoutError('test'),'webvpn')):
            probe=MagicMock();probe.probe_login_service.side_effect=error
            with patch('src.api.webvpn.WebVPNSession',return_value=probe) as factory:
                self.assertEqual(runtime.choose_access_mode('auto'),expected)
                factory.assert_called_once_with(access_mode='direct')
                probe.session.close.assert_called_once()
                probe.login.assert_not_called();probe.authenticate_icourse.assert_not_called()

    def test_complete_selection_honors_subscription_exclusions_and_limit(self):
        from src.runtime import config
        with patch.object(config, 'COURSE_SESSION_RULES', {}), patch.object(config, 'COURSE_SESSION_EXCLUSIONS', {}):
            selected, _ = entry.select_targets(self.remote, 'a'*40, ['10'], 1)
            self.assertEqual([t['sub_id'] for t in selected], ['1'])
            with patch.object(config, 'COURSE_SESSION_EXCLUSIONS', {'10': None}), self.assertRaises(ValueError):
                entry.select_targets(self.remote, 'a'*40, ['10'])
        with self.assertRaises(ValueError): entry.select_targets(self.remote, 'a'*40, [])

    def test_block_resume_never_repeats_complete_inference(self):
        plan, results = fixture()
        rows = sorted([r for v in results for r in v['chunks']], key=lambda r:r['chunk_id'])
        audio = self.root/'audio.raw'; atomic(audio, b'\0'*(600*16000*4))
        model = MagicMock()
        model.recognize_blocks.return_value = [rows[1]]
        save = MagicMock()
        result = runtime.transcribe_pending(model, plan, audio, rows[:1], save, time.monotonic()+10)
        self.assertEqual(result, rows)
        self.assertEqual(model.recognize_blocks.call_args.args[0][0]['chunk_id'], 1)
        model.recognize_blocks.assert_called_once(); save.assert_called_once()
        model.release_model.assert_called_once()

    def test_incomplete_block_is_saved_but_cannot_finalize(self):
        plan, _ = fixture()
        audio = self.root/'audio.raw'; atomic(audio, b'\0'*(600*16000*4))
        model = MagicMock()
        model.recognize_blocks.return_value = [dict(plan['blocks'][0], text='', quality_state='missing_audio',
            missing_intervals=[{'start':0,'end':120,'error_code':'retry_timeout'}])]
        save = MagicMock()
        with self.assertRaises(RuntimeError):
            runtime.transcribe_pending(model, plan, audio, [], save, time.monotonic()+10)
        save.assert_called_once(); model.release_model.assert_called_once()

    def test_time_budget_stops_before_inference(self):
        plan, _ = fixture(); model = MagicMock()
        audio = self.root/'audio.raw'; atomic(audio, b'')
        with self.assertRaises(runtime.Paused):
            runtime.transcribe_pending(model, plan, audio, [], MagicMock(), time.monotonic()-1)
        model.recognize_blocks.assert_not_called()

    def test_mlx_truncation_echo_retry_and_deadline_without_gpu(self):
        core = SimpleNamespace(bfloat16='bf16')
        package = SimpleNamespace(core=core)
        infer = MagicMock(return_value=SimpleNamespace(text='准确的课堂原文',truncated=True))
        model = MLXTranscriber('/unused'); model._model = object(); model.set_terms(['条件期望'])
        with patch.dict(sys.modules, {'mlx':package,'mlx.core':core,'mlx_qwen3_asr':SimpleNamespace(transcribe=infer)}):
            with self.assertRaises(QwenTokenBudgetError): model._recognize([0],deadline=time.monotonic()+10)
            infer.reset_mock(); row = model._recognize([0],deadline=time.monotonic()-1)
            self.assertEqual(row['quality_state'],'retry_timeout'); infer.assert_not_called()
            infer.return_value = SimpleNamespace(text='条件期望',truncated=False)
            with patch('scripts.local_history.backends.context_echo', return_value=True), \
                 patch('scripts.local_history.backends.low_information', return_value=False):
                row = model._recognize([0],deadline=time.monotonic()+10)
            self.assertEqual(row['quality_state'],'unresolved_context_echo')
            self.assertEqual(infer.call_count,2)
            self.assertEqual(infer.call_args.kwargs['context'],'')

    def test_retry_pause_and_skip_failed_are_explicit(self):
        args = SimpleNamespace(hours=1,retry_failed=False)
        failed = {'status':'finished','lectures':{'10-1':{'status':'failed'}}}
        self.store.save('progress.enc',failed)
        with patch('scripts.local_history_refresh.verify_resume'), patch('scripts.local_history_refresh.doctor',return_value=0), \
             patch('scripts.local_history.runtime.process',side_effect=runtime.Paused('test')) as process:
            entry.run(self.store,self.manifest,args)
            self.assertEqual(process.call_args.args[2]['sub_id'],'2')
            self.assertEqual(self.store.read('progress.enc')['status'],'paused')
            args.retry_failed=True; process.reset_mock()
            entry.run(self.store,self.manifest,args)
            self.assertEqual(process.call_args.args[2]['sub_id'],'1')

    def test_partial_review_requires_explicit_switch(self):
        self.candidate(self.targets[0])
        with self.assertRaises(ValueError): entry.review(self.store, self.manifest)
        entry.review(self.store, self.manifest, completed_only=True)
        self.assertEqual(len(self.store.read('approval.enc')['approvals']), 1)
        self.assertIn('原笔记', (self.store.root/'review.md').read_text())
        self.assertEqual((self.store.root/'review.md').stat().st_mode & 0o777, 0o600)

    def test_replacements_preserve_receipts_and_unselected_rows_and_are_idempotent(self):
        for target in self.targets: self.candidate(target)
        entry.review(self.store, self.manifest)
        review = self.store.read('approval.enc')
        self.assertTrue(publication.prepare_database(self.store, self.manifest, review, self.remote))
        self.assertFalse(publication.prepare_database(self.store, self.manifest, review, self.remote))
        with sqlite3.connect(self.remote) as conn:
            row = conn.execute('SELECT summary,emailed_at,failure_notified_at,retry_generation FROM lectures WHERE sub_id="1"').fetchone()
            self.assertEqual(row[0], '新完整摘要'); self.assertTrue(row[1]); self.assertEqual(row[2:], ('old-receipt', 3))
            self.assertEqual(conn.execute('SELECT summary FROM lectures WHERE sub_id="9"').fetchone()[0], '不应改变')
            self.assertEqual(conn.execute('SELECT value FROM meta WHERE key="unrelated"').fetchone()[0], 'preserve')

    def test_stale_selected_data_prevents_push(self):
        for target in self.targets: self.candidate(target)
        entry.review(self.store, self.manifest)
        with sqlite3.connect(self.remote) as conn:
            conn.execute('UPDATE lectures SET summary="远端新内容" WHERE sub_id="1"')
        with self.assertRaises(ValueError):
            publication.prepare_database(self.store, self.manifest, self.store.read('approval.enc'), self.remote)

    def test_candidate_or_checkpoint_changed_after_review_is_rejected(self):
        for target in self.targets: self.candidate(target)
        entry.review(self.store, self.manifest)
        state = self.store.read('10-1.enc'); state['review']['new_field'] = True
        self.store.save('10-1.enc', state)
        with self.assertRaises(ValueError):
            publication.prepare_database(self.store, self.manifest, self.store.read('approval.enc'), self.remote)

    def test_short_or_bad_audio_cannot_enter_review(self):
        self.candidate(self.targets[0])
        state = self.store.read('10-1.enc'); state['spec']['audio_diagnostics']['decode_error_counts'] = {'decode_error': 1}
        self.store.save('10-1.enc', state)
        with self.assertRaises(ValueError): runtime.candidate_files(self.store, self.targets[0])

    def test_acquisition_mode_is_frozen_and_checked_at_candidate_boundary(self):
        self.candidate(self.targets[0])
        state = self.store.read('10-1.enc'); state['spec']['requested_audio_acquisition'] = 'aac_auto'
        self.store.save('10-1.enc',state)
        with self.assertRaisesRegex(ValueError,'acquisition mode'):
            runtime.candidate_files(self.store,self.targets[0])
        self.manifest['audio_acquisition']='aac_auto'; self.store.save('manifest.enc',self.manifest)
        with self.assertRaisesRegex(ValueError,'lacks verified AAC'):
            runtime.candidate_files(self.store,self.targets[0])

    def test_resume_rejects_changed_acquisition_mode(self):
        model = self.root/'model'; model.mkdir()
        vad = self.root/'vad.onnx'; atomic(vad,b'vad')
        manifest = dict(self.manifest,model_path=str(model.resolve()),vad_sha256=file_hash(vad),cloud_review=False,
                        session_rules={},audio_acquisition='aac_auto')
        args=SimpleNamespace(model=model,vad_model=vad,cloud_review=False,audio_mode='mp4')
        with patch.dict(os.environ,GITHUB_REPOSITORY=manifest['repository']), \
             patch('scripts.local_history_refresh.source_identity',return_value=manifest['source']), \
             patch('scripts.local_history_refresh.backend_identity',return_value=manifest['backend']):
            with self.assertRaisesRegex(ValueError,'audio acquisition'):
                entry.verify_resume(manifest,args)
            args.audio_mode='aac_auto'; entry.verify_resume(manifest,args)

    def test_cli_default_retains_cloud_review_and_missing_services_block(self):
        with patch.object(entry,'REPO',self.root), patch.dict(os.environ,GITHUB_REPOSITORY='owner/repository'), \
             patch.object(entry,'doctor',return_value=0) as doctor:
            entry.main(['doctor'])
            self.assertTrue(doctor.call_args.args[0].cloud_review)
        args=SimpleNamespace(model=self.root,vad_model=self.root/'vad',run_dir=self.root/'run',
                             cloud_review=True,audio_mode='aac_auto',aligner=self.root/'missing')
        with patch.object(entry.sys,'platform','darwin'), patch('builtins.print') as output:
            self.assertEqual(entry.doctor(args),2)
            result=json.loads(output.call_args.args[0])
            self.assertFalse(result['alignment_model_exists'])

    def test_aligner_weights_and_review_dependencies_are_frozen(self):
        aligner=self.root/'aligner';aligner.mkdir();atomic(aligner/'model.safetensors',b'first')
        args=SimpleNamespace(cloud_review=True,aligner=aligner)
        with patch('scripts.local_history_refresh.importlib.metadata.version',return_value='test'):
            first=entry.review_identity(args)
            atomic(aligner/'model.safetensors',b'second')
            self.assertNotEqual(first,entry.review_identity(args))
            self.assertEqual(first['alignment_model_path'],str(aligner.resolve()))

    def test_full_review_candidate_cannot_use_the_old_local_bypass(self):
        self.candidate(self.targets[0])
        self.manifest.update(cloud_review=True,review_runtime={'alignment_model_path':'/cached'})
        self.store.save('manifest.enc',self.manifest)
        state=self.store.read('10-1.enc')
        state['spec']['review_runtime']=self.manifest['review_runtime']
        state['review']['local_policy']='local_asr_without_cloud_rescue'
        self.store.save('10-1.enc',state)
        with self.assertRaisesRegex(ValueError,'full review pipeline'):
            runtime.candidate_files(self.store,self.targets[0])

    def test_apply_requires_exact_approval_before_remote_access(self):
        with patch('scripts.production_db.load_remote') as load:
            with self.assertRaises(ValueError): publication.apply(self.store, self.manifest, {}, 'bad')
            load.assert_not_called()

    def test_active_campus_workflow_blocks_acquisition(self):
        payload = {'workflow_runs': [{'id': 123, 'path': '.github/workflows/qwen_production_stage.yml'}]}
        with patch('scripts.production_db.command', return_value=json.dumps([payload]).encode()):
            with self.assertRaises(RuntimeError): runtime.check_actions('owner/repo')

    def test_preflight_block_is_visible_and_can_resume_without_retry_failed(self):
        args=SimpleNamespace(hours=1,retry_failed=False)
        with patch('scripts.local_history_refresh.verify_resume'), patch('scripts.local_history_refresh.doctor',return_value=0), \
             patch('scripts.local_history.runtime.process',side_effect=[runtime.PreflightBlocked(['123']),runtime.Paused('test')]) as process:
            self.assertEqual(entry.run(self.store,self.manifest,args),2)
            progress=self.store.read('progress.enc')
            self.assertEqual(progress['status'],'blocked')
            self.assertEqual(progress['lectures']['10-1']['actions'],['123'])
            entry.run(self.store,self.manifest,args)
            self.assertEqual(process.call_count,2)
            self.assertEqual(process.call_args.args[2]['sub_id'],'1')
            self.assertEqual(self.store.read('progress.enc')['status'],'paused')

    def test_explicit_concurrent_login_keeps_actions_and_passes_authorization(self):
        payload = {'workflow_runs': [{'id': 123, 'path': '.github/workflows/history_refresh.yml'}]}
        with patch('scripts.production_db.command', return_value=json.dumps([payload]).encode()) as command:
            self.assertEqual(runtime.check_actions('owner/repo', allow_active=True), ['123'])
            self.assertEqual(command.call_count, 5)
            self.assertTrue(all(call.args[0][:2] == ['gh','api'] for call in command.call_args_list))
        args = SimpleNamespace(hours=1, retry_failed=False, allow_active_actions=True)
        with patch('scripts.local_history_refresh.verify_resume'), patch('scripts.local_history_refresh.doctor',return_value=0), \
             patch('scripts.local_history.runtime.process',side_effect=runtime.Paused('test')) as process:
            entry.run(self.store,self.manifest,args)
            self.assertTrue(process.call_args.kwargs['allow_active_actions'])

    def test_real_encrypted_publication_to_local_bare_git_and_repeat_apply(self):
        from scripts.production_db import command as real_command
        from src.data.sharder import shard_database, load_index, reassemble_database
        bare, checkout = self.root/'remote.git', self.root/'checkout'
        real_command(['git','init','-q','--bare',str(bare)])
        real_command(['git','init','-q',str(checkout)])
        real_command(['git','checkout','-q','-b','data'],cwd=checkout)
        with patch.dict(os.environ, DB_ENCRYPTION_KEY='k'*64, COURSE_IDS='10', GITHUB_REPOSITORY='owner/repository'):
            shard_database(str(self.remote), str(checkout/'data'), 'k'*64)
            real_command(['git','add','data'],cwd=checkout)
            real_command(['git','-c','user.name=test','-c','user.email=test@example.invalid',
                          'commit','-q','-m','initial encrypted data'],cwd=checkout)
            real_command(['git','push',str(bare),'HEAD:refs/heads/data'],cwd=checkout)
            revision = real_command(['git','rev-parse','HEAD'],cwd=checkout).decode().strip()
            self.manifest['baseline_revision'] = revision
            self.store.save('manifest.enc', self.manifest)
            self.store.save_bytes('baseline.db.enc', self.remote.read_bytes())
            for target in self.targets: self.candidate(target)
            entry.review(self.store,self.manifest)
            review = self.store.read('approval.enc')
            url = 'https://github.com/owner/repository.git'
            def local_command(args, **kwargs):
                return real_command([str(bare) if a == url else a for a in args], **kwargs)
            def local_load(path):
                current = real_command(['git','rev-parse','refs/heads/data'],cwd=bare).decode().strip()
                with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                    storage = Path(tmp); (storage/'shards').mkdir()
                    paths = real_command(['git','ls-tree','-r','--name-only',current,'data/'],cwd=bare).decode().splitlines()
                    for name in paths:
                        destination = storage/'icourse-index.enc' if name.endswith('icourse-index.enc') else storage/'shards'/Path(name).name
                        destination.write_bytes(real_command(['git','show',current+':'+name],cwd=bare))
                    reassemble_database(load_index(str(storage/'icourse-index.enc'),'k'*64),str(storage/'shards'),str(path),'k'*64)
                return current
            with patch('scripts.production_db.command', side_effect=local_command), \
                 patch('scripts.production_db.load_remote', side_effect=local_load):
                new_head = publication.apply(self.store,self.manifest,review,policy.digest(review))
                self.assertNotEqual(new_head,revision)
                self.assertEqual(publication.apply(self.store,self.manifest,review,policy.digest(review)),new_head)
            restored = self.root/'readback.db'; self.assertEqual(local_load(restored),new_head)
            with sqlite3.connect(restored) as conn:
                self.assertEqual(conn.execute('SELECT summary FROM lectures WHERE sub_id="1"').fetchone()[0],'新完整摘要')
                self.assertTrue(conn.execute('SELECT emailed_at FROM lectures WHERE sub_id="1"').fetchone()[0])
            self.assertTrue(self.store.exists('backup-'+revision+'.db.enc'))
            self.assertEqual(self.store.read('publication.enc')['status'],'published')

    def test_end_to_end_local_candidate_uses_normal_runner_without_services(self):
        self._normal_runner()

    def test_full_review_calls_alignment_video_vision_and_cloud_rescue(self):
        self._normal_runner(full_review=True)

    def test_local_missing_asr_uses_server_fallback_and_keeps_original_rows(self):
        self._normal_runner(full_review=True,missing_asr=True)

    def test_short_gap_survives_failed_rescue_with_notice_review_and_apply(self):
        self._normal_runner(full_review=True,missing_asr=True,failed_fallback=True,resume=True)
        tag='10-1';state=self.store.read(tag+'.enc')
        self.assertFalse(state['recognition_coverage']['complete'])
        self.assertEqual(state['recognition_coverage']['missing_seconds'],1)
        progress=self.store.read('progress.enc');progress['lectures'][tag]={'status':'complete'}
        self.store.save('progress.enc',progress)
        entry.review(self.store,self.manifest,completed_only=True)
        self.assertIn('属于不完整转录', (self.store.root/'review.md').read_text())
        self.assertTrue(publication.prepare_database(self.store,self.manifest,self.store.read('approval.enc'),self.remote))
        state['recognition_coverage']['missing_seconds']=0;self.store.save(tag+'.enc',state)
        with self.assertRaises(ValueError):runtime.candidate_files(self.store,self.targets[0])

    def test_fifteen_second_gap_stops_before_summary(self):
        with self.assertRaisesRegex(ValueError,'incomplete'):
            self._normal_runner(full_review=True,missing_asr=True,failed_fallback=True,gap_seconds=15)

    def test_automatic_glossary_is_frozen_and_saved_in_candidate(self):
        self._normal_runner(full_review=True,automatic_terms=True)
        state=self.store.read('10-1.enc')
        self.assertEqual(state['spec']['glossary_snapshot']['lecture_date'],self.targets[0]['date'])
        self.assertEqual(state['spec']['glossary_snapshot']['history_revision'],self.manifest['baseline_revision'])
        state['spec']['glossary_snapshot']['terms_sha256']='invalid';self.store.save('10-1.enc',state)
        with self.assertRaisesRegex(ValueError,'glossary'):runtime.candidate_files(self.store,self.targets[0])

    def test_explicit_lecture_selection_never_substitutes_another_lesson(self):
        with patch('src.runtime.config.COURSE_SESSION_RULES',{}),patch('src.runtime.config.COURSE_SESSION_EXCLUSIONS',{}):
            targets,_=entry.select_targets(self.remote,'a'*40,['10'],lecture_ids=['2'])
            self.assertEqual([t['sub_id'] for t in targets],['2'])
            with self.assertRaises(ValueError):entry.select_targets(self.remote,'a'*40,['10'],lecture_ids=['404'])
            with self.assertRaises(ValueError):entry.select_targets(self.remote,'a'*40,['10'],limit=1,lecture_ids=['1','2'])

    def _normal_runner(self, full_review=False,missing_asr=False,failed_fallback=False,gap_seconds=1,automatic_terms=False,resume=False):
        target = self.targets[0]
        audio = self.root/'source.raw'; atomic(audio, b'\0'*(600*16000*4))
        class FakeASR(QwenTranscriber):
            def __init__(self, *args): super().__init__()
            def _init(self): self._model = object()
            def release_model(self): self._model = None
            def _recognize(self, samples, **kwargs):
                text='定义随机变量的分布与期望，计算方差并使用条件概率公式。'*70
                return {'text': text+('这次作业请看第十一页第一题。' if full_review else ''), 'quality_state': 'recognized'}
            def _recognize_resilient(self,samples,block,deadline):
                if missing_asr and block['chunk_id']==0:
                    parts=[{'start':block['start'],'end':block['start']+1,'text':'前文'},
                           {'start':block['start']+1+gap_seconds,'end':block['end'],'text':'后文'}]
                    return {'text':'前文\n后文','quality_state':'missing_audio','recognized_segments':parts,
                            'missing_intervals':[{'start':block['start']+1,'end':block['start']+1+gap_seconds,'error_code':'retry_timeout'}]}
                return super()._recognize_resilient(samples,block,deadline)
        class FakeVAD:
            def __init__(self,*args,**kwargs):self.queue=[]
            def accept_waveform(self,samples):pass
            def empty(self):return not self.queue
            @property
            def front(self):return self.queue[0]
            def pop(self):self.queue.pop(0)
            def flush(self):self.queue.append(SimpleNamespace(start=0,samples=range(239*16000)))
        client = MagicMock()
        client.get_course_detail.return_value = {'title':'概率论','teacher':'教师','lectures':[
            {'sub_id':'1','sub_title':'2026-10-04 第3节','date':'2026-10-04'}]}
        scheduler = MagicMock()
        scheduler.audio_downloader.get.return_value = SimpleNamespace(path=str(audio), process=SimpleNamespace(poll=lambda:0))
        diagnostics = {'audio_seconds':600,'media_seconds':600,'decode_return_code':0,
                       'stderr_complete':True,'decode_error_counts':{},'pcm_sample_aligned':True}
        summarizer = MagicMock(); summarizer.summarize.return_value = ('完整的新笔记','local-test')
        summarizer.summarize_with_keywords.return_value=('完整的新笔记','local-test',[])
        self.manifest['automatic_terms']=automatic_terms;self.store.save('manifest.enc',self.manifest)
        if full_review:
            self.manifest.update(cloud_review=True,review_runtime={'alignment_model_path':'/cached-aligner'})
            self.store.save('manifest.enc',self.manifest)
        client.get_ppt_list.return_value=[]
        client.get_video_url.return_value='https://test.invalid/video'
        client.get_stream_params.return_value=('https://test.invalid/video','')
        def aligned(report,selected,*args,**kwargs):
            c=selected[0];start=round(c['block_start']*1000)
            return ([{'chunk_id':c['id'],'text':c['quote'],'start_ms':start,'end_ms':start+10000,
                      'quote_start_ms':start+1000,'quote_end_ms':start+5000}],[],[],{})
        def rescue(path,key,intervals,**kwargs):
            interval=intervals[0]
            return ([(interval,[{'start_ms':interval['start_ms'],'end_ms':interval['end_ms'],
                                 'text':'作业第11页第1题'}])],kwargs['max_seconds'],False)
        def vision(frames):
            self.assertTrue(frames)
            return [{'status':'ok','text':'第11页第1题','views':[],
                     'references':[{'text':'11页','source':'deepseek_vision','legible':True},
                                   {'text':'第1题','page':11,'source':'deepseek_vision','legible':True}]} for f in frames]
        summarizer.homework_image_reader.return_value=vision
        from src.runtime import config
        with patch('src.api.auth_recovery.initial_authenticated_session'), patch('src.api.icourse.ICourseClient', return_value=client), \
             patch('scripts.local_history.runtime.choose_access_mode',return_value='webvpn'), \
             patch('src.runtime.scheduler.Scheduler', return_value=scheduler), patch('scripts.local_history.backends.MLXTranscriber', FakeASR), \
             patch('src.runtime.audio_preparation.collect_decode_diagnostics', return_value=diagnostics), \
             patch('sherpa_onnx.VoiceActivityDetector',FakeVAD), \
             patch('src.pipeline.ppt_pipeline.PPTPipeline.submit') as ppt, \
             patch('src.ai.summarizer.Summarizer', return_value=summarizer), patch.object(config,'DOUBAO_ASR_API_KEY','test' if full_review else ''), \
             patch('src.ai.qwen_audio_alignment.align_suspects',side_effect=aligned) as align, \
             patch('src.ai.doubao_asr.rescue_intervals_pcm',side_effect=rescue) as cloud, \
             patch('src.ai.qwen_missing_fallback.rescue_intervals_pcm',side_effect=(
                 (lambda *a,**k:([],k['max_seconds'],True)) if failed_fallback else rescue)) as fallback, \
             patch('src.ai.qwen_quality.review_quality',return_value=[]), \
             patch('src.pipeline.homework_visual.video_frame',return_value={'image':b'vision-test','error_code':None}) as frame, \
             patch.object(config,'COURSE_SESSION_RULES',{}), patch.object(config,'COURSE_SESSION_EXCLUSIONS',{}), \
             patch.object(config,'USE_OFFICIAL_TRANSCRIPT',False), \
             patch('scripts.local_history.runtime.check_actions', return_value=[]), \
             patch.dict(os.environ, AUTO_COURSE_TERMS='true' if automatic_terms else 'false'):
            ppt.return_value.drain.return_value = SimpleNamespace(failed=0)
            runtime.process(self.store, self.manifest, target, time.monotonic()+60)
            if resume:
                runtime.process(self.store, self.manifest, target, time.monotonic()+60)
        path, fresh = runtime.candidate_files(self.store, target)
        client.get_transcript_segments.assert_not_called()
        if full_review:
            if not failed_fallback:
                self.assertIn('第11页',fresh['lecture']['summary'])
                self.assertIn('1',fresh['lecture']['summary'])
                self.assertGreater(frame.call_count,0);self.assertGreater(cloud.call_count,0)
                self.assertEqual(align.call_args.kwargs['model_path'],'/cached-aligner')
                summarizer.homework_image_reader.assert_called_once()
            self.assertNotIn('local_policy',self.store.read('10-1.enc')['review'])
            if missing_asr:
                fallback.assert_called_once()
                state=self.store.read('10-1.enc')
                self.assertTrue(state['rows'][0]['missing_intervals'])
                self.assertEqual(bool(state['final_rows'][0]['missing_intervals']),failed_fallback)
                self.assertEqual(state['final_rows'][0]['quality_state'],'missing_audio' if failed_fallback else 'doubao_fallback')
                self.assertEqual(state['review']['attempts'][0]['interval']['kind'],'missing_asr')
        else:self.assertEqual(fresh['lecture']['summary'],'完整的新笔记')
        self.assertIsNone(fresh['lecture']['emailed_at'])
        self.assertTrue(self.store.read('10-1.enc')['review']['complete'])
        if failed_fallback:self.assertIn('1 秒语音未识别',fresh['lecture']['summary'])
        if automatic_terms:
            summarizer.summarize_with_keywords.assert_called_once()
            self.assertTrue(any(m['key'].startswith('auto_glossary:10:1') for m in fresh['meta']))
        with sqlite3.connect(self.remote) as conn:
            self.assertEqual(conn.execute('SELECT summary FROM lectures WHERE sub_id="1"').fetchone()[0], '旧摘要1')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_local_entry_consumes_real_aac_http_decode_and_preserves_audit(self):
        from test_aac_production import AACProductionTests
        from test_aac_ranges import Origin
        from src.runtime import config
        source = AACProductionTests().source(self.root, seconds=3, edited=True)
        self.manifest['audio_acquisition'] = 'aac_auto'
        self.store.save('manifest.enc', self.manifest)
        class FakeASR(QwenTranscriber):
            def __init__(self, *args): super().__init__()
            def _init(self): self._model = object()
            def release_model(self): self._model = None
            def _recognize(self, samples, **kwargs):
                return {'text':'定义随机变量的分布与期望，计算方差并使用条件概率公式。'*10,'quality_state':'recognized'}
            def prepare_pcm_stream(self, *args, audio_path=None, **kwargs):
                duration=Path(audio_path).stat().st_size/64000
                self.last_vad_windows=[[0,duration]]
                return [(0,duration)]
        summarizer=MagicMock(); summarizer.summarize.return_value=('真实AAC接口的模拟笔记','test')
        with Origin(source.read_bytes()) as origin:
            client=origin.client
            client.get_video_url=lambda *args:origin.url
            client.get_course_detail=lambda *args:{'title':'概率论','teacher':'教师','lectures':[
                {'sub_id':'1','sub_title':'2026-10-04 第3节','date':'2026-10-04'}]}
            client.close_media_session=lambda:None
            with patch('src.api.auth_recovery.initial_authenticated_session'), \
                 patch('scripts.local_history.runtime.choose_access_mode',return_value='webvpn'), \
                 patch('src.api.icourse.ICourseClient',return_value=client), \
                 patch('scripts.local_history.backends.MLXTranscriber',FakeASR), \
                 patch('src.pipeline.ppt_pipeline.PPTPipeline.submit') as ppt, \
                 patch('src.ai.summarizer.Summarizer',return_value=summarizer), \
                 patch('scripts.local_history.runtime.check_actions', return_value=[]), \
                 patch.object(config,'AUDIO_DIR',str(self.root/'scratch')), \
                 patch.object(config,'DOUBAO_ASR_API_KEY',''), patch.object(config,'COURSE_SESSION_RULES',{}), \
                 patch.object(config,'COURSE_SESSION_EXCLUSIONS',{}), patch.dict(os.environ,AUTO_COURSE_TERMS='false'):
                ppt.return_value.drain.return_value=SimpleNamespace(failed=0)
                runtime.process(self.store,self.manifest,self.targets[0],time.monotonic()+60)
        state=self.store.read('10-1.enc')
        audit=state['spec']['audio_diagnostics']['source_transport']
        self.assertEqual(audit['mode'],'aac_ranges')
        self.assertEqual(audit['aac_verified_samples'],audit['aac_expected_samples'])
        self.assertTrue(audit['aac_full_packet_coverage'])
        self.assertTrue(audit['aac_timeline_complete'])
        self.assertEqual(state['stage'],'complete')
        _,candidate=runtime.candidate_files(self.store,self.targets[0])
        self.assertEqual(candidate['lecture']['summary'],'真实AAC接口的模拟笔记')
        self.assertIsNone(candidate['lecture']['emailed_at'])


if __name__ == '__main__': unittest.main()
