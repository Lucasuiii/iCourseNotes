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
