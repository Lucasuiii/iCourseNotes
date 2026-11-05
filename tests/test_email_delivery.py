import email
import io
from contextlib import redirect_stdout
import unittest
from email.header import decode_header, make_header
from unittest.mock import MagicMock, patch

from src.api.emailer import (
    Emailer,
    _EMAIL_CSS,
    _md_to_html,
    _prepare_pdf_html,
    render_html_pdf,
)
from src.runtime.config import parse_receiver_emails


class _FakeSMTP:
    calls = []

    def __init__(self, host, port, timeout=None):
        if timeout != 30:
            raise AssertionError('SMTP connection must have a bounded timeout')
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, sender, password):
        self.login_args = (sender, password)

    def sendmail(self, sender, recipients, message):
        self.__class__.calls.append((sender, recipients, message))
        return {}


class _FakePDFDocument:
    def write_pdf(self):
        return b"%PDF-1.7 test"


class _FakePDFRenderer:
    last_html = ""

    def __init__(self, string):
        self.__class__.last_html = string

    def write_pdf(self):
        return _FakePDFDocument().write_pdf()


class EmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        _FakeSMTP.calls.clear()

    def test_timeout_phase_diagnostics_retry_without_sensitive_exception_text(self):
        for phase in ['connect', 'login', 'send']:
            with self.subTest(phase=phase):
                smtp = MagicMock()
                server = smtp.return_value.__enter__.return_value
                failure = TimeoutError('private address, credential, signed URL')
                if phase == 'connect': smtp.side_effect = failure
                elif phase == 'login': server.login.side_effect = failure
                else: server.sendmail.side_effect = failure
                output = io.StringIO()
                with patch('src.api.emailer.smtplib.SMTP_SSL', smtp), \
                     patch('src.api.emailer.time.sleep') as sleep, redirect_stdout(output):
                    self.assertFalse(self._emailer()._deliver(MagicMock()))
                self.assertEqual(smtp.call_count, 3)
                self.assertEqual(sleep.call_count, 2)
                self.assertEqual(smtp.call_args.kwargs['timeout'], 30)
                self.assertEqual(output.getvalue().count(f'(phase={phase})'), 3)
                self.assertNotIn('private', output.getvalue())
                self.assertNotIn('sender@example.com', output.getvalue())

    def test_export_smtp_login_failure_closes_connection(self):
        from scripts.export_course import _smtp_connect
        smtp = MagicMock()
        smtp.return_value.login.side_effect = TimeoutError('private')
        with patch('scripts.export_course.smtplib.SMTP_SSL', smtp):
            with self.assertRaises(TimeoutError): _smtp_connect()
        self.assertEqual(smtp.call_args.kwargs['timeout'], 30)
        smtp.return_value.close.assert_called_once()

    def test_recipient_list_parsing(self):
        self.assertEqual(
            parse_receiver_emails(
                "first@example.com, second@example.com;FIRST@example.com\n"
            ),
            ["first@example.com", "second@example.com"],
        )

    def test_pdf_html_inlines_cid_images(self):
        html = '<html><head></head><body><img src="cid:eq-1"></body></html>'
        rendered = _prepare_pdf_html(html, {"eq-1": b"png"})
        self.assertIn("data:image/png;base64,cG5n", rendered)
        self.assertNotIn("cid:eq-1", rendered)
        self.assertIn("@page", rendered)

    def test_renderer_receives_self_contained_html(self):
        result = render_html_pdf(
            '<html><head></head><body><img src="cid:eq"></body></html>',
            {"eq": b"formula"},
            renderer=_FakePDFRenderer,
        )
        self.assertTrue(result.startswith(b"%PDF"))
        self.assertIn("data:image/png;base64,", _FakePDFRenderer.last_html)

    def test_markdown_emphasis_is_rendered_as_red_highlight(self):
        rendered = _md_to_html("这是一个**关键结论**。")
        self.assertIn("<strong>关键结论</strong>", rendered)
        self.assertIn("strong { color: #c0392b; }", _EMAIL_CSS)

    def test_formulas_render_locally_and_embed_as_cid(self):
        cid_images = {}
        rendered = _md_to_html(
            r"矩阵 $$\begin{bmatrix}1&2\\3&4\end{bmatrix}$$ "
            r"分段 $$f(x)=\begin{cases}x^2,&x>0\\0,&x\le0\end{cases}$$",
            cid_images,
        )
        self.assertEqual(len(cid_images), 2)
        self.assertEqual(rendered.count('src="cid:latex-'), 2)
        self.assertNotIn("codecogs", rendered.lower())
        self.assertTrue(all(data.startswith(b"\x89PNG") for data in cid_images.values()))

    def test_formula_without_cid_uses_inline_data(self):
        rendered = _md_to_html("答案是 $x^2$。")
        self.assertIn("data:image/png;base64,", rendered)
        self.assertNotIn("https://", rendered)

    def test_remote_markdown_image_and_raw_html_are_removed(self):
        rendered = _md_to_html(
            '正文 ![remote](https://example.com/a.png) '
            '<img src="https://example.com/b.png">'
        )
        self.assertNotIn("<img", rendered)
        self.assertNotIn("https://example.com", rendered)

    @patch("src.api.emailer._render_latex_images", return_value={("x^2", False): None})
    def test_failed_local_formula_stays_readable(self, _render):
        rendered = _md_to_html("答案是 $x^2$。")
        self.assertIn("<code>x^2</code>", rendered)
        self.assertNotIn("codecogs", rendered.lower())

    def _emailer(self):
        sender = Emailer()
        sender.sender = "sender@example.com"
        sender.password = "password"
        sender.receivers = ["one@example.com", "two@example.com"]
        return sender

    @staticmethod
    def _items():
        return [{
            "course_title": "课程 A",
            "sub_title": "第 1 讲",
            "date": "2026-09-17",
            "summary": "### 要点\n\n内容",
        }]

    @patch("src.api.emailer.render_html_pdf", return_value=b"%PDF-1.7")
    @patch("src.api.emailer.smtplib.SMTP_SSL", _FakeSMTP)
    def test_successful_pdf_replaces_markdown_attachment(self, _render):
        self.assertTrue(self._emailer().send(self._items()))
        _, recipients, raw = _FakeSMTP.calls[-1]
        self.assertEqual(recipients, ["one@example.com", "two@example.com"])
        message = email.message_from_string(raw)
        filenames = [part.get_filename() for part in message.walk()]
        self.assertTrue(any(name and name.endswith(".pdf") for name in filenames))
        self.assertFalse(any(name and name.endswith(".md") for name in filenames))
        self.assertEqual(message["To"], "undisclosed-recipients:;")

    @patch("src.api.emailer.render_html_pdf", side_effect=RuntimeError("no PDF"))
    @patch("src.api.emailer.smtplib.SMTP_SSL", _FakeSMTP)
    def test_markdown_is_attached_when_pdf_generation_fails(self, _render):
        self.assertTrue(self._emailer().send(self._items()))
        message = email.message_from_string(_FakeSMTP.calls[-1][2])
        filenames = [part.get_filename() for part in message.walk()]
        self.assertTrue(any(name and name.endswith(".md") for name in filenames))
        self.assertFalse(any(name and name.endswith(".pdf") for name in filenames))

    @patch("src.api.emailer.smtplib.SMTP_SSL", _FakeSMTP)
    def test_failure_notice_is_private_and_excludes_raw_error(self):
        item = {
            "course_title": "课程 A",
            "sub_title": "第 1 讲",
            "error_stage": "summarize",
            "error_count": 3,
            "error_msg": "secret signed URL must not leak",
        }
        self.assertTrue(self._emailer().send_failure_notice([item]))
        _, recipients, raw = _FakeSMTP.calls[-1]
        self.assertEqual(recipients, ["one@example.com", "two@example.com"])
        message = email.message_from_string(raw)
        self.assertEqual(message["To"], "undisclosed-recipients:;")
        subject = str(make_header(decode_header(message["Subject"])))
        self.assertEqual(subject, "[FiCS] 有课程课次需要处理")
        body = "\n".join(
            part.get_payload(decode=True).decode(part.get_content_charset())
            for part in message.walk()
            if part.get_content_type() in {"text/plain", "text/html"}
        )
        self.assertIn("课程 A", body)
        self.assertIn("摘要生成", body)
        self.assertIn("不会因失败次数自动排除", body)
        self.assertNotIn("已暂停自动重试", body)
        self.assertNotIn("secret signed URL", body)

    @patch("src.api.emailer.smtplib.SMTP_SSL", _FakeSMTP)
    def test_quality_failure_notice_has_readable_stage(self):
        item = {
            "course_title": "课程 A",
            "sub_title": "第 1 讲",
            "error_stage": "content_quality",
            "error_count": 3,
        }
        self.assertTrue(self._emailer().send_failure_notice([item]))
        message = email.message_from_string(_FakeSMTP.calls[-1][2])
        body = "\n".join(
            part.get_payload(decode=True).decode(part.get_content_charset())
            for part in message.walk()
            if part.get_content_type() in {"text/plain", "text/html"}
        )
        self.assertIn("授课材料质量", body)


if __name__ == "__main__":
    unittest.main()
