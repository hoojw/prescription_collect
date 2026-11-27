"""Shared drug title/specification cleanup for supported commerce platforms."""

from __future__ import annotations

import re
from typing import Any


EXPIRY_SUFFIX_RE = re.compile(
    r"\s*(?:"
    r"【[^】]*(?:到期|有效期|效期|过期|临期)[^】]*】"
    r"|\[[^\]]*(?:到期|有效期|效期|过期|临期)[^\]]*\]"
    r")\s*$",
    re.IGNORECASE,
)


def strip_expiry_suffix(value: Any) -> str:
    """Remove trailing expiry labels while preserving all other title text."""
    text = str(value or "").strip()
    while text:
        cleaned = EXPIRY_SUFFIX_RE.sub("", text).strip()
        if cleaned == text:
            break
        text = cleaned
    return text
