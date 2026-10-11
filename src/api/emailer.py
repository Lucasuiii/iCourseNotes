import base64
import json
import math
import re
import smtplib
import subprocess
import time
import uuid
from pathlib import Path
import cairosvg
import bleach
from collections import OrderedDict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
from html import escape
from email.utils import formataddr

import markdown
from pygments.formatters import HtmlFormatter

from src.runtime import config
from src.api.summary_assets import lecture_assets, portable_attachment, image_subtype
from src.pipeline.summary_figures import FIGURE_REF
from src.api.email_markdown import (
    build_attachment_filename,
    build_course_markdown,
    build_pdf_attachment_filename,
)

_MD_EXTENSIONS= ["tables", "fenced_code", "nl2br", "sane_lists", "codehilite"]

_MD_EXTENSION_CONFIGS = {
    "codehilite": {
        "guess_lang": False,
        "linenums": False,
        "css_class": "highlight",
    }
}

_PYGMENTS_CSS = HtmlFormatter(style="friendly").get_style_defs(".highlight")

_EMAIL_CSS = """\
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #1a1a1a;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
}
h2 {
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 8px;
    margin-top: 32px;
}
h3 {
    color: #34495e;
    margin-top: 24px;
}
h3 small {
    color: #7f8c8d;
    font-weight: normal;
}
h4 { color: #555; margin-top: 18px; }
hr {
    border: none;
    border-top: 1px solid #e0e0e0;
    margin: 28px 0;
}
strong { color: #c0392b; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
}
th {
    background: #f5f6fa;
    font-weight: 600;
}
tr:nth-child(even) { background: #fafafa; }
pre {
    background: #f8f8f8;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 12px 16px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
}
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 13px;
}
p code {
    background: #f0f0f0;
    padding: 2px 5px;
    border-radius: 3px;
}
blockquote {
    border-left: 4px solid #3498db;
    margin: 12px 0;
    padding: 8px 16px;
    background: #f8f9fa;
    color: #555;
}
ul, ol { padding-left: 24px; }
li { margin-bottom: 4px; }
"""

_PDF_CSS = """\
@page {
    size: A4;
    margin: 18mm 16mm;
    @bottom-center {
        content: counter(page) " / " counter(pages);
        color: #7f8c8d;
        font-size: 9pt;
    }
}
body {
    max-width: none;
    margin: 0;
    padding: 0;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
}
img { max-width: 100% !important; height: auto !important; }
nav { break-inside: avoid; }
h2, h3, h4 { break-after: avoid; }
pre, blockquote, table { break-inside: avoid; }
"""

_MIN_INLINE_HEIGHT = 13  # minimum logical height for inline formulas (px)

_IMAGE_CACHE: dict[tuple[str, bool], tuple[int, int, bytes] | None] = {}
_MATH_RENDERER = Path(__file__).resolve().parents[2] / "scripts/render_math.js"
_SVG_SIZE = re.compile(r'\b(width|height)="([0-9.]+)ex"')
_MARKDOWN_TAGS = [
    "a", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "span", "strong",
    "table", "tbody", "td", "th", "thead", "tr", "ul",
]
_MARKDOWN_ATTRIBUTES = {"a": ["href", "title"], "code": ["class"],
                        "div": ["class"], "span": ["class"]}


