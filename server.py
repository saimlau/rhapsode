"""Local library web app for Rhapsode: drag-drop PDFs, a generation
queue on a warm GPU model, and a podcast-style read-along playlist.
Bound to 127.0.0.1 only. See docs/superpowers/specs/2026-07-12-gui-design.md."""

import asyncio
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

import rhapsode as p2a
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
        self._dirty = False
        self._last_save = 0.0
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
            self._dirty = False
            self._last_save = time.time()
            if bump:
                self.version += 1

    def touch(self, bump=True):
        """In-memory mutation happened; persist lazily (30 s debounce).
        High-frequency writers (progress ticks, playback positions) go
        through here instead of hammering the SSD with full-registry
        writes."""
        with self.lock:
            self._dirty = True
            if bump:
                self.version += 1
            if time.time() - self._last_save >= 30:
                self.save(bump=False)

    def flush(self):
        with self.lock:
            if self._dirty:
                self.save(bump=False)

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

    def update(self, pid, bump=True, persist=True, **fields):
        with self.lock:
            self.data["papers"][pid].update(fields)
            if persist:
                self.save(bump)
            else:
                self.touch(bump)

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
    """Single generation thread — the GPU is serial; the model stays warm
    during a batch and is parked/unloaded (with GROBID stopped) when the
    queue has been idle, so nothing heavy stays resident between bursts."""

    def __init__(self, lib, voice, speed, dpi, grobid_cfg=None, tts_cfg=None,
                 idle_exit_min=0):
        super().__init__(daemon=True)
        self.lib, self.voice, self.speed, self.dpi = lib, voice, speed, dpi
        self.grobid_cfg = grobid_cfg
        self.tts_cfg = tts_cfg or {}
        self.idle_exit_min = idle_exit_min or 0
        self.last_activity = time.time()
        self.q = queue.Queue()

    def enqueue(self, pid):
        self.q.put(pid)

    def _idle_tick(self):
        p2a.maybe_idle_models(self.tts_cfg.get("park_after_s", 300),
                              self.tts_cfg.get("unload_after_s", 1800))
        if self.grobid_cfg:
            import grobid
            grobid.maybe_stop(self.grobid_cfg.get("idle_stop_s", 600))
        self.lib.flush()  # persist any debounced updates while quiet
        if (self.idle_exit_min and self.q.empty()
                and time.time() - self.last_activity
                    >= self.idle_exit_min * 60):
            # nothing has touched the server (SSE heartbeats excluded) —
            # exit cleanly; SIGINT lets uvicorn shut down gracefully and
            # the run() finally-block stop GROBID and flush the registry
            print(f"idle for {self.idle_exit_min} min — shutting down")
            import signal
            os.kill(os.getpid(), signal.SIGINT)

    def run(self):
        while True:
            try:
                pid = self.q.get(timeout=60)
            except queue.Empty:
                try:
                    self._idle_tick()
                except Exception as e:  # a failed flush/park must never
                    print(f"idle tick failed: {e}")  # kill the only worker
                continue
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
                    # memory-only: SSE clients see it, disk doesn't —
                    # ~50 full-registry writes per paper served no one
                    self.lib.update(pid, persist=False,
                                    progress=round(frac, 3))

            try:
                info = p2a.generate_readalong(
                    self.lib.pdf_path(pid), self.lib.view_dir(pid),
                    self.voice, self.speed, self.dpi, progress,
                    grobid_cfg=self.grobid_cfg, tts_cfg=self.tts_cfg)
                fields = dict(status="ready", progress=1.0,
                              duration=round(info["duration"], 1),
                              warnings=info["warnings"])
                with self.lib.lock:
                    locked = self.lib.data["papers"][pid].get("meta_locked")
                if not locked:  # Zotero-sourced metadata is authoritative
                    fields.update(title=info["title"], authors=info["authors"],
                                  year=info["year"])
                self.lib.update(pid, **fields)
            except Exception as e:  # keep the queue alive on any failure
                self.lib.update(pid, status="error", error=str(e))
            finally:
                if self.grobid_cfg:  # generation time isn't GROBID idle time
                    import grobid
                    grobid.touch()


