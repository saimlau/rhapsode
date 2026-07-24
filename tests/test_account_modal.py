# tests/test_account_modal.py
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import secretbox
import server
from fastapi.testclient import TestClient


def _app():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("bob", "bob's long password")
    users.create("mallory", "mallory's long password")
    key = secretbox.load_key(secretbox.gen_key())
    worker = server.Worker(lib, "af_heart", 1.0, 150, users=users, secret_key=key)
    app = server.create_app(lib, worker,
                            {"password_hash": auth.hash_password("x"),
                             "multiuser": True}, users, secret_key=key)
    return app, lib, users


def _login(c, who):
    pw = {"bob": "bob's long password", "mallory": "mallory's long password"}[who]
    r = c.post("/login", data={"username": who, "password": pw},
               follow_redirects=False)
    return {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}


def test_put_then_get_hides_the_secret():
    app, lib, users = _app()
    c = TestClient(app)
    h = _login(c, "bob")
    c.put("/api/account/modal", headers=h, json={
        "tts": {"endpoint": "https://bob.modal.run",
                "token_id": "bob-id", "token_secret": "bob-SECRET-9999"}})
    body = c.get("/api/account/modal", headers=h).json()
    assert body["tts"]["attached"] is True
    assert body["tts"]["endpoint"] == "https://bob.modal.run"
    assert body["tts"]["last4"] == "9999"
    # the secret is never returned in any form
    import json as _j
    assert "bob-SECRET-9999" not in _j.dumps(body)
    # and the stored blob is not the plaintext
    assert "bob-SECRET-9999" not in (users.get_modal_enc("bob") or "")


def test_delete_clears_the_profile():
    app, lib, users = _app()
    c = TestClient(app)
    h = _login(c, "bob")
    c.put("/api/account/modal", headers=h, json={
        "tts": {"endpoint": "https://bob.modal.run",
                "token_id": "i", "token_secret": "s"}})
    c.delete("/api/account/modal", headers=h)
    assert c.get("/api/account/modal", headers=h).json()["tts"]["attached"] is False


def test_a_user_cannot_touch_another_users_profile():
    """There is no cross-user route at all: /api/account/* is always 'me'."""
    app, lib, users = _app()
    c = TestClient(app)
    c.put("/api/account/modal", headers=_login(c, "bob"), json={
        "tts": {"endpoint": "https://bob.modal.run",
                "token_id": "i", "token_secret": "s"}})
    # mallory's GET reflects mallory, never bob
    body = c.get("/api/account/modal", headers=_login(c, "mallory")).json()
    assert body["tts"]["attached"] is False


def test_saving_llm_does_not_wipe_tts_and_vice_versa():
    """The two forms save independently — PUT merges, it does not replace."""
    app, lib, users = _app()
    c = TestClient(app)
    h = _login(c, "bob")
    c.put("/api/account/modal", headers=h, json={
        "tts": {"endpoint": "https://bob.modal.run",
                "token_id": "i", "token_secret": "s"}})
    c.put("/api/account/modal", headers=h, json={
        "llm": {"api_base_url": "https://bob-llm.modal.run/v1", "api_key": "sk-KEY-4242"}})
    body = c.get("/api/account/modal", headers=h).json()
    assert body["tts"]["attached"] is True, "the LLM save must not wipe TTS"
    assert body["llm"]["attached"] is True
    assert body["llm"]["base_url"] == "https://bob-llm.modal.run/v1"
    assert body["llm"]["last4"] == "4242"
    # neither secret is ever returned
    import json as _j
    assert "sk-KEY-4242" not in _j.dumps(body)


def test_clearing_one_group_keeps_the_other():
    app, lib, users = _app()
    c = TestClient(app)
    h = _login(c, "bob")
    c.put("/api/account/modal", headers=h, json={
        "tts": {"endpoint": "https://bob.modal.run", "token_id": "i", "token_secret": "s"}})
    c.put("/api/account/modal", headers=h, json={
        "llm": {"api_base_url": "https://bob-llm.modal.run/v1", "api_key": "k"}})
    c.delete("/api/account/modal", headers=h, params={"group": "tts"})
    body = c.get("/api/account/modal", headers=h).json()
    assert body["tts"]["attached"] is False, "only TTS was cleared"
    assert body["llm"]["attached"] is True, "LLM survives a scoped TTS clear"
    # clearing the last remaining group drops the whole blob
    c.delete("/api/account/modal", headers=h, params={"group": "llm"})
    assert users.get_modal_enc("bob") is None


def test_llm_save_alone_then_get():
    app, lib, users = _app()
    c = TestClient(app)
    h = _login(c, "bob")
    r = c.put("/api/account/modal", headers=h, json={
        "llm": {"api_base_url": "https://bob-llm.modal.run/v1", "api_key": "sk-9"}})
    assert r.status_code == 200
    body = c.get("/api/account/modal", headers=h).json()
    assert body["llm"]["attached"] is True and body["tts"]["attached"] is False
