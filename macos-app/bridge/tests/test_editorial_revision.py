import base64,copy,hashlib,importlib.util,io,json,unittest
from pathlib import Path
from PIL import Image
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('bridge',Path(__file__).parents[1]/'app_bridge.py');bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
sha=lambda s:hashlib.sha256(s.encode()).hexdigest()
digest=lambda v:sha(json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':')))
class EditorialTests(unittest.TestCase):
 def setUp(self):
  source=patch.object(bridge,"active_source",return_value=Path(__file__).resolve().parents[3]);source.start();self.addCleanup(source.stop)
  b=io.BytesIO();Image.new('RGB',(20,20),'white').save(b,format='JPEG');raw=b.getvalue();i=hashlib.sha256(raw).hexdigest()
  f={'id':i,'mime':'image/jpeg','data':base64.b64encode(raw).decode(),'caption':'原图','seconds':10.,'kind':'homework','section_id':0}
  self.fig={'schema':1,'course_id':'1','sub_id':'2','status':'complete','figures':[f]}
  self.base='### 作业\n\n待核实。\n\n![原图](#icourse-figure-'+i+')';final=self.base.replace('待核实。','按板书列出的第1题。')
  new=copy.deepcopy(self.fig);new['editorial_summary_sha256']=sha(final)
  self.a={'schema':1,'kind':'independent_editorial_revision','course_id':'1','sub_id':'2','date':'2026-10-30','review_status':'reviewed_locally','model_calls':0,'checks':['a','b','c','d'],'blocking_issues':[],'base_summary':self.base,'base_summary_sha256':sha(self.base),'base_figures_sha256':digest(bridge.figure_payload(json.dumps(self.fig))['figures']),'summary':final,'summary_sha256':sha(final),'figure_state':new,'image_evidence':[]}
 def apply(self,a=None,text=None,fig=None,sub='2'):
  return bridge.apply_editorial_revision(text or self.base,fig or self.fig,a or self.a,'1',sub,'2026-10-30')
 def test_exact_reviewed_edit_and_original_pixels(self):
  text,fig=self.apply();self.assertEqual(text,self.a['summary']);self.assertEqual(fig['figures'],self.fig['figures'])
 def test_stale_text_rejected(self):
  with self.assertRaises(bridge.Refused):self.apply(text=self.base+' changed')
 def test_wrong_lecture_rejected(self):
  with self.assertRaises(bridge.Refused):self.apply(sub='9')
 def test_changed_original_caption_requires_new_base(self):
  f=copy.deepcopy(self.fig);f['figures'][0]['caption']='changed'
  with self.assertRaises(bridge.Refused):self.apply(fig=f)
 def test_tampered_final_rejected(self):
  a=copy.deepcopy(self.a);a['summary']+=' change'
  with self.assertRaises(bridge.Refused):self.apply(a)
 def test_unreviewed_or_unresolved_edit_rejected(self):
  for k,v in [('review_status','pending'),('blocking_issues',['not checked']),('checks',[])]:
   a=copy.deepcopy(self.a);a[k]=v
   with self.assertRaises(bridge.Refused):self.apply(a)
 def test_changed_pixel_or_time_rejected(self):
  a=copy.deepcopy(self.a);a['figure_state']['figures'][0]['seconds']=20
  with self.assertRaises(bridge.Refused):self.apply(a)
 def test_broken_image_reference_rejected(self):
  a=copy.deepcopy(self.a);a['summary']=a['summary'].replace('#icourse-figure-','#missing-');a['summary_sha256']=sha(a['summary']);a['figure_state']['editorial_summary_sha256']=a['summary_sha256']
  with self.assertRaises(bridge.Refused):self.apply(a)
if __name__=='__main__':unittest.main()
