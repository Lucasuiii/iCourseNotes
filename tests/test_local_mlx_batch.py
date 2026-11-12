"""Batch boundaries, checkpoint durability and reuse of real serial rescue logic."""
import tempfile
import time
import unittest
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import numpy as np
from scripts.local_history.backends import MLXBatchTranscriber, MLXTranscriber
from scripts.local_history import runtime
from test_qwen_production_pipeline import fixture


class BatchBackendTests(unittest.TestCase):
    def setUp(self):
        core=SimpleNamespace(clear_cache=lambda:None,bfloat16=object())
        self.modules=patch.dict('sys.modules',{'mlx':SimpleNamespace(core=core),'mlx.core':core})
        self.modules.start();self.addCleanup(self.modules.stop)
        self.plan,_=fixture()

    def backend(self, outputs):
        obj=MLXBatchTranscriber('/unused');obj._model=object()
        obj._init=lambda:None
        obj._initial_pair=lambda waves,deadline:outputs
        blocks=self.plan['blocks']
        load=lambda b:np.zeros(b['samples'],dtype='float32')
        return obj,blocks,load

    def test_serial_retries_repeated_or_embedded_context_without_hint(self):
        obj=MLXTranscriber('/unused'); obj._model=object()
        obj.set_terms(['矩阵','行列式','线性方程组','线性相关','线性无关'])
        context='术语：矩阵、行列式、线性方程组、线性相关、线性无关'
        samples=np.arange(160,dtype='float32')
        for text in (context*2, '这里是课堂讲解。'*20+context+'接着继续讲解。'*20):
            with self.subTest(text=text):
                transcribe=MagicMock(side_effect=[SimpleNamespace(text=text,truncated=False),
                                                  SimpleNamespace(text='补救后的完整课堂讲解',truncated=False)])
                with patch.dict('sys.modules',{'mlx_qwen3_asr':SimpleNamespace(transcribe=transcribe)}):
                    row=obj._recognize(samples,deadline=time.monotonic()+60)
                self.assertEqual(transcribe.call_count,2)
                self.assertIs(transcribe.call_args_list[0].args[0],samples)
                self.assertIs(transcribe.call_args_list[1].args[0],samples)
                self.assertEqual(transcribe.call_args_list[0].kwargs['context'],context)
                self.assertEqual(transcribe.call_args_list[1].kwargs['context'],'')
                self.assertEqual(row['text'],'补救后的完整课堂讲解')
                self.assertEqual(row['quality_state'],'recognized')

    def test_serial_unhinted_echo_stays_unresolved(self):
        obj=MLXTranscriber('/unused'); obj._model=object()
        obj.set_terms(['矩阵','行列式','线性方程组','线性相关','线性无关'])
        context='术语：矩阵、行列式、线性方程组、线性相关、线性无关'
        transcribe=MagicMock(return_value=SimpleNamespace(text=context,truncated=False))
        with patch.dict('sys.modules',{'mlx_qwen3_asr':SimpleNamespace(transcribe=transcribe)}):
            row=obj._recognize(np.zeros(160,dtype='float32'),deadline=time.monotonic()+60)
        self.assertEqual(transcribe.call_count,2)
        self.assertEqual(transcribe.call_args_list[1].kwargs['context'],'')
        self.assertEqual(row['quality_state'],'unresolved_context_echo')
        self.assertEqual(row['text'],'')

    def test_one_bad_lane_rescues_without_redecoding_the_good_lane(self):
        obj,blocks,load=self.backend([{'text':'bad','truncated':True},{'text':'正确的行列式说明','truncated':False}])
        obj._rescue_recognize=lambda samples,budget:{'text':'恢复后的原文','quality_state':'recognized'}
        checkpoints=[]
        rows=obj.recognize_blocks(blocks,load,checkpoint=lambda rows:checkpoints.append(list(rows)),keep_model=True)
        self.assertEqual(rows[0]['quality_state'],'bounded_retry')
        self.assertEqual(rows[0]['recognition_attempts'][0]['outcome'],'qwen_token_budget')
        self.assertEqual(len(rows[1]['recognition_attempts']),1)
        self.assertEqual(rows[1]['text'],'正确的行列式说明')
        self.assertEqual([len(v) for v in checkpoints],[1,2])
        self.assertIsNone(obj._prefetched)

    def test_context_echo_uses_the_same_unhinted_serial_adapter(self):
        obj,blocks,load=self.backend([{'text':'术语：矩阵、行列式、秩、线性方程组、线性相关、线性无关','truncated':False},
                                      {'text':'准确的课堂原文','truncated':False}])
        obj.set_terms(['矩阵','行列式','秩','线性方程组','线性相关','线性无关'])
        with patch.object(MLXTranscriber,'_recognize',return_value={'text':'','quality_state':'low_information'}) as retry:
            rows=obj.recognize_blocks(blocks,load,keep_model=True)
        retry.assert_called_once()
        self.assertTrue(retry.call_args.kwargs['unhinted'])
        self.assertIsNotNone(retry.call_args.kwargs['deadline'])
        self.assertTrue(rows[0]['mlx_batch']['context_echo'])
        self.assertEqual(rows[0]['quality_state'],'low_information')
        self.assertEqual(rows[1]['text'],'准确的课堂原文')

    def test_child_echo_forces_full_block_unhinted_rescue(self):
        child={'start_sample':0,'end_sample':480000,'sample_sha256':'a'*64,
               'context_echo':True,'truncated':False}
        obj,blocks,load=self.backend([
            {'text':'拼接后检测不到回显的课堂正文','truncated':False,
             'context_echo':True,'child_chunks':[child]},
            {'text':'准确的课堂原文','truncated':False,'context_echo':False,'child_chunks':[]}])
        obj.set_terms(['矩阵','行列式','线性方程组','线性相关','线性无关'])
        with patch.object(MLXTranscriber,'_recognize',return_value={'text':'补救后的完整原文','quality_state':'recognized'}) as retry:
            rows=obj.recognize_blocks(blocks,load,keep_model=True)
        retry.assert_called_once()
        self.assertEqual(len(retry.call_args.args[0]),blocks[0]['samples'])
        self.assertTrue(retry.call_args.kwargs['unhinted'])
        self.assertTrue(rows[0]['mlx_batch']['context_echo'])
        self.assertEqual(rows[0]['text'],'补救后的完整原文')
        self.assertEqual(rows[1]['text'],'准确的课堂原文')
        self.assertEqual(len(rows[1]['recognition_attempts']),1)

    def test_initial_pair_records_child_boundaries_and_flags(self):
        obj=MLXBatchTranscriber('/unused'); obj._model=object()
        obj.set_terms(['矩阵','行列式','线性方程组','线性相关','线性无关'])
        waves=[np.arange(8,dtype='float32'),np.arange(4,dtype='float32')]
        def split(wave,*args,**kwargs):
            return [(wave[:4],0.0),(wave[4:],4/16000)] if len(wave)>4 else [(wave,0.0)]
        holder=SimpleNamespace(get=lambda path:object())
        modules={'mlx_qwen3_asr':SimpleNamespace(transcribe=lambda *a,**k:SimpleNamespace(text='课堂解释',truncated=True)),
                 'mlx_qwen3_asr.chunking':SimpleNamespace(split_audio_into_chunks=split),
                 'mlx_qwen3_asr.tokenizer':SimpleNamespace(_TokenizerHolder=holder,join_text_parts=lambda parts,language:''.join(parts))}
        outputs=[{'text':'术语：矩阵、行列式、线性方程组、线性相关、线性无关','truncated':False},
                 {'text':'正常讲解','truncated':False}]
        with patch.dict('sys.modules',modules), patch('scripts.local_history.mlx_batch.decode_pair',return_value=outputs):
            result=obj._initial_pair(waves,deadline=time.monotonic()+60)
        self.assertEqual([(c['start_sample'],c['end_sample']) for c in result[0]['child_chunks']],[(0,4),(4,8)])
        self.assertEqual(result[0]['child_chunks'][0]['sample_sha256'],hashlib.sha256(waves[0][:4].tobytes()).hexdigest())
        self.assertTrue(result[0]['child_chunks'][0]['context_echo'])
        self.assertTrue(result[0]['child_chunks'][1]['truncated'])
        self.assertTrue(result[0]['context_echo'])
        self.assertTrue(result[0]['truncated'])
        self.assertFalse(result[1]['context_echo'])

    def test_native_failure_propagates_without_retry(self):
        obj,blocks,load=self.backend([])
        def fail(*args,**kwargs):raise MemoryError('native failure')
        obj._initial_pair=fail
        with self.assertRaises(MemoryError):obj.recognize_blocks(blocks,load,keep_model=True)
        self.assertIsNone(obj._prefetched)

    def test_pair_timeout_enters_existing_bounded_unhinted_rescue(self):
        obj,blocks,load=self.backend([])
        def timeout(*args,**kwargs):raise TimeoutError('pair deadline')
        obj._initial_pair=timeout
        obj._rescue_recognize=lambda samples,budget:{'text':'补救成功','quality_state':'recognized'}
        rows=obj.recognize_blocks(blocks,load,keep_model=True)
        self.assertTrue(all(r['mlx_batch']['timeout'] for r in rows))
        self.assertTrue(all(r['recognition_attempts'][0]['outcome']=='retry_timeout' for r in rows))
        self.assertTrue(all(r['quality_state']=='bounded_retry' for r in rows))

    def test_resume_saves_first_lane_even_when_second_lane_interrupts(self):
        plan=self.plan
        with tempfile.TemporaryDirectory() as tmp:
            audio=Path(tmp)/'audio.raw';audio.write_bytes(b'\0'*(600*16000*4))
            class Interrupted:
                batch_size=2
                def set_terms(self,terms):pass
                def release_model(self):self.released=True
                def recognize_blocks(self,blocks,load,**kwargs):
                    row=dict(blocks[0],text='已完成的第一块',quality_state='recognized')
                    kwargs['checkpoint']([row])
                    raise KeyboardInterrupt()
            obj=Interrupted();saved=[]
            with self.assertRaises(KeyboardInterrupt):
                runtime.transcribe_pending(obj,plan,audio,[],lambda r:saved.append(list(r)),time.monotonic()+60)
            self.assertTrue(obj.released)
            self.assertEqual([r['chunk_id'] for r in saved[-1]],[0])
            class Resume(Interrupted):
                def recognize_blocks(self,blocks,load,**kwargs):
                    self.ids=[b['chunk_id'] for b in blocks]
                    return [dict(b,text='尾块',quality_state='recognized') for b in blocks]
            resumed=Resume()
            result=runtime.transcribe_pending(resumed,plan,audio,saved[-1],lambda r:None,time.monotonic()+60)
            self.assertEqual(resumed.ids,[1]);self.assertEqual(len(result),2)

    def test_foreign_lane_cannot_enter_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio=Path(tmp)/'audio.raw';audio.write_bytes(b'\0'*(600*16000*4))
            class Wrong:
                batch_size=2
                def set_terms(self,terms):pass
                def release_model(self):pass
                def recognize_blocks(self,blocks,load,**kwargs):
                    return [dict(blocks[0],chunk_id=99,text='bad')]
            with self.assertRaises(ValueError):
                runtime.transcribe_pending(Wrong(),self.plan,audio,[],lambda r:self.fail('saved invalid row'),time.monotonic()+60)


if __name__=='__main__':unittest.main()
