"""Whole-lecture tolerance preserves gaps, source identity and audio gates."""
import copy
import unittest
from src.pipeline.prepared_lecture import assemble_material, validate_material, validate_audio_duration
from src.pipeline.recognition_coverage import recognition_coverage
from test_qwen_production_pipeline import fixture


def with_gaps(spans):
    plan, results = fixture()
    for index, gaps in spans.items():
        row = next(r for result in results for r in result['chunks'] if r['chunk_id'] == index)
        parts = []; position = row['start']
        for start, end in gaps:
            if start > position:
                parts.append(dict(start=position, end=start, text='已识别课堂内容。'))
            position = end
        if position < row['end']:
            parts.append(dict(start=position, end=row['end'], text='后续课堂内容。'))
        row.update(text='\n'.join(p['text'] for p in parts), recognized_segments=parts,
            quality_state='missing_audio', missing_intervals=[dict(start=a,end=b,error_code='retry_timeout') for a,b in gaps])
    for result in results:
        result['complete'] = not any(r.get('missing_intervals') for r in result['chunks'])
    return plan, results


class ShortRecognitionGapTests(unittest.TestCase):
    def test_real_trial_sized_gap_accepted_without_claiming_complete_or_mutating_worker(self):
        plan, results = with_gaps({0:[(21.195,28.695)]}); original = copy.deepcopy(results)
        material = assemble_material(plan,results,media_seconds=600,allow_short_missing=True)
        self.assertFalse(material['complete']);self.assertTrue(material['recognition_coverage']['accepted'])
        self.assertEqual(material['recognition_coverage']['missing_seconds'],7.5)
        self.assertEqual(results,original)
        self.assertEqual([(s['start_ms'],s['end_ms']) for s in material['segments'][:2]],[(0,21195),(28695,120000)])
        self.assertEqual(material['full_chunks'][0]['missing_intervals'],results[0]['chunks'][0]['missing_intervals'])
        with self.assertRaises(ValueError):assemble_material(plan,results)

    def test_strictly_less_than_fifteen_and_whole_lecture_total(self):
        for spans, allowed in [({0:[(10,24.999)]},True),({0:[(10,25)]},False),
                ({0:[(10,25.001)]},False),({0:[(10,18)],1:[(130,138)]},False),
                ({0:[(10,17)],1:[(130,137.5)]},True)]:
            with self.subTest(spans=spans):
                plan, results=with_gaps(spans)
                if allowed:assemble_material(plan,results,allow_short_missing=True)
                else:
                    with self.assertRaisesRegex(ValueError,'incomplete'):assemble_material(plan,results,allow_short_missing=True)

    def test_overlap_counted_once_and_no_sample_rounding_false_pass(self):
        plan,results=with_gaps({0:[(119,120)],1:[(119,120)]})
        material=assemble_material(plan,results,allow_short_missing=True)
        self.assertEqual(material['recognition_coverage']['missing_seconds'],1)
        plan,results=with_gaps({0:[(10.000001,25)]})
        with self.assertRaises(ValueError):assemble_material(plan,results,allow_short_missing=True)

    def test_missing_shard_missing_block_and_false_worker_success_still_rejected(self):
        plan,results=with_gaps({0:[(10,11)]})
        for invalid in (results[:1], [dict(results[0],chunks=[]),results[1]],
                [dict(results[0],complete=True),results[1]]):
            with self.assertRaises(ValueError):assemble_material(plan,invalid,allow_short_missing=True)

    def test_cached_material_cannot_hide_increase_or_forge_coverage(self):
        plan,results=with_gaps({0:[(10,11)]})
        material=assemble_material(plan,results,allow_short_missing=True)
        validate_material(material,'10','1')
        for mutate in ('erase','change','claim_complete'):
            bad=copy.deepcopy(material)
            if mutate=='erase':bad.pop('recognition_coverage')
            elif mutate=='change':bad['recognition_coverage']['missing_seconds']=0
            else:bad['complete']=True
            with self.assertRaises(ValueError):validate_material(bad,'10','1')

    def test_unaligned_partial_text_cannot_bypass_gate(self):
        plan,results=with_gaps({0:[(10,11)]});results[0]['chunks'][0]['recognized_segments']=[]
        with self.assertRaisesRegex(ValueError,'aligned'):assemble_material(plan,results,allow_short_missing=True)

    def test_audio_duration_gate_remains_strict(self):
        with self.assertRaises(ValueError):validate_audio_duration(100,600)
        plan,results=with_gaps({0:[(10,11)]})
        with self.assertRaisesRegex(ValueError,'audio'):assemble_material(plan,results,media_seconds=1800,allow_short_missing=True)