def _render_latex_images(formulas: list[tuple[str, bool]]) -> dict:
    """Render unique TeX formulas locally in one Node process, then to PNG.

    Return logical pixel dimensions and PNG bytes. A failed formula is kept as
    escaped text by the caller; no external formula service is contacted.
    """
    unique = list(dict.fromkeys(formulas))
    missing = [formula for formula in unique if formula not in _IMAGE_CACHE]
    if missing:
        try:
            process = subprocess.run(
                ["node", str(_MATH_RENDERER)],
                input=json.dumps([
                    {"tex": tex, "display": block} for tex, block in missing
                ]),
                text=True,
                capture_output=True,
                check=True,
                timeout=45,
            )
            svgs = json.loads(process.stdout)
            if not isinstance(svgs, list) or len(svgs) != len(missing):
                raise ValueError("Unexpected formula renderer output")
            for formula, svg in zip(missing, svgs):
                try:
                    if not isinstance(svg, str) or not svg.startswith("<svg"):
                        raise ValueError("Formula did not render")
                    # MathJax emits only internal SVG path references. Reject
                    # any active/external resource before passing it to CairoSVG.
                    if len(svg) > 5_000_000 or re.search(
                        r"<(?:image|foreignObject)\b|"
                        r"(?:xlink:)?href\s*=\s*['\"](?!#)|"
                        r"url\s*\(\s*['\"]?(?!#)", svg, re.I
                    ):
                        raise ValueError("Formula contains an external resource")
                    root_tag = svg.split(">", 1)[0]
                    sizes = dict((axis, float(value)) for axis, value
                                 in _SVG_SIZE.findall(root_tag))
                    width = max(1, math.ceil(sizes["width"] * 8))
                    height = max(1, math.ceil(sizes["height"] * 8))
                    if width > 4096 or height > 2048:
                        raise ValueError("Formula image is too large")
                    png = cairosvg.svg2png(
                        bytestring=svg.encode("utf-8"),
                        output_width=width * 3,
                        output_height=height * 3,
                    )
                    _IMAGE_CACHE[formula] = (width, height, png)
                except Exception as exc:
                    print(f"[LaTeX Render] Formula failed: {type(exc).__name__}")
                    _IMAGE_CACHE[formula] = None
        except Exception as exc:
            print(f"[LaTeX Render] Batch failed: {type(exc).__name__}")
            for formula in missing:
                _IMAGE_CACHE[formula] = None
    return {formula: _IMAGE_CACHE[formula] for formula in unique}


def _md_to_html(md_text: str, cid_images: dict | None = None,
                assets: dict[str, bytes] | None = None) -> str:
    """Convert Markdown to styled HTML, rendering LaTeX math as images.

    Processing order: extract LaTeX → markdown convert → restore as <img>.
    This prevents the markdown engine from corrupting backslash escapes.

    Args:
        md_text: Markdown source text.
        cid_images: When provided (dict), embed PNGs via CID references.
                    Otherwise use inline data URIs. The dict is populated
                    with {cid_name: png_bytes} entries for the caller to attach
                    to the MIME message.
    """
    latex_map: dict[str, str] = {}
    counter = 0
    marker = uuid.uuid4().hex.upper()
    figure_map = {}
    def stash_figure(match):
        raw = (assets or {}).get(match[2])
        if raw is None:
            raise ValueError('Classroom figure assets are missing')
        key = f'ICOURSEFIGURE{marker}{len(figure_map)}END'
        cid = 'figure-'+match[2]
        if cid_images is not None:
            cid_images[cid] = raw
            src = 'cid:'+cid
        else:
            src = 'data:image/jpeg;base64,'+base64.b64encode(raw).decode('ascii')
        figure_map[key] = (f'<img src="{src}" alt="{escape(match[1], quote=True)}" '
                           'style="max-width:100%;height:auto;">')
        return key
    md_text = FIGURE_REF.sub(stash_figure, md_text)

    def _stash(match):
        nonlocal counter
        key = f"ICOURSEMATH{marker}{counter}END"
        counter += 1
        latex_map[key] = match.group(0)
        return key

    def _stash_block(match):
        """Stash \\[...\\] as $$...$$ for uniform downstream handling."""
        nonlocal counter
        key = f"ICOURSEMATH{marker}{counter}END"
        counter += 1
        latex_map[key] = "$$" + match.group(1) + "$$"
        return key

    def _stash_inline(match):
        """Stash \\(...\\) as $...$ for uniform downstream handling."""
        nonlocal counter
        key = f"ICOURSEMATH{marker}{counter}END"
        counter += 1
        latex_map[key] = "$" + match.group(1) + "$"
        return key

    # Extract LaTeX in order: block first, then inline
    # 1) $$...$$ block formulas
    text = re.sub(r"\$\$(.+?)\$\$", _stash, md_text, flags=re.DOTALL)
    # 2) \[...\] block formulas (normalize to $$...$$)
    text = re.sub(r"\\\[(.+?)\\\]", _stash_block, text, flags=re.DOTALL)
    # 3) $...$ inline formulas (not $$)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _stash, text)
    # 4) \(...\) inline formulas (normalize to $...$)
    text = re.sub(r"\\\((.+?)\\\)", _stash_inline, text)

    html = markdown.markdown(
        text,
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXTENSION_CONFIGS,
    )
    # Course material can contain raw HTML or Markdown images. Remove active
    # markup and remote images before restoring our own local formula PNGs.
    html = bleach.clean(
        html, tags=_MARKDOWN_TAGS, attributes=_MARKDOWN_ATTRIBUTES,
        protocols=["http", "https", "mailto"], strip=True,
    )

    latex_info: dict[str, tuple[str, bool]] = {}
    for key, original in latex_map.items():
        is_block = original.startswith("$$")
        latex_content = original[2:-2] if is_block else original[1:-1]
        latex_info[key] = (latex_content, is_block)

    images = _render_latex_images(list(latex_info.values())) if latex_info else {}

    for key, (latex_content, is_block) in latex_info.items():
        rendered = images[(latex_content, is_block)]
        w, h, img_data = rendered if rendered else (None, None, None)

        if is_block:
            if w and h:
                src = _resolve_src(img_data, cid_images)
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:middle;border:none;display:inline-block;">'
                    f'</div>'
                )
            else:
                img_tag = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<code>{escape(latex_content)}</code></div>'
                )
        else:
            if w and h:
                # Enforce minimum height so formulas aren't smaller than text
                if h < _MIN_INLINE_HEIGHT:
                    scale = _MIN_INLINE_HEIGHT / h
                    w = max(1, int(w * scale))
                    h = _MIN_INLINE_HEIGHT

                src = _resolve_src(img_data, cid_images)
                img_tag = (
                    f'<img src="{src}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" '
                    f'style="width:{w}px;height:{h}px;max-width:none;'
                    f'vertical-align:-3px;border:none;margin:0 2px;">'
                )
            else:
                img_tag = f'<code>{escape(latex_content)}</code>'

        html = html.replace(key, img_tag)

    for key, tag in figure_map.items():
        html = html.replace(key, tag)
    return html


