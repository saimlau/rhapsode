"""Dashboard payload: the resume list orders by when you last OPENED a paper,
not how deep the saved position is — a 90%-done paper must not outrank one you
opened this morning at 10%.
"""

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
    worker = server.Worker(lib, "af_heart", 1.0, 150)

    def paper(pid, at):
        d = root / pid / "readalong"
        d.mkdir(parents=True)
        (d / "index.html").write_text("<p>reader</p>")
        lib.data["papers"][pid] = {"id": pid, "status": "ready", "title": pid,
                                   "authors": None, "year": None, "resume_t": at,
                                   "duration": 2000, "added": 1.0}
        lib.data["order"].append(pid)

    paper("deep", 1600)      # 80% in, but not opened recently
    paper("shallow", 200)    # 10% in
    lib.save()
    return TestClient(server.create_app(lib, worker, {}, None))


def _order(c):
    return [r["id"] for r in c.get("/api/dashboard").json()["resume"]]


def test_resume_falls_back_to_depth_before_any_open():
    c = _app()
    assert _order(c) == ["deep", "shallow"], "no open history: deeper first"


def test_resume_orders_by_last_opened():
    c = _app()
    c.get("/view/shallow/index.html")
    time.sleep(0.01)
    c.get("/view/deep/index.html")
    assert _order(c) == ["deep", "shallow"], "the just-opened paper leads"
    time.sleep(0.01)
    c.get("/view/shallow/index.html")
    assert _order(c) == ["shallow", "deep"], "reopening moves it to the front"


def test_listening_refreshes_last_opened():
    c = _app()
    c.get("/view/shallow/index.html")
    time.sleep(0.01)
    c.post("/api/papers/deep/position", json={"t": 1650})
    assert _order(c) == ["deep", "shallow"], \
        "an active position update counts as touching the paper"


def test_only_opening_the_reader_stamps_it():
    """Fetching an asset (a page image, the audio) is not opening the paper —
    only index.html should reorder the shelf."""
    c = _app()
    c.get("/view/shallow/index.html")            # shallow opened last
    time.sleep(0.01)
    # deep's cover/audio being fetched must NOT promote it over shallow
    c.get("/view/deep/page-000.png")
    assert _order(c) == ["shallow", "deep"], \
        "a non-index asset fetch must not reorder the shelf"



def test_dashboard_playlists_list_only_ready_members():
    """The wall's playlist chips should scope to a playlist's papers that are
    actually on the shelf — a pending or errored member has no cover to show."""
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    worker = server.Worker(lib, "af_heart", 1.0, 150)

    def paper(pid, status):
        (root / pid / "readalong").mkdir(parents=True)
        (root / pid / "readalong" / "index.html").write_text("x")
        lib.data["papers"][pid] = {"id": pid, "status": status, "title": pid,
                                   "authors": None, "year": None, "resume_t": 0,
                                   "duration": 100, "added": 1.0}
        lib.data["order"].append(pid)

    paper("ready1", "ready")
    paper("ready2", "ready")
    paper("pending1", "pending")
    lib.data["playlists"]["reading"] = {
        "name": "Reading", "order": ["ready1", "pending1", "ready2"]}
    lib.save()
    c = TestClient(server.create_app(lib, worker, {}, None))
    pls = c.get("/api/dashboard").json()["playlists"]
    assert len(pls) == 1 and pls[0]["name"] == "Reading"
    assert pls[0]["order"] == ["ready1", "ready2"], \
        "a pending member has no cover and must be dropped, order preserved"


def test_empty_folder_is_surfaced_as_a_tree_node():
    """Empty folders are containers in the tree, so unlike leaf-with-no-ready-
    members they must still appear (the folder browser needs them)."""
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    worker = server.Worker(lib, "af_heart", 1.0, 150)
    lib.data["playlists"]["empty"] = {"name": "Empty", "order": [], "parent": None}
    lib.save()
    pls = c_get_playlists(server.create_app(lib, worker, {}, None))
    assert [p["name"] for p in pls] == ["Empty"]


def c_get_playlists(app):
    return TestClient(app).get("/api/dashboard").json()["playlists"]



def _folder_app():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    worker = server.Worker(lib, "af_heart", 1.0, 150)

    def paper(pid, at, dur):
        d = root / pid / "readalong"; d.mkdir(parents=True)
        (d / "index.html").write_text("x")
        lib.data["papers"][pid] = {"id": pid, "status": "ready", "title": pid,
                                   "authors": None, "year": None, "resume_t": at,
                                   "duration": dur, "added": 1.0}
        lib.data["order"].append(pid)

    for pid, at, dur in [("p1", 1000, 1000), ("p2", 300, 1000), ("p3", 0, 1000)]:
        paper(pid, at, dur)
    lib.data["playlists"]["sheep"] = {"name": "Sheep Model", "parent": None,
                                      "order": ["p1", "p2", "p3"]}
    lib.save()
    return TestClient(server.create_app(lib, worker, {}, None)), lib


def test_resume_is_playlist_aware_when_opened_from_a_folder():
    c, lib = _folder_app()
    c.get("/view/p2/index.html?pl=sheep")         # opened from the folder
    h = c.get("/api/dashboard").json()["resume"][0]
    assert h["playlist_path"] == "Sheep Model"
    assert h["pos"] == 2 and h["count"] == 3
    assert abs(h["listened_frac"] - (1300 / 3000)) < 0.001   # time through folder
    assert h["anchor_id"] == "p2"


def test_current_advances_past_a_finished_paper():
    c, lib = _folder_app()
    lib.data["papers"]["p2"]["resume_t"] = 980     # p2 now finished (>=95%)
    lib.save()
    c.get("/view/p2/index.html?pl=sheep")
    h = c.get("/api/dashboard").json()["resume"][0]
    assert h["id"] == "p3" and h["pos"] == 3, "hero points at the next unfinished"
    assert h["anchor_id"] == "p2"


def test_opening_flat_clears_the_playlist_context():
    c, lib = _folder_app()
    c.get("/view/p2/index.html?pl=sheep")
    assert c.get("/api/dashboard").json()["resume"][0].get("playlist_path")
    c.get("/view/p2/index.html")                   # reopened without a folder
    assert c.get("/api/dashboard").json()["resume"][0].get("playlist_path") is None


def test_forget_removes_a_paper_from_history():
    c, lib = _folder_app()
    c.get("/view/p2/index.html")                   # p2 in progress, flat
    assert "p2" in [r["id"] for r in c.get("/api/dashboard").json()["resume"]]
    c.post("/api/papers/p2/forget")
    assert "p2" not in [r["id"] for r in c.get("/api/dashboard").json()["resume"]]
    assert "p2" in lib.data["papers"], "the paper itself is not deleted"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all dashboard tests passed")
