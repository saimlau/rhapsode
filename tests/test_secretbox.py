# tests/test_secretbox.py
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import secretbox


def test_round_trip():
    key = secretbox.load_key(secretbox.gen_key())
    blob = secretbox.encrypt("hunter2-token", key, b"bob")
    assert secretbox.decrypt(blob, key, b"bob") == "hunter2-token"


def test_blob_is_not_the_plaintext():
    key = secretbox.load_key(secretbox.gen_key())
    blob = secretbox.encrypt("secret-value", key, b"bob")
    assert "secret-value" not in blob
    assert "secret-value" not in base64.b64decode(blob).decode("latin-1")


def test_wrong_key_fails_closed():
    k1 = secretbox.load_key(secretbox.gen_key())
    k2 = secretbox.load_key(secretbox.gen_key())
    blob = secretbox.encrypt("x", k1, b"bob")
    with pytest.raises(Exception):
        secretbox.decrypt(blob, k2, b"bob")


def test_wrong_aad_fails_closed():
    """A blob copied into another user's record must not decrypt."""
    key = secretbox.load_key(secretbox.gen_key())
    blob = secretbox.encrypt("x", key, b"bob")
    with pytest.raises(Exception):
        secretbox.decrypt(blob, key, b"mallory")


def test_load_key_rejects_wrong_length():
    with pytest.raises(ValueError):
        secretbox.load_key(base64.b64encode(b"too-short").decode())
