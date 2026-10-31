"""AES-256-GCM helpers for encrypting small secrets (e.g. TOTP seeds) at rest."""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from payroll import app_settings

_NONCE_BYTES = 12
_TAG_BYTES = 16


def _resolve_key(key: bytes | None) -> bytes:
    return key if key is not None else base64.b64decode(app_settings.encryption_key)


def encrypt_secret(plaintext: str, key: bytes | None = None) -> str:
    """Encrypt ``plaintext``; returns urlsafe-base64 of ``nonce + ciphertext``."""
    try:
        aes = AESGCM(_resolve_key(key))
    except ValueError as exc:
        raise ValueError("invalid encryption key") from exc
    nonce = os.urandom(_NONCE_BYTES)
    return base64.urlsafe_b64encode(nonce + aes.encrypt(nonce, plaintext.encode(), None)).decode()


def decrypt_secret(token: str, key: bytes | None = None) -> str:
    """Decrypt a token from ``encrypt_secret``; any failure raises ``ValueError``."""
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        if len(raw) < _NONCE_BYTES + _TAG_BYTES:
            raise ValueError("token too short")
        aes = AESGCM(_resolve_key(key))
        return aes.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], None).decode()
    except (InvalidTag, binascii.Error, UnicodeError) as exc:
        raise ValueError("could not decrypt secret") from exc
