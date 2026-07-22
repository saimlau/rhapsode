"""Multi-user isolation, tested adversarially.

Storage is one registry with an owner per paper, so isolation is a CODE
invariant rather than a filesystem boundary — that was the accepted trade for
free dedupe and a one-field migration. This file is the other side of that
bargain. It attacks the direct-object routes, which are the dangerous ones: a
paper id is derived from the PDF's hash, so it is guessable by anyone holding
the same PDF.

Every check answers one question: can Mallory reach Alice's paper?
"""

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import auth
import server


def _app():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("alice", "alice's long password", admin=False)
    users.create("mallory", "mallory's long password", admin=False)
    users.create("boss", "the boss's long password", admin=True)
    worker = server.Worker(lib, "af_heart", 1.0, 150)

    def paper(pid, owner, shared=False, status="ready"):
        d = root / pid / "readalong"
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text("<p>secret read-along</p>")
        (d / "narration.m4a").write_bytes(b"\x00\x01audio")
        (root / pid / "paper.pdf").write_bytes(b"%PDF-1.4 fake")
        lib.data["papers"][pid] = {
            "id": pid, "hash": pid[-6:], "filename": f"{pid}.pdf",
            "title": f"{owner}'s paper", "authors": None, "year": None,
            "status": status, "progress": 1.0, "error": None,
            "duration": 60.0, "resume_t": 0.0, "added": 1.0,
            "owner": owner, "shared": shared}
        lib.data["order"].append(pid)
        lib.save()

    paper("alice-secret", "alice")
    paper("alice-published", "alice", shared=True)
    paper("mallory-own", "mallory")
    app = server.create_app(lib, worker,
                            {"password_hash": auth.hash_password("unused"),
                             "multiuser": True}, users)
    return app, lib, users


def _as(client, who):
    pw = {"alice": "alice's long password",
          "mallory": "mallory's long password",
          "boss": "the boss's long password"}[who]
    r = client.post("/login", data={"username": who, "password": pw},
                    follow_redirects=False)
    assert r.status_code == 303, r.status_code
    return {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}


# --------------------------------------------------------------- listing

def test_library_shows_only_your_own_and_shared():
    app, _lib, _u = _app()
    c = TestClient(app)
    seen = c.get("/api/library", headers=_as(c, "mallory")).json()["papers"]
    assert set(seen) == {"mallory-own", "alice-published"}, seen
    assert "alice-secret" not in seen
    # and the order list must not name what the papers map hides
    order = c.get("/api/library", headers=_as(c, "mallory")).json()["order"]
    assert "alice-secret" not in order


def test_dashboard_does_not_leak_titles_or_counts():
    app, _lib, _u = _app()
    c = TestClient(app)
    d = c.get("/api/dashboard", headers=_as(c, "mallory")).json()
    blob = json.dumps(d)
    assert "alice's paper" not in blob or "alice-published" in blob
    assert "alice-secret" not in blob
    assert d["stats"]["papers"] == 2, d["stats"]


# ------------------------------------------------- direct-object routes

def test_mallory_cannot_reach_alices_paper_by_id():
    app, _lib, _u = _app()
    c = TestClient(app)
    h = _as(c, "mallory")
    for method, path in [
            ("get", "/view/alice-secret/index.html"),
            ("get", "/view/alice-secret/narration.m4a"),
            ("delete", "/api/papers/alice-secret"),
            ("post", "/api/papers/alice-secret/regenerate"),
            ("post", "/api/papers/alice-secret/share"),
            ("post", "/api/papers/alice-secret/position"),
    ]:
        kw = {"headers": h}
        if method == "post":
            kw["json"] = {}
        r = getattr(c, method)(path, **kw)
        assert r.status_code == 404, f"{method} {path} -> {r.status_code}"


def test_a_wrong_id_and_someone_elses_id_look_identical():
    """A 403 on a real id and a 404 on a made-up one would confirm which
    papers exist."""
    app, _lib, _u = _app()
    c = TestClient(app)
    h = _as(c, "mallory")
    real = c.get("/view/alice-secret/index.html", headers=h)
    fake = c.get("/view/no-such-paper-at-all/index.html", headers=h)
    assert real.status_code == fake.status_code == 404


