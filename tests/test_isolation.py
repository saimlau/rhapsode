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
import time
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



# ---------------------------------------------------------------------
# Regressions from the adversarial security review (2026-07-21). Each of
# these passed review-free code and failed the review; they exist so the
# hole cannot reopen quietly.
# ---------------------------------------------------------------------

def test_sse_stream_does_not_push_other_users_papers():
    """The stream is a read path like /api/library, and it pushed the ENTIRE
    registry to every open tab — handing every user every other user's paper
    ids, titles and error strings. library.html opens it on page load, so this
    was the leak that made the guessable ids usable."""
    import asyncio
    from starlette.requests import Request as SReq
    app, _lib, _u = _app()

    endpoint = next(r.endpoint for r in app.router.routes
                    if getattr(r, "path", None) == "/api/events")

    async def first_data_frame():
        req = SReq({"type": "http", "headers": [], "method": "GET",
                    "path": "/api/events", "query_string": b""})
        req.state.user = "mallory"          # what the auth gate would set
        resp = await endpoint(req)
        async for chunk in resp.body_iterator:
            if str(chunk).startswith("data:"):
                return str(chunk)
        return ""

    frame = asyncio.run(first_data_frame())
    assert frame, "the stream must push an initial snapshot"
    assert "alice-secret" not in frame, f"SSE leaked a private paper: {frame[:200]}"
    assert "CONFIDENTIAL" not in frame
    assert "mallory-own" in frame, "the caller's own papers must still arrive"


def test_by_path_ingest_is_refused_when_accounts_are_on():
    """It reads an arbitrary path off the server's disk — right for a
    localhost install, catastrophic on a shared host: a user could name
    another user's paper.pdf and have it ingested into their own shelf."""
    app, lib, _u = _app()
    c = TestClient(app)
    victim = str((lib.root / "alice-secret" / "paper.pdf").resolve())
    r = c.post("/api/papers/by-path", headers=_as(c, "mallory"),
               json={"path": victim})
    assert r.status_code == 404, f"by-path must not exist in multiuser: {r.status_code}"
    assert len(lib.data["papers"]) == 3, "no new paper may have been created"


def test_playlists_belong_to_someone():
    app, lib, _u = _app()
    c = TestClient(app)
    alice, mallory = _as(c, "alice"), _as(c, "mallory")
    plid = c.post("/api/playlists", headers=alice,
                  json={"name": "Job applications"}).json()["id"]
    # a stranger can neither see, rename, nor destroy it
    assert c.put(f"/api/playlists/{plid}", headers=mallory,
                 json={"name": "pwned"}).status_code == 404
    assert c.delete(f"/api/playlists/{plid}", headers=mallory).status_code == 404
    assert plid in lib.data["playlists"]
    assert lib.data["playlists"][plid]["name"] == "Job applications"
    # nor is it listed to them
    assert plid not in c.get("/api/library", headers=mallory).json()["playlists"]
    # the owner still can
    assert c.delete(f"/api/playlists/{plid}", headers=alice).status_code == 200


def test_two_users_may_both_have_a_playlist_called_reading():
    app, lib, _u = _app()
    c = TestClient(app)
    a = c.post("/api/playlists", headers=_as(c, "alice"),
               json={"name": "Reading"}).json()["id"]
    m = c.post("/api/playlists", headers=_as(c, "mallory"),
               json={"name": "Reading"}).json()["id"]
    assert a != m, "identically named playlists must not be the same object"


def test_playlist_add_is_not_an_existence_oracle():
    """It validated with the raw registry lookup, so a real-but-invisible id
    returned 200 and a bogus one 404 — confirming whether a colleague holds a
    specific document."""
    app, _lib, _u = _app()
    c = TestClient(app)
    h = _as(c, "mallory")
    plid = c.post("/api/playlists", headers=h, json={"name": "Mine"}).json()["id"]
    real = c.post(f"/api/playlists/{plid}/papers", headers=h,
                  json={"id": "alice-secret"})
    bogus = c.post(f"/api/playlists/{plid}/papers", headers=h,
                   json={"id": "no-such-paper"})
    assert real.status_code == bogus.status_code == 404


