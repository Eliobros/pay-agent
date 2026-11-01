"""Password hashing, authenticated secret encryption and rate limiting.

Two different operations live here on purpose:

* passwords are **hashed** (Argon2id, one way, never reversible);
* provider credentials are **encrypted** (AES-256-GCM, reversible, needs the
  master key that lives in the environment, never in the database).
"""

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Argon2id is the library default (time_cost=3, memory_cost=64 MiB, parallelism=4).
_hasher = PasswordHasher()
# Fixed dummy hash so a login for an unknown email costs the same as a real one.
_DUMMY_HASH = _hasher.hash("payagent-timing-equalizer")


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (InvalidHashError, VerificationError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def dummy_verify(password: str) -> None:
    """Burn the same work as a real verification, for non-existing accounts."""
    verify_password(_DUMMY_HASH, password)


# ---------------------------------------------------------------- sessions
def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Only the hash of a session token is stored, so a DB leak is not a login."""
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest((left or "").encode(), (right or "").encode())


# ---------------------------------------------------------------- secret encryption
class EncryptionNotConfigured(RuntimeError):
    """Raised when CREDENTIAL_ENCRYPTION_KEY is missing or malformed."""


ENVELOPE_VERSION = "v1"


def generate_key() -> str:
    """A fresh base64 master key for CREDENTIAL_ENCRYPTION_KEY."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _master_key() -> bytes:
    raw = os.getenv("CREDENTIAL_ENCRYPTION_KEY") or ""
    if not raw:
        raise EncryptionNotConfigured(
            "CREDENTIAL_ENCRYPTION_KEY is not set; cannot store payment credentials"
        )
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as e:
        raise EncryptionNotConfigured("CREDENTIAL_ENCRYPTION_KEY is not valid base64") from e
    if len(key) != 32:
        raise EncryptionNotConfigured(
            "CREDENTIAL_ENCRYPTION_KEY must decode to 32 bytes (AES-256)"
        )
    return key


def encrypt_secret(plaintext: str, aad: str) -> str:
    """AES-256-GCM encryption. ``aad`` binds the ciphertext to its owner/context."""
    nonce = os.urandom(12)
    ciphertext = AESGCM(_master_key()).encrypt(nonce, plaintext.encode(), aad.encode())
    return ".".join(
        (
            ENVELOPE_VERSION,
            base64.urlsafe_b64encode(nonce).decode(),
            base64.urlsafe_b64encode(ciphertext).decode(),
        )
    )


def decrypt_secret(envelope: str, aad: str) -> str:
    parts = (envelope or "").split(".")
    if len(parts) != 3 or parts[0] != ENVELOPE_VERSION:
        raise ValueError("unsupported secret envelope")
    nonce = base64.urlsafe_b64decode(parts[1])
    ciphertext = base64.urlsafe_b64decode(parts[2])
    return AESGCM(_master_key()).decrypt(nonce, ciphertext, aad.encode()).decode()


def mask_secret(secret: str) -> str:
    """What the dashboard is allowed to see after a secret was saved."""
    if not secret:
        return ""
    tail = secret[-4:] if len(secret) > 8 else ""
    return f"••••{tail}"


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Fixed-window counter in memory, enough for a single-process deployment."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float = 60.0) -> bool:
        """Record an attempt. Returns False when the limit is already reached."""
        now = time.time()
        with self._lock:
            recent = [t for t in self._hits.get(key, []) if now - t < window]
            recent.append(now)
            self._hits[key] = recent
            if len(self._hits) > 5000:  # keep the dict from growing forever
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < window}
            return len(recent) <= limit

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
