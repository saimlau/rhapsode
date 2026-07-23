# tests/test_settings_page.py
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import server
from fastapi.testclient import TestClient


def _app():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("bob", "bob's long password")
    worker = server.Worker(lib, "af_heart", 1.0, 150, users=users)
    app = server.create_app(lib, worker,
                            {"password_hash": auth.hash_password("x"),
                             "multiuser": True}, users, secret_key=None)
    return app


def test_settings_page_served_to_a_signed_in_user():
    c = TestClient(_app())
    r = c.post("/login", data={"username": "bob", "password": "bob's long password"},
               follow_redirects=False)
    h = {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}
    resp = c.get("/settings", headers=h)
    assert resp.status_code == 200
    assert "Modal" in resp.text


def test_settings_page_404_when_signed_out():
    c = TestClient(_app())
    assert c.get("/settings", follow_redirects=False).status_code in (303, 401, 404)
