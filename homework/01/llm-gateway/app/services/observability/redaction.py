"""敏感数据控制：正文不入库，只留长度与 hash；密钥/PII 模式脱敏。"""
from __future__ import annotations

import hashlib
import re

# 常见密钥形态：sk-...、Bearer xxx、anthropic/x-api-key 等
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer|api[_-]?key|x-api-key)\s*[:=]?\s*[A-Za-z0-9\-._]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?END[A-Z ]*PRIVATE KEY-----", re.S),
]


def redact(text: str) -> str:
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def fingerprint(payload: str | bytes) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def text_meta(text: str) -> dict[str, int | str]:
    """落库用的元信息：只含长度与 hash，绝不含正文。"""
    return {"chars": len(text), "sha256": fingerprint(text)}
