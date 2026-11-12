"""The local and cloud finalizers share source scope and transcript assembly."""
import unittest
from src.pipeline.prepared_lecture import finalization_material, assemble_material
from test_qwen_production_pipeline import fixture


class PreparedMaterialPolicyTests(unittest.TestCase):
    def test_frozen_scope_support_and_alignment_are_carried_together(self):
        plan,results=fixture()
        specification={'plan':plan,'media_seconds':600,'review_scope':'theory',
            'official_support':[{'text':'辅助字幕','start':0,'end':1}],
            'review_runtime':{'alignment_model_path':'/fixture/aligner'}}
        material=finalization_material(specification,results,audio_path='/fixture/audio.raw')
        self.assertEqual(material['transcript'],assemble_material(plan,results,media_seconds=600)['transcript'])
        self.assertEqual(material['review_scope'],'theory')
        self.assertEqual(material['official_support'],specification['official_support'])
        self.assertEqual(material['alignment_model_path'],'/fixture/aligner')
        self.assertEqual(material['audio_path'],'/fixture/audio.raw')

    def test_old_scope_resolution_is_preserved_for_saved_ledgers(self):
        plan,results=fixture()
        material=finalization_material({'plan':plan,'media_seconds':600},results)
        self.assertNotIn('review_scope',material)
        self.assertEqual(material['official_support'],[])
