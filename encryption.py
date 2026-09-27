"""
Envelope encryption for wallet mnemonics.

Flow:
  1. Generate a random per-wallet data key.
  2. Encrypt the mnemonic with the data key (Fernet/AES).
  3. Encrypt ("wrap") the data key itself with the master key.
  4. Store both ciphertexts. Never store the raw data key or mnemonic.

To decrypt: unwrap the data key with the master key, then decrypt the
mnemonic with the data key.

PRODUCTION NOTE: MASTER_ENCRYPTION_KEY should come from a KMS/HSM, not an
env var. This module isolates that dependency behind wrap_key/unwrap_key
so swapping in AWS KMS / GCP KMS / Vault later is a small change here,
not a rewrite of the callers.
"""

import base64
import os
from cryptography.fernet import Fernet

from config import config

_master_fernet = Fernet(config.MASTER_ENCRYPTION_KEY.encode()) if config.MASTER_ENCRYPTION_KEY else None


def _require_master_key():
    if _master_fernet is None:
        raise RuntimeError(
            "MASTER_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "and put it in .env. In production this should come from a KMS, not a flat file."
        )


def generate_data_key() -> bytes:
    return Fernet.generate_key()


def wrap_key(data_key: bytes) -> str:
    _require_master_key()
    return _master_fernet.encrypt(data_key).decode()


def unwrap_key(wrapped_key: str) -> bytes:
    _require_master_key()
    return _master_fernet.decrypt(wrapped_key.encode())


def encrypt_mnemonic(mnemonic: str) -> tuple[str, str]:
    """Returns (encrypted_mnemonic, wrapped_data_key) for storage."""
    data_key = generate_data_key()
    f = Fernet(data_key)
    encrypted_mnemonic = f.encrypt(mnemonic.encode()).decode()
    wrapped_data_key = wrap_key(data_key)
    return encrypted_mnemonic, wrapped_data_key


def decrypt_mnemonic(encrypted_mnemonic: str, wrapped_data_key: str) -> str:
    data_key = unwrap_key(wrapped_data_key)
    f = Fernet(data_key)
    return f.decrypt(encrypted_mnemonic.encode()).decode()
