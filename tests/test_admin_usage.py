# tests/test_admin_usage.py
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
    users.create("admin", None, admin=True, pw_hash=auth.hash_password("adminpw is long"))
    users.create("bob", "bob's long password")
    worker = server.Worker(lib, "af_heart", 1.0, 150, users=users)
    lib.data["papers"]["p"] = {"id": "p", "owner": "bob", "billed": "operator",
                               "duration": 3600, "status": "ready"}
    lib.data["order"].append("p")
    lib.save()
    app = server.create_app(lib, worker,
                            {"password_hash": auth.hash_password("x"),
                             "multiuser": True}, users, secret_key=None)
    return app, users


def _login(c, who, pw):
    r = c.post("/login", data={"username": who, "password": pw},
               follow_redirects=False)
    return {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}


def test_users_payload_has_usage_and_health():
    app, users = _app()
    c = TestClient(app)
    h = _login(c, "admin", "adminpw is long")
    d = c.get("/api/users", headers=h).json()
    bob = next(u for u in d["users"] if u["name"] == "bob")
    assert bob["operator_hours"] == 1.0
    assert bob["self_hosting"] is False
    assert "health" in d and "free_gb" in d["health"]


def test_admin_sets_a_quota():
    app, users = _app()
    c = TestClient(app)
    h = _login(c, "admin", "adminpw is long")
    assert c.put("/api/users/bob", headers=h,
                 json={"quota": {"tts_hours": 3.0}}).status_code == 200
    assert users.get_quota("bob") == {"tts_hours": 3.0}
    # null clears it
    c.put("/api/users/bob", headers=h, json={"quota": {"tts_hours": None}})
    assert users.get_quota("bob") == {}


def test_non_admin_cannot_set_a_quota():
    app, users = _app()
    c = TestClient(app)
    h = _login(c, "bob", "bob's long password")
    r = c.put("/api/users/bob", headers=h, json={"quota": {"tts_hours": 1.0}})
    assert r.status_code == 404
    assert users.get_quota("bob") == {}