def test_basic_auth_cache_does_not_outlive_the_account():
    """Caching the scrypt result keeps Basic usable for machine clients, but
    an entry with no invalidation kept a deleted user working."""
    app, _lib, users = _app()
    c = TestClient(app)
    hdr = {"Authorization": "Basic " + base64.b64encode(
        b"mallory:mallory's long password").decode()}
    assert c.get("/api/library", headers=hdr).status_code == 200   # caches it
    users.delete("mallory")
    assert c.get("/api/library", headers=hdr).status_code == 401, \
        "a deleted account must lose access immediately"


def test_basic_auth_cache_does_not_outlive_a_password_change():
    app, _lib, users = _app()
    c = TestClient(app)
    hdr = {"Authorization": "Basic " + base64.b64encode(
        b"mallory:mallory's long password").decode()}
    assert c.get("/api/library", headers=hdr).status_code == 200
    users.set_password("mallory", "a brand new password")
    assert c.get("/api/library", headers=hdr).status_code == 401, \
        "the old password must stop working"


def test_status_counts_only_what_you_can_see():
    app, _lib, _u = _app()
    c = TestClient(app)
    st = c.get("/api/status", headers=_as(c, "mallory")).json()
    assert sum(st["papers"].values()) == 2, \
        f"global counts reveal other users' uploads: {st['papers']}"


def test_uploading_a_shared_paper_does_not_rewrite_its_metadata():
    """Re-adding a shared paper used to overwrite its owner's title/authors
    and lock them."""
    app, lib, _u = _app()
    c = TestClient(app)
    before = lib.data["papers"]["alice-published"]["title"]
    pdf = (lib.root / "alice-published" / "paper.pdf").read_bytes()
    c.post("/api/papers", headers=_as(c, "mallory"),
           files={"file": ("x.pdf", pdf, "application/pdf")},
           data={"title": "MALLORY'S TITLE", "authors": "Mallory"})
    assert lib.data["papers"]["alice-published"]["title"] == before


def test_a_readers_position_does_not_move_the_owners():
    app, lib, _u = _app()
    c = TestClient(app)
    c.post("/api/papers/alice-published/position", headers=_as(c, "mallory"),
           json={"t": 42.0})
    assert lib.data["papers"]["alice-published"]["resume_t"] == 0.0, \
        "a reader must not move the owner's place"
    mine = c.get("/api/library", headers=_as(c, "mallory")
                 ).json()["papers"]["alice-published"]["resume_t"]
    assert mine == 42.0, "but the reader must keep their own"


def test_one_invite_cannot_mint_two_accounts_concurrently():
    """Check-then-create is not atomic unless it is held under one lock."""
    import threading
    root = Path(tempfile.mkdtemp())
    users = auth.Users(root)
    users.create("boss", "a long enough password", admin=True)
    tok = users.mint_invite("boss")
    made, errors = [], []

    def go(i):
        try:
            made.append(users.redeem(tok, f"racer{i}", "a long enough password"))
        except ValueError as e:
            errors.append(str(e))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(made) == 1, f"a single-use invite minted {len(made)} accounts"
    assert len(errors) == 7



# --------------------------------------------------------------------
# Second review pass (2026-07-21). Three of these are regressions of the
# FIRST pass's fixes — new code is where new bugs live.
# --------------------------------------------------------------------

def test_a_playlist_with_no_owner_is_not_everybodys():
    """Every playlist predating accounts has owner=None. Reading that as
    "public" handed the operator's own playlists to every invitee."""
    app, lib, _u = _app()
    c = TestClient(app)
    with lib.lock:
        lib.data["playlists"]["reading"] = {"name": "Reading", "order": []}
        lib.save()
    h = _as(c, "mallory")
    assert "reading" not in c.get("/api/library", headers=h).json()["playlists"]
    assert c.put("/api/playlists/reading", headers=h,
                 json={"name": "pwned"}).status_code == 404
    assert c.delete("/api/playlists/reading", headers=h).status_code == 404
    assert lib.data["playlists"]["reading"]["name"] == "Reading"


