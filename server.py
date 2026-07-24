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
import secrets
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
from starlette.concurrency import run_in_threadpool

import auth
import secretbox
import rhapsode as p2a
from extraction import clean_text, extract_segments

MAX_UPLOAD = 100 * 1024 * 1024
PAPERS_PER_USER = 200      # per non-admin account; the operator
                           # pays for every paper's GPU time
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

    def _new_plid(self, name, owner, parent):
        base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "pl"
        if owner:
            base = f"{owner}--{base}"
        plid = base
        while plid in self.data["playlists"]:      # two "Corrosion" folders
            plid = f"{base}-{secrets.token_hex(2)}"  # under different parents
        self.data["playlists"][plid] = {"name": name, "order": [],
                                        "owner": owner, "parent": parent}
        return plid

    def resolve_folder(self, segments, owner=None):
        """Ensure a folder path exists and return the LEAF plid, creating any
        missing ancestor folders. The ONLY place the tree is built: migration,
        the Zotero plugin's "A / B" names, and the New-folder UI all route
        through here, so the "/" convention and the parent field never drift."""
        with self.lock:
            parent = None
            leaf = None
            for seg in [s.strip() for s in segments if s.strip()]:
                match = next(
                    (pid for pid, pl in self.data["playlists"].items()
                     if pl.get("parent") == parent and pl.get("name") == seg
                     and pl.get("owner") == owner), None)
                leaf = match or self._new_plid(seg, owner, parent)
                parent = leaf
            if leaf is not None:
                self.save()
            return leaf

    def playlist_by_name(self, name, owner=None):
        """Find-or-create by a possibly-slashed path name; returns the leaf
        plid. 'Osteo Lab / Sheep Model' resolves into the real tree."""
        return self.resolve_folder(str(name).split("/"), owner)

    def create_folder(self, name, owner=None, parent=None):
        """A single new folder (leaf) under an explicit parent plid."""
        with self.lock:
            plid = self._new_plid(name, owner, parent)
            self.save()
            return plid

    def pdf_path(self, pid):
        return self.root / pid / "paper.pdf"

    def view_dir(self, pid):
        return self.root / pid / "readalong"


# --- metered resources: paid backends the operator fronts. A resource's
# per-user usage is what quota gating and the admin dashboard read. v1 has
# one; a shared Anthropic/OpenAI/Gemini key later registers as another.
METERS = {"tts_hours": {"label": "audio hours", "unit": "h"}}


def operator_tts_hours(lib, who):
    """Audio hours of `who`'s ready papers synthesised on the OPERATOR's Modal
    account (billed absent = operator). Derived from the library, so deleting a
    paper drops the usage — no separate ledger to fall out of sync."""
    total = 0.0
    for p in lib.snapshot()["papers"].values():
        if (p.get("status") == "ready" and p.get("owner") == who
                and p.get("billed", "operator") == "operator"):
            total += p.get("duration") or 0
    return round(total / 3600, 4)


def decrypt_profile(users, who, key):
    """The owner's decrypted Modal profile, or None (no key, none attached, or
    a blob that won't decrypt — treat all three as 'run on the operator')."""
    if not (users and who and key):
        return None
    blob = users.get_modal_enc(who)
    if not blob:
        return None
    try:
        return json.loads(secretbox.decrypt(blob, key, who.encode()))
    except Exception:
        return None      # never surface the reason; fall back quietly


QUOTA_BLOCK_MSG = ("Over your shared-compute quota. Attach your own Modal in "
                   "Settings, or ask an admin to raise your cap.")


class Worker(threading.Thread):
    """Single generation thread — the GPU is serial; the model stays warm
    during a batch and is parked/unloaded (with GROBID stopped) when the
    queue has been idle, so nothing heavy stays resident between bursts."""

    def __init__(self, lib, voice, speed, dpi, grobid_cfg=None, tts_cfg=None,
                 idle_exit_min=0, llm_cfg=None, users=None, secret_key=None):
        super().__init__(daemon=True)
        self.lib, self.voice, self.speed, self.dpi = lib, voice, speed, dpi
        self.grobid_cfg = grobid_cfg
        self.tts_cfg = tts_cfg or {}
        self.llm_cfg = llm_cfg
        self.users = users
        self.secret_key = secret_key
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

    def _resolve_creds(self, owner):
        """(tts_cfg, llm_cfg, billed) for this paper's owner: their own Modal
        if attached (bills them), else the operator's shared endpoint."""
        prof = decrypt_profile(self.users, owner, self.secret_key)
        tts, llm, billed = self.tts_cfg, self.llm_cfg, "operator"
        if prof and prof.get("tts"):
            t = prof["tts"]
            tts = {**self.tts_cfg, "backend": "modal",
                   "modal_endpoint": t["endpoint"],
                   "modal_token_id": t["token_id"],
                   "modal_token_secret": t["token_secret"]}
            billed = "self"
        if prof and prof.get("llm"):
            l = prof["llm"]
            llm = {**(self.llm_cfg or {}), "enabled": True,
                   "api_base_url": l["api_base_url"], "api_key": l["api_key"]}
        return tts, llm, billed

    def _quota_block(self, owner, billed):
        """The block message if this operator-billed job is over the owner's
        cap, else None. Self-billed jobs and uncapped users are never blocked."""
        if billed != "operator" or not self.users or not owner:
            return None
        cap = self.users.get_quota(owner).get("tts_hours")
        if cap is None:
            return None
        if operator_tts_hours(self.lib, owner) >= cap:
            return QUOTA_BLOCK_MSG
        return None

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

            # resolve creds once per job by the paper's OWNER: their own Modal
            # profile (bills them, billed="self") if attached, else the
            # operator's shared endpoint (billed="operator")
            with self.lib.lock:
                owner = self.lib.data["papers"][pid].get("owner")
            tts_cfg, llm_cfg, billed = self._resolve_creds(owner)

            blocked = self._quota_block(owner, billed)
            if blocked:
                self.lib.update(pid, status="blocked", progress=0.0,
                                error=blocked)
                continue

            try:
                prepared = self._take_prefetch(pid)
                if prepared is None:
                    progress(0.0, "extracting text")
                    prepared = p2a.prepare_units(self.lib.pdf_path(pid),
                                                 self.grobid_cfg, llm_cfg)
                else:
                    # extraction already happened in the background; say so
                    # rather than showing a blank stage until the first unit
                    progress(0.0, "starting narration")
                # extraction for the NEXT paper runs while this one narrates,
                # so neither remote container idles into a cold start. The
                # prefetch deliberately uses the operator self.llm_cfg — a v1
                # simplification, since LLM tokens are not metered this phase.
                self._start_prefetch()
                info = p2a.generate_readalong(
                    self.lib.pdf_path(pid), self.lib.view_dir(pid),
                    self.voice, self.speed, self.dpi, progress,
                    grobid_cfg=self.grobid_cfg, tts_cfg=tts_cfg,
                    llm_cfg=llm_cfg, prepared=prepared)
                fields = dict(status="ready", progress=1.0, billed=billed,
                              duration=round(info["duration"], 1),
                              warnings=info["warnings"])
                with self.lib.lock:
                    locked = self.lib.data["papers"][pid].get("meta_locked")
                if not locked:  # Zotero-sourced metadata is authoritative
                    fields.update(title=info["title"], authors=info["authors"],
                                  year=info["year"])
                self.lib.update(pid, **fields)
            except BaseException as e:  # keep the queue alive on ANY failure
                # ONE handler, deliberately. This used to be two: `except
                # RuntimeError: ... raise` followed by `except Exception`. A
                # re-raise from a matched clause is not offered to its
                # siblings, so every RuntimeError — a Modal endpoint error, an
                # ffmpeg failure, a CUDA OOM — escaped the loop and killed the
                # only worker thread permanently, while the HTTP server kept
                # answering 200 and nothing alerted.
                if isinstance(e, RuntimeError) and \
                        "cannot schedule new futures" in str(e):
                    # an interpreter shutting down makes pool.submit raise;
                    # that is an artifact of exiting, not a bad paper — leave
                    # it 'generating' so crash recovery re-queues it
                    print("shutdown during generation; paper will resume")
                    return
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise                      # a real stop must still stop
                try:
                    self.lib.update(pid, status="error", error=str(e))
                except Exception as inner:     # never let bookkeeping kill it
                    print(f"could not record failure for {pid}: {inner}")
            finally:
                self.last_activity = time.time()  # work isn't idle time —
                # the idle-exit clock starts when the batch ENDS
                if self.grobid_cfg:
                    import grobid
                    grobid.touch()


