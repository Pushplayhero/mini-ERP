"""Unit tests for app.core.crypto (ADR-018 / VULN-06 MultiFernet encryption)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.core.crypto import (
    ENCRYPTED_SECRET_PREFIX,
    decrypt_secret,
    encrypt_secret,
    needs_reencryption,
    rotate_encrypted_secret,
)
from app.core.exceptions import SecretDecryptionError, SecretEncryptionConfigError
from app.core.settings import get_settings


def test_encrypt_decrypt_roundtrip_and_non_determinism(monkeypatch: pytest.MonkeyPatch) -> None:
    key1 = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", key1)
    get_settings.cache_clear()
    try:
        ct1 = encrypt_secret("whsec_secret_payload_alpha")
        ct2 = encrypt_secret("whsec_secret_payload_alpha")
        assert ct1.startswith(ENCRYPTED_SECRET_PREFIX)
        assert ct2.startswith(ENCRYPTED_SECRET_PREFIX)
        assert ct1 != ct2
        assert decrypt_secret(ct1) == "whsec_secret_payload_alpha"
        assert decrypt_secret(ct2) == "whsec_secret_payload_alpha"
        assert needs_reencryption(ct1) is False
    finally:
        get_settings.cache_clear()


def test_multifernet_key_rotation_and_reencryption(monkeypatch: pytest.MonkeyPatch) -> None:
    key_old = Fernet.generate_key().decode("ascii")
    key_new = Fernet.generate_key().decode("ascii")

    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", key_old)
    get_settings.cache_clear()
    try:
        encrypted_under_old = encrypt_secret("whsec_rotate_me_001")
    finally:
        get_settings.cache_clear()

    # Deploy with [key_new, key_old] -> old ciphertext still decrypts, needs re-encryption
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", f"{key_new},{key_old}")
    get_settings.cache_clear()
    try:
        assert decrypt_secret(encrypted_under_old) == "whsec_rotate_me_001"
        assert needs_reencryption(encrypted_under_old) is True
        assert needs_reencryption("legacy_plaintext_secret") is True

        rotated = rotate_encrypted_secret(encrypted_under_old)
        assert rotated.startswith(ENCRYPTED_SECRET_PREFIX)
        assert rotated != encrypted_under_old
        assert needs_reencryption(rotated) is False

        upgraded_plain = rotate_encrypted_secret("legacy_plaintext_secret")
        assert upgraded_plain.startswith(ENCRYPTED_SECRET_PREFIX)
        assert decrypt_secret(upgraded_plain) == "legacy_plaintext_secret"
        assert needs_reencryption(upgraded_plain) is False
    finally:
        get_settings.cache_clear()

    # Retire key_old completely -> rotated ciphertext decrypts with key_new only
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", key_new)
    get_settings.cache_clear()
    try:
        assert decrypt_secret(rotated) == "whsec_rotate_me_001"
        with pytest.raises(SecretDecryptionError, match="Failed to decrypt stored secret"):
            decrypt_secret(encrypted_under_old)
    finally:
        get_settings.cache_clear()


def test_tampered_ciphertext_or_wrong_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    key_a = Fernet.generate_key().decode("ascii")
    key_b = Fernet.generate_key().decode("ascii")

    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", key_a)
    get_settings.cache_clear()
    try:
        ct = encrypt_secret("whsec_sensitive_token")
    finally:
        get_settings.cache_clear()

    # Tamper payload
    tampered = ct[:-4] + ("AAAA" if not ct.endswith("AAAA") else "BBBB")
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", key_a)
    get_settings.cache_clear()
    try:
        with pytest.raises(SecretDecryptionError) as exc_info:
            decrypt_secret(tampered)
        assert key_a not in str(exc_info.value)
        assert "whsec_sensitive_token" not in str(exc_info.value)
    finally:
        get_settings.cache_clear()

    # Wrong key
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", key_b)
    get_settings.cache_clear()
    try:
        with pytest.raises(SecretDecryptionError):
            decrypt_secret(ct)
        with pytest.raises(SecretDecryptionError):
            needs_reencryption(ct)
        with pytest.raises(SecretDecryptionError):
            rotate_encrypted_secret(ct)
    finally:
        get_settings.cache_clear()


def test_production_without_encryption_keys_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(SecretEncryptionConfigError, match="MINI_ERP_SECRET_ENCRYPTION_KEYS"):
            encrypt_secret("whsec_anything")
    finally:
        get_settings.cache_clear()


def test_empty_plaintext_or_malformed_prefix_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("MINI_ERP_SECRET_ENCRYPTION_KEYS", key)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="empty secret"):
            encrypt_secret("")
        with pytest.raises(SecretDecryptionError):
            decrypt_secret(ENCRYPTED_SECRET_PREFIX)
        with pytest.raises(SecretDecryptionError):
            needs_reencryption(ENCRYPTED_SECRET_PREFIX)
        with pytest.raises(SecretDecryptionError):
            rotate_encrypted_secret(ENCRYPTED_SECRET_PREFIX)
        assert decrypt_secret("plain_legacy_value") == "plain_legacy_value"
    finally:
        get_settings.cache_clear()
