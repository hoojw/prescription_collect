"""TOP and Qimen request signing helpers.

The algorithm follows the Qimen documentation: sort all non-empty request
parameters except ``sign``, concatenate key and value pairs, append the exact
request body, then apply the selected digest algorithm.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping


class SignatureError(ValueError):
    """Raised when a requested signing algorithm is unsupported."""


def _canonical_bytes(params: Mapping[str, object], body: bytes = b"") -> bytes:
    chunks: list[bytes] = []
    for key in sorted(params):
        if key == "sign":
            continue
        value = params[key]
        if key == "" or value is None or str(value) == "":
            continue
        chunks.append(str(key).encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
    chunks.append(body)
    return b"".join(chunks)


def sign_request(
    params: Mapping[str, object],
    body: bytes,
    secret: str,
    sign_method: str,
) -> str:
    """Return the uppercase signature for a TOP/Qimen request."""
    if not secret:
        raise SignatureError("signing secret is empty")

    method = (sign_method or "md5").strip().lower().replace("_", "-")
    canonical = _canonical_bytes(params, body)
    secret_bytes = secret.encode("utf-8")

    if method in {"md5", ""}:
        digest = hashlib.md5(secret_bytes + canonical + secret_bytes).hexdigest()
    elif method in {"hmac", "hmac-md5"}:
        digest = hmac.new(secret_bytes, canonical, hashlib.md5).hexdigest()
    elif method in {"hmac-sha256", "sha256"}:
        digest = hmac.new(secret_bytes, canonical, hashlib.sha256).hexdigest()
    else:
        raise SignatureError(f"unsupported sign_method: {sign_method}")
    return digest.upper()


def signature_matches(
    params: Mapping[str, object],
    body: bytes,
    secret: str,
) -> bool:
    supplied = str(params.get("sign") or "").strip().upper()
    if not supplied:
        return False
    expected = sign_request(
        params=params,
        body=body,
        secret=secret,
        sign_method=str(params.get("sign_method") or "md5"),
    )
    return hmac.compare_digest(supplied, expected)
