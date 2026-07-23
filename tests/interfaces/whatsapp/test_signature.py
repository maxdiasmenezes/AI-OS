"""Tests for raw-body HMAC-SHA256 webhook signature validation."""

import hashlib
import hmac

import interfaces.whatsapp.signature as signature_module
from interfaces.whatsapp.signature import verify_signature

_APP_SECRET = "test-app-secret"
_BODY = b'{"hello": "world"}'


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_is_accepted():
    header = _sign(_APP_SECRET, _BODY)

    assert verify_signature(_APP_SECRET, _BODY, header) is True


def test_signature_for_a_different_body_is_rejected():
    header = _sign(_APP_SECRET, b'{"different": "body"}')

    assert verify_signature(_APP_SECRET, _BODY, header) is False


def test_signature_with_wrong_secret_is_rejected():
    header = _sign("wrong-secret", _BODY)

    assert verify_signature(_APP_SECRET, _BODY, header) is False


def test_missing_header_is_rejected():
    assert verify_signature(_APP_SECRET, _BODY, None) is False


def test_header_without_sha256_prefix_is_rejected():
    assert verify_signature(_APP_SECRET, _BODY, "not-a-real-signature") is False


def test_malformed_hex_is_rejected():
    assert verify_signature(_APP_SECRET, _BODY, "sha256=not-hex") is False


def test_hex_shorter_than_64_characters_is_rejected():
    header = _sign(_APP_SECRET, _BODY)[: -1]  # drop the last hex character

    assert verify_signature(_APP_SECRET, _BODY, header) is False


def test_hex_longer_than_64_characters_is_rejected():
    header = _sign(_APP_SECRET, _BODY) + "0"  # one extra hex character

    assert verify_signature(_APP_SECRET, _BODY, header) is False


def test_uses_hmac_compare_digest_for_the_comparison(monkeypatch):
    # Spies on (rather than replaces the outcome of) the real
    # hmac.compare_digest, proving the constant-time comparison path is
    # actually exercised rather than a plain `==`.
    calls = []
    real_compare_digest = hmac.compare_digest

    def spy_compare_digest(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(signature_module.hmac, "compare_digest", spy_compare_digest)

    header = _sign(_APP_SECRET, _BODY)
    assert verify_signature(_APP_SECRET, _BODY, header) is True

    assert len(calls) == 1
