"""Batch boundaries, checkpoint durability and reuse of real serial rescue logic."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from scripts.local_history.backends import MLXBatchTranscriber, MLXTranscriber
from scripts.local_history import runtime
from test_qwen_production_pipeline import fixture


class BatchBackendTests(unittest.TestCase):
    def setUp(self):
        core=SimpleNamespace(clear_cache=lambda:None)
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
