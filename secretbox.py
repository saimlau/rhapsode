# secretbox.py
"""Reversible encryption for per-user secrets (Modal profiles).

Unlike passwords and invite tokens, which are HASHED (one-way), a Modal
profile must be recovered to be used, so it is ENCRYPTED with AES-256-GCM.
The 256-bit key lives in config.toml, never in users.json, so a backup of the
library directory alone cannot decrypt anything. The username is bound in as
additional authenticated data (AAD): a ciphertext moved to another user's
record fails to decrypt.
"""

import base64
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE = 12  # bytes; the standard GCM nonce length


def gen_key():
    """A fresh base64 key for the operator to paste into config.toml."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def load_key(b64):
    raw = base64.b64decode(b64)
    if len(raw) != 32:
        raise ValueError("secrets key must be base64 of exactly 32 bytes")
    return raw


def encrypt(plaintext, key, aad):
    nonce = secrets.token_bytes(_NONCE)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
    return base64.b64encode(nonce + ct).decode()


def decrypt(blob, key, aad):
    raw = base64.b64decode(blob)
    nonce, ct = raw[:_NONCE], raw[_NONCE:]
    return AESGCM(key).decrypt(nonce, ct, aad).decode()
