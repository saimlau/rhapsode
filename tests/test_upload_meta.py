# tests/test_upload_meta.py
import io, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
import fitz
from fastapi.testclient import TestClient
import server


def _pdf_bytes():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello test paper content for upload.")
    return doc.tobytes()


def _client():
    root = Path(tempfile.mkdtemp())
    lib = server.Library(root)
    worker = server.Worker(lib, "af_heart", 1.0, 150)  # never .start()ed
    return TestClient(server.create_app(lib, worker)), lib


def test_upload_with_metadata_and_playlist():
    client, lib = _client()
    r = client.post("/api/papers",
                    files={"file": ("t.pdf", io.BytesIO(_pdf_bytes()),
                                    "application/pdf")},
                    data={"title": "Curated Title", "authors": "A. Author",
                          "year": "2024", "playlist": "Lab / Shelf"})
    assert r.status_code == 200, r.text
    body = r.json()
    pid = body["id"]
    entry = lib.data["papers"][pid]
    assert entry["title"] == "Curated Title"
    assert entry["authors"] == "A. Author"
    assert entry["year"] == 2024
    assert entry["meta_locked"] is True
    plid = body["playlist"]
    assert lib.data["playlists"][plid]["name"] == "Lab / Shelf"
    assert pid in lib.data["playlists"][plid]["order"]


def test_upload_without_metadata_unchanged():
    client, lib = _client()
    r = client.post("/api/papers",
                    files={"file": ("t.pdf", io.BytesIO(_pdf_bytes()),
                                    "application/pdf")})
    assert r.status_code == 200
    assert "playlist" not in r.json()
    entry = lib.data["papers"][r.json()["id"]]
    assert not entry.get("meta_locked")


if __name__ == "__main__":
    test_upload_with_metadata_and_playlist()
    test_upload_without_metadata_unchanged()
    print("all upload-meta tests passed")
