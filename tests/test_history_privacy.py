"""Your history is YOURS. An admin can SEE every paper, but another person's
reading position is not the admin's history — and how far a colleague has read
is not the admin's business either."""
import os, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth, server
from fastapi.testclient import TestClient


def _app():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("admin", None, admin=True, pw_hash=auth.hash_password("adminpw is long"))
    users.create("tester", "tester's long password")
    worker = server.Worker(lib, "af_heart", 1.0, 150, users=users)
    d = root / "t1" / "readalong"; d.mkdir(parents=True)
    (d / "index.html").write_text("x")
    (d / "manifest.json").write_text('{"units": []}')
    # the tester's own paper, which they are half way through
    lib.data["papers"]["t1"] = {"id": "t1", "status": "ready",
                                "title": "The tester's paper", "owner": "tester",
                                "authors": None, "year": None,
                                "resume_t": 600, "last_opened": time.time(),
                                "duration": 1200, "added": 1.0}
    lib.data["order"].append("t1")
    lib.save()
    app = server.create_app(lib, worker,
                            {"multiuser": True,
                             "password_hash": auth.hash_password("x")}, users)
    return TestClient(app), lib


def _login(c, who, pw):
    r = c.post("/login", data={"username": who, "password": pw},
               follow_redirects=False)
    return {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}


def test_another_persons_paper_is_not_in_my_history():
    c, _ = _app()
    d = c.get("/api/dashboard", headers=_login(c, "admin", "adminpw is long")).json()
    assert [r["id"] for r in d["resume"]] == [], \
        "the tester's half-read paper must not appear in the admin's history"


def test_the_owner_still_sees_their_own_history():
    c, _ = _app()
    d = c.get("/api/dashboard",
              headers=_login(c, "tester", "tester's long password")).json()
    assert [r["id"] for r in d["resume"]] == ["t1"]


def test_an_admin_can_still_see_the_paper_itself():
    """Visibility is unchanged — only whose POSITION is reported."""
    c, _ = _app()
    h = _login(c, "admin", "adminpw is long")
    lib = c.get("/api/library", headers=h).json()
    assert "t1" in lib["papers"], "an admin can still see the paper"
    assert lib["papers"]["t1"]["resume_t"] in (0, None), \
        "but not how far its owner has read"


def test_reading_a_shared_paper_keeps_the_two_places_apart():
    c, lib = _app()
    lib.data["papers"]["t1"]["shared"] = True
    lib.save()
    h = _login(c, "admin", "adminpw is long")
    c.post("/api/papers/t1/position", headers=h, json={"t": 100})
    mine = c.get("/api/library", headers=h).json()["papers"]["t1"]["resume_t"]
    assert mine == 100, "my own place in a shared paper is mine"
    assert lib.data["papers"]["t1"]["resume_t"] == 600, "the owner's is untouched"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
