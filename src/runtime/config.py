import os
import re

from src.runtime.session_rules import (
    parse_course_session_exclusions,
    parse_course_session_rules,
    parse_session_override_dates,
)

STUDENT_ID = os.environ.get("StuId", "")
PASSWORD = os.environ.get("UISPsw", "")

WEBVPN_BASE = "https://webvpn.fudan.edu.cn"
IDP_BASE = "https://id.fudan.edu.cn"
ICOURSE_BASE = "https://icourse.fudan.edu.cn"

WEBVPN_AES_KEY = b"wrdvpnisthebest!"
WEBVPN_AES_IV = b"wrdvpnisthebest!"

TENANT_CODE = "222"
GROUP_CODE = "2095000001"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 模型服务商配置（按列表顺序作为优先级，从前往后尝试）。
# 用户可以在这里随意添加/删除/重排服务商和模型。
# 兼容性：只设置 DASHSCOPE_API_KEY 也能跑（modelscope 项的 api_key 直接读取它）。
# 同名 provider 多次出现 → resolve_model_providers() 把它们的 models 合并到首次出
# 现的那条；这避免 Summarizer 内部按 name 索引 client 字典时被后写覆盖。
MODEL_PROVIDERS: list[dict] = [
    {
        "name": "modelscope",
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url_env": "DASHSCOPE_BASE_URL",
        "default_base_url": "https://api-inference.modelscope.cn/v1/",
        "models": [
            "deepseek-ai/DeepSeek-V4-Pro",
            "deepseek-ai/DeepSeek-V4-Flash"
        ],
    },
    {
        "name": "deepseek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "default_base_url": "https://api.deepseek.com/v1",
        "models": [
            "deepseek-v4-flash"
        ],
    },
    # {
    #     "name": "modelscope",
    #     "api_key_env": "DASHSCOPE_API_KEY",
    #     "base_url_env": "DASHSCOPE_BASE_URL",
    #     "default_base_url": "https://api-inference.modelscope.cn/v1/",
    #     "models": [
    #         "deepseek-ai/DeepSeek-V3.2",
    #         "ZhipuAI/GLM-5",
    #         "MiniMax/MiniMax-M2.5",
    #         "Qwen/Qwen3.5-397B-A17B",
    #     ],
    # },
    {
        "name": "gemini",
        "api_key_env": "GEMINI_API_KEY",
        "base_url_env": "GEMINI_BASE_URL",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "models": [
            "gemini-2.5-flash",
            "gemini-3-flash-preview",
        ],
    }
]

# Vision is deliberately separate from text-provider fallback: V4 Pro and
# third-party text endpoints must never silently receive images.
HOMEWORK_VISION_MODEL = 'deepseek-flash'


def resolve_model_providers() -> list[dict]:
    """Resolve MODEL_PROVIDERS into runtime configs.

    Drops providers whose api_key env var is unset. Same-name entries get
    their model lists merged into the first occurrence (Summarizer's client
    dict keys on name and would otherwise collide).

    Returns:
        list of {name, api_key, base_url, models}.
    """
    resolved: list[dict] = []
    by_name: dict[str, dict] = {}
    for p in MODEL_PROVIDERS:
        api_key = os.environ.get(p["api_key_env"], "").strip()
        if not api_key:
            continue
        base_url = (
            os.environ.get(p.get("base_url_env", ""), "").strip()
            or p.get("default_base_url", "")
        )
        if not base_url:
            continue
        if p["name"] in by_name:
            existing = by_name[p["name"]]
            for m in p["models"]:
                if m not in existing["models"]:
                    existing["models"].append(m)
            continue
        entry = {
            "name": p["name"],
            "api_key": api_key,
            "base_url": base_url,
            "models": list(p["models"]),
        }
        resolved.append(entry)
        by_name[p["name"]] = entry
    return resolved


# Legacy compatibility shims (kept so other modules importing these don't break)
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# QQ SMTP
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()


def parse_receiver_emails(raw: str) -> list[str]:
    """Parse, trim and de-duplicate a private recipient list.

    Commas, semicolons and newlines are accepted so the GitHub Secret stays
    convenient to edit.  Address syntax is intentionally left to the SMTP
    server; this function only prevents empty and duplicate envelope entries.
    """
    recipients: list[str] = []
    seen: set[str] = set()
    for value in re.split(r"[,;\n\r]+", raw or ""):
        address = value.strip()
        key = address.casefold()
        if address and key not in seen:
            recipients.append(address)
            seen.add(key)
    return recipients


_RECEIVER_EMAILS_RAW = (
    os.environ.get("RECEIVER_EMAILS", "").strip()
    or os.environ.get("RECEIVER_EMAIL", "").strip()
)
RECEIVER_EMAILS = parse_receiver_emails(_RECEIVER_EMAILS_RAW)

