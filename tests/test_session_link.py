"""One-time login links: the Zotero plugin's auto-login. An authenticated
client mints a single-use, short-lived token; opening it sets the session
cookie for that same user and redirects to a local path."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth, server
from fastapi.testclient import TestClient


def _app():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("bob", "bob's long password")
    worker = server.Worker(lib, "af_heart", 1.0, 150, users=users)
    return server.create_app(lib, worker,
                             {"multiuser": True, "password_hash": auth.hash_password("x")},
                             users)


def _login(c):
    r = c.post("/login", data={"username": "bob", "password": "bob's long password"},
               follow_redirects=False)
    return {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}


def test_mint_requires_authentication():
    c = TestClient(_app())
    assert c.post("/api/session-link").status_code == 401   # login-gated /api


def test_link_logs_in_the_same_user_and_redirects():
    app = _app()
    c = TestClient(app)
    link = c.post("/api/session-link", headers=_login(c)).json()["path"]
    fresh = TestClient(app)                                  # no cookie yet
    r = fresh.get(link + "?next=/?play=abc", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?play=abc"
    cookie = r.cookies.get(auth.COOKIE)
    assert cookie, "the link must set a session cookie"
    me = TestClient(app).get("/api/me", headers={"Cookie": f"{auth.COOKIE}={cookie}"}).json()
    assert me["user"] == "bob"


def test_link_is_single_use():
    app = _app()
    c = TestClient(app)
    link = c.post("/api/session-link", headers=_login(c)).json()["path"]
    fresh = TestClient(app)
    assert fresh.get(link, follow_redirects=False).headers["location"] == "/"
    # a second use is dead — bounced to /login, no cookie
    r2 = fresh.get(link, follow_redirects=False)
    assert r2.headers["location"] == "/login"
    assert auth.COOKIE not in r2.cookies


def test_no_open_redirect():
    app = _app()
    c = TestClient(app)
    for evil in ("//evil.com", "/\\evil.com", "https://evil.com"):
        link = c.post("/api/session-link", headers=_login(c)).json()["path"]
        r = TestClient(app).get(link, params={"next": evil}, follow_redirects=False)
        assert r.headers["location"] == "/", f"{evil!r} must not be honored"


def test_unknown_token_bounces_to_login():
    r = TestClient(_app()).get("/auth/not-a-real-token", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