def create_app(lib, worker, auth_cfg=None, users=None, secret_key=None):
    app = FastAPI(title="Rhapsode")

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request, exc):
        # FastAPI's default handler echoes the offending input back, and a
        # bare non-finite JSON float (Infinity / NaN) then crashes the response
        # serializer (json.dumps(allow_nan=False)) into a 500. Return a fixed
        # 400 that never re-serializes attacker input.
        return JSONResponse({"detail": "invalid request body"}, status_code=400)

    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request, exc):
        # A browser navigating to a missing OR access-gated page (e.g. /admin
        # with accounts off) should see a friendly page, not raw JSON. This
        # also catches unmatched routes (Starlette raises a 404 here). API and
        # fetch callers still get JSON, so the frontend and tests are unchanged.
        # The 404 message is deliberately generic: it must not reveal whether a
        # page exists-but-forbidden versus does-not-exist (the isolation rule).
        wants_html = "text/html" in request.headers.get("accept", "")
        if (exc.status_code == 404 and wants_html
                and not request.url.path.startswith("/api/")):
            page = REPO / "notfound.html"
            body = page.read_text() if page.is_file() else "<h1>Not found</h1>"
            return HTMLResponse(body, status_code=404)
        # default behaviour for every other case (status, JSON detail, headers)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
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
        if entry.get("status") == "reserving":
            return False        # a half-created upload is nobody's paper yet
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
    async def _security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        # The read-along is generated HTML carrying PDF-derived strings. Its
        # JSON is escaped now, but a CSP is the second line of defence that
        # was entirely absent: no remote script, no framing by anyone else.
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "media-src 'self'; frame-ancestors 'self'; base-uri 'none'; "
            "form-action 'self'")
        return resp

    @app.middleware("http")
    async def _touch_activity(request, call_next):
        # every real request counts as activity for idle-exit — except the
        # long-lived SSE stream, whose heartbeats would keep us alive forever
        if not request.url.path.startswith("/api/events"):
            worker.last_activity = time.time()
        return await call_next(request)

    # scrypt is deliberately slow, so remember Basic headers that already
    # verified — machine clients (the Zotero plugin) send one per request
    # header hash -> (username, users-version). Caching the scrypt result is
    # what keeps Basic auth usable for machine clients, but a cache with no
    # invalidation outlives the account: a deleted user, or one whose password
    # was changed, kept working until the process restarted. Stamping each
    # entry with the store's revision makes every entry stale the moment the
    # user table changes.
    _basic_ok = {}
    _auth_state_lock = threading.Lock()   # _basic_ok, _attempts, _streams are
                                          # now touched from threadpool workers
    _basic_inflight = {}                  # header-hash -> Future; the event
                                          # loop is single-threaded, so this
                                          # map needs no lock

    async def _verify_basic(header, key):
        # Coalesce concurrent verifications of the SAME header onto one scrypt.
        # A burst of a correct credential (or a flood of one wrong credential)
        # then costs a single hash and a single throttle count, not N — which
        # both stops a legit machine client's parallel burst from false-429ing
        # itself on a cold cache, and keeps a same-credential flood from
        # pinning the CPU.
        existing = _basic_inflight.get(key)
        if existing is not None:
            return await existing
        fut = asyncio.get_event_loop().create_future()
        _basic_inflight[key] = fut
        try:
            result = await run_in_threadpool(_basic_valid, header)
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            _basic_inflight.pop(key, None)

    # A per-caller sliding-window throttle. nginx limit_req is the better place
    # (docs/hosting.md), but the app must not depend on the proxy being
    # configured: every miss on these paths costs a deliberate scrypt hash.
    _attempts = {}

    def _throttled(key, limit=10, window=60):
        now = time.time()
        with _auth_state_lock:
            hits = [t for t in _attempts.get(key, []) if now - t < window]
            _attempts[key] = hits + [now]
            if len(_attempts) > 4096:         # bounded
                _attempts.clear()
            return len(hits) >= limit

    trust_proxy = bool(auth_cfg.get("trust_proxy"))

    def _client_ip(request):
        # The socket peer is UNSPOOFABLE over an established connection; an
        # X-Forwarded-For header is fully client-controlled. Default to the
        # peer, so an attacker cannot land in a fresh throttle bucket per
        # request by rotating XFF. Behind the documented nginx the peer is
        # 127.0.0.1 for everyone (one shared bucket — safe, just conservative);
        # set [auth] trust_proxy = true to read the client the proxy reports.
        # Take the RIGHTMOST token: nginx's $remote_addr overwrites to a single
        # value, and $proxy_add_x_forwarded_for appends the real peer last —
        # either way the last hop is the one your proxy vouched for, and any
        # value the client injected sits to its left, untrusted.
        if trust_proxy:
            fwd = request.headers.get("x-forwarded-for", "")
            if fwd:
                return fwd.split(",")[-1].strip()
        return request.client.host if request.client else "?"

    def _users_rev():
        return users.revision if multiuser else 0

    def _basic_valid(header):
        if not header.startswith("Basic "):
            return None
        import hashlib as _h
        key = _h.sha256(header.encode()).hexdigest()
        hit = _basic_ok.get(key)
        if hit is not None:
            if hit[1] == _users_rev():
                return hit[0] or True
            with _auth_state_lock:
                _basic_ok.pop(key, None)     # stale: don't let it linger
        try:
            import base64 as _b
            _user, _, pw = _b.b64decode(header[6:]).decode().partition(":")
        except Exception:
            return None
        # Capture the revision BEFORE the ~tens-of-ms scrypt, not after. A
        # delete or password-change committing DURING the hash bumps the live
        # revision; stamping with the post-hash value would mark this stale
        # result "current" and cache a resurrected account. Stamped with the
        # pre-hash revision, the very next lookup is a miss and re-validates.
        rev = _users_rev()
        who = (users.check(_user, pw) if multiuser else
               ("" if auth.verify_password(pw, auth_cfg.get("password_hash", ""))
                else None))
        if who is None:
            return None
        with _auth_state_lock:     # _basic_ok is touched from threadpool workers
            if len(_basic_ok) > 512:   # bounded in both regimes; the single-
                _basic_ok.clear()      # user branch is what is live today
            _basic_ok[key] = (who or None, rev)
        return who or True

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
                or path.startswith("/join") or path.startswith("/auth/"):
            return await call_next(request)   # /auth/<token> IS the login step
        claims = auth.session_claims(request.cookies.get(auth.COOKIE, ""), secret)
        if claims is not None:
            who, epoch = claims
            # The epoch pins the session to the account generation it was
            # issued under: a deleted account, a changed password, or a
            # username later recreated all rotate it, and turning multiuser
            # off changes the marker so an invitee's cookie stops working
            # instead of silently becoming the single-user session.
            want = (users.epoch(who) if multiuser and who
                    else ("multi" if multiuser else "single"))
            if epoch == want and (not multiuser or not who or users.exists(who)):
                request.state.user = who
                return await call_next(request)
        # scrypt is deliberately expensive, and this middleware is async, so
        # verifying inline froze the whole event loop for every other client
        # for the duration — from an UNAUTHENTICATED request path.
        header = request.headers.get("authorization", "")
        basic = None
        if header.startswith("Basic "):
            # A never-seen credential is a cache miss and a full scrypt hash,
            # so the flood the /login throttle stops just moves here unless the
            # Basic path is throttled too. Count a request only when it will
            # START a new scrypt: a currently-valid cache hit is free, and a
            # request coalescing onto an already-running verification of the
            # same header adds no new hash for the throttle to bound.
            import hashlib as _h
            key = _h.sha256(header.encode()).hexdigest()
            hit = _basic_ok.get(key)
            cheap = hit is not None and hit[1] == _users_rev()
            if (not cheap and key not in _basic_inflight
                    and _throttled(f"basic:{_client_ip(request)}", limit=20)):
                return JSONResponse({"detail": "too many attempts"},
                                    status_code=429)
            basic = await _verify_basic(header, key)
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
        resp.set_cookie(auth.COOKIE,
                        auth.issue(secret, who or "",
                                   epoch=users.epoch(who) if multiuser and who
                                   else ("multi" if multiuser else "single")),
                        httponly=True, secure=True, samesite="lax",
                        max_age=auth.DEFAULT_TTL, path="/")
        return resp

    # one-time login links: an already-authenticated machine client (the Zotero
    # plugin, with its stored credentials) mints a short-lived single-use token
    # so the browser tab it opens lands signed in as the SAME account, without
    # the password ever touching a URL.
    _session_links = {}   # token -> (user, expiry epoch)

    @app.post("/api/session-link")
    def make_session_link(request: Request):
        if not (multiuser or auth_cfg.get("password_hash")):
            raise HTTPException(404, "not found")   # no login gate to hand off
        who = caller(request) or ""                 # "" = the single-user account
        now = time.time()
        for t in [t for t, (_u, e) in _session_links.items() if e < now]:
            _session_links.pop(t, None)             # sweep expired
        token = secrets.token_urlsafe(24)
        _session_links[token] = (who, now + 60)     # 60s to open the tab
        return {"path": f"/auth/{token}"}

    @app.get("/auth/{token}")
    def consume_session_link(token: str, next: str = "/"):
        got = _session_links.pop(token, None)        # single use
        if not got or got[1] < time.time():
            return Response(status_code=303, headers={"Location": "/login"})
        # local paths only — never redirect to another origin
        dest = next if (next.startswith("/")
                        and not next.startswith(("//", "/\\"))) else "/"
        return _login_cookie(
            Response(status_code=303, headers={"Location": dest}), got[0])

    @app.post("/login")
    def login_submit(request: Request, username: str = Form(""),
                     password: str = Form("")):
        if _throttled(f"login:{_client_ip(request)}"):
            raise HTTPException(429, "too many attempts; wait a minute")
        who = users.check(username, password) if multiuser else (
            "" if auth.verify_password(password,
                                       auth_cfg.get("password_hash", "")) else None)
        if who is None:
            # same page, same wording, no hint about which part was wrong
            return Response(status_code=303, headers={"Location": "/login?bad=1"})
        return _login_cookie(
            Response(status_code=303, headers={"Location": "/dashboard"}), who)

    # ------------------------------------------------------------- invites
    def _num(v):
        """A number from a JSON body, or 400 — not a 500 traceback."""
        try:
            n = float(v if v is not None else 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "t must be a number")
        if n != n or n in (float("inf"), float("-inf")) or n < 0:
            raise HTTPException(400, "t must be a finite, non-negative number")
        return n

    def _esc(v):
        return (str(v).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;")
                .replace("'", "&#39;"))

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
        # `bad` is echoed from the query string, and `token` from the path:
        # both are attacker-controlled and land in HTML, so escape them
        return HTMLResponse(page.read_text()
                            .replace("{{token}}", _esc(token))
                            .replace("{{error}}", _esc(bad))
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
        # give a new account the default shared-compute cap (admins, who are
        # bootstrapped rather than invited, stay uncapped); the admin can raise
        # or clear it per user on the Manage page
        cap = auth_cfg.get("default_tts_hours")
        if cap:
            users.set_quota(who, "tts_hours", float(cap))
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
        papers = lib.snapshot()["papers"]
        counts, hours = {}, {}
        for p in papers.values():
            o = p.get("owner")
            counts[o] = counts.get(o, 0) + 1
            if p.get("status") == "ready":
                hours[o] = hours.get(o, 0.0) + (p.get("duration") or 0)
        rows = []
        for n in users.names():
            rows.append({
                "name": n, "admin": users.is_admin(n),
                "papers": counts.get(n, 0),
                "hours": round(hours.get(n, 0.0) / 3600, 2),
                "operator_hours": operator_tts_hours(lib, n),
                "self_hosting": users.has_modal(n),
                "quota": users.get_quota(n)})
        by_status = {}
        for p in papers.values():
            by_status[p.get("status")] = by_status.get(p.get("status"), 0) + 1
        try:
            st = os.statvfs(lib.root)
            free_gb = round(st.f_bavail * st.f_frsize / 1e9, 1)
        except OSError:
            free_gb = None
        health = {"queued": by_status.get("pending", 0),
                  "generating": by_status.get("generating", 0),
                  "needs_attention": by_status.get("error", 0)
                                     + by_status.get("blocked", 0),
                  "free_gb": free_gb}
        return {"users": rows, "invites": users.open_invites(), "health": health,
                "meters": METERS}

    @app.put("/api/users/{name}")
    def set_user_quota(name: str, request: Request, body: dict):
        who = caller(request)
        if not multiuser or not who or not users.is_admin(who):
            raise HTTPException(404, "not found")
        if not users.exists(name):
            raise HTTPException(404, "unknown user")
        for res, val in (body.get("quota") or {}).items():
            users.set_quota(name, res, None if val is None else float(val))
        return {"ok": True}

    # -------------------------------------------------- self-service Modal
    # /api/account/* is ALWAYS "me": the caller, resolved from the session,
    # never a path parameter. There is deliberately no route that names
    # another user, so no user can read or write anyone else's profile.
    def _me(request):
        who = caller(request)
        if not multiuser or not who:
            raise HTTPException(404, "not found")
        return who

    def _last4(s):
        return (s or "")[-4:]

    @app.get("/api/account/modal")
    def account_modal(request: Request):
        who = _me(request)
        prof = decrypt_profile(users, who, secret_key) or {}
        tts, llm = prof.get("tts") or {}, prof.get("llm") or {}
        # Never the secret: presence, the non-secret endpoint/base_url, and
        # the last 4 chars of the token as a "which key is this?" hint only.
        return {
            "enabled": bool(secret_key),
            "tts": {"attached": bool(tts),
                    "endpoint": tts.get("endpoint", ""),
                    "last4": _last4(tts.get("token_secret"))},
            "llm": {"attached": bool(llm),
                    "base_url": llm.get("api_base_url", ""),
                    "last4": _last4(llm.get("api_key"))},
            "usage": {"tts_hours": operator_tts_hours(lib, who)},
            "quota": users.get_quota(who),
        }

    @app.put("/api/account/modal")
    def set_account_modal(request: Request, body: dict):
        who = _me(request)
        if not secret_key:
            raise HTTPException(400, "per-user Modal is not enabled on this "
                                     "server ([secrets] key is unset)")
        # MERGE into the existing profile: the TTS and LLM forms save
        # independently, so writing one group must never wipe the other. Use
        # DELETE ?group=... to remove a group.
        prof = decrypt_profile(users, who, secret_key) or {}
        changed = False
        tts = body.get("tts")
        if isinstance(tts, dict) and tts.get("endpoint"):
            prof["tts"] = {"endpoint": str(tts["endpoint"]),
                           "token_id": str(tts.get("token_id", "")),
                           "token_secret": str(tts.get("token_secret", ""))}
            changed = True
        llm = body.get("llm")
        if isinstance(llm, dict) and llm.get("api_base_url"):
            prof["llm"] = {"api_base_url": str(llm["api_base_url"]),
                           "api_key": str(llm.get("api_key", ""))}
            changed = True
        if not changed:
            raise HTTPException(400, "nothing to save")
        blob = secretbox.encrypt(json.dumps(prof), secret_key, who.encode())
        users.set_modal_enc(who, blob)
        return {"ok": True}

    @app.delete("/api/account/modal")
    def clear_account_modal(request: Request, group: str = ""):
        who = _me(request)
        # ?group=tts|llm clears just that credential (keeping the other);
        # no group clears the whole profile.
        if group in ("tts", "llm") and secret_key:
            prof = decrypt_profile(users, who, secret_key) or {}
            prof.pop(group, None)
            if prof:
                users.set_modal_enc(who, secretbox.encrypt(
                    json.dumps(prof), secret_key, who.encode()))
            else:
                users.clear_modal_enc(who)
        else:
            users.clear_modal_enc(who)
        return {"ok": True}

    @app.post("/api/account/modal/test")
    def test_account_modal(request: Request, group: str = "tts"):
        who = _me(request)
        prof = decrypt_profile(users, who, secret_key) or {}
        if group == "llm":
            llm = prof.get("llm")
            if not llm:
                raise HTTPException(400, "no LLM endpoint attached")
            # a free auth/reachability probe of the caller's OWN endpoint:
            # GET {base}/models (the OpenAI-compatible discovery route)
            try:
                import requests
                base = str(llm["api_base_url"]).rstrip("/")
                r = requests.get(base + "/models", timeout=15, headers={
                    "Authorization": f"Bearer {llm.get('api_key', '')}"})
                r.raise_for_status()
                return {"ok": True}
            except Exception:
                return {"ok": False,
                        "error": "Could not reach your LLM endpoint. Check the "
                                 "base URL and API key in your settings."}
        tts = prof.get("tts")
        if not tts:
            raise HTTPException(400, "no Modal TTS endpoint attached")
        cfg = {"backend": "modal", "modal_endpoint": tts["endpoint"],
               "modal_token_id": tts["token_id"],
               "modal_token_secret": tts["token_secret"]}
        # one tiny synth against the caller's OWN endpoint — the only place the
        # server contacts Modal, and only on an explicit click by the payer
        unit = {"text": "test.", "kind": "body"}
        try:
            for _ in p2a._modal_unit_audio([unit], "af_heart", 1.0, cfg):
                break
            return {"ok": True}
        except Exception:
            # never surface the exception text: it can contain the endpoint/token
            return {"ok": False,
                    "error": "Could not reach your Modal endpoint. Check the "
                             "URL and token pair in your settings."}

    @app.delete("/api/invites/{key}")
    def revoke_invite(key: str, request: Request):
        who = caller(request)
        if not multiuser or not who or not users.is_admin(who):
            raise HTTPException(403, "admins only")
        try:
            users.revoke_invite(key)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        who = caller(request)
        if not multiuser or not who or not users.is_admin(who):
            raise HTTPException(404, "not found")
        page = REPO / "admin.html"
        if not page.is_file():
            return HTMLResponse("<p>admin.html missing</p>", status_code=500)
        return HTMLResponse(page.read_text())

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        who = caller(request)
        if not multiuser or not who:
            raise HTTPException(404, "not found")
        page = REPO / "settings.html"
        if not page.is_file():
            return HTMLResponse("<p>settings.html missing</p>", status_code=500)
        return HTMLResponse(page.read_text())

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

    def _extraction_label(llm, grobid_cfg):
        model = (llm.get("model") or "LLM").split("/")[-1]   # drop "google/"
        base = llm.get("api_base_url") or ""
        if "modal.run" in base:
            return f"{model} on Modal"
        if llm.get("runner") == "ollama":
            return f"{model} on this machine"
        if llm.get("runner") == "api":
            return f"{model} via API"
        return f"{model} via {llm.get('runner')}"

    def _folder_path(plid):
        segs, cur = [], plid
        while cur:
            pl = lib.data["playlists"].get(cur)
            if not pl:
                break
            segs.insert(0, pl.get("name") or cur)
            cur = pl.get("parent")
        return " / ".join(segs)

    DONE = 0.95

    def _with_playlist(item, papers):
        """If the resumed paper was opened from a folder that still holds it,
        turn the item into a playlist-aware one: shown paper = the current
        (next unfinished) member, plus time-through-folder progress."""
        plid = item.pop("pl", None)
        folder = lib.data["playlists"].get(plid) if plid else None
        if not folder:
            return item
        members = [(m, papers[m]) for m in folder.get("order", [])
                   if m in papers and papers[m].get("status") == "ready"]
        if not any(m == item["id"] for m, _ in members):
            return item                      # not in this folder anymore
        durs = [(pp.get("duration") or 0) for _, pp in members]
        total = sum(durs) or 1
        listened = sum(min(pp.get("resume_t") or 0, pp.get("duration") or 0)
                       for _, pp in members)
        done = lambda pp: (pp.get("duration") and
                           (pp.get("resume_t") or 0) >= DONE * pp["duration"])
        idx = next(i for i, (m, _) in enumerate(members) if m == item["id"])
        cur = idx
        while cur < len(members) and done(members[cur][1]):
            cur += 1                          # skip finished papers
        if cur >= len(members):
            cur = idx                         # all done: stay on the anchor
        cid, cp = members[cur]
        return {"id": cid, "title": cp.get("title"), "authors": cp.get("authors"),
                "year": cp.get("year"), "at": cp.get("resume_t"),
                "duration": cp.get("duration"), "playlist_id": plid,
                "playlist_path": _folder_path(plid), "pos": cur + 1,
                "count": len(members), "listened_frac": round(listened / total, 4),
                "anchor_id": item["id"]}    # the paper you listened to; forget targets it

    @app.get("/api/dashboard")
    def dashboard_data(request: Request):
        snap = _scoped(caller(request))
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
        def card(pid, p):
            return {"id": pid, "title": p.get("title"),
                    "authors": p.get("authors"), "year": p.get("year"),
                    "at": p.get("resume_t"), "duration": p.get("duration")}
        resume = sorted(
            ({**card(pid, p), "opened": p.get("last_opened") or 0,
              "pl": p.get("last_playlist")}
             for pid, p in papers.items()
             if p.get("status") == "ready" and (p.get("resume_t") or 0) > 30),
            # most recently opened first; papers predating last_opened fall
            # back to how far in you are, so nothing vanishes on upgrade
            key=lambda r: (-(r["opened"]), -(r["at"] or 0)))[:8]
        resume = [_with_playlist(r, papers) for r in resume]
        # the whole shelf, newest first, for the cover wall
        shelf = [card(pid, p) for pid, p in sorted(
            papers.items(), key=lambda kv: -(kv[1].get("added") or 0))
            if p.get("status") == "ready"]
        recent = [{"id": r["id"], "title": r["title"]} for r in shelf[:5]]
        # playlists as grid-scoping groups: their ready members, in order,
        # already filtered to what this caller may see by _scoped()
        ready_ids = {r["id"] for r in shelf}
        # every visible folder (already ownership-scoped by _scoped), INCLUDING
        # empty ones — they are the tree's container nodes — with members
        # filtered to what has a cover to show
        playlists = [
            {"id": plid, "name": pl.get("name") or plid,
             "parent": pl.get("parent"),
             "order": [pid for pid in pl.get("order", []) if pid in ready_ids]}
            for plid, pl in (snap.get("playlists") or {}).items()]
        failed = [{"id": pid, "title": p.get("title"),
                   "error": p.get("error"), "status": p.get("status")}
                  for pid, p in papers.items()
                  if p.get("status") in ("error", "blocked")]
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
            "resume": resume, "shelf": shelf, "playlists": playlists,
            "recent": recent,
            "failed": failed,
            # deliberately NOT pinged: a health check would wake a GPU
            # container and bill for it. Report what is configured.
            "machinery": {
                "voice": f"Kokoro {worker.voice}"
                         + (" on Modal" if tts.get("backend") == "modal"
                            else " on this machine"),
                "extraction": _extraction_label(llm, worker.grobid_cfg)
                              if llm.get("enabled")
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

    def _scoped(who):
        """The registry as `who` may see it. ONE implementation, used by the
        library, the dashboard and the SSE stream — three copies of this rule
        is how the stream came to leak while the other two were correct."""
        snap = lib.snapshot()
        if not multiuser or who is None:
            return snap
        snap["papers"] = {pid: e for pid, e in snap["papers"].items()
                          if visible(e, who)}
        for e in snap["papers"].values():
            # a reader of a shared paper keeps their own place in it
            by = e.pop("resume_by", None) or {}
            if e.get("owner") != who and who in by:
                e["resume_t"] = by[who]
        snap["order"] = [pid for pid in snap["order"] if pid in snap["papers"]]
        snap["playlists"] = {
            plid: {**pl, "order": [pid for pid in pl["order"]
                                   if pid in snap["papers"]]}
            for plid, pl in snap.get("playlists", {}).items()
            if pl.get("owner") == who or users.is_admin(who)}
        return snap

    @app.get("/api/library")
    def get_library(request: Request):
        return _scoped(caller(request))

    def _ingest(filename, data, owner=None):
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file exceeds 100 MB")
        # MAX_UPLOAD caps one file; nothing capped how many. Every paper is
        # GPU time on the operator's account, so an invited user gets a
        # ceiling — generous enough to be invisible in normal use.
        if multiuser and owner and not users.is_admin(owner):
            with lib.lock:
                mine = sum(1 for e in lib.data["papers"].values()
                           if e.get("owner") == owner)
            if mine >= PAPERS_PER_USER:
                raise HTTPException(
                    429, f"you have {mine} papers, the limit is "
                         f"{PAPERS_PER_USER}. Delete one, or ask an admin.")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, f"{filename}: not a PDF")
        digest = hashlib.sha1(data).hexdigest()[:10]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-",
                      Path(filename or "paper").stem)[:40].strip("-.")
        # dedupe check and id choice happen under ONE lock: apart, two users
        # uploading the same PDF at once both saw "no match" and then raced
        # for the same directory
        with lib.lock:
            for pid, entry in lib.data["papers"].items():
                # dedupe only against papers this caller can already see:
                # answering "you already have that" about someone else's
                # private paper would confirm they hold it
                if entry.get("hash") == digest and visible(entry, owner):
                    return JSONResponse({"id": pid, "duplicate": True})
            pid = f"{slug or 'paper'}-{digest}"
            # A colliding id means another account already holds this exact
            # PDF. Disambiguating with the owner's name (or a counter) made
            # the id's SHAPE reveal that, re-opening the oracle the dedupe
            # check closes. A nonce reveals nothing.
            while pid in lib.data["papers"]:
                pid = f"{slug or 'paper'}-{digest}-{secrets.token_hex(3)}"
            lib.data["papers"][pid] = {"id": pid, "status": "reserving",
                                       "hash": digest, "owner": owner}
        try:
            paper_dir = lib.root / pid
            paper_dir.mkdir(parents=True, exist_ok=True)
            (paper_dir / "paper.pdf").write_bytes(data)
        except Exception:
            with lib.lock:                     # release the reserved id
                lib.data["papers"].pop(pid, None)
            raise

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

    def _apply_meta(pid, body, who=None):
        """Curated (Zotero) metadata is authoritative: apply + lock it, and
        optionally append to a named playlist. Returns plid or None."""
        # ...but only for a paper this caller owns. Uploading a copy of a
        # SHARED paper used to rewrite its owner's title/authors/year and lock
        # them, letting one user edit another's metadata by re-adding it.
        entry = lib.paper(pid)
        if multiuser and who is not None and entry.get("owner") not in (None, who):
            return None
        fields = {k: body[k] for k in ("title", "authors") if body.get(k)}
        if body.get("year"):
            try:
                fields["year"] = int(body["year"])
            except (TypeError, ValueError, OverflowError):
                pass          # a junk year is dropped, not a 500 after ingest
                              # (OverflowError: int(float('inf')) from year:1e999)
        if fields:
            fields["meta_locked"] = True
            lib.update(pid, **fields)
        target = str(body.get("playlist_id", "")).strip()
        name = str(body.get("playlist", "")).strip()
        if target:
            pl = lib.data["playlists"].get(target)
            # only a folder the caller owns; a bad id just files nothing
            if not pl or (multiuser and who is not None
                          and pl.get("owner") not in (None, who)
                          and not (users and users.is_admin(who))):
                return None
            plid = target
        elif name:
            plid = lib.playlist_by_name(name, owner=who)
        else:
            return None
        with lib.lock:
            order = lib.data["playlists"][plid]["order"]
            if pid not in order:
                order.append(pid)
                lib.save()
        return plid

    @app.post("/api/papers")
    def add_paper(request: Request, file: UploadFile, title: str = Form(""),
                  authors: str = Form(""), year: str = Form(""),
                  playlist: str = Form(""), playlist_id: str = Form("")):
        resp = _ingest(file.filename, file.file.read(MAX_UPLOAD + 1),
                       caller(request))
        pid = json.loads(resp.body)["id"]
        plid = _apply_meta(pid, {"title": title, "authors": authors,
                                 "year": year, "playlist": playlist,
                                 "playlist_id": playlist_id},
                           caller(request))
        if plid:
            return JSONResponse({**json.loads(resp.body), "playlist": plid})
        return resp

    @app.post("/api/papers/by-path")
    def add_by_path(request: Request, body: dict):
        """Ingest a local PDF by absolute path (used by the Zotero plugin
        against a localhost server; remote plugins upload via /api/papers)."""
        # This route reads an arbitrary path off the server's own disk. That is
        # exactly right for a localhost install, where the caller IS the owner
        # of the filesystem, and catastrophic on a shared host: a user could
        # name another user's <root>/<pid>/paper.pdf — or any file on the box —
        # and have it ingested into their own shelf. Remote clients already
        # upload bytes through POST /api/papers, so the route simply does not
        # exist when accounts are on.
        if multiuser:
            raise HTTPException(404, "not found")
        src = Path(str(body.get("path", ""))).expanduser()
        if not src.is_file():
            raise HTTPException(404, f"no such file: {src}")
        resp = _ingest(src.name, src.read_bytes(), caller(request))
        pid = json.loads(resp.body)["id"]
        plid = _apply_meta(pid, body, caller(request))
        if plid:
            return JSONResponse({**json.loads(resp.body), "playlist": plid})
        return resp

    # Playlists predate accounts and had NO ownership at all: any user could
    # rename or delete anyone's, every name was globally visible, and two
    # users who both made "Reading" silently shared one object. They now carry
    # an owner and go through the same shape of check as papers.
    def own_playlist(plid, request):
        who = caller(request)
        with lib.lock:
            pl = lib.data["playlists"].get(plid)
            if not pl:
                raise HTTPException(404, "unknown playlist")
            if multiuser and who is not None:
                # An ownerless playlist is NOT everybody's. Every playlist that
                # predates accounts has owner=None, so treating None as public
                # handed the operator's own playlists to every invitee.
                if pl.get("owner") != who and not users.is_admin(who):
                    raise HTTPException(404, "unknown playlist")
            return pl

    @app.post("/api/playlists")
    def create_playlist(request: Request, body: dict):
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "playlist name required")
        who = caller(request)
        parent = body.get("parent")
        if parent:
            # a subfolder under an explicit parent: `name` is a LEAF, so a "/"
            # is invalid (the UI's New-subfolder path)
            if "/" in name:
                raise HTTPException(400, "a folder name cannot contain '/'")
            own_playlist(parent, request)
            return {"id": lib.create_folder(name, owner=who, parent=parent)}
        if "/" in name:
            # a slashed PATH with no explicit parent (the Zotero plugin's
            # "Grandparent / Parent / Child"): resolve it into the folder tree,
            # find-or-create each level. Idempotent, so re-sending never
            # duplicates a folder.
            return {"id": lib.playlist_by_name(name, owner=who)}
        return {"id": lib.create_folder(name, owner=who, parent=None)}

    @app.put("/api/playlists/{plid}")
    def update_playlist(plid: str, request: Request, body: dict):
        pl = own_playlist(plid, request)
        who = caller(request)
        with lib.lock:
            if body.get("name"):
                if "/" in str(body["name"]):
                    raise HTTPException(400, "a folder name cannot contain '/'")
                pl["name"] = str(body["name"]).strip()
            if "parent" in body:                     # reparent (drag a folder)
                new_parent = body["parent"] or None
                if new_parent:
                    own_playlist(new_parent, request)   # must own the target
                    cur = new_parent                    # and no cycle: the
                    while cur:                          # target must not be
                        if cur == plid:                 # this folder or below
                            raise HTTPException(400, "cannot move a folder into "
                                                     "itself or its own subfolder")
                        cur = lib.data["playlists"].get(cur, {}).get("parent")
                pl["parent"] = new_parent
            if "order" in body:
                if not isinstance(body["order"], list) or \
                        not all(isinstance(x, str) for x in body["order"]):
                    raise HTTPException(400, "order must be a list of ids")
                # compare against the caller-visible slice and splice it back,
                # or the response is an oracle for members they cannot see
                mine = [pid for pid in pl["order"]
                        if pid in lib.data["papers"]
                        and visible(lib.data["papers"][pid], who)]
                if sorted(body["order"]) != sorted(mine):
                    raise HTTPException(400, "order must contain exactly the "
                                             "playlist papers you can see")
                it = iter(body["order"])
                pl["order"] = [next(it) if pid in set(mine) else pid
                               for pid in pl["order"]]
            lib.save()
        return {"ok": True}

    @app.delete("/api/playlists/{plid}")
    def delete_playlist(plid: str, request: Request):
        own_playlist(plid, request)
        with lib.lock:
            del lib.data["playlists"][plid]
            lib.save()
        return {"ok": True}

    @app.post("/api/playlists/{plid}/papers")
    def playlist_add(plid: str, request: Request, body: dict):
        pid = str(body.get("id", ""))
        see(pid, request)          # NOT lib.paper(): that answered 200 for a
        pl = own_playlist(plid, request)   # real-but-invisible id, 404 for a
        with lib.lock:                     # bogus one — an existence oracle
            if pid not in pl["order"]:
                pl["order"].append(pid)
                lib.save()
        return {"ok": True}

    @app.delete("/api/playlists/{plid}/papers/{pid}")
    def playlist_remove(plid: str, pid: str, request: Request):
        pl = own_playlist(plid, request)
        with lib.lock:
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

    @app.post("/api/papers/{pid}/forget")
    def forget_paper(pid: str, request: Request):
        """Remove a paper from the resume history: clear the caller's saved
        position so it leaves 'in progress'. Does not delete the paper."""
        entry = see(pid, request)
        who = caller(request)
        with lib.lock:
            if multiuser and who is not None and entry.get("owner") != who:
                (entry.get("resume_by") or {}).pop(who, None)   # a reader's own
            else:
                entry["resume_t"] = 0.0
                entry["last_opened"] = 0
                entry["last_playlist"] = None
            lib.save()
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
        entry = see(pid, request)
        who = caller(request)
        if multiuser and who is not None and entry.get("owner") != who:
            # a shared paper is read by several people; one listener's place
            # must not move everyone else's. Non-owners keep their own.
            with lib.lock:
                entry.setdefault("resume_by", {})[who] = _num(body.get("t"))
                lib.touch(bump=False)
            return {"ok": True}
        # in-memory immediately, disk at most every 30 s (worst case on a
        # crash: the resume point is half a minute stale)
        lib.update(pid, bump=False, persist=False,
                   resume_t=_num(body.get("t")), last_opened=time.time())
        return {"ok": True}

    @app.get("/api/status")
    def status_endpoint(request: Request):
        import grobid
        who = caller(request)
        with lib.lock:
            counts = {}
            for p in lib.data["papers"].values():
                if not visible(p, who):   # counting everyone's papers told a
                    continue              # user how many others had uploaded
                counts[p["status"]] = counts.get(p["status"], 0) + 1
        out = {"kokoro": p2a.pipeline_status(), "grobid": grobid.status(),
               "papers": counts}
        if not multiuser or who is None or users.is_admin(who):
            out["queue"] = worker.q.qsize()   # global depth: admins only
        return out

    @app.put("/api/queue")
    def reorder(request: Request, body: dict):
        order = body.get("order", [])
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
            raise HTTPException(400, "order must be a list of paper ids")
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
    def settings(request: Request, body: dict = None):
        who = caller(request)
        if multiuser and who is not None and not users.is_admin(who):
            # one object shared by everyone: a colleague toggling auto-advance
            # would change it for the whole lab
            raise HTTPException(403, "settings are shared; ask an admin")
        return _settings(body or {})

    def _settings(body: dict):
        with lib.lock:
            if "auto_advance" in body:
                if not isinstance(body["auto_advance"], bool):
                    raise HTTPException(400, "auto_advance must be true or false")
                lib.data["settings"]["auto_advance"] = body["auto_advance"]
            lib.save()
        return {"ok": True}

    payload_cache = {"key": None, "body": ""}
    _streams = {}          # open SSE streams per caller

    @app.get("/api/events")
    async def events(request: Request):
        # async generator: SSE clients cost zero threadpool tokens (the old
        # sync version pinned one of ~40 pool threads per open tab), and the
        # serialized snapshot is shared across clients per version.
        # The stream is a READ PATH like /api/library and must filter the same
        # way: it previously pushed the whole registry to every open tab, which
        # handed every user every other user's paper ids, titles and errors.
        who = caller(request)
        # every open tab holds a socket and a task; without a cap one client
        # can exhaust the file-descriptor limit and wedge the server
        if _streams.get(who, 0) >= 8:
            raise HTTPException(429, "too many open event streams")
        _streams[who] = _streams.get(who, 0) + 1
        opened_epoch = users.epoch(who) if multiuser and who else None

        async def gen():
            last, idle = 0, 0
            while True:
                # a stream opened before the account was deleted OR its
                # password changed would otherwise keep delivering that user's
                # view for the life of the socket — the revocation the epoch
                # promises must reach the streaming channel too
                if multiuser and who is not None and (
                        not users.exists(who)
                        or users.epoch(who) != opened_epoch):
                    return
                v = lib.version
                if v != last:
                    last, idle = v, 0
                    # cache per (version, viewer): one viewer's filtered
                    # payload must never be served to the next one
                    if payload_cache.get("key") != (v, who):
                        payload_cache["key"] = (v, who)
                        payload_cache["body"] = json.dumps(_scoped(who))
                    yield f"data: {payload_cache['body']}\n\n"
                elif idle >= 20:
                    idle = 0
                    # heartbeat: without periodic yields a dead client's
                    # generator is never closed and leaks
                    yield ": ping\n\n"
                else:
                    idle += 1
                await asyncio.sleep(0.7)

        async def counted():
            try:
                async for chunk in gen():
                    yield chunk
            finally:
                _streams[who] = max(0, _streams.get(who, 1) - 1)

        return StreamingResponse(counted(), media_type="text/event-stream")

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

    @app.get("/api/papers/{pid}/preview")
    def paper_preview(pid: str, request: Request):
        """The opening sentence(s), so the dashboard can remind you what a
        paper IS before you press play. Reads the already-generated manifest;
        no PDF work, no GPU."""
        entry = see(pid, request)
        mf = lib.view_dir(pid) / "manifest.json"
        try:
            units = json.loads(mf.read_text()).get("units") or []
        except (OSError, ValueError):
            return {"text": ""}
        title = " ".join((entry.get("title") or "").lower().split())
        out = []
        for u in units:
            t = (u.get("text") or "").strip()
            norm = " ".join(t.lower().split())
            # skip the title unit (units[0] is the title) and bare headings
            if len(t) <= 12 or (title and (norm in title or title in norm)):
                continue
            out.append(t)
            if sum(len(x) for x in out) > 320:
                break
        return {"text": " ".join(out)[:360]}

    @app.get("/view/{pid}/{path:path}")
    def view(pid: str, request: Request, path: str = "", pl: str = ""):
        see(pid, request)          # the audio itself, not just the listing
        if path in ("", "index.html"):
            # "pick up where you left off" orders by when you last OPENED a
            # paper, not how deep the saved position is. `pl` records the
            # FOLDER you opened it from, so the hero can show progress through
            # that playlist; opening without one clears it back to per-paper.
            folder = lib.data["playlists"].get(pl) if pl else None
            context = pl if (folder and pid in folder.get("order", [])) else None
            lib.update(pid, bump=False, persist=False,
                       last_opened=time.time(), last_playlist=context)
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