# Manual workflow switch.  A boolean input is used instead of a lecture ID so
# a public repository's Actions metadata never exposes private course details.
RETRY_ALL_FAILED = os.environ.get("RETRY_ALL_FAILED", "").strip().lower() in {
    "1", "true", "yes", "on",
}
# Populated only by the guarded date-rerun workflow.  Ordinary scheduled and
# frontend runs leave this empty and retain their existing selection behavior.
RERUN_TARGET_IDS = frozenset(
    part for part in os.environ.get("RERUN_TARGET_IDS", "").split(",") if part
)
# Backward-compatible first recipient for older integrations.
RECEIVER_EMAIL = RECEIVER_EMAILS[0] if RECEIVER_EMAILS else ""
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

# Database & Storage
DATA_DIR = os.environ.get("DATA_DIR", "data")
VIDEO_DIR = os.path.join(DATA_DIR, "videos")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")  # ffmpeg-decoded f32le scratch buffers
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "icourse.db"))

# Qwen 1.7B is the sole local recognizer; sherpa-onnx is VAD only.
SILERO_VAD_PATH = os.environ.get('SILERO_VAD_PATH', 'silero_vad.onnx')
ASR_BACKEND = 'qwen'
# Inference thread count.  4 fully saturates a 4-vCPU GitHub runner.
ASR_NUM_THREADS = int(os.environ.get("ASR_NUM_THREADS", "4"))

# ── Scheduler concurrency knobs.  All overridable via env. ────────────────
# image_pool: image downloads are tiny and IO-bound, 20 saturates bandwidth
# without hammering the iCourse server.
IMAGE_WORKERS = int(os.environ.get("IMAGE_WORKERS", "20"))
# OCR pool: pool size is the hard ceiling; a fixed BoundedSemaphore(OCR_MAX_TARGET)
# gates live concurrency since RapidOCR is single-threaded CPU-bound.
OCR_MAX_WORKERS = int(os.environ.get("OCR_MAX_WORKERS", "8"))
# Fixed cap — no dynamic CPU-based adjustment.  RapidOCR is single-threaded;
# more than 2 concurrent workers don't increase throughput on 4-core runners.
OCR_MAX_TARGET = int(os.environ.get("OCR_MAX_TARGET", "2"))
# Two concurrent ffmpeg audio extractions: the current lecture being
# transcribed + one pre-decoded for the next lecture.  Bandwidth-fair sharing
# at 20 MB/s split = ~10 MB/s each.
VIDEO_DOWNLOAD_CONCURRENCY = int(
    os.environ.get("VIDEO_DOWNLOAD_CONCURRENCY", "2")
)
# Timestamp-preserving production preparation prefers verified AAC batches.
# mp4 explicitly restores the signed-range FFmpeg acquisition path.
AUDIO_ACQUISITION = os.environ.get("AUDIO_ACQUISITION", "aac_auto").strip()
if AUDIO_ACQUISITION not in ('aac_auto', 'mp4'):
    raise ValueError('Invalid audio acquisition mode')

# Local Qwen is primary.  When set, Seed-ASR 2.0 only rescues bounded
# VAD-confirmed speech windows with empty or near-empty local recognition.
DOUBAO_ASR_API_KEY = os.environ.get("DOUBAO_ASR_API_KEY", "").strip()

# Official subtitles are secondary evidence, never the primary transcript.
# The workflow enables their completeness check and conservative gap fill.
USE_OFFICIAL_TRANSCRIPT = (
    os.environ.get("USE_OFFICIAL_TRANSCRIPT", "").strip().lower()
    in ("1", "true", "yes")
)

# 监控的课程 ID 列表
COURSE_IDS = [
    c.strip()
    for c in os.environ.get("COURSE_IDS", "").split(",")
    if c.strip()
]

# Optional private per-course schedule allowlist.  Example secret value:
# 12345=周一第1-2节|周三第6-8节
# Courses omitted from the rules keep all playable lectures.
COURSE_SESSION_RULES = parse_course_session_rules(
    os.environ.get("COURSE_SESSION_RULES", "")
)
COURSE_SESSION_EXCLUSIONS = parse_course_session_exclusions(
    os.environ.get('COURSE_SESSION_EXCLUSIONS', '')
)
COURSE_SESSION_OVERRIDE_DATES = parse_session_override_dates(
    os.environ.get("COURSE_SESSION_OVERRIDE_DATES", "")
)

# 学期级课程目录爬取（已弃用 — main.py 现在自动发现所有学期）。
# 保留此变量仅用于兼容老部署环境，新部署无需设置。
# 例：CRAWL_TERM=25
CRAWL_TERM = os.environ.get("CRAWL_TERM", "").strip()
