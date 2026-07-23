# tests/test_cred_resolution.py
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import secretbox
import server


def _setup():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("bob", "bob's long password")
    key = secretbox.load_key(secretbox.gen_key())
    op_tts = {"backend": "modal", "modal_endpoint": "https://operator.modal.run",
              "modal_token_id": "op-id", "modal_token_secret": "op-secret"}
    worker = server.Worker(lib, "af_heart", 1.0, 150, tts_cfg=op_tts,
                           users=users, secret_key=key)
    return lib, users, key, worker


def test_owner_without_profile_uses_operator():
    lib, users, key, worker = _setup()
    tts, llm, billed = worker._resolve_creds("bob")
    assert billed == "operator"
    assert tts["modal_endpoint"] == "https://operator.modal.run"


def test_owner_with_profile_uses_their_endpoint():
    lib, users, key, worker = _setup()
    profile = {"tts": {"endpoint": "https://bob.modal.run",
                       "token_id": "bob-id", "token_secret": "bob-secret"}}
    users.set_modal_enc("bob", secretbox.encrypt(json.dumps(profile), key, b"bob"))
    tts, llm, billed = worker._resolve_creds("bob")
    assert billed == "self"
    assert tts["modal_endpoint"] == "https://bob.modal.run"
    assert tts["modal_token_id"] == "bob-id"
    assert tts["backend"] == "modal"


def test_corrupt_blob_falls_back_to_operator_not_crash():
    lib, users, key, worker = _setup()
    users.set_modal_enc("bob", "not-a-valid-blob")
    tts, llm, billed = worker._resolve_creds("bob")
    assert billed == "operator"
