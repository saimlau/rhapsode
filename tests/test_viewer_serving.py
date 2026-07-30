"""One canonical viewer serves every paper.

A viewer fix used to reach only papers generated after it shipped, because the
reader was copied into each paper's folder at generation time — so improving it
meant re-narrating the whole library. The route now serves the repo's
viewer.html and lets it fetch the paper's manifest."""
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
from fastapi.testclient import TestClient

STALE = "<html>FROZEN COPY generated long ago</html>"


def _client():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    worker = server.Worker(lib, "af_heart", 1.0, 150)
    d = root / "p1" / "readalong"; d.mkdir(parents=True)
    (d / "index.html").write_text(STALE)          # what generation left behind
    (d / "manifest.json").write_text(json.dumps({"title": "P", "units": []}))
    lib.data["papers"]["p1"] = {"id": "p1", "status": "ready", "title": "P",
                                "authors": None, "year": None, "resume_t": 0,
                                "duration": 1, "added": 1.0}
    lib.data["order"].append("p1")
    lib.save()
    return TestClient(server.create_app(lib, worker, {}, None)), lib


def test_an_existing_paper_gets_the_current_viewer():
    c, _ = _client()
    body = c.get("/view/p1/index.html").text
    assert "FROZEN COPY" not in body, \
        "the paper's own stale copy must not be served"
    canonical = (Path(server.REPO) / "viewer.html").read_text()
    assert body == canonical, "the repo's viewer.html is what ships"


def test_the_bare_directory_url_also_gets_it():
    c, _ = _client()
    assert "FROZEN COPY" not in c.get("/view/p1/").text


def test_the_manifest_is_served_for_the_viewer_to_fetch():
    c, _ = _client()
    r = c.get("/view/p1/manifest.json")
    assert r.status_code == 200 and r.json()["title"] == "P"


def test_the_served_viewer_has_no_paper_inlined():
    """It must still hold the placeholder, so it fetches rather than shipping a
    16 MB manifest inside the HTML."""
    c, _ = _client()
    assert "__PAPER_DATA__" in c.get("/view/p1/index.html").text


def test_a_paper_without_a_manifest_falls_back_to_its_own_file():
    """Nothing that used to work may stop working."""
    c, lib = _client()
    (lib.root / "p1" / "readalong" / "manifest.json").unlink()
    assert "FROZEN COPY" in c.get("/view/p1/index.html").text


def test_other_assets_are_untouched():
    c, lib = _client()
    (lib.root / "p1" / "readalong" / "page-000.png").write_bytes(b"PNG")
    assert c.get("/view/p1/page-000.png").content == b"PNG"


def test_the_offline_bundle_still_inlines_its_data():
    """A folder copied to a tablet is opened from file://, where fetch() is
    blocked — so the written bundle must stay self-contained."""
    import rhapsode
    with tempfile.TemporaryDirectory() as t:
        out = Path(t); rhapsode.write_viewer(out, {"title": "X", "units": [],
                                                   "sections": [], "audio": "a.m4a"})
        html = (out / "index.html").read_text()
        assert "__PAPER_DATA__" not in html, "the bundle must have data inlined"
        assert '"title": "X"' in html or '"title":"X"' in html
        assert (out / "manifest.json").is_file()


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
