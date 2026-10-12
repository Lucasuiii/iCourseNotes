import subprocess,tempfile,zipfile
from pathlib import Path
import unittest
class LocalExportTests(unittest.TestCase):
 def test_real_dom_and_portable_utf8_zip(self):
  root=Path(__file__).resolve().parents[1]
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'notes.zip';r=subprocess.run(['node','scripts/test_frontend_exports.cjs',str(p)],cwd=root,capture_output=True,text=True,timeout=30)
   self.assertEqual(r.returncode,0,r.stdout+r.stderr)
   with zipfile.ZipFile(p) as z:
    self.assertIsNone(z.testzip());self.assertEqual(len(z.namelist()),2)
    self.assertIn('数值算法_复习.md',z.namelist());self.assertIn('$A=QR$',z.read('数值算法_复习.md').decode())
    image=next(x for x in z.namelist() if x.endswith('.jpg'));self.assertEqual(z.read(image),b'\xff\xd8\xff\xd9')
