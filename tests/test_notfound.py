"""A browser hitting a missing or access-gated page gets a friendly HTML 404;
API/fetch callers keep getting JSON, so nothing else changes."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import server
from fastapi.testclient import TestClient

HTML = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _client(multiuser=False):
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    worker = server.Worker(lib, "af_heart", 1.0, 150)
    users, auth_cfg = None, {}
    if multiuser:
        users = auth.Users(root)
        users.create("admin", None, admin=True,
                     pw_hash=auth.hash_password("adminpw is long"))
        auth_cfg = {"multiuser": True, "password_hash": auth.hash_password("x")}
    return TestClient(server.create_app(lib, worker, auth_cfg, users))


def test_browser_unknown_page_gets_html():
    r = _client().get("/no-such-page", headers=HTML)
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "library" in r.text.lower()          # the friendly page, not raw JSON


def test_browser_gated_admin_gets_html_not_json():
    # accounts off -> /admin 404s; a browser should still see the nice page
    r = _client(multiuser=False).get("/admin", headers=HTML)
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "detail" not in r.text                # not the raw {"detail":...}


def test_api_404_stays_json_even_for_a_browser():
    r = _client().get("/api/not-a-route", headers=HTML)
    assert r.status_code == 404
    assert "application/json" in r.headers["content-type"]


def test_fetch_style_404_stays_json():
    r = _client().get("/no-such-page", headers={"accept": "*/*"})
    assert r.status_code == 404
    assert "application/json" in r.headers["content-type"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all notfound tests passed")
