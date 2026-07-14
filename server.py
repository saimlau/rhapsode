"""Local library web app for paper2audio: drag-drop PDFs, a generation
queue on a warm GPU model, and a podcast-style read-along playlist.
Bound to 127.0.0.1 only. See docs/superpowers/specs/2026-07-12-gui-design.md."""

import hashlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)

import paper2audio as p2a
from extraction import clean_text, extract_segments

MAX_UPLOAD = 100 * 1024 * 1024
REPO = Path(__file__).resolve().parent


class Library:
    """Registry of papers under one root folder; atomic JSON persistence."""

    def __init__(self, root):
        self.root = root
        self.file = root / "library.json"
        self.lock = threading.RLock()
        self.version = 1
        self.data = {"papers": {}, "order": [], "playlists": {},
                     "settings": {"auto_advance": True}}
        if self.file.is_file():
            self.data.update(json.loads(self.file.read_text()))
        self.data.setdefault("playlists", {})  # pre-playlist registries

    def save(self, bump=True):
        with self.lock:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            os.replace(tmp, self.file)
            if bump:
                self.version += 1

    def snapshot(self):
        with self.lock:
            return {**json.loads(json.dumps(self.data)),
                    "version": self.version}

    def paper(self, pid):
        with self.lock:
            entry = self.data["papers"].get(pid)
            if not entry:
                raise HTTPException(404, f"unknown paper: {pid}")
            return entry

    def update(self, pid, bump=True, **fields):
        with self.lock:
            self.data["papers"][pid].update(fields)
            self.save(bump)

    def playlist_by_name(self, name):
        """Find-or-create a playlist; the slug id makes repeats idempotent."""
        with self.lock:
            plid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "pl"
            if plid not in self.data["playlists"]:
                self.data["playlists"][plid] = {"name": name, "order": []}
                self.save()
            return plid

    def pdf_path(self, pid):
        return self.root / pid / "paper.pdf"

    def view_dir(self, pid):
        return self.root / pid / "readalong"


class Worker(threading.Thread):
    """Single generation thread — the GPU is serial; the model stays warm."""

    def __init__(self, lib, voice, speed, dpi, grobid_cfg=None):
        super().__init__(daemon=True)
        self.lib, self.voice, self.speed, self.dpi = lib, voice, speed, dpi
        self.grobid_cfg = grobid_cfg
        self.q = queue.Queue()

    def enqueue(self, pid):
        self.q.put(pid)

    def run(self):
        while True:
            pid = self.q.get()
            with self.lib.lock:
                entry = self.lib.data["papers"].get(pid)
                if entry is None or entry["status"] != "pending":
                    continue  # deleted, or a duplicate queue entry
                entry.update(status="generating", progress=0.0, error=None)
                self.lib.save()
            last = [0.0]

            def progress(frac, _label):
                if frac - last[0] >= 0.02 or frac >= 1.0:
                    last[0] = frac
                    self.lib.update(pid, progress=round(frac, 3))

            try:
                info = p2a.generate_readalong(
                    self.lib.pdf_path(pid), self.lib.view_dir(pid),
                    self.voice, self.speed, self.dpi, progress,
                    grobid_cfg=self.grobid_cfg)
                self.lib.update(pid, status="ready", progress=1.0,
                                title=info["title"], authors=info["authors"],
                                year=info["year"],
                                duration=round(info["duration"], 1),
                                warnings=info["warnings"])
            except Exception as e:  # keep the queue alive on any failure
                self.lib.update(pid, status="error", error=str(e))


