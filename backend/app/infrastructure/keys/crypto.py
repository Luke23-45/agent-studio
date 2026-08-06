"""
Envelope encryption for provider credentials (Arch 6.3.9, P0-8).

Fernet envelope: the data key is injected via ``PROVIDER_KEY_ENCRYPTION_KEY``
(KMS-injected in production) or falls back to a local key file in dev
(``PROVIDER_KEY_ENCRYPTION_KEY_FILE``, auto-created on first use). Per-row
``kms_ref`` columns can record the KMS key reference without changing the
ciphertext layout, making the design KMS-ready.

Secrets never leave the backend and are never logged.
"""

import structlog
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)


class KeyUnavailableError(RuntimeError):
    """Raised when encryption/decryption cannot proceed (never silent)."""


class ProviderKeyDecryptionError(RuntimeError):
    """Raised when a stored secret cannot be decrypted with the master key."""


def _fernet() -> Any:
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise KeyUnavailableError(
            "cryptography package missing; install dependencies (pip install -e '.[all]')"
        ) from e
    return Fernet


def _validate_key(key: str) -> bytes:
    try:
        import base64

        raw = key.encode("ascii")
        if len(base64.urlsafe_b64decode(raw)) != 32:
            raise ValueError
        return raw
    except Exception as e:
        raise KeyUnavailableError(
            "PROVIDER_KEY_ENCRYPTION_KEY is not a valid Fernet key"
            " (generate with: python -c \"from cryptography.fernet import Fernet;"
            " print(Fernet.generate_key().decode())\")"
        ) from e


@lru_cache(maxsize=1)
def get_master_key() -> bytes:
    """Return the master encryption key: env-injected first, file fallback."""
    if settings.PROVIDER_KEY_ENCRYPTION_KEY:
        return _validate_key(settings.PROVIDER_KEY_ENCRYPTION_KEY)

    key_file = Path(settings.PROVIDER_KEY_ENCRYPTION_KEY_FILE)
    if key_file.exists():
        return key_file.read_bytes().strip()

    if settings.is_production:
        raise KeyUnavailableError(
            "PROVIDER_KEY_ENCRYPTION_KEY is not set; refusing to auto-generate"
            " a master key in production (inject via env/KMS instead)"
        )

    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32))
    key_file.write_bytes(key)
    logger.warning(
        "provider_master_key_generated_dev_fallback",
        path=str(key_file),
        hint="set PROVIDER_KEY_ENCRYPTION_KEY in production",
    )
    return key


def encrypt_secret(plaintext: str) -> str:
    """Envelope-encrypt a provider API key; returns a URL-safe token."""
    return _fernet()(get_master_key()).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a provider API key token with the master key."""
    try:
        return _fernet()(get_master_key()).decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as e:
        raise ProviderKeyDecryptionError(
            "stored provider key cannot be decrypted (master key changed or KMS key rotated?)"
        ) from e