def test_audio_bytes_are_not_served_to_a_stranger():
    app, _lib, _u = _app()
    c = TestClient(app)
    r = c.get("/view/alice-secret/narration.m4a", headers=_as(c, "mallory"))
    assert r.status_code == 404
    assert b"audio" not in r.content


# ------------------------------------------------------- shared papers

def test_shared_is_readable_by_all_but_writable_only_by_its_owner():
    app, _lib, _u = _app()
    c = TestClient(app)
    h = _as(c, "mallory")
    assert c.get("/view/alice-published/index.html", headers=h).status_code == 200
    # visible, but sharing is not handing over control
    assert c.delete("/api/papers/alice-published", headers=h).status_code == 403
    assert c.post("/api/papers/alice-published/regenerate",
                  headers=h).status_code == 403
    assert c.post("/api/papers/alice-published/share", headers=h,
                  json={"shared": False}).status_code == 403


def test_owner_can_publish_and_unpublish():
    app, _lib, _u = _app()
    c = TestClient(app)
    alice, mallory = _as(c, "alice"), _as(c, "mallory")
    assert c.post("/api/papers/alice-secret/share", headers=alice,
                  json={"shared": True}).json()["shared"] is True
    assert c.get("/view/alice-secret/index.html",
                 headers=mallory).status_code == 200
    c.post("/api/papers/alice-secret/share", headers=alice, json={"shared": False})
    assert c.get("/view/alice-secret/index.html",
                 headers=mallory).status_code == 404


# --------------------------------------------------------------- admins

def test_admin_can_reach_everything():
    app, _lib, _u = _app()
    c = TestClient(app)
    h = _as(c, "boss")
    assert c.get("/view/alice-secret/index.html", headers=h).status_code == 200
    assert set(c.get("/api/library", headers=h).json()["papers"]) == \
        {"alice-secret", "alice-published", "mallory-own"}


def test_only_admins_may_mint_invites_or_list_users():
    app, _lib, _u = _app()
    c = TestClient(app)
    for who, code in (("mallory", 403), ("boss", 200)):
        h = _as(c, who)
        assert c.post("/api/invites", headers=h).status_code == code
        assert c.get("/api/users", headers=h).status_code == code
        assert c.delete("/api/users/alice", headers=h).status_code == code


# ------------------------------------------------------- sessions/gate

def test_no_session_means_no_access():
    app, _lib, _u = _app()
    c = TestClient(app)
    assert c.get("/api/library").status_code == 401
    assert c.get("/view/alice-secret/index.html",
                 follow_redirects=False).status_code in (303, 401)


def test_a_forged_cookie_is_refused():
    app, _lib, _u = _app()
    c = TestClient(app)
    forged = base64.urlsafe_b64encode(b"alice").decode().rstrip("=") + \
        ".9999999999.bm90LWEtc2lnbmF0dXJl"
    r = c.get("/api/library", headers={"Cookie": f"{auth.COOKIE}={forged}"})
    assert r.status_code == 401


def test_a_deleted_account_stops_working_immediately():
    app, _lib, users = _app()
    c = TestClient(app)
    h = _as(c, "mallory")
    assert c.get("/api/library", headers=h).status_code == 200
    users.delete("mallory")
    assert c.get("/api/library", headers=h).status_code == 401, \
        "a valid cookie for a deleted user must stop working"


def test_basic_auth_is_scoped_to_that_user():
    """The Zotero plugin authenticates with Basic; it must land in the right
    shelf, not a shared one."""
    app, _lib, _u = _app()
    c = TestClient(app)
    hdr = {"Authorization": "Basic " + base64.b64encode(
        b"mallory:mallory's long password").decode()}
    seen = c.get("/api/library", headers=hdr).json()["papers"]
    assert set(seen) == {"mallory-own", "alice-published"}
    assert c.get("/view/alice-secret/index.html",
                 headers=hdr).status_code == 404
    bad = {"Authorization": "Basic " + base64.b64encode(
        b"mallory:wrong").decode()}
    assert c.get("/api/library", headers=bad).status_code == 401


