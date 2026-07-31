"""One header, on every page. They used to be four hand-written copies:
Settings was missing from three of them, Manage from two, and settings.html
signed out with a GET link to a POST-only route — so its Sign out did nothing.
The header is now built server-side and substituted into each page."""
import os, re, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth, server
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
PAGES = {"/dashboard": "dashboard.html", "/": "library.html",
         "/settings": "settings.html", "/admin": "admin.html"}


def _app(multiuser=True):
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = None
    if multiuser:                      # server.py: multiuser IS "users exist"
        users = auth.Users(root)
        users.create("admin", None, admin=True,
                     pw_hash=auth.hash_password("adminpw is long"))
        users.create("tester", "tester's long password")
    worker = server.Worker(lib, "af_heart", 1.0, 150, users=users)
    app = server.create_app(lib, worker,
                            {"password_hash": auth.hash_password("x")}, users)
    return TestClient(app)


def _login(c, who, pw):
    r = c.post("/login", data={"username": who, "password": pw},
               follow_redirects=False)
    return {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}


def _nav(html):
    m = re.search(r"<nav>(.*?)</nav>", html, re.S)
    assert m, "the page shipped without a header"
    return m.group(1)


def test_every_page_carries_the_marker_and_no_hand_written_nav():
    for filename in PAGES.values():
        src = (REPO / filename).read_text(encoding="utf-8")
        assert "<!--nav-->" in src, f"{filename} lost the header marker"
        assert "<nav" not in src, \
            f"{filename} grew its own header again — it will drift"


def test_an_admin_sees_the_same_four_links_on_every_page():
    c = _app()
    h = _login(c, "admin", "adminpw is long")
    for path in PAGES:
        nav = _nav(c.get(path, headers=h).text)
        for label in ("Home", "Library", "Settings", "Manage"):
            assert f">{label}</a>" in nav, f"{label} missing from {path}"


def test_a_plain_user_sees_settings_but_never_manage():
    c = _app()
    h = _login(c, "tester", "tester's long password")
    for path in ("/dashboard", "/", "/settings"):
        nav = _nav(c.get(path, headers=h).text)
        assert ">Settings</a>" in nav
        assert "Manage" not in nav, f"/admin was advertised on {path}"


def test_the_current_page_is_marked_and_only_the_current_page():
    c = _app()
    h = _login(c, "admin", "adminpw is long")
    for path in PAGES:
        nav = _nav(c.get(path, headers=h).text)
        assert nav.count('class="here"') == 1, f"{path} marks itself once"
        assert f'class="here" href="{path}"' in nav


def test_sign_out_posts_everywhere():
    """A GET link to /logout is a dead button: the route is POST-only."""
    c = _app()
    h = _login(c, "admin", "adminpw is long")
    for path in PAGES:
        nav = _nav(c.get(path, headers=h).text)
        assert 'method="post" action="/logout"' in nav, \
            f"{path} cannot sign out"


def test_single_user_mode_offers_neither_settings_nor_manage():
    c = _app(multiuser=False)
    r = c.post("/login", data={"password": "x"}, follow_redirects=False)
    h = {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}
    nav = _nav(c.get("/dashboard", headers=h).text)
    assert "Settings" not in nav and "Manage" not in nav
    assert 'action="/logout"' in nav, "a password-protected server can sign out"
