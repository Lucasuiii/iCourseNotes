"""Identical content-quality decisions through CPU, MLX and batch entry points."""
import copy
import time
import unittest
from unittest.mock import MagicMock

import numpy as np
from src.ai.qwen_transcriber import QwenTranscriber, NORMAL_TOKENS, UNHINTED_TOKENS
from src.ai.recognition_policy import MAX_NEW_TOKENS, normalize_terms
from src.ai.qwen_segmentation import split_inference_audio
from scripts.local_history.backends import MLXTranscriber, MLXBatchTranscriber


class RecognitionPolicyTests(unittest.TestCase):
    def backends(self):
        return (QwenTranscriber(), MLXTranscriber('/unused'), MLXBatchTranscriber('/unused'))

    def test_same_prompt_recovery_and_full_input_for_every_backend(self):
        terms=['矩阵','行列式','秩','线性方程组','线性相关','线性无关']
        context='术语：'+'、'.join(terms)
        samples=np.zeros(16000,dtype='float32')
        for backend in self.backends():
            with self.subTest(backend=type(backend).__name__):
                backend.set_terms(terms)
                backend._decode=MagicMock(side_effect=[{'text':'讲解。'*100+context*3}, {'text':'线性无关的定义。'}])
                deadline=time.monotonic()+600
                row=backend._recognize(samples,deadline=deadline)
                self.assertEqual(row['text'],'线性无关的定义。')
                self.assertEqual(row['quality_state'],'unhinted_retry')
                self.assertTrue(row['context_echo_recovery']['detected'])
                first,second=backend._decode.call_args_list
                self.assertIs(first.args[0],samples);self.assertIs(second.args[0],samples)
                self.assertEqual(first.kwargs['context'],context)
                self.assertEqual(second.kwargs['context'],'')
                self.assertEqual(first.kwargs['deadline'],second.kwargs['deadline'])
                self.assertEqual(second.kwargs['phase'],'unhinted_echo_retry')
        self.assertEqual(NORMAL_TOKENS,MAX_NEW_TOKENS)
        self.assertEqual(UNHINTED_TOKENS,MAX_NEW_TOKENS)

    def test_unhinted_and_repeated_echo_remain_explicit_failures(self):
        terms=['矩阵','条件数','扰动','范数','逆矩阵']
        context='术语：'+'、'.join(terms)
        for backend in self.backends():
            backend.set_terms(terms)
            for unhinted in (False,True):
                with self.subTest(backend=type(backend).__name__,unhinted=unhinted):
                    backend._decode=MagicMock(return_value={'text':context*2})
                    row=backend._recognize([],unhinted=unhinted)
                    self.assertEqual(row['text'],'')
                    self.assertEqual(row['quality_state'],'unresolved_context_echo')
                    self.assertEqual(backend._decode.call_count,1 if unhinted else 2)

    def test_child_flag_and_generation_audit_survive_shared_recovery(self):
        for backend in self.backends():
            backend.set_terms(['矩阵'])
            before={'text':'局部回显被子块检查标记','context_echo':True,
                    'generation_diagnostics':[{'generation_phase':'normal'}]}
            after={'text':'矩阵','generation_diagnostics':[{'generation_phase':'unhinted_echo_retry'}]}
            backend._decode=MagicMock(side_effect=copy.deepcopy([before,after]))
            row=backend._recognize([])
            self.assertEqual(row['text'],'矩阵')
            self.assertEqual(len(row['generation_diagnostics']),2)

    def test_deadline_never_accepts_partial_or_starts_expired_decode(self):
        for backend in self.backends():
            backend._decode=MagicMock(return_value={'text':'部分结果','quality_state':'retry_timeout'})
            self.assertEqual(backend._recognize([],deadline=time.monotonic()-1)['quality_state'],'retry_timeout')
            backend._decode.assert_not_called()
            self.assertEqual(backend._recognize([])['text'],'')

    def test_native_clips_cover_every_original_sample_and_use_shared_checks(self):
        samples=np.arange(62*16000,dtype='float32')
        clips=split_inference_audio(samples)
        self.assertTrue(all(len(clip)<=30*16000 for clip,start in clips))
        self.assertEqual([start for clip,start in clips],list(np.cumsum([0]+[len(clip) for clip,start in clips[:-1]])))
        np.testing.assert_array_equal(np.concatenate([clip for clip,start in clips]),samples)
        for backend in self.backends()[:2]:
            backend.set_terms(['矩阵','条件数','扰动','范数','逆矩阵'])
            context='术语：矩阵、条件数、扰动、范数、逆矩阵'
            backend._decode_chunk=MagicMock(return_value={'text':context})
            row=backend._recognize(samples,unhinted=True)
            self.assertEqual(row['quality_state'],'unresolved_context_echo')
            self.assertEqual(backend._decode_chunk.call_count,len(clips))
            self.assertEqual([len(call.args[0]) for call in backend._decode_chunk.call_args_list],
                             [len(clip) for clip,start in clips])
            self.assertEqual(row['child_chunks'][-1]['end_sample'],len(samples))
            self.assertTrue(all(child['context_echo'] for child in row['child_chunks']))

    def test_terms_are_normalized_identically_without_changing_order(self):
        values=[' 矩阵 ','矩阵','',None,'秩']+[f'术语{i}' for i in range(40)]
        expected=normalize_terms(values)
        self.assertEqual(expected[:2],['矩阵','秩']);self.assertEqual(len(expected),30)
        for backend in self.backends():
            backend.set_terms(values)
            self.assertEqual(backend._terms,expected)
