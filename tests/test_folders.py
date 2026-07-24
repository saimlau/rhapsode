"""Playlist folders: the parent tree, its one-time migration from slashed
names, and the endpoint deltas that let the dashboard file papers into folders.
"""

import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz
from fastapi.testclient import TestClient

import auth
import server


def _lib():
    return server.Library(Path(tempfile.mkdtemp()))


# ------------------------------------------------------------- resolver

def test_resolve_folder_creates_ancestors_once():
    lib = _lib()
    leaf = lib.resolve_folder(["Osteo Lab", "Sheep Model"])
    pls = lib.data["playlists"]
    # a root "Osteo Lab" and a child "Sheep Model" under it
    root = next(p for p in pls.values() if p["name"] == "Osteo Lab")
    child = pls[leaf]
    assert child["name"] == "Sheep Model"
    assert child["parent"] == next(pid for pid, p in pls.items() if p is root)
    assert root["parent"] is None
    # idempotent: the same path returns the same leaf, creates nothing new
    n = len(pls)
    assert lib.resolve_folder(["Osteo Lab", "Sheep Model"]) == leaf
    assert len(lib.data["playlists"]) == n


def test_name_path_and_explicit_parent_reach_the_same_node():
    lib = _lib()
    a = lib.playlist_by_name("Osteo Lab / Sheep Model")
    parent = lib.data["playlists"][a]["parent"]
    b = lib.create_folder("Sheep Model", parent=parent)
    # a folder created explicitly under the same parent with the same name is
    # NOT auto-merged (create_folder always makes a new node); the path
    # resolver reuses, create_folder does not — distinct ids
    assert a != b
    # but resolving the path again reuses a, never b
    assert lib.playlist_by_name("Osteo Lab / Sheep Model") == a


def test_two_folders_named_the_same_under_different_parents():
    lib = _lib()
    x = lib.playlist_by_name("A / Corrosion")
    y = lib.playlist_by_name("B / Corrosion")
    assert x != y
    assert lib.data["playlists"][x]["name"] == "Corrosion"
    assert lib.data["playlists"][y]["name"] == "Corrosion"
    assert lib.data["playlists"][x]["parent"] != lib.data["playlists"][y]["parent"]


# ------------------------------------------------------------ migration

def test_migration_builds_the_tree_losslessly_and_is_idempotent():
    lib = _lib()
    # a pre-tree registry: slashed names, members, some empty parents
    lib.data["playlists"] = {
        "osteo": {"name": "Osteo Lab", "order": []},
        "sheep": {"name": "Osteo Lab / Sheep Model", "order": ["p1", "p2"]},
        "corr": {"name": "Osteo Lab / Sheep Model / Corrosion", "order": ["p3"]},
        "robo": {"name": "Robotics Research", "order": ["p4"]},
    }
    lib.save()
    server._migrate_playlist_tree(lib)
    pls = lib.data["playlists"]

    # names are now leaves, ids preserved, members intact
    assert pls["sheep"]["name"] == "Sheep Model"
    assert pls["sheep"]["order"] == ["p1", "p2"]
    assert pls["corr"]["name"] == "Corrosion"
    assert pls["corr"]["order"] == ["p3"]
    # the parent chain: corr -> sheep -> osteo -> root
    assert pls["corr"]["parent"] == "sheep"
    assert pls["sheep"]["parent"] == "osteo"
    assert pls["osteo"]["parent"] is None
    assert pls["robo"]["parent"] is None
    # no paper lost anywhere
    all_members = [pid for p in pls.values() for pid in p["order"]]
    assert sorted(all_members) == ["p1", "p2", "p3", "p4"]

    # idempotent: a second run changes nothing
    before = {k: dict(v) for k, v in pls.items()}
    server._migrate_playlist_tree(lib)
    assert {k: dict(v) for k, v in lib.data["playlists"].items()} == before