def test_migration_stamps_playlists_as_well_as_papers():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    lib.data["papers"]["old"] = {"id": "old", "status": "ready"}
    lib.data["playlists"]["old-pl"] = {"name": "Old", "order": []}
    lib.save()
    server._bootstrap_users(lib, {"multiuser": True, "admin_user": "saimai",
                                  "password_hash": auth.hash_password("pw12345678")})
    assert lib.data["papers"]["old"]["owner"] == "saimai"
    assert lib.data["playlists"]["old-pl"]["owner"] == "saimai", \
        "the migration forgot playlists entirely"


def test_a_deleted_username_cannot_be_reclaimed():
    """Ownership is keyed on the username and invitees pick their own, so a
    freed name was a claimable capability over a departed member's shelf."""
    app, _lib, users = _app()
    users.delete("mallory")
    try:
        users.create("mallory", "a brand new long password")
        raise AssertionError("a retired username must not be reusable")
    except ValueError:
        pass


def test_redeem_does_not_unburn_an_invite_whose_account_exists():
    """create() inserts the user before it saves, so a failing save left a
    live account behind a still-usable single-use invite."""
    import os
    root = Path(tempfile.mkdtemp())
    users = auth.Users(root)
    users.create("boss", "a long enough password", admin=True)
    tok = users.mint_invite("boss")
    real_replace, boom = os.replace, {"n": 0}

    def flaky(a, b):
        if boom["n"] == 0 and str(b).endswith("users.json"):
            boom["n"] = 1
            raise OSError(28, "No space left on device")
        return real_replace(a, b)

    os.replace = flaky
    try:
        try:
            users.redeem(tok, "first", "a long enough password")
        except OSError:
            pass
    finally:
        os.replace = real_replace
    if users.exists("first"):
        assert not users.invite_ok(tok), \
            "the account was created, so the invite must stay burnt"


def test_corrupt_users_file_refuses_to_start():
    """A truncated users.json read as "no accounts yet" — the bootstrap then
    recreated the admin and orphaned every paper to a vanished username."""
    root = Path(tempfile.mkdtemp())
    auth.Users(root).create("saimai", "a long enough password", admin=True)
    (root / auth.USERS_FILE).write_text('{"users": {"sai')     # truncated
    try:
        auth.Users(root)
        raise AssertionError("a corrupt account table must not start empty")
    except SystemExit:
        pass


def test_settings_are_not_writable_by_everyone():
    app, _lib, _u = _app()
    c = TestClient(app)
    assert c.put("/api/settings", headers=_as(c, "mallory"),
                 json={"auto_advance": False}).status_code == 403
    assert c.put("/api/settings", headers=_as(c, "boss"),
                 json={"auto_advance": False}).status_code == 200


def test_reserving_placeholder_is_never_listed():
    app, lib, _u = _app()
    c = TestClient(app)
    with lib.lock:
        lib.data["papers"]["half-made"] = {"id": "half-made", "status": "reserving",
                                           "hash": "x", "owner": "mallory"}
        lib.save()
    seen = c.get("/api/library", headers=_as(c, "mallory")).json()["papers"]
    assert "half-made" not in seen


def test_join_page_escapes_what_it_echoes():
    app, _lib, users = _app()
    c = TestClient(app)
    tok = users.mint_invite("boss")
    r = c.get(f"/join/{tok}?bad=<img src=x onerror=alert(1)>")
    assert "<img src=x" not in r.text, "the ?bad= echo is not escaped"
    assert "&lt;img" in r.text