def _migrate_playlist_tree(lib):
    """One-time: turn slashed playlist names ("A / B / C") into a real parent
    tree, in place, keeping each playlist's own id and members. Idempotent
    (guarded by a flag), lossless, and shared with resolve_folder so the
    convention and the field can never diverge."""
    with lib.lock:
        if lib.data.get("playlist_tree_migrated"):
            return
        pls = lib.data.get("playlists") or {}
        changed = 0
        # shallowest paths first, so an ancestor is normalised before its child
        for plid, pl in sorted(pls.items(),
                               key=lambda kv: (kv[1].get("name") or "").count("/")):
            pl.setdefault("parent", None)
            segs = [x.strip() for x in (pl.get("name") or plid).split("/")
                    if x.strip()]
            if len(segs) <= 1:
                pl["name"] = segs[0] if segs else (pl.get("name") or plid)
                pl["parent"] = None
                continue
            pl["parent"] = lib.resolve_folder(segs[:-1], pl.get("owner"))
            pl["name"] = segs[-1]
            changed += 1
        lib.data["playlist_tree_migrated"] = True
        lib.save()
        if changed:
            print(f"  [playlists] arranged {changed} slashed name(s) into a "
                  f"folder tree", flush=True)


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
        try:
            users.create(admin, None, admin=True, pw_hash=pw_hash)
        except ValueError as e:
            raise SystemExit(
                f"error: [auth] admin_user={auth_cfg.get('admin_user')!r} is "
                f"not a usable username ({e}). Use 3-32 characters from "
                f"letters, digits, dot, dash or underscore.")
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
        for pl in lib.data.get("playlists", {}).values():
            if pl.get("owner") is None:      # ...and playlists, which the
                pl["owner"] = owner          # first version forgot entirely
                changed += 1
        if changed:
            lib.save()
            print(f"  [auth] {changed} existing paper(s) now owned by "
                  f"'{owner}', private", flush=True)
    return users


