"""Issue reports. A report saying "mispronounced" cannot be acted on, so the
server pins each one to the paper, the timestamp and the sentence that was
being read. Reading them back is admin-only."""
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth, server
from fastapi.testclient import TestClient


def _single():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    worker = server.Worker(lib, "af_heart", 1.0, 150)
    d = root / "p1" / "readalong"; d.mkdir(parents=True)
    (d / "index.html").write_text("x")
    (d / "manifest.json").write_text(json.dumps({"units": [
        {"kind": "body", "text": "The modulus was 68.9 GPa.", "t0": 0, "t1": 10},
        {"kind": "body", "text": "Poisson ratio of 0.33.", "t0": 10, "t1": 20}]}))
    lib.data["papers"]["p1"] = {"id": "p1", "status": "ready", "title": "A Paper",
                                "authors": None, "year": None, "resume_t": 0,
                                "duration": 20, "added": 1.0}
    lib.data["order"].append("p1")
    lib.save()
    return TestClient(server.create_app(lib, worker, {}, None)), lib


def test_report_captures_the_sentence_being_read():
    c, lib = _single()
    r = c.post("/api/issues", json={"paper": "p1", "t": 12.5,
                                    "kind": "pronunciation",
                                    "note": "Poisson sounded like poison"})
    assert r.status_code == 200
    rows = [json.loads(x) for x in
            (lib.root / "issues.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    got = rows[0]
    assert got["paper"] == "p1" and got["title"] == "A Paper"
    assert got["kind"] == "pronunciation" and got["t"] == 12.5
    assert "Poisson ratio" in got["heard"], \
        f"the sentence at t=12.5 must be captured: {got['heard']!r}"


def test_an_unknown_kind_is_not_stored_verbatim():
    c, lib = _single()
    c.post("/api/issues", json={"paper": "p1", "t": 0, "kind": "<script>",
                                "note": "x"})
    row = json.loads((lib.root / "issues.jsonl").read_text().splitlines()[0])
    assert row["kind"] == "other"


def test_reports_append_and_are_listed_newest_first():
    c, lib = _single()
    for i in range(3):
        c.post("/api/issues", json={"paper": "p1", "t": i, "note": f"note {i}"})
    got = c.get("/api/issues").json()["issues"]
    assert [g["note"] for g in got] == ["note 2", "note 1", "note 0"]


def test_the_issue_file_is_private():
    """A report can quote a private paper's text."""
    c, lib = _single()
    c.post("/api/issues", json={"paper": "p1", "t": 0, "note": "x"})
    mode = (lib.root / "issues.jsonl").stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def _multi():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    users = auth.Users(root)
    users.create("admin", None, admin=True, pw_hash=auth.hash_password("adminpw is long"))
    users.create("bob", "bob's long password")
    worker = server.Worker(lib, "af_heart", 1.0, 150, users=users)
    d = root / "secret" / "readalong"; d.mkdir(parents=True)
    (d / "manifest.json").write_text('{"units": []}')
    lib.data["papers"]["secret"] = {"id": "secret", "status": "ready",
                                    "title": "Admin's paper", "owner": "admin",
                                    "authors": None, "year": None, "resume_t": 0,
                                    "duration": 5, "added": 1.0}
    lib.data["order"].append("secret")
    lib.save()
    app = server.create_app(lib, worker,
                            {"multiuser": True,
                             "password_hash": auth.hash_password("x")}, users)
    return TestClient(app), lib


def _login(c, who, pw):
    r = c.post("/login", data={"username": who, "password": pw},
               follow_redirects=False)
    return {"Cookie": f"{auth.COOKIE}={r.cookies[auth.COOKIE]}"}


def test_cannot_report_on_a_paper_you_cannot_see():
    c, lib = _multi()
    r = c.post("/api/issues", headers=_login(c, "bob", "bob's long password"),
               json={"paper": "secret", "t": 0, "note": "probing"})
    assert r.status_code == 404, "another user's paper must stay invisible"
    assert not (lib.root / "issues.jsonl").exists(), "nothing may be written"


def test_only_an_admin_reads_the_reports():
    c, lib = _multi()
    c.post("/api/issues", headers=_login(c, "admin", "adminpw is long"),
           json={"paper": "secret", "t": 0, "note": "mine"})
    assert c.get("/api/issues",
                 headers=_login(c, "bob", "bob's long password")).status_code == 404
    got = c.get("/api/issues", headers=_login(c, "admin", "adminpw is long")).json()
    assert got["issues"][0]["note"] == "mine"
    assert got["issues"][0]["who"] == "admin", "the reporter is recorded"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
