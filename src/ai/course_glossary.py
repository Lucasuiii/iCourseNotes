"""Small course-specific terminology hints, not transcription evidence."""
import json
import os
import hashlib
import unicodedata
from pathlib import Path

GLOSSARY_PATH = Path(__file__).resolve().parents[2] / "prompts" / "course_glossary.json"
MAX_TERMS = 30
MAX_TERM_CHARS = 40


ENCRYPTED_GLOSSARY_PATH = GLOSSARY_PATH.with_suffix('.enc')
PRIVATE_GLOSSARY_PATH = GLOSSARY_PATH.parents[1]/'.local-history-refresh/course_glossary.json'
GLOSSARY_MAGIC = b'ICG1'
GLOSSARY_CONTEXT = b'icoursenotes-course-glossary-v1'


def encrypt_glossary(entries, password):
    """Encrypt course mappings using a domain-separated key and authenticated data."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not isinstance(entries, list) or not isinstance(password, str) or len(password) < 32:
        raise ValueError('Invalid glossary encryption input')
    key = hashlib.sha256(GLOSSARY_CONTEXT + b'\0' + password.encode()).digest()
    nonce = os.urandom(12)
    raw = json.dumps(entries, ensure_ascii=False, separators=(',', ':')).encode()
    return GLOSSARY_MAGIC + nonce + AESGCM(key).encrypt(nonce, raw, GLOSSARY_CONTEXT)


def decrypt_glossary(data, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    if not isinstance(password, str) or len(password) < 32 or not data.startswith(GLOSSARY_MAGIC):
        raise ValueError('Invalid encrypted glossary')
    key = hashlib.sha256(GLOSSARY_CONTEXT + b'\0' + password.encode()).digest()
    try:
        return json.loads(AESGCM(key).decrypt(data[4:16], data[16:], GLOSSARY_CONTEXT))
    except InvalidTag:
        raise ValueError('Encrypted glossary authentication failed') from None


def _entries(path):
    selected = path or os.environ.get('COURSE_GLOSSARY_PATH') or None
    if selected is None:
        selected = (PRIVATE_GLOSSARY_PATH if PRIVATE_GLOSSARY_PATH.is_file() else
                    ENCRYPTED_GLOSSARY_PATH if ENCRYPTED_GLOSSARY_PATH.is_file() else GLOSSARY_PATH)
    selected = Path(selected)
    if selected.stat().st_size > 1024*1024:
        raise ValueError('Glossary exceeds size limit')
    raw = selected.read_bytes()
    if raw.startswith(GLOSSARY_MAGIC):
        return decrypt_glossary(raw, os.environ.get('DB_ENCRYPTION_KEY', ''))
    return json.loads(raw)


def _normalize(title: str) -> str:
    return "".join(unicodedata.normalize("NFKC", title).split()).casefold()


def course_terms(title: str, path: Path | None = None) -> list[str]:
    """Exact normalized title/alias matching; invalid config never blocks ASR."""
    if not title:
        return []
    try:
        entries = _entries(path)
        if not isinstance(entries, list):
            return []
        for entry in entries[:100]:
            if not isinstance(entry, dict):
                continue
            aliases = entry.get("courses", [])
            if not isinstance(aliases, list) or not any(
                isinstance(alias, str) and _normalize(alias) == _normalize(title)
                for alias in aliases
            ):
                continue
            values = entry.get("terms", [])
            if not isinstance(values, list):
                return []
            terms = []
            for value in values:
                if not isinstance(value, str):
                    continue
                value = value.strip()
                if (not value or len(value) > MAX_TERM_CHARS
                        or any(unicodedata.category(c).startswith("C") for c in value)
                        or any(c in value for c in "<>") or value in terms):
                    continue
                terms.append(value)
                if len(terms) == MAX_TERMS:
                    break
            return terms
    except (OSError, ValueError, TypeError):
        pass
    return []


def terminology_reference(title: str) -> str:
    terms = course_terms(title)
    if not terms:
        return ""
    return ("\n\n<terminology_reference>\n"
            + json.dumps(terms, ensure_ascii=False)
            + "\n</terminology_reference>")
