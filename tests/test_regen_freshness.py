"""Regenerating a paper must actually reach the listener: the saved position is
cleared (new audio has new timings) and the reused asset URLs must revalidate
rather than serve the browser's cached copy of the OLD narration."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
from fastapi.testclient import TestClient


def _client():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    worker = server.Worker(lib, "af_heart", 1.0, 150)      # never started
    d = root / "p1" / "readalong"; d.mkdir(parents=True)
    (d / "index.html").write_text("<p>reader</p>")
    (d / "narration.m4a").write_bytes(b"audio-v1")
    lib.data["papers"]["p1"] = {"id": "p1", "status": "ready", "title": "P",
                                "authors": None, "year": None, "resume_t": 900,
                                "duration": 1200, "added": 1.0,
                                "resume_by": {"bob": 500}}
    lib.data["order"].append("p1")
    lib.save()
    return TestClient(server.create_app(lib, worker, {}, None)), lib


def test_regenerate_clears_the_saved_position():
    c, lib = _client()
    assert lib.data["papers"]["p1"]["resume_t"] == 900
    c.post("/api/papers/p1/regenerate")
    entry = lib.data["papers"]["p1"]
    assert entry["resume_t"] == 0, "a rebuilt narration has new timings"
    assert "resume_by" not in entry, "other readers' stale places go too"
    assert entry["status"] == "pending"


def test_view_assets_must_revalidate():
    """Without this the browser keeps serving the pre-regeneration audio."""
    c, _ = _client()
    for path in ("index.html", "narration.m4a"):
        r = c.get(f"/view/p1/{path}")
        assert r.status_code == 200
        assert "no-cache" in r.headers.get("cache-control", ""), \
            f"{path} must revalidate so a regenerated paper is picked up"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