def test_worker_survives_a_runtime_error():
    """`raise` inside a matched except clause escapes the whole try block, so
    every RuntimeError killed the only worker thread permanently while the
    HTTP server kept answering 200."""
    import queue as _q
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    w = server.Worker(lib, "af_heart", 1.0, 150)
    for pid in ("boom", "fine"):
        (root / pid).mkdir(parents=True, exist_ok=True)
        (root / pid / "paper.pdf").write_bytes(b"%PDF-1.4 stub")
        lib.data["papers"][pid] = {"id": pid, "status": "pending",
                                   "progress": 0.0, "owner": None}
        lib.data["order"].append(pid)
    lib.save()
    calls = []

    def fake_generate(pdf, out, *a, **kw):
        calls.append(pdf)
        if "boom" in str(pdf):
            raise RuntimeError("modal endpoint error: something went wrong")
        return {"duration": 1.0, "warnings": [], "title": "t",
                "authors": None, "year": None}

    # the worker extracts before it generates; stub both so the only failure
    # in play is the RuntimeError this test is about
    real_gen = server.p2a.generate_readalong
    real_prep = server.p2a.prepare_units
    server.p2a.generate_readalong = fake_generate
    server.p2a.prepare_units = lambda *a, **kw: ([], {})
    try:
        w.enqueue("boom")
        w.enqueue("fine")
        w.start()
        for _ in range(100):
            if lib.data["papers"]["fine"]["status"] in ("ready", "error"):
                break
            time.sleep(0.1)
    finally:
        server.p2a.generate_readalong = real_gen
        server.p2a.prepare_units = real_prep
    assert w.is_alive(), "the worker thread died on a RuntimeError"
    assert lib.data["papers"]["boom"]["status"] == "error"
    assert len(calls) == 2, "the second paper was never attempted"



def test_an_invite_can_be_revoked_before_it_is_used():
    """A link that cannot be withdrawn is a bearer credential with a two-week
    life, and it travels in a URL — browser history, chat logs, access logs."""
    app, _lib, users = _app()
    c = TestClient(app)
    admin = _as(c, "boss")
    tok = c.post("/api/invites", headers=admin).json()["token"]
    key = c.get("/api/users", headers=admin).json()["invites"][0]["key"]
    assert c.delete(f"/api/invites/{key}", headers=admin).status_code == 200
    assert not users.invite_ok(tok)
    assert c.get(f"/join/{tok}").status_code == 410


def test_only_an_admin_may_revoke_or_open_the_admin_page():
    app, _lib, _u = _app()
    c = TestClient(app)
    key = c.get("/api/users", headers=_as(c, "boss")).json()
    tok_key = c.post("/api/invites", headers=_as(c, "boss")).json()["token"]
    assert c.get("/admin", headers=_as(c, "mallory")).status_code == 404
    assert c.get("/admin", headers=_as(c, "boss")).status_code == 200
    assert c.delete("/api/invites/whatever",
                    headers=_as(c, "mallory")).status_code == 403


def test_a_user_cannot_upload_without_limit():
    """Every paper is GPU time on the operator's account."""
    app, lib, _u = _app()
    c = TestClient(app)
    server.PAPERS_PER_USER = 3
    try:
        h = _as(c, "mallory")
        codes = []
        for i in range(5):
            r = c.post("/api/papers", headers=h,
                       files={"file": (f"p{i}.pdf",
                                       b"%PDF-1.4 unique " + str(i).encode(),
                                       "application/pdf")})
            codes.append(r.status_code)
        assert 429 in codes, f"no quota was enforced: {codes}"
    finally:
        server.PAPERS_PER_USER = 200


def test_security_headers_are_present():
    """The read-along is generated HTML carrying PDF-derived strings; a CSP is
    the second line of defence that was entirely absent."""
    app, _lib, _u = _app()
    c = TestClient(app)
    r = c.get("/api/library", headers=_as(c, "mallory"))
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp and "frame-ancestors 'self'" in csp



def test_malformed_bodies_are_rejected_not_crashed():
    """Fuzzing found reachable 500s: any account could spray tracebacks into
    the log and, on the position route, poison a stored value."""
    app, lib, _u = _app()
    c = TestClient(app)
    h = _as(c, "mallory")
    h["Content-Type"] = "application/json"
    for raw in ('{"t": Infinity}', '{"t": NaN}', '{"t": -1}', '{"t": "abc"}'):
        r = c.post("/api/papers/mallory-own/position", headers=h, content=raw)
        assert r.status_code == 400, f"{raw} -> {r.status_code}"
    for body in ({"order": "notalist"}, {"order": [1, 2]}, {"order": {}}):
        assert c.put("/api/queue", headers=h, json=body).status_code == 400
    assert c.put("/api/settings", headers=_as(c, "boss"),
                 json={"auto_advance": "yes"}).status_code == 400
    assert lib.data["papers"]["mallory-own"]["resume_t"] == 0.0


