from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

_ASSOCIATED_DATA = b"asset-tracker/api-credential/v1"
_NONCE_BYTES = 12


class CryptoConfigError(RuntimeError):
    pass


def key_bytes(key_hex: str) -> bytes:
    """Decode a 64-char hex MASTER_KEY into 32 raw bytes."""
    try:
        key = bytes.fromhex(key_hex)
    except ValueError as exc:
        raise CryptoConfigError("MASTER_KEY must be a 64-character hex string") from exc

    if len(key) != 32:
        raise CryptoConfigError("MASTER_KEY must decode to exactly 32 bytes")
    return key


def _key() -> bytes:
    return key_bytes(settings.master_key)


def encrypt_with_key(key_hex: str, text: str) -> bytes:
    """Encrypt UTF-8 text with AES-256-GCM. Returns nonce || ciphertext."""
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key_bytes(key_hex)).encrypt(
        nonce,
        text.encode("utf-8"),
        _ASSOCIATED_DATA,
    )
    return nonce + ciphertext


def decrypt_with_key(key_hex: str, payload: bytes) -> str:
    if len(payload) <= _NONCE_BYTES:
        raise ValueError("Encrypted payload is too short")
    nonce, ciphertext = payload[:_NONCE_BYTES], payload[_NONCE_BYTES:]
    plaintext = AESGCM(key_bytes(key_hex)).decrypt(nonce, ciphertext, _ASSOCIATED_DATA)
    return plaintext.decode("utf-8")


def encrypt_text(plaintext: str) -> bytes:
    """Encrypt UTF-8 text with AES-256-GCM using settings.master_key."""
    return encrypt_with_key(settings.master_key, plaintext)


def decrypt_text(payload: bytes) -> str:
    return decrypt_with_key(settings.master_key, payload)
