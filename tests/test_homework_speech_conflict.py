"""Board readings that disagree with the lecture transcript are not corroborated (38404/747288)."""
import unittest
from src.ai.homework_visual_evidence import (apply_speech_conflicts, candidate_key, spoken_numbers,
                                             spoken_subitems)

QUOTE = '这边还有题啊'
CANDIDATE = {'id': 60, 'quote': QUOTE, 'block_start': 6300, 'block_end': 6425}
KEY = candidate_key(60, QUOTE)


def ref(text, page=None):
    return {'candidate_id': KEY, 'text': text, 'page': page, 'seconds': [6309, 6480],
            'multi_frame_agreement': True, 'audio_agreement': False, 'audio_conflict': False,
            'supported': True, 'tentative': False}


def visual(*refs):
    return {'status': 'references_supported', 'reference_status': 'supported', 'reference_evidence': list(refs)}


class SpeechConflictTests(unittest.TestCase):
    def test_spoken_numerals_and_subitems(self):
        text = '来，一百呃一百五十八，呃第一题的第二题，呃第二题的第二小题，然后是。'
        self.assertEqual(spoken_numbers(text), [100, 158, 1, 2, 2, 2])
        self.assertEqual(spoken_subitems(text), {(1, 2), (2, 2)})
        self.assertEqual(spoken_numbers('一百零二页，十一，二十三'), [102, 11, 23])

    def test_dropped_leading_digit_and_wrong_subitem_are_flagged(self):
        chunks = [{'start': 6190, 'end': 6300, 'text': '来，一百呃一百五十八，呃第一题的第二题，呃第二题的第二小题，然后是。'}]
        out = apply_speech_conflicts(visual(ref('58页'), ref('第1(2)题', 58), ref('第2(3)题', 58)), chunks, [CANDIDATE])
        by_text = {e['text']: e for e in out['reference_evidence']}
        self.assertEqual(by_text['58页']['speech_conflict'], {'spoken': '158页'})
        self.assertEqual(by_text['第2(3)题']['speech_conflict'], {'spoken': '第2(2)题'})
        self.assertEqual(by_text['第1(2)题']['speech_conflict'], {'spoken': '158页'})
        self.assertTrue(all(e['audio_conflict'] and not e['supported'] for e in by_text.values()))
        self.assertEqual((out['status'], out['reference_status']), ('needs_verification', 'unverified'))

    def test_agreeing_or_unrelated_speech_changes_nothing(self):
        board = visual(ref('158页'), ref('第2(2)题', 158))
        same = [{'start': 6200, 'end': 6300, 'text': '一百五十八页，第二题的第二小题'}]
        self.assertIs(apply_speech_conflicts(board, same, [CANDIDATE]), board)
        other = [{'start': 6200, 'end': 6300, 'text': '第三十八个例子，二十五分钟'}]
        board = visual(ref('58页'))
        self.assertIs(apply_speech_conflicts(board, other, [CANDIDATE]), board)
        far = [{'start': 100, 'end': 200, 'text': '一百五十八'}]
        self.assertIs(apply_speech_conflicts(board, far, [CANDIDATE]), board)


if __name__ == '__main__':
    unittest.main()
