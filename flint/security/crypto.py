"""
FlintX Field-Level Encryption
Encrypts sensitive fields at rest using Fernet (AES-128-CBC + HMAC-SHA256).

SENSITIVE FIELDS ENCRYPTED:
  - Payout email addresses (PayPal, Wise)
  - Bank account details if collected
  - Any PII beyond name/email

Set FIELD_ENCRYPTION_KEY in Railway Variables:
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os
import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken


def _get_fernet():
    key = os.getenv("FIELD_ENCRYPTION_KEY")
    if not key:
        # Derive from SECRET_KEY if no dedicated key set
        secret = os.getenv("SECRET_KEY", "change-this-in-production")
        key_bytes = hashlib.sha256(secret.encode()).digest()
        key = base64.urlsafe_b64encode(key_bytes)
    elif isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def encrypt_field(value: str) -> str:
    """Encrypt a sensitive string field before storing."""
    if not value:
        return value
    try:
        return _get_fernet().encrypt(value.encode()).decode()
    except Exception as e:
        print(f"[CRYPTO] encrypt_field error: {e}")
        return value


def decrypt_field(value: str) -> str:
    """Decrypt a sensitive string field after reading."""
    if not value:
        return value
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        return value  # Return as-is if not encrypted (handles migration)
