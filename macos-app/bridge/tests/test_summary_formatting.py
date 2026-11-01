"""Format overlays cannot cross lectures, mask changed prose, or alter figures."""
import copy, hashlib, importlib.util, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('bridge',Path(__file__).parents[1]/'app_bridge.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
class FormattingTests(unittest.TestCase):
    def setUp(self):
        base='### 递推\n\nT(n) = O(n)\n\n![原图](#icourse-figure-'+'a'*64+')'
        final=base.replace('T(n) = O(n)', '$$\nT(n)=O(n)\n$$')
        sha=lambda s:hashlib.sha256(s.encode()).hexdigest()
        self.a={'schema':1,'kind':'summary_formatting','course_id':'1','sub_id':'2','date':'2026-10-30','model_calls':0,'figures_unchanged':True,'base_summary':base,'summary':final,'base_summary_sha256':sha(base),'summary_sha256':sha(final),'edits':[{'before':'T(n) = O(n)','after':'$$\nT(n)=O(n)\n$$'}]}
    def apply(self,a=None,summary=None,sub='2'):
        return bridge.apply_summary_formatting(summary or self.a['base_summary'],a or self.a,'1',sub,'2026-10-30')
    def test_exact_base_and_idempotent(self):
        self.assertEqual(self.apply(),self.a['summary']);self.assertEqual(self.apply(summary=self.a['summary']),self.a['summary'])
    def test_stale_base_rejected(self):
        with self.assertRaises(bridge.Refused):self.apply(summary='another draft')
    def test_wrong_lecture_rejected(self):
        with self.assertRaises(bridge.Refused):self.apply(sub='3')
    def test_tampering_rejected(self):
        a=copy.deepcopy(self.a);a['summary']+='new'
        with self.assertRaises(bridge.Refused):self.apply(a)
    def test_figure_change_rejected_even_with_rehashed_payload(self):
        a=copy.deepcopy(self.a);a['summary']=a['summary'].replace('原图','别的图');a['summary_sha256']=hashlib.sha256(a['summary'].encode()).hexdigest()
        with self.assertRaises(bridge.Refused):self.apply(a)
    def test_edit_ledger_required(self):
        a=copy.deepcopy(self.a);a['edits']=[]
        with self.assertRaises(bridge.Refused):self.apply(a)
if __name__=='__main__':unittest.main()