def _resolve_src(img_data: bytes, cid_images: dict | None) -> str:
    """Keep formula images inside the email/PDF, never on a remote URL."""
    if cid_images is not None:
        cid = f"latex-{uuid.uuid4().hex[:12]}"
        cid_images[cid] = img_data
        return f"cid:{cid}"
    return "data:image/png;base64," + base64.b64encode(img_data).decode("ascii")


def _prepare_pdf_html(html: str, cid_images: dict[str, bytes]) -> str:
    """Inline CID formula/classroom images and add print-specific CSS."""
    rendered = html
    for cid, png_data in cid_images.items():
        encoded = base64.b64encode(png_data).decode("ascii")
        rendered = rendered.replace(
            f"cid:{cid}", f"data:image/{image_subtype(png_data)};base64,{encoded}"
        )
    pdf_style = f"<style>{_PDF_CSS}</style>"
    if "</head>" in rendered:
        rendered = rendered.replace("</head>", f"{pdf_style}</head>", 1)
    else:
        rendered = f"{pdf_style}{rendered}"
    return rendered


def render_html_pdf(html: str, cid_images: dict[str, bytes],
                    renderer=None) -> bytes:
    """Render the already-built rich email HTML into a PDF.

    ``renderer`` is injectable for unit tests.  Importing WeasyPrint lazily
    keeps non-email tools usable when the optional native PDF stack is absent.
    """
    if renderer is None:
        from weasyprint import HTML  # noqa: PLC0415
        from weasyprint.urls import URLFetcher  # noqa: PLC0415

        renderer = HTML
        return renderer(
            string=_prepare_pdf_html(html, cid_images),
            url_fetcher=URLFetcher(
                allowed_protocols=["data"], fail_on_errors=True
            ),
        ).write_pdf()
    return renderer(string=_prepare_pdf_html(html, cid_images)).write_pdf()