def test_migration_creates_a_missing_ancestor():
    lib = _lib()
    # "A / B" exists but "A" does not as its own playlist
    lib.data["playlists"] = {"b": {"name": "A / B", "order": ["p1"]}}
    lib.save()
    server._migrate_playlist_tree(lib)
    pls = lib.data["playlists"]
    b = pls["b"]
    assert b["name"] == "B"
    parent = pls[b["parent"]]
    assert parent["name"] == "A" and parent["parent"] is None


# --------------------------------------------------- endpoints (multiuser)

def _app():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("alice", "alice's long password")
    users.create("mallory", "mallory's long password")
    worker = server.Worker(lib, "af_heart", 1.0, 150)
    app = server.create_app(lib, worker,
                            {"password_hash": auth.hash_password("x"),
                             "multiuser": True}, users)
    return app, lib


def _login(c, who):
    pw = {"alice": "alice's long password",
          "mallory": "mallory's long password"}[who]
    r = c.post("/login", data={"username": who, "password": pw},
               follow_redirects=False)
    return {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}


def _pdf():
    d = fitz.open()
    d.new_page().insert_text((72, 72), "Readable body text for extraction.")
    b = d.tobytes()
    d.close()
    return b


def test_create_subfolder_under_an_owned_folder():
    app, lib = _app()
    c = TestClient(app)
    h = _login(c, "alice")
    parent = c.post("/api/playlists", headers=h, json={"name": "Osteo Lab"}).json()["id"]
    child = c.post("/api/playlists", headers=h,
                   json={"name": "Sheep Model", "parent": parent}).json()["id"]
    assert lib.data["playlists"][child]["parent"] == parent
    # a slash in a LEAF name (under an explicit parent) is refused — the leaf
    # can't itself be a path
    assert c.post("/api/playlists", headers=h,
                  json={"name": "a / b", "parent": parent}).status_code == 400


def test_slashed_name_with_no_parent_resolves_into_the_tree():
    """The Zotero plugin POSTs a full 'Grandparent / Parent / Child' path (no
    explicit parent) to ensure the folders exist; it must resolve into the tree,
    not 400."""
    app, lib = _app()
    c = TestClient(app)
    h = _login(c, "alice")
    r = c.post("/api/playlists", headers=h, json={"name": "Osteo Lab / Sheep Model"})
    assert r.status_code == 200, r.text
    leaf = r.json()["id"]
    assert lib.data["playlists"][leaf]["name"] == "Sheep Model"
    parent = lib.data["playlists"][leaf]["parent"]
    assert lib.data["playlists"][parent]["name"] == "Osteo Lab"
    # idempotent: the same path returns the same leaf, no duplicate folder
    again = c.post("/api/playlists", headers=h,
                   json={"name": "Osteo Lab / Sheep Model"}).json()["id"]
    assert again == leaf
    assert sum(1 for p in lib.data["playlists"].values()
               if p["name"] == "Sheep Model") == 1


def test_cannot_make_a_subfolder_under_someone_elses_folder():
    app, lib = _app()
    c = TestClient(app)
    a = _login(c, "alice")
    parent = c.post("/api/playlists", headers=a, json={"name": "Alice Lab"}).json()["id"]
    m = _login(c, "mallory")
    r = c.post("/api/playlists", headers=m,
               json={"name": "sneak", "parent": parent})
    assert r.status_code == 404


def test_upload_files_the_paper_into_a_folder():
    app, lib = _app()
    c = TestClient(app)
    h = _login(c, "alice")
    folder = c.post("/api/playlists", headers=h, json={"name": "Inbox"}).json()["id"]
    r = c.post("/api/papers", headers=h,
               files={"file": ("x.pdf", _pdf(), "application/pdf")},
               data={"playlist_id": folder})
    pid = r.json()["id"]
    assert pid in lib.data["playlists"][folder]["order"]