def create_app(lib, worker):
    app = FastAPI(title="paper2audio")

    @app.get("/", response_class=HTMLResponse)
    def home():
        page = REPO / "library.html"
        if not page.is_file():
            return HTMLResponse("<p>library.html missing from the repo</p>",
                                status_code=500)
        return page.read_text(encoding="utf-8")

    @app.get("/api/library")
    def get_library():
        return lib.snapshot()

    def _ingest(filename, data):
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file exceeds 100 MB")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, f"{filename}: not a PDF")
        digest = hashlib.sha1(data).hexdigest()[:10]
        with lib.lock:
            for pid, entry in lib.data["papers"].items():
                if entry.get("hash") == digest:
                    return JSONResponse({"id": pid, "duplicate": True})
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-",
                      Path(filename or "paper").stem)[:40].strip("-.")
        pid = f"{slug or 'paper'}-{digest}"
        paper_dir = lib.root / pid
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / "paper.pdf").write_bytes(data)

        entry = {"id": pid, "hash": digest, "filename": filename,
                 "title": Path(filename or pid).stem, "authors": None,
                 "year": None, "status": "pending", "progress": 0.0,
                 "error": None, "duration": None, "resume_t": 0.0,
                 "added": time.time()}
        try:  # fast CPU pass: real title/authors on the card immediately
            # capped to the front matter — a full-document pass on a long
            # PDF blows past the Zotero plugin's HTTP timeout
            _, _, meta = extract_segments(paper_dir / "paper.pdf", max_pages=3)
            if meta["title"]:
                entry["title"] = clean_text(meta["title"])
            entry["authors"] = meta["authors"]
            entry["year"] = meta["year"]
        except Exception as e:
            entry.update(status="error", error=f"extraction failed: {e}")
        with lib.lock:
            lib.data["papers"][pid] = entry
            lib.data["order"].append(pid)
            lib.save()
        if entry["status"] == "pending":
            worker.enqueue(pid)
        return JSONResponse({"id": pid, "duplicate": False})

    @app.post("/api/papers")
    def add_paper(file: UploadFile):
        return _ingest(file.filename, file.file.read(MAX_UPLOAD + 1))

    @app.post("/api/papers/by-path")
    def add_by_path(body: dict):
        """Ingest a local PDF by absolute path (used by the Zotero plugin;
        the server is localhost-only, so the caller is on this machine).
        Optional `playlist` name: append the paper to it (created if new)."""
        src = Path(str(body.get("path", ""))).expanduser()
        if not src.is_file():
            raise HTTPException(404, f"no such file: {src}")
        resp = _ingest(src.name, src.read_bytes())
        name = str(body.get("playlist", "")).strip()
        if name:
            pid = json.loads(resp.body)["id"]
            plid = lib.playlist_by_name(name)
            with lib.lock:
                order = lib.data["playlists"][plid]["order"]
                if pid not in order:
                    order.append(pid)
                    lib.save()
            return JSONResponse({**json.loads(resp.body), "playlist": plid})
        return resp

    @app.post("/api/playlists")
    def create_playlist(body: dict):
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "playlist name required")
        return {"id": lib.playlist_by_name(name)}

    @app.put("/api/playlists/{plid}")
    def update_playlist(plid: str, body: dict):
        with lib.lock:
            pl = lib.data["playlists"].get(plid)
            if not pl:
                raise HTTPException(404, "unknown playlist")
            if body.get("name"):
                pl["name"] = str(body["name"]).strip()
            if "order" in body:
                if sorted(body["order"]) != sorted(pl["order"]):
                    raise HTTPException(400, "order must contain exactly the "
                                             "playlist's papers")
                pl["order"] = body["order"]
            lib.save()
        return {"ok": True}

    @app.delete("/api/playlists/{plid}")
    def delete_playlist(plid: str):
        with lib.lock:
            if plid not in lib.data["playlists"]:
                raise HTTPException(404, "unknown playlist")
            del lib.data["playlists"][plid]
            lib.save()
        return {"ok": True}

    @app.post("/api/playlists/{plid}/papers")
    def playlist_add(plid: str, body: dict):
        pid = str(body.get("id", ""))
        lib.paper(pid)
        with lib.lock:
            pl = lib.data["playlists"].get(plid)
            if not pl:
                raise HTTPException(404, "unknown playlist")
            if pid not in pl["order"]:
                pl["order"].append(pid)
                lib.save()
        return {"ok": True}

    @app.delete("/api/playlists/{plid}/papers/{pid}")
    def playlist_remove(plid: str, pid: str):
        with lib.lock:
            pl = lib.data["playlists"].get(plid)
            if not pl:
                raise HTTPException(404, "unknown playlist")
            pl["order"] = [x for x in pl["order"] if x != pid]
            lib.save()
        return {"ok": True}

    @app.delete("/api/papers/{pid}")
    def delete_paper(pid: str):
        with lib.lock:
            entry = lib.paper(pid)
            if entry["status"] == "generating":
                raise HTTPException(409, "paper is generating; try again after")
            del lib.data["papers"][pid]
            lib.data["order"] = [x for x in lib.data["order"] if x != pid]
            for pl in lib.data["playlists"].values():
                pl["order"] = [x for x in pl["order"] if x != pid]
            lib.save()
        shutil.rmtree(lib.root / pid, ignore_errors=True)
        return {"ok": True}

    @app.post("/api/papers/{pid}/regenerate")
    def regenerate(pid: str):
        with lib.lock:
            entry = lib.paper(pid)
            if entry["status"] in ("generating", "pending"):
                return {"ok": True, "queued": False}  # dedupe repeat clicks
            entry.update(status="pending", progress=0.0, error=None)
            lib.save()
        worker.enqueue(pid)
        return {"ok": True, "queued": True}

    @app.post("/api/papers/{pid}/position")
    def position(pid: str, body: dict):
        lib.paper(pid)
        lib.update(pid, bump=False, resume_t=float(body.get("t", 0)))
        return {"ok": True}

    @app.put("/api/queue")
    def reorder(body: dict):
        order = body.get("order", [])
        with lib.lock:
            if sorted(order) != sorted(lib.data["order"]):
                raise HTTPException(400, "order must contain exactly the "
                                         "current paper ids")
            lib.data["order"] = order
            lib.save()
        return {"ok": True}

    @app.put("/api/settings")
    def settings(body: dict):
        with lib.lock:
            lib.data["settings"].update(
                {k: v for k, v in body.items() if k in ("auto_advance",)})
            lib.save()
        return {"ok": True}

    @app.get("/api/events")
    def events():
        def gen():
            last, idle = 0, 0
            while True:
                if lib.version != last:
                    last = lib.version
                    idle = 0
                    yield f"data: {json.dumps(lib.snapshot())}\n\n"
                elif idle >= 20:
                    idle = 0
                    # heartbeat: without periodic yields a dead client's
                    # generator is never closed and its thread leaks —
                    # enough reloads exhaust the pool and wedge the server
                    yield ": ping\n\n"
                else:
                    idle += 1
                time.sleep(0.7)
        return StreamingResponse(gen(), media_type="text/event-stream")

    def _tts(text, rate, voice):
        """Speak arbitrary text with the warm Kokoro model → WAV bytes.
        Used by the Speech Dispatcher generic module (Zotero read-aloud)."""
        import io
        import numpy as np
        import soundfile as sf
        text = (text or "").strip()
        if not text:
            raise HTTPException(400, "empty text")
        speed = min(max(float(rate or 1.0), 0.5), 2.0)
        pipeline = p2a.get_pipeline()
        parts = []
        with p2a.TTS_LOCK:
            for item in pipeline(text[:5000], voice=voice or worker.voice,
                                 speed=speed):
                audio = getattr(item, "audio", None)
                if audio is None:
                    _, _, audio = item
                parts.append(audio.detach().cpu().numpy())
        if not parts:
            raise HTTPException(500, "synthesis produced no audio")
        buf = io.BytesIO()
        sf.write(buf, np.concatenate(parts), p2a.SAMPLE_RATE,
                 format="WAV", subtype="PCM_16")
        return Response(content=buf.getvalue(), media_type="audio/wav")

    @app.post("/tts")
    def tts_post(text: str = Form(...), rate: float = Form(1.0),
                 voice: str = Form(None)):
        return _tts(text, rate, voice)

    @app.get("/tts")
    def tts_get(text: str, rate: float = 1.0, voice: str = None):
        return _tts(text, rate, voice)

    @app.get("/view/{pid}/{path:path}")
    def view(pid: str, path: str = ""):
        lib.paper(pid)
        base = lib.view_dir(pid).resolve()
        target = (base / (path or "index.html")).resolve()
        if base != target and base not in target.parents:
            raise HTTPException(403, "path outside paper folder")
        if not target.is_file():
            raise HTTPException(404, "not generated yet")
        return FileResponse(target)

    return app


