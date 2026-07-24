# tests/test_account_modal_probe.py
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import secretbox
import server
import rhapsode as p2a
from fastapi.testclient import TestClient


def _app(monkey_ok=True):
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("bob", "bob's long password")
    key = secretbox.load_key(secretbox.gen_key())
    worker = server.Worker(lib, "af_heart", 1.0, 150, users=users, secret_key=key)
    app = server.create_app(lib, worker,
                            {"password_hash": auth.hash_password("x"),
                             "multiuser": True}, users, secret_key=key)
    return app, lib


def _login(c):
    r = c.post("/login", data={"username": "bob", "password": "bob's long password"},
               follow_redirects=False)
    return {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}


def test_probe_ok(monkeypatch):
    app, lib = _app()
    c = TestClient(app)
    h = _login(c)
    c.put("/api/account/modal", headers=h, json={
        "tts": {"endpoint": "https://bob.modal.run",
                "token_id": "i", "token_secret": "s"}})
    monkeypatch.setattr(p2a, "_modal_unit_audio",
                        lambda *a, **k: iter([(0, [b"RIFFfakeaudio"])]))
    r = c.post("/api/account/modal/test", headers=h).json()
    assert r["ok"] is True


def test_probe_error_is_generic(monkeypatch):
    app, lib = _app()
    c = TestClient(app)
    h = _login(c)
    c.put("/api/account/modal", headers=h, json={
        "tts": {"endpoint": "https://bob.modal.run",
                "token_id": "i", "token_secret": "s-SEEKRIT"}})

    def boom(*a, **k):
        raise RuntimeError("401 from https://bob.modal.run with s-SEEKRIT")
    monkeypatch.setattr(p2a, "_modal_unit_audio", boom)
    r = c.post("/api/account/modal/test", headers=h).json()
    assert r["ok"] is False
    assert "s-SEEKRIT" not in r["error"]      # never echo the token


def test_llm_probe_ok(monkeypatch):
    app, lib = _app()
    c = TestClient(app)
    h = _login(c)
    c.put("/api/account/modal", headers=h, json={
        "llm": {"api_base_url": "https://bob-llm.modal.run/v1", "api_key": "k"}})
    import requests

    class _R:
        def raise_for_status(self):
            pass
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    r = c.post("/api/account/modal/test?group=llm", headers=h).json()
    assert r["ok"] is True


def test_llm_probe_error_is_generic(monkeypatch):
    app, lib = _app()
    c = TestClient(app)
    h = _login(c)
    c.put("/api/account/modal", headers=h, json={
        "llm": {"api_base_url": "https://bob-llm.modal.run/v1", "api_key": "sk-SEEKRIT"}})
    import requests

    def boom(*a, **k):
        raise RuntimeError("401 unauthorized for key sk-SEEKRIT")
    monkeypatch.setattr(requests, "get", boom)
    r = c.post("/api/account/modal/test?group=llm", headers=h).json()
    assert r["ok"] is False
    assert "sk-SEEKRIT" not in r["error"]      # never echo the key


def test_llm_probe_400_when_not_attached():
    app, lib = _app()
    c = TestClient(app)
    r = c.post("/api/account/modal/test?group=llm", headers=_login(c))
    assert r.status_code == 400
