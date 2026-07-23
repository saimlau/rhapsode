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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all dashboard tests passed")
