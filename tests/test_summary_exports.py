"""Export real synthetic JPEGs through HTML, MIME and portable Markdown."""
import base64
import copy
import email
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image
from scripts.export_course import _query_course, _build_html, _send_html_email, _send_md_email
from src.api.emailer import _prepare_pdf_html
from src.api.summary_assets import lecture_assets, portable_attachment
from src.data.database import Database
import test_email_delivery as delivery
from test_email_delivery import _FakeSMTP


class SummaryExportTests(unittest.TestCase):
    def setUp(self):
        out = io.BytesIO(); Image.new('RGB', (40, 30), (20, 120, 230)).save(out, 'JPEG')
        self.raw = out.getvalue(); self.digest = hashlib.sha256(self.raw).hexdigest()
        self.summary = '图示\n\n![课堂示意](#icourse-figure-'+self.digest+')\n\n<img src="https://untrusted.invalid/image">'
        self.state = {'schema':1, 'course_id':'10', 'sub_id':'1', 'status':'complete',
                      'figures':[{'id':self.digest, 'mime':'image/jpeg', 'data':base64.b64encode(self.raw).decode(), 'seconds':1}]}
        self.lecture = {'course_id':'10', 'sub_id':'1', 'sub_title':'Lesson', 'date':'2026-10-01', 'summary':self.summary,
                        'course_title':'Course', 'figure_state':self.state}

    def test_query_loads_only_selected_lesson_assets_and_checks_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp)/'test.db'))
            with db.conn:
                db.conn.execute("INSERT INTO courses VALUES ('10','Course','Teacher')")
                db.conn.execute("INSERT INTO lectures(sub_id,course_id,sub_title,date,summary) VALUES ('1','10','Lesson','2026-10-01',?)", (self.summary,))
                db.conn.execute('INSERT INTO meta VALUES (?,?)', ('summary_figures:1',json.dumps(self.state)))
            title, teacher, lectures = _query_course(db, '10', ['1'])
            self.assertEqual(lecture_assets(lectures[0]), {self.digest:self.raw})
            state = dict(self.state, course_id='other')
            with db.conn: db.conn.execute('UPDATE meta SET value=?', (json.dumps(state),))
            with self.assertRaises(ValueError): _query_course(db, '10')
            db.conn.close()

    def test_cid_and_inline_pdf_html_contain_original_jpeg_but_no_external_image(self):
        images = {}
        html = _build_html('Course', 'Teacher', [self.lecture], cid_images=images)
        self.assertIn('cid:figure-'+self.digest, html)
        self.assertNotIn('untrusted.invalid', html)
        pdf_html = _prepare_pdf_html(html, images)
        self.assertIn('data:image/jpeg;base64,'+base64.b64encode(self.raw).decode(), pdf_html)
        self.assertNotIn('cid:', pdf_html)
        self.assertIn('data:image/jpeg;base64,', _build_html('Course','Teacher',[self.lecture],pdf=True))
        _FakeSMTP.calls.clear()
        with patch('scripts.export_course._smtp_connect', return_value=_FakeSMTP('host',465,timeout=30)):
            _send_html_email('Subject', html, 'Plain', images)
        message = email.message_from_string(_FakeSMTP.calls[-1][2])
        image = next(p for p in message.walk() if p.get_content_type()=='image/jpeg')
        self.assertEqual(image.get_payload(decode=True), self.raw)
        self.assertEqual(image['Content-ID'], '<figure-'+self.digest+'>')

    def test_markdown_package_has_relative_reference_and_exact_jpeg(self):
        raw, filename = portable_attachment(self.summary, 'course.md', [self.lecture])
        self.assertEqual(filename, 'course.zip')
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            self.assertEqual(set(package.namelist()), {'course.md','figures/'+self.digest+'.jpg'})
            self.assertEqual(package.read('figures/'+self.digest+'.jpg'), self.raw)
            md = package.read('course.md').decode()
            self.assertIn('](figures/'+self.digest+'.jpg)', md)
            self.assertNotIn('#icourse-figure-',md)
        _FakeSMTP.calls.clear()
        with patch('scripts.export_course._smtp_connect', return_value=_FakeSMTP('host',465,timeout=30)):
            _send_md_email('Subject', [(raw,filename)])
        message = email.message_from_string(_FakeSMTP.calls[-1][2])
        attachment = next(p for p in message.walk() if p.get_content_type()=='application/zip')
        self.assertEqual(attachment.get_payload(decode=True), raw)

    def test_email_pdf_and_markdown_fallback_both_preserve_classroom_images(self):
        for pdf in (b'%PDF-1.7', RuntimeError('synthetic renderer failure')):
            _FakeSMTP.calls.clear()
            with patch('src.api.emailer.smtplib.SMTP_SSL', _FakeSMTP), patch('src.api.emailer.render_html_pdf',
                    **({'side_effect':pdf} if isinstance(pdf,Exception) else {'return_value':pdf})):
                self.assertTrue(delivery.EmailDeliveryTests()._emailer().send([self.lecture]))
            message = email.message_from_string(_FakeSMTP.calls[-1][2])
            self.assertEqual(next(p for p in message.walk() if p.get_content_type()=='image/jpeg').get_payload(decode=True), self.raw)
            if isinstance(pdf,Exception):
                package = next(p for p in message.walk() if p.get_content_type()=='application/zip')
                with zipfile.ZipFile(io.BytesIO(package.get_payload(decode=True))) as archive:
                    self.assertEqual(archive.read('figures/'+self.digest+'.jpg'), self.raw)

    def test_missing_changed_or_cross_lesson_assets_are_rejected(self):
        for state in (None, dict(self.state, sub_id='2'), dict(self.state, figures=[])):
            lecture = dict(self.lecture, figure_state=state)
            with self.assertRaises(ValueError): lecture_assets(lecture)
        lecture = copy.deepcopy(self.lecture)
        lecture['figure_state']['figures'][0]['data'] = base64.b64encode(b'not jpeg').decode()
        with self.assertRaises(ValueError): lecture_assets(lecture)