def lecture_rows(gaps_by_block, *, blocks=80, seconds=120.0):
    """A long lecture: each listed block keeps text around one missing interval."""
    rows = []
    for n in range(blocks):
        start, end = n*seconds, (n+1)*seconds
        row = dict(chunk_id=n, start=start, end=end, text='课堂内容。', quality_state='recognized')
        if n in gaps_by_block:
            a, b = start+gaps_by_block[n][0], start+gaps_by_block[n][1]
            parts = [p for p in (dict(start=start, end=a, text='前文。'), dict(start=b, end=end, text='后文。'))
                     if p['end'] > p['start']]
            row.update(text='\n'.join(p['text'] for p in parts), recognized_segments=parts,
                       quality_state='missing_audio', missing_intervals=[dict(start=a, end=b, error_code='retry_timeout')])
        rows.append(row)
    return rows


class LimitedBlockGapTests(unittest.TestCase):
    def test_few_failed_blocks_in_a_long_lecture_reach_the_summary(self):
        from src.pipeline.recognition_coverage import LIMITED_GAP_POLICY, missing_recognition_notice
        # 9/16 shape: start of class, the break and its neighbour, 178 s in a 9600 s lecture.
        coverage = recognition_coverage(lecture_rows({0: (0, 87.7), 29: (5, 65), 30: (0, 30.3)}), allow_short_missing=True)
        self.assertTrue(coverage['accepted']); self.assertFalse(coverage['complete'])
        self.assertEqual((coverage['policy'], coverage['gap_blocks'], coverage['limit_seconds']), (LIMITED_GAP_POLICY, 3, 288.0))
        self.assertIn('178 秒语音未识别', missing_recognition_notice(coverage))

    def test_more_blocks_or_more_time_still_stop_before_summary(self):
        for gaps in ({0: (0, 20), 10: (0, 20), 20: (0, 20), 30: (0, 20)},      # four blocks
                     {0: (0, 120), 1: (0, 120), 2: (0, 60)},                   # 300 s > 3 % of 9600 s
                     ):
            with self.subTest(gaps=gaps):
                self.assertFalse(recognition_coverage(lecture_rows(gaps), allow_short_missing=True)['accepted'])
        self.assertFalse(recognition_coverage(lecture_rows({0: (0, 20)}))['accepted'])  # strict callers unchanged

    def test_absolute_cap_and_short_rule_still_apply(self):
        long = lecture_rows({0: (0, 120), 1: (0, 120), 2: (0, 70)}, blocks=200)   # 310 s of 24000 s
        self.assertFalse(recognition_coverage(long, allow_short_missing=True)['accepted'])
        many = lecture_rows({0: (0, 3), 10: (0, 3), 20: (0, 3), 30: (0, 3)})       # 12 s in four blocks
        self.assertTrue(recognition_coverage(many, allow_short_missing=True)['accepted'])

    def test_stored_reports_are_rechecked_under_their_own_policy(self):
        from src.pipeline.recognition_coverage import validate_coverage_report, SHORT_GAP_POLICY
        rows = lecture_rows({0: (0, 60), 29: (0, 40)})
        plan = {'blocks': [dict(chunk_id=r['chunk_id'], start=r['start'], end=r['end']) for r in rows]}
        current = recognition_coverage(rows, allow_short_missing=True)
        self.assertEqual(validate_coverage_report(plan, copy.deepcopy(current)), current)
        old = recognition_coverage(rows, policy=SHORT_GAP_POLICY)
        self.assertFalse(old['accepted'])
        with self.assertRaises(ValueError):
            validate_coverage_report(plan, dict(old, accepted=True))
        forged = dict(current, gap_blocks=1)
        with self.assertRaises(ValueError):
            validate_coverage_report(plan, forged)
