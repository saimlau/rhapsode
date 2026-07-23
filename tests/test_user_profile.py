# tests/test_user_profile.py
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth


def _users():
    u = auth.Users(Path(tempfile.mkdtemp()))
    u.create("bob", "bob's long password")
    return u


def test_modal_enc_round_trips_and_persists():
    u = _users()
    assert u.get_modal_enc("bob") is None
    assert u.has_modal("bob") is False
    u.set_modal_enc("bob", "ENC-BLOB")
    assert u.get_modal_enc("bob") == "ENC-BLOB"
    assert u.has_modal("bob") is True
    # survives a reload from disk
    again = auth.Users(u.path.parent)
    assert again.get_modal_enc("bob") == "ENC-BLOB"


def test_clear_modal_enc():
    u = _users()
    u.set_modal_enc("bob", "ENC-BLOB")
    u.clear_modal_enc("bob")
    assert u.get_modal_enc("bob") is None
    assert u.has_modal("bob") is False


def test_quota_set_get_and_clear():
    u = _users()
    assert u.get_quota("bob") == {}
    u.set_quota("bob", "tts_hours", 3.0)
    assert u.get_quota("bob") == {"tts_hours": 3.0}
    u.set_quota("bob", "tts_hours", None)     # None removes the cap
    assert u.get_quota("bob") == {}
