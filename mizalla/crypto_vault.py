"""Credential Encryption Vault for PyWA Server Management.

Provides symmetric Fernet-based encryption and decryption for remote server passwords,
private keys, and sensitive connection credentials using the application's SECRET_KEY.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _get_fernet_key() -> bytes:
    """Derive a URL-safe base64-encoded 32-byte key from app secret key."""
    try:
        secret = current_app.config.get("SECRET_KEY", "mizalla-default-vault-key-seed")
    except RuntimeError:
        secret = "mizalla-default-vault-key-seed"

    if isinstance(secret, str):
        secret_bytes = secret.encode("utf-8")
    else:
        secret_bytes = bytes(secret)

    # Use SHA-256 to produce an exact 32-byte digest, then base64-encode for Fernet
    digest = hashlib.sha256(secret_bytes).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plain_text: Optional[str]) -> str:
    """Encrypt a plaintext secret (password or private key) into Fernet token string.
    
    Returns an empty string if plain_text is None or empty.
    """
    if not plain_text:
        return ""
    fernet = Fernet(_get_fernet_key())
    token = fernet.encrypt(plain_text.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_secret(cipher_text: Optional[str]) -> str:
    """Decrypt a Fernet token string back into plaintext.
    
    Returns an empty string if cipher_text is empty or invalid.
    """
    if not cipher_text:
        return ""
    try:
        fernet = Fernet(_get_fernet_key())
        decrypted = fernet.decrypt(cipher_text.encode("utf-8"))
        return decrypted.decode("utf-8")
    except (InvalidToken, Exception):
        return ""
