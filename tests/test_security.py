"""Password hashing and AES-256-GCM secret encryption."""

import pytest

from payagent import security


def test_password_hash_is_salted_and_verifiable():
    first = security.hash_password("correct-horse-battery")
    second = security.hash_password("correct-horse-battery")
    assert first != second
    assert security.verify_password(first, "correct-horse-battery")
    assert not security.verify_password(first, "wrong-password")


def test_encrypt_decrypt_roundtrip():
    envelope = security.encrypt_secret("super-secret", aad="store_demo:paypal:sandbox")
    assert "super-secret" not in envelope
    assert security.decrypt_secret(envelope, aad="store_demo:paypal:sandbox") == "super-secret"


def test_ciphertext_is_bound_to_its_context():
    envelope = security.encrypt_secret("super-secret", aad="store_demo:paypal:sandbox")
    with pytest.raises(Exception):
        security.decrypt_secret(envelope, aad="store_other:paypal:sandbox")


def test_tampered_ciphertext_is_rejected():
    envelope = security.encrypt_secret("super-secret", aad="ctx")
    version, nonce, ciphertext = envelope.split(".")
    tampered = ".".join((version, nonce, ciphertext[:-2] + "AA"))
    with pytest.raises(Exception):
        security.decrypt_secret(tampered, aad="ctx")


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(security.EncryptionNotConfigured):
        security.encrypt_secret("x", aad="ctx")


def test_mask_never_reveals_the_secret():
    assert security.mask_secret("abcdefghijklmnop") == "••••mnop"
    assert "abcdefghij" not in security.mask_secret("abcdefghijklmnop")


def test_rate_limiter_blocks_after_limit():
    limiter = security.RateLimiter()
    assert all(limiter.hit("k", 3) for _ in range(3))
    assert limiter.hit("k", 3) is False
    limiter.reset("k")
    assert limiter.hit("k", 3) is True
