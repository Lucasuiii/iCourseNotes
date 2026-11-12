import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from scripts.qwen_quality import context_echo,select_rescue_windows,review_quality,usable_ppt,locate_suspects,low_information,bounded_retry


class QualityTests(unittest.TestCase):
    def test_only_fillers_suppressed(self):
        for text in ('', ' 。 ', '嗯。', '啊！', '呵呵。', '嗯，啊，哎'):
            self.assertTrue(low_information(text))
        for text in ('矩阵', '我们。', '零', 'A', '嗯，这个矩阵', '啊乘上B'):
            self.assertFalse(low_information(text))

    def test_retry_deadline_and_restore(self):
        class Backend:
            def generate(self, **kwargs):
                return kwargs
        backend = Backend()
        model = SimpleNamespace(model=backend, max_new_tokens=2048)
        now = [10.0]
        with bounded_retry(model, list, clock=lambda: now[0]) as state:
            self.assertEqual(model.max_new_tokens, 256)
            old = lambda *a, **kw: False
            criteria = backend.generate(stopping_criteria=[old])['stopping_criteria']
            self.assertIs(criteria[0], old)
            self.assertFalse(criteria[1](None, None))
            now[0] = 70
            self.assertTrue(criteria[1](None, None))
            self.assertTrue(state['timed_out'])
        self.assertEqual(model.max_new_tokens, 2048)
        self.assertNotIn('generate', backend.__dict__)
        with self.assertRaises(RuntimeError):
            with bounded_retry(model, list):
                raise RuntimeError('test')
        self.assertEqual(model.max_new_tokens, 2048)
        self.assertNotIn('generate', backend.__dict__)

    def test_echo_is_not_single_term(self):
        context='数值算法。术语：良态问题、病态问题、扰动、Hilbert矩阵、逆矩阵、条件数、delta、范数。'
        self.assertTrue(context_echo(context,context))
        self.assertTrue(context_echo(context.split('术语：')[1],context))
        self.assertFalse(context_echo('希尔伯特矩阵是病态矩阵。',context))
        self.assertFalse(context_echo('',context))

    def test_echo_inside_long_or_repeated_output(self):
        context='术语：良态问题、病态问题、扰动、Hilbert矩阵、逆矩阵、条件数、范数'
        for repetitions in (2, 3):
            with self.subTest(repetitions=repetitions):
                self.assertTrue(context_echo(context * repetitions, context))
        body='我们先讨论这个例子的推导过程，然后再看误差如何传播。' * 8
        self.assertTrue(context_echo(body + context + body, context))
        self.assertTrue(context_echo('良态问题、病态问题、扰动、Hilbert矩阵、逆矩阵、条件数、范数', context))
        self.assertTrue(context_echo(body + '病态问题、扰动、Hilbert矩阵、逆矩阵、条件数' + body, context))
        self.assertFalse(context_echo('良态问题和病态问题的区别是，扰动很小时，逆矩阵和条件数可以帮助我们理解误差。', context))
        self.assertFalse(context_echo(context, ''))
        self.assertFalse(context_echo('这个矩阵是可逆矩阵。', '矩阵'))

    def test_cloud_budget_and_actual_windows(self):
        chunks=[{'start':0,'end':122},{'start':120,'end':242},{'start':240,'end':360}]
        windows=[(i,i+20) for i in range(0,360,25)]
        result=select_rescue_windows(chunks,[0,1,2],windows)
        self.assertLessEqual(sum(x['end_ms']-x['start_ms'] for x in result),120000)
        self.assertLessEqual(len(result),10)
        self.assertTrue(all(x['end_ms']-x['start_ms']<=60000 for x in result))
        self.assertEqual({x['chunk_id'] for x in result},{0,1,2})
        self.assertEqual(len(result),3)
        for x in result:
            chunk=chunks[x['chunk_id']]
            self.assertTrue(chunk['start']*1000<=x['start_ms']<x['end_ms']<=chunk['end']*1000)
            self.assertTrue(any(a*1000<x['end_ms'] and x['start_ms']<b*1000 for a,b in windows))
        self.assertEqual(select_rescue_windows(chunks,[0],[]),[])

    def test_long_silence_not_bridged(self):
        r=select_rescue_windows([{'start':0,'end':120}],[0],[(0,10),(90,100)])
        self.assertTrue(all(x['end_ms']-x['start_ms']<=10000 for x in r))

    def test_desktop_and_stale_page_filtered(self):
        self.assertFalse(usable_ppt('此电脑 回收站 巡检.exe 多媒体值班室',0))
        self.assertFalse(usable_ppt('矩阵逆与条件数',-5220))
        self.assertTrue(usable_ppt('矩阵逆与条件数',-100))

    def test_review_rejects_invented_ids_and_preserves_text(self):
        client=MagicMock()
        client.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({'suspects':[
                {'id':True,'reason':'bad'},{'id':99,'reason':'bad'},
                {'id':0,'reason':'虚构引用','quote':'这个引用并不存在于字幕'},
                {'id':0,'reason':'术语不通顺','quote':'这个水路误差需要分析'},
                {'id':0,'reason':'duplicate'}]})))])
        # Valid entry must be within the four-item response cap.
        r={'full_chunks':[{'start':0,'end':120,'text':'这个水路误差需要分析'}]}
        self.assertEqual(review_quality(client,'model',r,{}),[{'id':0,'reason':'术语不通顺','quote':'这个水路误差需要分析'}])
        self.assertEqual(r['full_chunks'][0]['text'],'这个水路误差需要分析')
        client.chat.completions.create.assert_called_once()

    def test_quote_anchor_selects_end_not_middle(self):
        quote='我们讨论这个舍入误差的条件'
        chunks=[{'start':0,'end':120,'text':'开始讲课。'+quote}]
        suspects=[{'id':0,'quote':quote,'reason':'疑点'}]
        refs=[{'start':100,'end':108,'text':quote}]
        intervals,located,unresolved=locate_suspects(chunks,suspects,refs,[(99,110)])
        self.assertEqual(len(located),1)
        self.assertEqual(unresolved,[])
        self.assertEqual(intervals[0]['start_ms'],97000)
        self.assertEqual(intervals[0]['end_ms'],111000)

    def test_missing_repeated_and_ambiguous_anchor_skip_upload(self):
        quote='我们讨论这个舍入误差的条件'
        chunk={'start':0,'end':120,'text':quote}
        suspects=[{'id':0,'quote':quote}]
        refs=[{'start':10,'end':18,'text':quote},{'start':90,'end':98,'text':quote}]
        for chunks,subtitles,vad in (([chunk],[],[(0,120)]),
                                    ([{**chunk,'text':quote+quote}],refs,[(0,120)]),
                                    ([chunk],refs,[(0,120)]),
                                    ([chunk],refs[:1],[])):
            intervals,_,unresolved=locate_suspects(chunks,suspects,subtitles,vad)
            self.assertEqual(intervals,[])
            self.assertEqual(len(unresolved),1)

    def test_mild_transcription_difference_can_anchor(self):
        chunks=[{'start':0,'end':120,'text':'这里讨论这个水路误差怎么分析'}]
        suspects=[{'id':0,'quote':chunks[0]['text']}]
        refs=[{'start':30,'end':40,'text':'这里讨论这个舍入误差怎么分析'}]
        intervals,_,_=locate_suspects(chunks,suspects,refs,[(30,40)])
        self.assertEqual(len(intervals),1)

    def test_budget_does_not_cut_out_suspect(self):
        quote='这里讨论这个舍入误差怎么分析'
        intervals,_,unresolved=locate_suspects([{'start':0,'end':120,'text':quote}],
                         [{'id':0,'quote':quote}],[{'start':30,'end':40,'text':quote}],[(30,40)],budget=5)
        self.assertEqual(intervals,[])
        self.assertEqual(unresolved[0]['state'],'anchor_exceeds_budget_share')


if __name__=='__main__':
    unittest.main()