def test_moving_a_paper_keeps_it_in_other_folders():
    app, lib = _app()
    c = TestClient(app)
    h = _login(c, "alice")
    src = c.post("/api/playlists", headers=h, json={"name": "Src"}).json()["id"]
    dst = c.post("/api/playlists", headers=h, json={"name": "Dst"}).json()["id"]
    keep = c.post("/api/playlists", headers=h, json={"name": "Keep"}).json()["id"]
    pid = c.post("/api/papers", headers=h,
                 files={"file": ("x.pdf", _pdf(), "application/pdf")}).json()["id"]
    # symlink it into src and keep
    for f in (src, keep):
        c.post(f"/api/playlists/{f}/papers", headers=h, json={"id": pid})
    # move src -> dst = add to dst, remove from src
    c.post(f"/api/playlists/{dst}/papers", headers=h, json={"id": pid})
    c.delete(f"/api/playlists/{src}/papers/{pid}", headers=h)
    assert pid not in lib.data["playlists"][src]["order"]
    assert pid in lib.data["playlists"][dst]["order"]
    assert pid in lib.data["playlists"][keep]["order"], "the other folder is untouched"
    assert pid in lib.data["papers"], "the paper itself is not deleted"


def test_dashboard_tree_includes_empty_folders_with_parent():
    app, lib = _app()
    c = TestClient(app)
    h = _login(c, "alice")
    parent = c.post("/api/playlists", headers=h, json={"name": "Osteo Lab"}).json()["id"]
    child = c.post("/api/playlists", headers=h,
                   json={"name": "Sheep Model", "parent": parent}).json()["id"]
    pls = {p["id"]: p for p in c.get("/api/dashboard", headers=h).json()["playlists"]}
    assert parent in pls and child in pls, "empty folders are tree nodes, kept"
    assert pls[child]["parent"] == parent
    # mallory sees neither
    hm = _login(c, "mallory")
    seen = {p["id"] for p in c.get("/api/dashboard", headers=hm).json()["playlists"]}
    assert parent not in seen and child not in seen



def test_reparent_a_folder():
    app, lib = _app()
    c = TestClient(app)
    h = _login(c, "alice")
    a = c.post("/api/playlists", headers=h, json={"name": "A"}).json()["id"]
    b = c.post("/api/playlists", headers=h, json={"name": "B"}).json()["id"]
    # move B under A
    assert c.put(f"/api/playlists/{b}", headers=h, json={"parent": a}).status_code == 200
    assert lib.data["playlists"][b]["parent"] == a
    # move B back to root
    c.put(f"/api/playlists/{b}", headers=h, json={"parent": None})
    assert lib.data["playlists"][b]["parent"] is None


def test_reparent_rejects_a_cycle():
    app, lib = _app()
    c = TestClient(app)
    h = _login(c, "alice")
    a = c.post("/api/playlists", headers=h, json={"name": "A"}).json()["id"]
    b = c.post("/api/playlists", headers=h, json={"name": "B", "parent": a}).json()["id"]
    # A under B would loop (B is A's child)
    assert c.put(f"/api/playlists/{a}", headers=h, json={"parent": b}).status_code == 400
    # A under A is also refused
    assert c.put(f"/api/playlists/{a}", headers=h, json={"parent": a}).status_code == 400
    assert lib.data["playlists"][a]["parent"] is None


def test_cannot_reparent_under_someone_elses_folder():
    app, lib = _app()
    c = TestClient(app)
    mine = c.post("/api/playlists", headers=_login(c, "mallory"),
                  json={"name": "Mine"}).json()["id"]
    a = _login(c, "alice")
    theirs = c.post("/api/playlists", headers=a, json={"name": "Theirs"}).json()["id"]
    # mallory cannot move her folder under alice's
    r = c.put(f"/api/playlists/{mine}", headers=_login(c, "mallory"),
              json={"parent": theirs})
    assert r.status_code == 404


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all folder tests passed")