# ---------------------------------------------------------------- queue

def test_reorder_cannot_name_papers_you_cannot_see():
    app, _lib, _u = _app()
    c = TestClient(app)
    h = _as(c, "mallory")
    r = c.put("/api/queue", headers=h,
              json={"order": ["mallory-own", "alice-published", "alice-secret"]})
    assert r.status_code == 400
    ok = c.put("/api/queue", headers=h,
               json={"order": ["alice-published", "mallory-own"]})
    assert ok.status_code == 200


def test_reorder_leaves_other_users_papers_in_place():
    app, lib, _u = _app()
    c = TestClient(app)
    before = list(lib.data["order"])
    c.put("/api/queue", headers=_as(c, "mallory"),
          json={"order": ["alice-published", "mallory-own"]})
    after = lib.data["order"]
    assert sorted(after) == sorted(before), "no paper may vanish or appear"
    assert after.index("alice-secret") == before.index("alice-secret"), \
        "a paper the caller cannot see must not move"


# -------------------------------------------------------------- ingest

def test_upload_is_owned_by_the_uploader():
    app, lib, _u = _app()
    c = TestClient(app)
    pdf = b"%PDF-1.4 mallory's very own file"
    r = c.post("/api/papers", headers=_as(c, "mallory"),
               files={"file": ("x.pdf", pdf, "application/pdf")})
    pid = r.json()["id"]
    assert lib.data["papers"][pid]["owner"] == "mallory"
    assert lib.data["papers"][pid]["shared"] is False


def test_dedupe_does_not_reveal_someone_elses_private_copy():
    """Answering "you already have that" about Alice's private paper would
    confirm she holds it — and hand over her id."""
    app, lib, _u = _app()
    c = TestClient(app)
    pdf = b"%PDF-1.4 the very same document"
    first = c.post("/api/papers", headers=_as(c, "alice"),
                   files={"file": ("shared-doc.pdf", pdf, "application/pdf")})
    apid = first.json()["id"]
    second = c.post("/api/papers", headers=_as(c, "mallory"),
                    files={"file": ("shared-doc.pdf", pdf, "application/pdf")})
    mpid = second.json()["id"]
    assert mpid != apid, "Mallory must not be handed Alice's paper id"
    assert second.json().get("duplicate") is not True
    assert lib.data["papers"][mpid]["owner"] == "mallory"
    # the same user uploading twice still dedupes
    again = c.post("/api/papers", headers=_as(c, "mallory"),
                   files={"file": ("shared-doc.pdf", pdf, "application/pdf")})
    assert again.json()["id"] == mpid and again.json()["duplicate"] is True


# ------------------------------------------------------------ migration

def test_bootstrap_creates_admin_and_stamps_existing_papers():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    lib.data["papers"]["old-paper"] = {
        "id": "old-paper", "status": "ready", "title": "predates accounts"}
    lib.data["order"].append("old-paper")
    lib.save()
    cfg = {"multiuser": True, "admin_user": "saimai",
           "password_hash": auth.hash_password("the existing password")}
    users = server._bootstrap_users(lib, cfg)
    assert users.is_admin("saimai")
    # the operator's existing password must keep working
    assert users.check("saimai", "the existing password") == "saimai"
    assert lib.data["papers"]["old-paper"]["owner"] == "saimai"
    assert lib.data["papers"]["old-paper"]["shared"] is False
    # idempotent
    server._bootstrap_users(lib, cfg)
    assert auth.Users(root).names() == ["saimai"]


def test_single_user_mode_is_untouched():
    """No accounts configured: a localhost install behaves exactly as before."""
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    worker = server.Worker(lib, "af_heart", 1.0, 150)
    app = server.create_app(lib, worker, None, None)
    c = TestClient(app)
    assert c.get("/api/library").status_code == 200


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all isolation tests passed")
