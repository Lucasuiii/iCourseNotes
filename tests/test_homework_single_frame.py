"""Single-frame hints remain visible without becoming confirmed assignments."""
import copy
import unittest

from src.ai.homework_review import ensure_homework_notice, homework_prompt
from src.ai.homework_visual_evidence import assess_visual


def evidence(*, texts=('25页', '第1(1)(3)题'), legible=True, times=(100,)):
    frames = [{'candidate_id': 'cue', 'seconds': time,
               'references': [{'text': text, 'page': 25 if text.endswith('题') else None,
                               'source': 'deepseek_vision', 'legible': legible}
                              for text in texts]} for time in times]
    return {'candidates': [{}], 'visual': assess_visual({'candidate_ids': ['cue'], 'frames': frames})}


class SingleFrameHomeworkTests(unittest.TestCase):
    def test_clear_single_frame_keeps_page_and_subquestions_as_tentative(self):
        saved = evidence(); before = copy.deepcopy(saved)
        self.assertEqual(saved['visual']['reference_status'], 'unverified')
        self.assertTrue(all(r['tentative'] and not r['supported']
                            for r in saved['visual']['reference_evidence']))
        prompt = homework_prompt(saved)
        self.assertIn('tentative=true', prompt); self.assertIn('存疑、待核实', prompt)
        source = '### 课程事项提醒\n\n作业按助教通知核对。\n\n### 随机变量\n\n定义。'
        result = ensure_homework_notice(source, saved)
        self.assertIn('画面线索（存疑）：第25页：第1(1)(3)题', result)
        self.assertIn('是否为本次作业待核实', result)
        self.assertNotIn('板书列出的练习', result)
        self.assertLess(result.index('画面线索'), result.index('### 随机变量'))
        self.assertEqual(ensure_homework_notice(result, saved), result)
        self.assertEqual(saved, before)

    def test_model_already_lists_tentative_item_without_duplicate(self):
        saved = evidence()
        source = '### 作业与课务\n\n第25页第1（1）（3）题（存疑，待核实）。'
        self.assertEqual(ensure_homework_notice(source, saved), source)
        for text in ('第26页第1(1)(3)题（存疑）。', '第25页第11题（待核实）。',
                     '第25页第1题（待核实）。', '第25页第1(1)(3)题。'):
            result = ensure_homework_notice('### 作业与课务\n\n' + text, saved)
            self.assertIn('画面线索（存疑）：第25页：第1(1)(3)题', result)
            self.assertEqual(ensure_homework_notice(result, saved), result)

    def test_page_only_and_unknown_page_never_invent_missing_fields(self):
        page = ensure_homework_notice('正文', evidence(texts=('25页',)))
        self.assertIn('画面线索（存疑）：第25页。', page)
        self.assertNotIn('第1题', page)
        saved = evidence(texts=('第1题',))
        saved['visual']['reference_evidence'][0]['page'] = None
        item = ensure_homework_notice('正文', saved)
        self.assertIn('画面线索（存疑）：第1题。', item)
        self.assertNotIn('第25页', item)
        self.assertEqual(ensure_homework_notice(item, saved), item)

    def test_unclear_legacy_unassessed_and_conflicting_refs_are_not_hints(self):
        source = '### 课程事项提醒\n\n作业请核对。'
        self.assertEqual(ensure_homework_notice(source, evidence(legible=False)), source)
        saved = evidence()
        for ref in saved['visual']['reference_evidence']: ref.pop('tentative')
        self.assertEqual(ensure_homework_notice(source, saved), source)
        saved = evidence()
        saved['visual'] = assess_visual(saved['visual'], [{'status': 'complete',
            'candidate_id': 'cue', 'cloud_text': '第26页第2题'}])
        self.assertTrue(all(r['audio_conflict'] and not r['tentative']
                            for r in saved['visual']['reference_evidence']))
        self.assertEqual(ensure_homework_notice(source, saved), source)

    def test_repeated_same_instant_cannot_upgrade_single_frame_confidence(self):
        saved = evidence(times=(100, 100, 100))
        for ref in saved['visual']['reference_evidence']:
            self.assertTrue(ref['tentative']); self.assertFalse(ref['supported'])
        supported = evidence(times=(100, 110))
        result = ensure_homework_notice('正文', supported)
        self.assertIn('板书列出的练习：第25页：1(1)(3)', result)
        self.assertNotIn('存疑', result)

    def test_supported_item_elsewhere_is_not_duplicated_as_a_tentative_hint(self):
        saved = evidence()
        saved['visual']['reference_evidence'].append({
            'text': '第1(1)(3)题', 'page': 25, 'supported': True, 'audio_conflict': False})
        result = ensure_homework_notice('正文', saved)
        self.assertIn('板书列出的练习：第25页：1(1)(3)', result)
        self.assertNotIn('存疑', result)


if __name__ == '__main__':
    unittest.main()