def test_a_login_flood_is_throttled():
    """Each attempt costs a deliberate scrypt hash; nginx limit_req is the
    real defence but the app must not depend on the proxy being configured."""
    app, _lib, _u = _app()
    c = TestClient(app)
    codes = [c.post("/login", data={"username": "mallory", "password": "wrong"},
                    follow_redirects=False).status_code for _ in range(14)]
    assert 429 in codes, f"no throttle: {sorted(set(codes))}"



# --------------------------------------------------------------------
# Third review pass (2026-07-21). The two HIGHs are regressions of the
# second pass's own fixes — the pattern that keeps justifying another pass.
# --------------------------------------------------------------------

def test_basic_cache_is_not_poisoned_by_a_mutation_during_verify():
    """TOCTOU: a delete committing DURING the scrypt used to be stamped with
    the POST-mutation revision, marking a resurrected account 'current'. The
    fix samples the revision BEFORE the hash, so the next lookup re-validates."""
    app, _lib, users = _app()
    c = TestClient(app)
    hdr = {"Authorization": "Basic " + base64.b64encode(
        b"mallory:mallory's long password").decode()}
    # force the race deterministically: delete mallory the instant her hash is
    # being checked, exactly the window the fix must cover
    real_check = users.check

    def racing_check(name, pw):
        who = real_check(name, pw)
        if name == "mallory":
            users.delete("mallory")        # commits mid-"request"
        return who

    users.check = racing_check
    try:
        first = c.get("/api/library", headers=hdr).status_code
    finally:
        users.check = real_check
    # whatever the first response, the account is now gone and every later
    # request MUST be rejected — the cache must not have been stamped current
    assert c.get("/api/library", headers=hdr).status_code == 401, \
        "a deleted account survived in the Basic cache"


def test_basic_auth_flood_is_throttled_too():
    """The scrypt-flood the /login throttle stops must not just move to the
    Authorization header."""
    app, _lib, _u = _app()
    c = TestClient(app)
    codes = [c.get("/api/library", headers={
        "Authorization": "Basic " + base64.b64encode(
            f"mallory:wrong-{i}".encode()).decode()}).status_code
        for i in range(30)]
    assert 429 in codes, f"unbounded scrypt via Basic: {sorted(set(codes))}"


def test_bare_non_finite_json_body_is_400_not_500():
    """A top-level Infinity/NaN body crashed the default validation-error
    handler when it echoed the value back through allow_nan=False."""
    app, _lib, _u = _app()
    c = TestClient(app, raise_server_exceptions=False)
    h = _as(c, "mallory")
    h["Content-Type"] = "application/json"
    for raw in ("Infinity", "-Infinity", "NaN", "1e999"):
        r = c.post("/api/papers/mallory-own/position", headers=h, content=raw)
        assert r.status_code == 400, f"bare {raw} -> {r.status_code}"


def test_sse_stream_ends_on_a_password_change():
    """Deletion ended the stream but a password change did not — the epoch
    revocation must reach the streaming channel, not only the next request."""
    import asyncio
    app, _lib, users = _app()
    endpoint = next(r.endpoint for r in app.router.routes
                    if getattr(r, "path", None) == "/api/events")
    from starlette.requests import Request as SReq

    async def run():
        req = SReq({"type": "http", "headers": [], "method": "GET",
                    "path": "/api/events", "query_string": b""})
        req.state.user = "mallory"
        resp = await endpoint(req)
        it = resp.body_iterator
        await it.__anext__()                 # first frame, stream established
        users.set_password("mallory", "a brand new long password")
        # the next tick must terminate rather than keep delivering her view
        ended = False
        for _ in range(4):
            try:
                await asyncio.wait_for(it.__anext__(), timeout=3)
            except StopAsyncIteration:
                ended = True
                break
            except asyncio.TimeoutError:
                break
        return ended

    assert asyncio.run(run()), "the stream outlived the password change"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all isolation tests passed")
