"""
Raw-body HMAC-SHA256 signature validation for WhatsApp Cloud API webhooks.

Meta signs each webhook POST body with the app secret and sends it as
`X-Hub-Signature-256: sha256=<hex>`. Validating this - over the exact raw
bytes received, before any JSON parsing - is what proves a request
actually came from Meta and not from an arbitrary caller who found the
URL.
"""

import hashlib
import hmac
import re

_SIGNATURE_PREFIX = "sha256="
# Accepts only sha256=<64 hex characters> - anything else (wrong length,
# non-hex characters, a different algorithm prefix) is rejected outright,
# before any comparison is attempted.
_SIGNATURE_PATTERN = re.compile(r"^sha256=[0-9a-fA-F]{64}$")


def verify_signature(app_secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """Return True if `signature_header` is a valid HMAC-SHA256 of `raw_body`."""

    if not signature_header or not _SIGNATURE_PATTERN.match(signature_header):
        return False

    provided_hex = signature_header[len(_SIGNATURE_PREFIX):]
    expected_hex = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    return hmac.compare_digest(provided_hex, expected_hex)
