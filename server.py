"""Local library web app for Rhapsode: drag-drop PDFs, a generation
queue on a warm GPU model, and a podcast-style read-along playlist.
Bound to 127.0.0.1 only."""

import asyncio
import concurrent.futures
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
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)

import auth
import rhapsode as p2a
from extraction import clean_text, extract_segments

MAX_UPLOAD = 100 * 1024 * 1024
REPO = Path(__file__).resolve().parent


class Library:
    """Registry of papers under one root folder; atomic JSON persistence."""

    def __init__(self, root):
        self.root = root
        self.file = root / "library.json"
        self.bak = root / "library.json.bak"
        self.lock = threading.RLock()
        self.version = 1
        self._dirty = False
        self._last_save = 0.0
        self.data = {"papers": {}, "order": [], "playlists": {},
                     "settings": {"auto_advance": True}}
        # A registry lost or corrupted by an unclean shutdown takes playlists
        # and playback positions with it, so fall back to the previous good
        # copy. (is_file() is False for a corrupt entry too, hence the retry.)
        for src in (self.file, self.bak):
            try:
                if src.is_file():
                    self.data.update(json.loads(src.read_text()))
                    if src is self.bak:
                        print(f"warning: library.json unreadable — recovered "
                              f"from {self.bak.name}")
                    break
            except (OSError, ValueError) as e:
                print(f"warning: could not read {src.name} ({e})")
        self.data.setdefault("playlists", {})  # pre-playlist registries

    def save(self, bump=True):
        with self.lock:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            # retain the previous good copy under a different name: filesystem
            # damage that takes out library.json (or its directory entry) then
            # still leaves playlists and positions recoverable
            try:
                if self.file.is_file():
                    shutil.copy2(self.file, self.bak)
            except OSError:
                pass  # a backup is best-effort; never block the save
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
                 idle_exit_min=0, llm_cfg=None):
        super().__init__(daemon=True)
        self.lib, self.voice, self.speed, self.dpi = lib, voice, speed, dpi
        self.grobid_cfg = grobid_cfg
        self.tts_cfg = tts_cfg or {}
        self.llm_cfg = llm_cfg
        self.idle_exit_min = idle_exit_min or 0
        self.last_activity = time.time()
        self.q = queue.Queue()
        self._prefetch = None   # (pid, Future): next paper extracted early

    def enqueue(self, pid):
        self.last_activity = time.time()
        self.q.put(pid)

    def _prefetch_ok(self):
        """Only overlap extraction with synthesis when synthesis is REMOTE.
        With local Kokoro, extraction (which may drive a local LLM) would
        contend with it for the same GPU — the way to cook a laptop."""
        return (self.tts_cfg or {}).get("backend") == "modal"

    def _start_prefetch(self):
        """Pull the next queued paper forward and extract it in the
        background. It stays 'pending' in the registry, so a restart re-queues
        it normally and nothing is lost if we never get to it."""
        if not self._prefetch_ok() or self._prefetch is not None:
            return
        try:
            nxt = self.q.get_nowait()
        except queue.Empty:
            return
        with self.lib.lock:
            entry = self.lib.data["papers"].get(nxt)
            if entry is None or entry["status"] != "pending":
                return  # deleted or already handled; drop it like the main loop
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(p2a.prepare_units, self.lib.pdf_path(nxt),
                          self.grobid_cfg, self.llm_cfg)
        fut.add_done_callback(lambda _f: pool.shutdown(wait=False))
        self._prefetch = (nxt, fut)

    def _take_prefetch(self, pid):
        """The prepared extraction for pid, if we ran one. Errors are re-raised
        here so a failed prefetch fails that paper, exactly as inline would."""
        if not self._prefetch or self._prefetch[0] != pid:
            return None
        _, fut = self._prefetch
        self._prefetch = None
        return fut.result()

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
            time.sleep(1)  # narrow the check-to-kill race: anything that
            if (not self.q.empty()  # arrived meanwhile vetoes the exit
                    or time.time() - self.last_activity
                        < self.idle_exit_min * 60):
                return
            # nothing has touched the server (SSE heartbeats excluded) —
            # exit cleanly; SIGINT lets uvicorn shut down gracefully and
            # the run() finally-block stop GROBID and flush the registry
            print(f"idle for {self.idle_exit_min} min — shutting down")
            import signal
            signal.raise_signal(signal.SIGINT)  # cross-platform SIGINT

    def run(self):
        while True:
            # a paper pulled forward by the prefetch is no longer in the queue,
            # so it must be taken from the slot — otherwise it is extracted and
            # then never generated, and the occupied slot disables all further
            # prefetching
            if self._prefetch is not None:
                pid = self._prefetch[0]
            else:
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
                    self._prefetch = None  # drop it, or we spin on it forever
                    continue  # deleted, or a duplicate queue entry
                entry.update(status="generating", progress=0.0, error=None)
                self.lib.save()
            last = [0.0, ""]

            def progress(frac, label):
                # push on a stage CHANGE too, not just a 2 % move: extraction
                # sits at 0 % for minutes, and that is exactly when the user
                # most needs to be told what is happening
                if frac - last[0] >= 0.02 or frac >= 1.0 or label != last[1]:
                    last[0], last[1] = frac, label
                    # memory-only: SSE clients see it, disk doesn't —
                    # ~50 full-registry writes per paper served no one
                    self.lib.update(pid, persist=False,
                                    progress=round(frac, 3), stage=label)

            try:
                prepared = self._take_prefetch(pid)
                if prepared is None:
                    progress(0.0, "extracting text")
                    prepared = p2a.prepare_units(self.lib.pdf_path(pid),
                                                 self.grobid_cfg, self.llm_cfg)
                else:
                    # extraction already happened in the background; say so
                    # rather than showing a blank stage until the first unit
                    progress(0.0, "starting narration")
                # extraction for the NEXT paper runs while this one narrates,
                # so neither remote container idles into a cold start
                self._start_prefetch()
                info = p2a.generate_readalong(
                    self.lib.pdf_path(pid), self.lib.view_dir(pid),
                    self.voice, self.speed, self.dpi, progress,
                    grobid_cfg=self.grobid_cfg, tts_cfg=self.tts_cfg,
                    llm_cfg=self.llm_cfg, prepared=prepared)
                fields = dict(status="ready", progress=1.0,
                              duration=round(info["duration"], 1),
                              warnings=info["warnings"])
                with self.lib.lock:
                    locked = self.lib.data["papers"][pid].get("meta_locked")
                if not locked:  # Zotero-sourced metadata is authoritative
                    fields.update(title=info["title"], authors=info["authors"],
                                  year=info["year"])
                self.lib.update(pid, **fields)
            except RuntimeError as e:
                # an interpreter shutting down makes pool.submit raise; that is
                # an artifact of exiting, not a bad paper — leave it 'generating'
                # so crash recovery re-queues it instead of failing it forever
                if "cannot schedule new futures" in str(e):
                    print("shutdown during generation; paper will resume")
                    return
                raise
            except Exception as e:  # keep the queue alive on any failure
                self.lib.update(pid, status="error", error=str(e))
            finally:
                self.last_activity = time.time()  # work isn't idle time —
                # the idle-exit clock starts when the batch ENDS
                if self.grobid_cfg:
                    import grobid
                    grobid.touch()