def run(root, port, voice, speed, dpi, open_browser=False, grobid_cfg=None,
        tts_cfg=None, idle_exit_min=0, llm_cfg=None, auth_cfg=None,
        secrets_cfg=None):
    root = Path(root)
    if not root.exists() and not root.parent.exists():
        sys.exit(f"error: library location unavailable (is the volume "
                 f"mounted?): {root}")
    root.mkdir(parents=True, exist_ok=True)

    lib = Library(root)
    _migrate_playlist_tree(lib)
    users = _bootstrap_users(lib, auth_cfg or {})
    secret_key = None
    keyb64 = (secrets_cfg or {}).get("key")
    if keyb64:
        try:
            secret_key = secretbox.load_key(keyb64)
        except ValueError as e:
            sys.exit(f"error: [secrets] key is invalid ({e}). Run "
                     f"'rhapsode --gen-key' for a fresh one.")
    worker = Worker(lib, voice, speed, dpi, grobid_cfg, tts_cfg, idle_exit_min,
                    llm_cfg, users=users, secret_key=secret_key)
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
        uvicorn.run(create_app(lib, worker, auth_cfg, users, secret_key),
                    host="127.0.0.1", port=port,
                    log_level="warning", timeout_graceful_shutdown=3)
    finally:
        import grobid
        grobid.stop()   # don't orphan a JVM we started
        if llm_cfg:
            import llm
            llm.release(llm_cfg)  # nor leave a model pinning GPU memory
        lib.flush()     # persist debounced positions/progress