def _free_port(start):
    for port in range(start, start + 10):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"no free port in {start}..{start + 9}")


def run(root, port, voice, speed, dpi, open_browser=False, grobid_cfg=None):
    root = Path(root)
    if not root.exists() and not root.parent.exists():
        sys.exit(f"error: library location unavailable (is the volume "
                 f"mounted?): {root}")
    root.mkdir(parents=True, exist_ok=True)

    lib = Library(root)
    worker = Worker(lib, voice, speed, dpi, grobid_cfg)
    with lib.lock:  # crash recovery: re-queue anything left mid-generation
        for pid, entry in lib.data["papers"].items():
            if entry["status"] in ("generating", "pending"):
                entry["status"] = "pending"
                worker.enqueue(pid)
            if entry.get("year") is None:  # migration: entries predating year
                try:
                    import fitz
                    from extraction import _page_year
                    entry["year"] = _page_year(fitz.open(lib.pdf_path(pid))[0])
                except Exception:
                    pass
        lib.save()
    worker.start()

    port = _free_port(port)
    url = f"http://127.0.0.1:{port}"
    print(f"paper2audio library: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: subprocess.Popen(
            ["xdg-open", url], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)).start()
    # never-ending SSE streams would make graceful shutdown wait forever
    # (Ctrl+C seemingly dead); force-close connections after a short grace
    uvicorn.run(create_app(lib, worker), host="127.0.0.1", port=port,
                log_level="warning", timeout_graceful_shutdown=3)