def create_app(lib, worker, auth_cfg=None, users=None):
    app = FastAPI(title="Rhapsode")
    auth_cfg = auth_cfg or {}
    secret = auth.load_secret(lib.root)
    multiuser = users is not None

    # ---------------------------------------------------------- ownership
    # Storage is one registry with an owner per paper, so isolation is a code
    # invariant rather than a filesystem boundary. It therefore lives in
    # exactly these three helpers, and every route that names a paper goes
    # through one of them — the direct-object routes (/view, /api/papers/{id})
    # matter more than the listing, since an id can be guessed.
    def caller(request):
        """Username for this request, or None when accounts are off."""
        return getattr(request.state, "user", None) if multiuser else None

    def visible(entry, who):
        if not multiuser or who is None:
            return True
        return entry.get("owner") == who or entry.get("shared") or \
            users.is_admin(who)

    def see(pid, request):
        """The paper, if this caller may see it — 404 otherwise, never 403:
        a wrong id and someone else's id must be indistinguishable."""
        entry = lib.paper(pid)
        if not visible(entry, caller(request)):
            raise HTTPException(404, "unknown paper")
        return entry

    def own(pid, request):
        """The paper, if this caller may change it. Sharing a paper does not
        hand over control of it, so `shared` is deliberately not enough."""
        entry = see(pid, request)
        who = caller(request)
        if multiuser and who is not None and entry.get("owner") != who \
                and not users.is_admin(who):
            raise HTTPException(403, "that paper belongs to someone else")
        return entry

    @app.middleware("http")
    async def _touch_activity(request, call_next):
        # every real request counts as activity for idle-exit — except the
        # long-lived SSE stream, whose heartbeats would keep us alive forever
        if not request.url.path.startswith("/api/events"):
            worker.last_activity = time.time()
        return await call_next(request)

    # scrypt is deliberately slow, so remember Basic headers that already
    # verified — machine clients (the Zotero plugin) send one per request
    _basic_ok = {}          # header hash -> username (None in single-user)

    def _basic_valid(header):
        if not header.startswith("Basic "):
            return None
        import hashlib as _h
        key = _h.sha256(header.encode()).hexdigest()
        if key in _basic_ok:
            return _basic_ok[key] or True
        try:
            import base64 as _b
            _user, _, pw = _b.b64decode(header[6:]).decode().partition(":")
        except Exception:
            return None
        if multiuser:
            who = users.check(_user, pw)
            if who:
                _basic_ok[key] = who
                return who
            return None
        if auth.verify_password(pw, auth_cfg.get("password_hash", "")):
            _basic_ok[key] = None
            return True
        return None

    @app.middleware("http")
    async def _require_login(request, call_next):
        """Session gate. Off entirely when no password is configured, so a
        localhost install keeps working exactly as before. Browsers carry the
        session cookie; machine clients (Zotero plugin, speechd, curl) may
        send HTTP Basic with the same password instead."""
        if not auth_cfg.get("password_hash") and not multiuser:
            return await call_next(request)
        path = request.url.path
        if path in ("/login", "/logout") or path.startswith("/favicon") \
                or path.startswith("/join"):
            return await call_next(request)
        who = auth.session_user(request.cookies.get(auth.COOKIE, ""), secret)
        if who is not None and (not multiuser or users.exists(who)):
            request.state.user = who      # a deleted account stops working
            return await call_next(request)
        basic = _basic_valid(request.headers.get("authorization", ""))
        if basic:
            request.state.user = basic if isinstance(basic, str) else None
            return await call_next(request)
        # a browser gets the login page; an API caller gets a clean 401
        if path.startswith("/api/") or path.startswith("/tts"):
            return JSONResponse({"detail": "login required"}, status_code=401)
        return Response(status_code=303, headers={"Location": "/login"})

    @app.get("/login", response_class=HTMLResponse)
    def login_page(bad: int = 0):
        page = REPO / "login.html"
        if not page.is_file():
            return HTMLResponse("<p>login.html missing</p>", status_code=500)
        html = page.read_text()
        if not bad:
            html = html.replace('<p class="bad">', '<p class="bad" hidden>')
        return HTMLResponse(html)

    def _login_cookie(resp, who):
        resp.set_cookie(auth.COOKIE, auth.issue(secret, who or ""),
                        httponly=True, secure=True, samesite="lax",
                        max_age=auth.DEFAULT_TTL, path="/")
        return resp

    @app.post("/login")
    def login_submit(username: str = Form(""), password: str = Form("")):
        who = users.check(username, password) if multiuser else (
            "" if auth.verify_password(password,
                                       auth_cfg.get("password_hash", "")) else None)
        if who is None:
            # same page, same wording, no hint about which part was wrong
            return Response(status_code=303, headers={"Location": "/login?bad=1"})
        return _login_cookie(
            Response(status_code=303, headers={"Location": "/dashboard"}), who)

    # ------------------------------------------------------------- invites
    @app.get("/join/{token}", response_class=HTMLResponse)
    def join_page(token: str, bad: str = ""):
        page = REPO / "join.html"
        if not multiuser or not page.is_file():
            raise HTTPException(404, "not found")
        if not users.invite_ok(token):
            return HTMLResponse(page.read_text()
                                .replace("{{token}}", "")
                                .replace("{{error}}", "This invite link is "
                                         "invalid, already used, or expired.")
                                .replace("{{formhidden}}", "hidden"),
                                status_code=410)
        return HTMLResponse(page.read_text()
                            .replace("{{token}}", token)
                            .replace("{{error}}", bad)
                            .replace("{{formhidden}}", ""))

    @app.post("/join/{token}")
    def join_submit(token: str, username: str = Form(""),
                    password: str = Form("")):
        if not multiuser:
            raise HTTPException(404, "not found")
        try:
            who = users.redeem(token, username, password)
        except ValueError as e:
            return Response(status_code=303, headers={
                "Location": f"/join/{token}?bad={quote(str(e))}"})
        return _login_cookie(
            Response(status_code=303, headers={"Location": "/dashboard"}), who)

    @app.get("/api/me")
    def whoami(request: Request):
        who = caller(request)
        return {"user": who, "admin": bool(who and users.is_admin(who)),
                "multiuser": multiuser}

    @app.post("/api/invites")
    def make_invite(request: Request):
        who = caller(request)
        if not multiuser or not who or not users.is_admin(who):
            raise HTTPException(403, "admins only")
        token = users.mint_invite(who)
        return {"token": token, "path": f"/join/{token}"}

    @app.get("/api/users")
    def list_users(request: Request):
        who = caller(request)
        if not multiuser or not who or not users.is_admin(who):
            raise HTTPException(403, "admins only")
        counts = {}
        for entry in lib.snapshot()["papers"].values():
            counts[entry.get("owner")] = counts.get(entry.get("owner"), 0) + 1
        return {"users": [{"name": n, "admin": users.is_admin(n),
                           "papers": counts.get(n, 0)} for n in users.names()],
                "invites": users.open_invites()}

    @app.delete("/api/users/{name}")
    def remove_user(name: str, request: Request):
        who = caller(request)
        if not multiuser or not who or not users.is_admin(who):
            raise HTTPException(403, "admins only")
        try:
            users.delete(name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/papers/{pid}/share")
    def share_paper(pid: str, request: Request, body: dict = None):
        entry = own(pid, request)
        with lib.lock:
            entry["shared"] = bool((body or {}).get("shared", True))
            lib.save()
        return {"ok": True, "shared": entry["shared"]}

    @app.post("/logout")
    def logout():
        resp = Response(status_code=303, headers={"Location": "/login"})
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page():
        page = REPO / "dashboard.html"
        if not page.is_file():
            return HTMLResponse("<p>dashboard.html missing</p>",
                                status_code=500)
        return HTMLResponse(page.read_text())

    @app.get("/api/dashboard")
    def dashboard_data(request: Request):
        snap = lib.snapshot()
        who = caller(request)
        if multiuser and who is not None:
            snap["papers"] = {pid: e for pid, e in snap["papers"].items()
                              if visible(e, who)}
            snap["order"] = [x for x in snap["order"] if x in snap["papers"]]
        papers = snap["papers"]
        by_status = {}
        hours = 0.0
        for p in papers.values():
            by_status[p.get("status", "?")] = by_status.get(p.get("status", "?"), 0) + 1
            hours += (p.get("duration") or 0)
        generating = [{"id": pid, "title": p.get("title"),
                       "stage": p.get("stage"), "progress": p.get("progress")}
                      for pid, p in papers.items()
                      if p.get("status") == "generating"]
        resume = sorted(
            ({"id": pid, "title": p.get("title"), "at": p.get("resume_t"),
              "duration": p.get("duration")}
             for pid, p in papers.items()
             if p.get("status") == "ready" and (p.get("resume_t") or 0) > 30),
            key=lambda r: -(r["at"] or 0))[:5]
        recent = [{"id": pid, "title": p.get("title")}
                  for pid, p in sorted(papers.items(),
                                       key=lambda kv: -(kv[1].get("added") or 0))
                  if p.get("status") == "ready"][:5]
        failed = [{"id": pid, "title": p.get("title"), "error": p.get("error")}
                  for pid, p in papers.items() if p.get("status") == "error"]
        try:
            st = os.statvfs(lib.root)
            free_gb = st.f_bavail * st.f_frsize / 1e9
        except OSError:
            free_gb = None
        tts = (worker.tts_cfg or {})
        llm = (worker.llm_cfg or {})
        return {
            "stats": {"papers": len(papers), "hours": round(hours / 3600, 1),
                      "by_status": by_status},
            "generating": generating, "queued": by_status.get("pending", 0),
            "resume": resume, "recent": recent, "failed": failed,
            # deliberately NOT pinged: a health check would wake a GPU
            # container and bill for it. Report what is configured.
            "machinery": {
                "voice": f"Kokoro {worker.voice}"
                         + (" on Modal" if tts.get("backend") == "modal"
                            else " on this machine"),
                "extraction": (f"{llm.get('model') or 'LLM'} via "
                               f"{llm.get('runner')}") if llm.get("enabled")
                              else ("GROBID" if (worker.grobid_cfg or {}).get("enabled")
                                    else "built-in heuristics"),
                "free_gb": round(free_gb, 1) if free_gb is not None else None,
            },
        }

    @app.get("/", response_class=HTMLResponse)
    def home():
        page = REPO / "library.html"
        if not page.is_file():
            return HTMLResponse("<p>library.html missing from the repo</p>",
                                status_code=500)
        return page.read_text(encoding="utf-8")

    @app.get("/api/library")
    def get_library(request: Request):
        snap = lib.snapshot()
        who = caller(request)
        if multiuser and who is not None:
            snap["papers"] = {pid: e for pid, e in snap["papers"].items()
                              if visible(e, who)}
            snap["order"] = [pid for pid in snap["order"]
                             if pid in snap["papers"]]
            for pl in snap.get("playlists", {}).values():
                pl["order"] = [pid for pid in pl["order"]
                               if pid in snap["papers"]]
        return snap

    def _ingest(filename, data, owner=None):
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file exceeds 100 MB")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, f"{filename}: not a PDF")
        digest = hashlib.sha1(data).hexdigest()[:10]
        with lib.lock:
            for pid, entry in lib.data["papers"].items():
                # dedupe only against papers this caller can already see:
                # answering "you already have that" about someone else's
                # private paper would confirm they hold it
                if entry.get("hash") == digest and visible(entry, owner):
                    return JSONResponse({"id": pid, "duplicate": True})
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-",
                      Path(filename or "paper").stem)[:40].strip("-.")
        pid = f"{slug or 'paper'}-{digest}"
        if owner and pid in lib.data["papers"]:
            pid = f"{pid}-{owner}"      # same PDF, another user's own copy
        paper_dir = lib.root / pid
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / "paper.pdf").write_bytes(data)

        entry = {"id": pid, "hash": digest, "filename": filename,
                 "title": Path(filename or pid).stem, "authors": None,
                 "year": None, "status": "pending", "progress": 0.0,
                 "error": None, "duration": None, "resume_t": 0.0,
                 "added": time.time(), "owner": owner, "shared": False}
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

    def _apply_meta(pid, body):
        """Curated (Zotero) metadata is authoritative: apply + lock it, and
        optionally append to a named playlist. Returns plid or None."""
        fields = {k: body[k] for k in ("title", "authors") if body.get(k)}
        if body.get("year"):
            fields["year"] = int(body["year"])
        if fields:
            fields["meta_locked"] = True
            lib.update(pid, **fields)
        name = str(body.get("playlist", "")).strip()
        if not name:
            return None
        plid = lib.playlist_by_name(name)
        with lib.lock:
            order = lib.data["playlists"][plid]["order"]
            if pid not in order:
                order.append(pid)
                lib.save()
        return plid

    @app.post("/api/papers")
    def add_paper(request: Request, file: UploadFile, title: str = Form(""),
                  authors: str = Form(""), year: str = Form(""),
                  playlist: str = Form("")):
        resp = _ingest(file.filename, file.file.read(MAX_UPLOAD + 1),
                       caller(request))
        pid = json.loads(resp.body)["id"]
        plid = _apply_meta(pid, {"title": title, "authors": authors,
                                 "year": year, "playlist": playlist})
        if plid:
            return JSONResponse({**json.loads(resp.body), "playlist": plid})
        return resp

    @app.post("/api/papers/by-path")
    def add_by_path(request: Request, body: dict):
        """Ingest a local PDF by absolute path (used by the Zotero plugin
        against a localhost server; remote plugins upload via /api/papers)."""
        src = Path(str(body.get("path", ""))).expanduser()
        if not src.is_file():
            raise HTTPException(404, f"no such file: {src}")
        resp = _ingest(src.name, src.read_bytes(), caller(request))
        pid = json.loads(resp.body)["id"]
        plid = _apply_meta(pid, body)
        if plid:
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
    def delete_paper(pid: str, request: Request):
        own(pid, request)
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
    def regenerate(pid: str, request: Request):
        own(pid, request)
        with lib.lock:
            entry = lib.paper(pid)
            if entry["status"] in ("generating", "pending"):
                return {"ok": True, "queued": False}  # dedupe repeat clicks
            entry.update(status="pending", progress=0.0, error=None)
            lib.save()
        worker.enqueue(pid)
        return {"ok": True, "queued": True}

    @app.post("/api/papers/{pid}/position")
    def position(pid: str, request: Request, body: dict):
        see(pid, request)
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
    def reorder(request: Request, body: dict):
        order = body.get("order", [])
        who = caller(request)
        with lib.lock:
            if not multiuser or who is None:
                if sorted(order) != sorted(lib.data["order"]):
                    raise HTTPException(400, "order must contain exactly the "
                                             "current paper ids")
                lib.data["order"] = order
            else:
                # the caller can only see their own slice, so they may only
                # permute that slice; other users' papers keep their places
                mine = [pid for pid in lib.data["order"]
                        if visible(lib.data["papers"][pid], who)]
                if sorted(order) != sorted(mine):
                    raise HTTPException(400, "order must contain exactly the "
                                             "papers you can see")
                it = iter(order)
                lib.data["order"] = [next(it) if pid in set(mine) else pid
                                     for pid in lib.data["order"]]
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
        parts = []
        if worker.tts_cfg.get("backend") == "modal":
            # speechd voice rides the configured backend too — don't
            # force-load a local model the user configured away
            unit = [{"text": text[:5000], "pause": 0}]
            for _u, chunks in p2a._modal_unit_audio(
                    unit, voice or worker.voice, speed, worker.tts_cfg):
                parts += [w for w, _words in chunks]
        else:
            pipeline = p2a.get_pipeline()
            with p2a.TTS_LOCK:
                for item in pipeline(text[:5000],
                                     voice=voice or worker.voice,
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
    def view(pid: str, request: Request, path: str = ""):
        see(pid, request)          # the audio itself, not just the listing
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


def _bootstrap_users(lib, auth_cfg):
    """Turn a single-password install into a named-account one, once.

    Returns the Users store, or None to stay single-user. Both steps are
    idempotent: the operator's existing password keeps working (so an upgrade
    cannot lock them out), and every paper that predates accounts becomes
    theirs, private.
    """
    if not auth_cfg.get("multiuser"):
        return None
    users = auth.Users(lib.root)
    admin = auth.normalise_username(auth_cfg.get("admin_user") or "admin")
    if not users.names():
        pw_hash = auth_cfg.get("password_hash")
        if not pw_hash:
            print("  [auth] multiuser is on but no password_hash is set; "
                  "no admin account was created", flush=True)
            return users
        users.create(admin, None, admin=True, pw_hash=pw_hash)
        print(f"  [auth] created admin account '{admin}' from the existing "
              f"password", flush=True)
    owner = users.admins()[0] if users.admins() else admin
    with lib.lock:
        changed = 0
        for entry in lib.data["papers"].values():
            if entry.get("owner") is None:
                entry["owner"] = owner       # papers predating accounts
                entry.setdefault("shared", False)
                changed += 1
        if changed:
            lib.save()
            print(f"  [auth] {changed} existing paper(s) now owned by "
                  f"'{owner}', private", flush=True)
    return users


def run(root, port, voice, speed, dpi, open_browser=False, grobid_cfg=None,
        tts_cfg=None, idle_exit_min=0, llm_cfg=None, auth_cfg=None):
    root = Path(root)
    if not root.exists() and not root.parent.exists():
        sys.exit(f"error: library location unavailable (is the volume "
                 f"mounted?): {root}")
    root.mkdir(parents=True, exist_ok=True)

    lib = Library(root)
    users = _bootstrap_users(lib, auth_cfg or {})
    worker = Worker(lib, voice, speed, dpi, grobid_cfg, tts_cfg, idle_exit_min,
                    llm_cfg)
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
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # never-ending SSE streams would make graceful shutdown wait forever
    # (Ctrl+C seemingly dead); force-close connections after a short grace
    try:
        uvicorn.run(create_app(lib, worker, auth_cfg, users), host="127.0.0.1",
                    port=port,
                    log_level="warning", timeout_graceful_shutdown=3)
    finally:
        import grobid
        grobid.stop()   # don't orphan a JVM we started
        if llm_cfg:
            import llm
            llm.release(llm_cfg)  # nor leave a model pinning GPU memory
        lib.flush()     # persist debounced positions/progress
