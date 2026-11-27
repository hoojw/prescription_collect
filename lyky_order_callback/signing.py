"""Signature verification defined by the 邻医云 open-platform documentation."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping


class SignatureError(ValueError):
    """Raised for malformed signature input."""


def sign_form(form: Mapping[str, str], app_secret: str) -> str:
    """Return Base64(MD5-hexdigest(concatenated sorted system parameter values)).

    The provider states that every system-level parameter other than
    ``signature`` participates, together with ``app_secret``. Application
    parameters are enclosed in ``body`` and therefore excluded.
    """

    if not app_secret:
        raise SignatureError("app_secret is required")
    values: dict[str, str] = {}
    for key, value in form.items():
        if key in {"signature", "body"}:
            continue
        if key == "app_secret":
            raise SignatureError("app_secret must not be supplied by the caller")
        values[str(key)] = str(value)
    values["app_secret"] = app_secret
    source = "".join(values[key] for key in sorted(values))
    digest_hex = hashlib.md5(source.encode("utf-8")).hexdigest().encode("ascii")
    return base64.b64encode(digest_hex).decode("ascii")


def signature_matches(form: Mapping[str, str], app_secret: str) -> bool:
    supplied = str(form.get("signature") or "")
    if not supplied:
        return False
    try:
        expected = sign_form(form, app_secret)
    except SignatureError:
        return False
    return hmac.compare_digest(supplied, expected)