def create_app(lib, worker):
    app = FastAPI(title="Rhapsode")

    @app.middleware("http")
    async def _touch_activity(request, call_next):
        # every real request counts as activity for idle-exit — except the
        # long-lived SSE stream, whose heartbeats would keep us alive forever
        if not request.url.path.startswith("/api/events"):
            worker.last_activity = time.time()
        return await call_next(request)

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
        pid = json.loads(resp.body)["id"]

        # Zotero's curated metadata is authoritative: apply it (also on
        # duplicates, healing bad extracted titles) and lock it so the
        # generation worker doesn't overwrite it later
        fields = {k: body[k] for k in ("title", "authors") if body.get(k)}
        if body.get("year"):
            fields["year"] = int(body["year"])
        if fields:
            fields["meta_locked"] = True
            lib.update(pid, **fields)

        name = str(body.get("playlist", "")).strip()
        if name:
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
        # in-memory immediately, disk at most every 30 s (worst case on a
        # crash: the resume point is half a minute stale)
        lib.update(pid, bump=False, persist=False,
                   resume_t=float(body.get("t", 0)))
        return {"ok": True}

    @app.get("/api/status")
    def status_endpoint():
        import grobid
        with lib.lock:
            counts = {}
            for p in lib.data["papers"].values():
                counts[p["status"]] = counts.get(p["status"], 0) + 1
        return {"kokoro": p2a.pipeline_status(),
                "grobid": grobid.status(),
                "papers": counts, "queue": worker.q.qsize()}

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

    payload_cache = {"v": -1, "body": ""}

    @app.get("/api/events")
    async def events():
        # async generator: SSE clients cost zero threadpool tokens (the old
        # sync version pinned one of ~40 pool threads per open tab), and the
        # serialized snapshot is shared across clients per version
        async def gen():
            last, idle = 0, 0
            while True:
                v = lib.version
                if v != last:
                    last, idle = v, 0
                    if payload_cache["v"] != v:
                        payload_cache["v"] = v
                        payload_cache["body"] = json.dumps(lib.snapshot())
                    yield f"data: {payload_cache['body']}\n\n"
                elif idle >= 20:
                    idle = 0
                    # heartbeat: without periodic yields a dead client's
                    # generator is never closed and leaks
                    yield ": ping\n\n"
                else:
                    idle += 1
                await asyncio.sleep(0.7)
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


def run(root, port, voice, speed, dpi, open_browser=False, grobid_cfg=None,
        tts_cfg=None, idle_exit_min=0):
    root = Path(root)
    if not root.exists() and not root.parent.exists():
        sys.exit(f"error: library location unavailable (is the volume "
                 f"mounted?): {root}")
    root.mkdir(parents=True, exist_ok=True)

    lib = Library(root)
    worker = Worker(lib, voice, speed, dpi, grobid_cfg, tts_cfg, idle_exit_min)
    with lib.lock:  # crash recovery: re-queue anything left mid-generation
        for pid, entry in lib.data["papers"].items():
            if entry["status"] in ("generating", "pending"):
                entry["status"] = "pending"
                worker.enqueue(pid)
            if "year" not in entry:  # migration: entries predating year
                try:                 # (None = already tried; don't re-parse
                    import fitz      # every year-less PDF on each launch)
                    from extraction import _page_year
                    entry["year"] = _page_year(fitz.open(lib.pdf_path(pid))[0])
                except Exception:
                    entry["year"] = None
        lib.save()
    worker.start()

    port = _free_port(port)
    url = f"http://127.0.0.1:{port}"
    print(f"Rhapsode library: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: subprocess.Popen(
            ["xdg-open", url], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)).start()
    # never-ending SSE streams would make graceful shutdown wait forever
    # (Ctrl+C seemingly dead); force-close connections after a short grace
    try:
        uvicorn.run(create_app(lib, worker), host="127.0.0.1", port=port,
                    log_level="warning", timeout_graceful_shutdown=3)
    finally:
        import grobid
        grobid.stop()   # don't orphan a JVM we started
        lib.flush()     # persist debounced positions/progress
