from __future__ import annotations

import re
from typing import Any


REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(r"(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|credential|license[_-]?key)", re.I)
_INLINE_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|credential|license[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")


def redact_sensitive(value: Any) -> Any:
    """Recursively redact secret-bearing fields and common inline credentials."""
    if isinstance(value, dict):
        return {
            key: REDACTED if _SENSITIVE_KEY.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        text = _BEARER.sub(f"Bearer {REDACTED}", value)
        return _INLINE_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    return value
