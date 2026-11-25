"""Symmetric authenticated encryption primitives (ADR-018 / VULN-06).

Provides `MultiFernet`-backed envelope encryption for secrets stored at rest
(e.g. `webhook_endpoints.signing_secret`, `previous_signing_secret`, and
Phase 4 Taiwan e-invoice credentials).

Format:
    enc:v1:<fernet-token>

Key rotation:
- `MINI_ERP_SECRET_ENCRYPTION_KEYS` accepts comma-separated or JSON list keys.
- `keys[0]` is always the active primary key used for new encryptions/rotations.
- `keys[1:]` remain active for decryption and seamless rotation via
  `rotate_encrypted_secret(...)` / `manage_integrations webhooks reencrypt-secrets`.
- In staging/production (`settings.is_production_like`), missing keys fail closed.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.exceptions import SecretDecryptionError, SecretEncryptionConfigError
from app.core.settings import get_settings

logger = logging.getLogger("app.core.crypto")

ENCRYPTED_SECRET_PREFIX = "enc:v1:"
_DEV_FALLBACK_SEED = b"mini-erp-dev-only-secret-encryption-key-v1"
_DEV_FALLBACK_WARNED = False


def _dev_fallback_key() -> bytes:
    """Deterministic local dev/test key so multi-process CLI/worker works out of the box."""
    digest = hashlib.sha256(_DEV_FALLBACK_SEED).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache(maxsize=16)
def _build_fernet_bundle(
    keys_tuple: tuple[str, ...],
    is_production_like: bool,
) -> tuple[Fernet, MultiFernet]:
    global _DEV_FALLBACK_WARNED

    if not keys_tuple:
        if is_production_like:
            raise SecretEncryptionConfigError(
                "MINI_ERP_SECRET_ENCRYPTION_KEYS must be set when running outside "
                "development/test environments."
            )
        if not _DEV_FALLBACK_WARNED:
            logger.warning(
                "MINI_ERP_SECRET_ENCRYPTION_KEYS is unset; using deterministic "
                "development-only fallback key. Do NOT use this in staging/production."
            )
            _DEV_FALLBACK_WARNED = True
        primary = Fernet(_dev_fallback_key())
        return primary, MultiFernet([primary])

    instances: list[Fernet] = []
    for raw_key in keys_tuple:
        if not raw_key or any(ch.isspace() for ch in raw_key):
            raise SecretEncryptionConfigError(
                "Invalid Fernet key configured in MINI_ERP_SECRET_ENCRYPTION_KEYS."
            )
        try:
            instances.append(Fernet(raw_key.encode("ascii")))
        except (UnicodeEncodeError, ValueError, TypeError) as exc:
            raise SecretEncryptionConfigError(
                "Invalid Fernet key configured in MINI_ERP_SECRET_ENCRYPTION_KEYS."
            ) from exc

    return instances[0], MultiFernet(instances)


def _get_fernet_bundle() -> tuple[Fernet, MultiFernet]:
    settings = get_settings()
    return _build_fernet_bundle(
        tuple(settings.secret_encryption_keys),
        settings.is_production_like,
    )


def encrypt_secret(plaintext: str) -> str:
    """Encrypt plaintext secret using the primary Fernet key (`enc:v1:<token>`)."""
    if not plaintext:
        raise ValueError("Cannot encrypt an empty secret.")
    _, multi = _get_fernet_bundle()
    token = multi.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_SECRET_PREFIX}{token}"


def decrypt_secret(stored_value: str) -> str:
    """Decrypt an `enc:v1:` secret, or pass through legacy unencrypted plaintext."""
    if not stored_value.startswith(ENCRYPTED_SECRET_PREFIX):
        return stored_value

    raw_token = stored_value.removeprefix(ENCRYPTED_SECRET_PREFIX)
    if not raw_token:
        raise SecretDecryptionError("Encrypted secret payload is empty.")

    _, multi = _get_fernet_bundle()
    try:
        plaintext_bytes = multi.decrypt(raw_token.encode("ascii"))
        return plaintext_bytes.decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise SecretDecryptionError(
            "Failed to decrypt stored secret with configured encryption keys."
        ) from exc


def needs_reencryption(stored_value: str) -> bool:
    """Return True if stored_value is unencrypted plaintext or encrypted by a retired key."""
    if not stored_value.startswith(ENCRYPTED_SECRET_PREFIX):
        return True

    raw_token = stored_value.removeprefix(ENCRYPTED_SECRET_PREFIX)
    if not raw_token:
        raise SecretDecryptionError("Encrypted secret payload is empty.")

    primary, multi = _get_fernet_bundle()
    try:
        token_bytes = raw_token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SecretDecryptionError(
            "Encrypted secret token contains non-ASCII characters."
        ) from exc

    try:
        primary.decrypt(token_bytes)
        return False
    except InvalidToken:
        try:
            multi.decrypt(token_bytes)
        except InvalidToken as exc:
            raise SecretDecryptionError(
                "Failed to verify stored secret against any configured encryption key."
            ) from exc
        return True


def rotate_encrypted_secret(stored_value: str) -> str:
    """Re-encrypt plaintext or rotate older MultiFernet ciphertext to the active primary key."""
    if not stored_value.startswith(ENCRYPTED_SECRET_PREFIX):
        return encrypt_secret(stored_value)

    raw_token = stored_value.removeprefix(ENCRYPTED_SECRET_PREFIX)
    if not raw_token:
        raise SecretDecryptionError("Encrypted secret payload is empty.")

    _, multi = _get_fernet_bundle()
    try:
        rotated_token = multi.rotate(raw_token.encode("ascii")).decode("ascii")
        return f"{ENCRYPTED_SECRET_PREFIX}{rotated_token}"
    except (InvalidToken, UnicodeEncodeError, ValueError) as exc:
        raise SecretDecryptionError(
            "Failed to rotate stored secret with configured encryption keys."
        ) from exc