class Emailer:
    """Send course summary emails via QQ SMTP SSL."""

    def __init__(self):
        self.host = config.SMTP_HOST
        self.port = config.SMTP_PORT
        self.sender = config.SMTP_EMAIL
        self.password = config.SMTP_PASSWORD
        self.receivers = list(config.RECEIVER_EMAILS)

    def _deliver(self, msg) -> bool:
        """Deliver an already-built message without exposing recipients."""
        for attempt in range(3):
            phase = 'connect'
            try:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as server:
                    phase = 'login'
                    server.login(self.sender, self.password)
                    phase = 'send'
                    rejected = server.sendmail(
                        self.sender, self.receivers, msg.as_string()
                    )
                    if rejected:
                        raise smtplib.SMTPRecipientsRefused(rejected)
                print("[Emailer] Sent successfully (subject redacted)")
                return True
            except Exception as e:
                print(f"[Emailer] Attempt {attempt + 1}/3 failed: "
                      f"{type(e).__name__} (phase={phase})")
                if attempt < 2:
                    time.sleep(2 ** attempt)

        print("[Emailer] All send attempts failed.")
        return False

    def send_failure_notice(self, items: list[dict]) -> bool:
        """Send one private, deduplicated digest for paused lecture failures.

        Raw exception messages and identifiers are deliberately excluded:
        only course/lecture labels, the processing stage, and attempt count
        are needed for the user to decide whether to retry.
        """
        if not items:
            return True
        if not self.receivers:
            print("[Emailer] No receiver configured.")
            return False

        stage_labels = {
            "no_video": "录播暂不可用",
            "no_audio": "录播没有音轨",
            "transcribe": "语音转写",
            "content_quality": "授课材料质量",
            "summarize": "摘要生成",
            "pipeline": "课程处理",
        }
        plain_lines = [
            "以下课次连续处理失败，已暂停自动重试：",
            "",
        ]
        html_rows = []
        for item in items:
            stage = stage_labels.get(item.get("error_stage"), "课程处理")
            attempts = int(item.get("error_count") or 0)
            course_title = str(item.get("course_title") or "未知课程")
            sub_title = str(item.get("sub_title") or "未知课次")
            plain_lines.append(
                f"- {course_title} / {sub_title}：{stage}，失败 {attempts} 次"
            )
            html_rows.append(
                "<tr>"
                f"<td>{escape(course_title)}</td>"
                f"<td>{escape(sub_title)}</td>"
                f"<td>{escape(stage)}</td>"
                f"<td>{attempts}</td>"
                "</tr>"
            )

        instruction = (
            "系统不会继续每天重复尝试。如需重试，请手动运行 Single Run，"
            "并勾选“Retry all paused failed lectures”。"
        )
        plain_lines.extend(["", instruction])
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            "<p>以下课次连续处理失败，已暂停自动重试：</p>"
            "<table style='border-collapse:collapse' border='1' cellpadding='6'>"
            "<thead><tr><th>课程</th><th>课次</th><th>阶段</th>"
            "<th>失败次数</th></tr></thead><tbody>"
            + "".join(html_rows)
            + "</tbody></table>"
            f"<p>{escape(instruction)}</p>"
            "</body></html>"
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "[FiCS] 有课程课次需要处理"
        msg["From"] = formataddr(("iCourse Subscriber", self.sender))
        msg["To"] = "undisclosed-recipients:;"
        msg.attach(MIMEText("\n".join(plain_lines), "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        return self._deliver(msg)

    def send(self, items: list[dict]) -> bool:
        """Send a single email containing all lecture summaries.

        LaTeX formulas are rendered as PNG images and embedded directly into
        the email via CID attachments, so they display on all clients
        (including mobile) without loading external images.

        Args:
            items: List of dicts, each with keys:
                   course_title, sub_title, date, summary
                   Optional key:
                   is_update — bool, True for re-summarized lectures (v2
                               PPT-aware format replacing an older v1 summary).
                               Adds an "（含 PPT 识别·更新）" subject suffix and
                               an inline 更新 badge per affected lecture.

        Returns:
            True if email was sent successfully, False otherwise.
        """
        if not items:
            return True
        if not self.receivers:
            print("[Emailer] No receiver configured.")
            return False

        any_update = any(item.get("is_update") for item in items)

        # Group by course (preserve insertion order)
        courses: OrderedDict[str, list[dict]] = OrderedDict()
        for item in items:
            courses.setdefault(item["course_title"], []).append(item)

        # Subject
        parts = [f"{ct} ({len(lecs)})" for ct, lecs in courses.items()]
        subject = f"[FiCS] {', '.join(parts)}"
        if any_update:
            subject += "（含 PPT 识别·更新）"

        # Plain text (Markdown as-is, readable without rendering)
        plain_sections = []
        for course_title, lectures in courses.items():
            plain_sections.append(f"{'=' * 40}")
            plain_sections.append(f"课程：{course_title}")
            plain_sections.append(f"{'=' * 40}")
            for lec in lectures:
                tag = "[更新] " if lec.get("is_update") else ""
                plain_sections.append(
                    f"\n--- {tag}{lec['sub_title']} ({lec['date']}) ---\n"
                )
                plain_sections.append(lec["summary"])
        plain = "\n".join(plain_sections)

        # HTML (Markdown → styled HTML with CID-embedded LaTeX images)
        cid_images: dict[str, bytes] = {}

        # Pre-compute one anchor ID per course so TOC and headings stay in sync
        course_anchors = {
            course_title: f"course-{i}"
            for i, course_title in enumerate(courses)
        }

        # Build table of contents
        toc_items = [
            f'<li><a href="#{anchor}" style="color:#3498db;text-decoration:none;">'
            f"{escape(course_title)}</a></li>"
            for course_title, anchor in course_anchors.items()
        ]
        toc_html = (
            '<nav style="background:#f8f9fa;border:1px solid #e0e0e0;'
            'border-radius:6px;padding:16px 20px;margin-bottom:28px;">'
            '<strong style="color:#2c3e50;font-size:16px;">目录</strong>'
            '<ol style="margin:8px 0 0;padding-left:20px;">'
            + "\n".join(toc_items)
            + "</ol></nav>"
        )

        update_badge = (
            '<span style="background:#ff9800;color:white;padding:2px 8px;'
            'border-radius:3px;font-size:12px;margin-right:8px;'
            'vertical-align:middle;">更新</span>'
        )

        body_parts = [toc_html]
        for course_title, lectures in courses.items():
            anchor = course_anchors[course_title]
            body_parts.append(f'<h2 id="{anchor}">{escape(course_title)}</h2>')
            for lec in lectures:
                badge = update_badge if lec.get("is_update") else ""
                body_parts.append(
                    f"<h3>{badge}{escape(lec['sub_title'])} "
                    f"<small>({escape(lec['date'])})</small></h3>"
                )
                body_parts.append(
                    _md_to_html(lec["summary"], cid_images=cid_images, assets=lecture_assets(lec))
                )
                body_parts.append("<hr>")

        html = (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'>"
            f"<style>{_EMAIL_CSS}\n{_PYGMENTS_CSS}</style>"
            "</head><body>"
            + "\n".join(body_parts)
            + "</body></html>"
        )

        # Build MIME: related > alternative > (plain, html) + image attachments
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = formataddr(("iCourse Subscriber", self.sender))
        # Keep personal recipient addresses out of message headers.  SMTP
        # envelope delivery still sends the same message to every address.
        msg["To"] = "undisclosed-recipients:;"

        msg_alt = MIMEMultipart("alternative")
        msg_alt.attach(MIMEText(plain, "plain", "utf-8"))
        msg_alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(msg_alt)

        # Keep a portable Markdown representation ready as a fallback.  Normal
        # delivery uses the HTML body plus PDF and does not duplicate content
        # with a second attachment.
        markdown_fallbacks = [
            portable_attachment(
                build_course_markdown(course_title, lectures),
                build_attachment_filename(course_title, lectures),
                lectures,
            )
            for course_title, lectures in courses.items()
        ]

        # The PDF is rendered from the exact HTML body used by the email, so
        # headings, tables, code blocks and already-rendered formula images
        # remain consistent.  PDF failure is deliberately fail-open: the rich
        # HTML is still sent and Markdown is attached as the archive fallback.
        pdf_attached = False
        try:
            pdf_bytes = render_html_pdf(html, cid_images)
            if len(courses) == 1:
                course_title, lectures = next(iter(courses.items()))
                pdf_filename = build_pdf_attachment_filename(
                    course_title, lectures
                )
            else:
                pdf_filename = "course-summaries.pdf"
            pdf_part = MIMEApplication(pdf_bytes, _subtype="pdf")
            pdf_part.add_header(
                "Content-Disposition",
                "attachment",
                filename=("utf-8", "", pdf_filename),
            )
            msg.attach(pdf_part)
            pdf_attached = True
            print(f"[Emailer] PDF ready ({len(pdf_bytes)} bytes)")
        except Exception as e:
            print(f"[Emailer] PDF generation skipped ({type(e).__name__}).")

        if not pdf_attached:
            for markdown_bytes, markdown_filename in markdown_fallbacks:
                if markdown_filename.endswith('.zip'):
                    markdown_part = MIMEApplication(markdown_bytes, _subtype='zip')
                else:
                    markdown_part = MIMEText(markdown_bytes.decode('utf-8'), "markdown", "utf-8")
                markdown_part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=("utf-8", "", markdown_filename),
                )
                msg.attach(markdown_part)
            print("[Emailer] Attached Markdown fallback.")

        # Attach CID images
        for cid, png_data in cid_images.items():
            subtype = image_subtype(png_data)
            img_part = MIMEImage(png_data, subtype)
            img_part.add_header("Content-ID", f"<{cid}>")
            img_part.add_header("Content-Disposition", "inline",
                                filename=f"{cid}.{subtype}")
            msg.attach(img_part)

        if cid_images:
            print(f"[Emailer] Embedded {len(cid_images)} images as CID")

        return self._deliver(msg)
